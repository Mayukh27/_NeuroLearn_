"""Assessment orchestration for the fixed mixed-method research protocol.

One study session produces one 10-item assessment: a single MCRF decision
(from the learner's MCRF/CRS state for the completed study session as a
whole) sets ONE difficulty for all of questions 1--5; a single LEGACY
decision (from the learner's durable historical performance) sets ONE
difficulty for all of questions 6--10. Both decisions are made up front, at
/generate time, before any question is generated or shown — never
recomputed from an in-assessment answer. This is protocol-level, not
item-level, adaptivity: a question's correctness, response time, or outcome
is used only for scoring/research logging and never changes the difficulty
of any question, including subsequent ones in the same block.

All ten questions are generated and persisted at /generate time, before Q1
is ever presented to the student. /answer therefore never generates a
question — it only validates the current question, records the answer,
computes scoring/research response information, and returns the
already-existing next question. The engines themselves remain in
:mod:`ml.adaptive_engine`; this router owns only their protocol, evidence
flow, and durable study logging.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from auth.security import get_current_user
from data.database import (
    advance_challenge_progress,
    apply_xp,
    complete_study_session,
    get_canonical_assessment_session_for_study,
    get_completed_video_behavioral_score,
    get_completed_video_context,
    get_or_create_active_study_session,
    get_recent_scores_pct,
    get_student_results,
    get_assessment_session,
    record_completed_video,
    refresh_behavioral_summary,
    save_assessment_result,
    save_assessment_session,
    save_crs_record,
    save_generated_questions,
    save_question_responses,
    save_research_crs_decision,
    save_research_legacy_decision,
    save_single_question_response,
    update_assessment_adaptive_state,
    StudyConsentRequired,
)
from data.db import get_db
from data.models_orm import User
from ml import adaptive_engine, question_generator
from ml.llm_providers import LLMProviderError
from ml.question_generator import QuestionGenerationError
from schemas.models import (
    AssessmentResult,
    AssessmentSession,
    GenerateAssessmentRequest,
    SubmitAdaptiveAnswerRequest,
    SubmitAssessmentRequest,
)


router = APIRouter(prefix="/api/assessment", tags=["Assessment"])

TOTAL_QUESTIONS = 10
MCRF_QUESTION_COUNT = 5
METHOD_SEQUENCE = ["MCRF"] * MCRF_QUESTION_COUNT + ["LEGACY"] * (TOTAL_QUESTIONS - MCRF_QUESTION_COUNT)
PER_QUESTION_TIME_LIMIT_SECONDS = 42
TIME_LIMIT_SECONDS = TOTAL_QUESTIONS * PER_QUESTION_TIME_LIMIT_SECONDS


def _apply_xp(user: User, amount: int, db: Session) -> dict:
    xp_result = apply_xp(user, amount)
    db.commit()
    return xp_result


def _method_for_question(question_index: int) -> str:
    if not 0 <= question_index < TOTAL_QUESTIONS:
        raise ValueError(f"Question index must be between 0 and {TOTAL_QUESTIONS - 1}")
    return METHOD_SEQUENCE[question_index]


def _question_text_key(question: dict) -> str:
    return re.sub(r"\s+", " ", str(question.get("question", "")).strip().casefold())


def _generate_unique_question(
    *,
    transcript_text: str,
    difficulty: str,
    topic_id: str,
    existing_questions: list[dict],
    method: str,
    question_index: int,
) -> dict:
    """Generate exactly one new, non-duplicate item for the current session.

    Content quality (well-formed, non-generic, transcript-grounded,
    non-duplicate-within-a-call) is handled inside question_generator.py
    itself, with a small bounded number of internally-varied attempts per
    call. This loop's only remaining job is the one thing only the caller
    can know: whether the result duplicates a sibling question already
    generated elsewhere in *this* assessment. `question_index` is
    forwarded as `segment_offset`, so each of the ten questions in an
    assessment is grounded in a different part of the transcript.

    NO STATIC FALLBACK: if the LLM provider fails (connection, timeout,
    missing model, bad response) or a valid question can't be produced,
    this raises an HTTPException — it never substitutes a static
    question-bank item. A generation failure must be visible, not hidden.
    """
    existing_ids = {str(question.get("id")) for question in existing_questions}
    existing_texts = {_question_text_key(question) for question in existing_questions}
    last_detail = "unknown error"
    for attempt in range(3):
        try:
            generated = question_generator.generate_questions(
                transcript_text=transcript_text,
                difficulty=difficulty,
                num_questions=1,
                topic_id=topic_id,
                segment_offset=question_index + attempt,
            )
        except LLMProviderError as exc:
            # Infra-level failure (Ollama unreachable, model not pulled,
            # request timed out, unparseable response) — this is never a
            # "try again" situation the same way a content-quality
            # rejection is, so fail the whole assessment generation
            # immediately and clearly rather than burning more attempts.
            logger.error(f"LLM provider unavailable while generating question {question_index + 1}: {exc}")
            raise HTTPException(status_code=502, detail=f"LLM provider unavailable: {exc}") from exc
        except QuestionGenerationError as exc:
            logger.warning(f"Question generation failed validation for question {question_index + 1}: {exc}")
            last_detail = str(exc)
            continue

        question = dict(generated[0])
        if question.get("id") in existing_ids or _question_text_key(question) in existing_texts:
            last_detail = "duplicate of an existing question in this assessment"
            logger.warning(f"Question rejected: {last_detail} (question {question_index + 1})")
            continue
        question["adaptive_method"] = method
        question["decision_index"] = question_index + 1
        return question

    raise HTTPException(
        status_code=503,
        detail=f"Question generation failed validation for question {question_index + 1}: {last_detail}",
    )


async def _generate_question_block(
    *,
    transcript_text: str,
    difficulty: str,
    topic_id: str,
    method: str,
    start_index: int,
    count: int,
    existing_questions: list[dict],
) -> list[dict]:
    """Generate a whole block of `count` questions at one, already-fixed
    difficulty (MCRF for Q1-5, LEGACY for Q6-10) and return them.

    All ten questions must exist, persisted, before Q1 is ever shown to the
    student — there is no "answer Q_n -> generate Q_n+1" step anywhere.
    This is called twice from generate_assessment, once per block, before
    the assessment session is first returned; /answer never calls this (or
    anything else that generates a question).

    NO STATIC FALLBACK: `_generate_unique_question` already raises a clear
    HTTPException on failure — that exception is intentionally left to
    propagate straight out of this function and out of /generate. It must
    NOT be caught here and papered over with a question-bank substitute.
    """
    generated: list[dict] = []
    for offset in range(count):
        index = start_index + offset
        # run_in_threadpool: generation makes a blocking HTTP call to the
        # LLM provider; running it off the event loop keeps other requests
        # responsive while the whole 10-question set is generated up front.
        question = await run_in_threadpool(
            _generate_unique_question,
            transcript_text=transcript_text,
            difficulty=difficulty,
            topic_id=topic_id,
            existing_questions=existing_questions + generated,
            method=method,
            question_index=index,
        )
        generated.append(question)
    return generated


def _assessment_score(responses: list[dict]) -> float:
    answered = [response for response in responses if response.get("correctness") is not None]
    if not answered:
        return 0.0
    return round(100.0 * sum(bool(response["correctness"]) for response in answered) / len(answered), 1)


def _responses_for_method(responses: list[dict], method: str) -> list[dict]:
    return [response for response in responses if response.get("condition") == method]


def _decision_for_question(
    *,
    question_index: int,
    student_id: str,
    session: dict,
    durable_scores: list[float],
    responses: list[dict],
) -> dict:
    """Ask the method assigned to *this* question for its difficulty.

    MCRF receives the completed-video B/C evidence plus cumulative assessment
    performance and response timing.  LEGACY deliberately receives only the
    traditional score/history/timing inputs; video behavioral evidence,
    transcript complexity, and CRS are not passed into its baseline path.
    """
    method = _method_for_question(question_index)
    if question_index == 0:
        return adaptive_engine.get_initial_difficulty(
            student_id=student_id,
            attention_score=session["attention_score_during_video"],
            previous_score=durable_scores[-1] if durable_scores else None,
            previous_scores=durable_scores,
            transcript_text=session.get("transcript_text") or "",
        )

    prior_question = session["questions"][question_index - 1]
    prior_difficulty = prior_question.get("difficulty") or session.get("difficulty", "medium")
    completed_for_method = _responses_for_method(responses, method)
    # At the MCRF -> LEGACY boundary no LEGACY item has yet been answered.
    # The baseline's history therefore begins with the assessment performance
    # accumulated so far, as a genuine traditional performance/history input.
    evidence_responses = completed_for_method or responses
    cumulative_score = _assessment_score(evidence_responses)
    timing_values = [
        float(response["response_time_seconds"])
        for response in evidence_responses
        if response.get("response_time_seconds") is not None
    ]
    total_time = sum(timing_values)
    time_limit = PER_QUESTION_TIME_LIMIT_SECONDS * max(len(evidence_responses), 1)
    assessment_history = [
        _assessment_score(evidence_responses[:index + 1])
        for index in range(len(evidence_responses))
    ]
    previous_scores = (durable_scores + assessment_history[:-1])[-5:]
    was_correct = bool(evidence_responses[-1].get("correctness")) if evidence_responses else False

    if method == "LEGACY":
        return adaptive_engine._determine_difficulty_legacy(
            student_id=student_id,
            current_score=cumulative_score,
            # Neutral rather than video-derived: this is the CRS-free
            # performance/history baseline, not an MCRF multimodal decision.
            attention_score=50,
            time_spent=total_time,
            time_limit=time_limit,
            previous_difficulty=prior_difficulty,
            previous_scores=previous_scores,
        )

    return adaptive_engine.determine_difficulty(
        student_id=student_id,
        current_score=cumulative_score,
        attention_score=session["attention_score_during_video"],
        time_spent=total_time,
        time_limit=time_limit,
        previous_difficulty=prior_difficulty,
        previous_scores=previous_scores,
        transcript_text=session.get("transcript_text") or "",
        was_correct=was_correct,
    )


def _decide_legacy_block_difficulty(
    *,
    student_id: str,
    durable_scores: list[float],
) -> dict:
    """Determine the single, frozen LEGACY difficulty for Q6-10, up front,
    before any assessment question exists or is answered.

    This is the LEGACY-condition analogue of `get_initial_difficulty()`'s
    role for the MCRF block: it uses only evidence that exists *before* the
    assessment starts (the student's durable historical scores), never
    current-assessment answers, so both block difficulties can be decided
    together before question generation begins. It intentionally goes
    through `_get_initial_difficulty_legacy` (the rule-cascade engine's own
    initial-difficulty path) rather than `_determine_difficulty_legacy`,
    since the latter requires a just-answered score/timing pair that does
    not exist yet at this point in the flow. `attention_score=50` (neutral)
    matches the existing design: the LEGACY condition deliberately does not
    receive video behavioral-cue evidence.
    """
    legacy_initial = adaptive_engine._get_initial_difficulty_legacy(
        student_id=student_id,
        attention_score=50,
        previous_score=durable_scores[-1] if durable_scores else None,
    )
    difficulty = legacy_initial["difficulty"]
    reason = legacy_initial["adaptive_metadata"]["reason"]
    return {
        "difficulty": difficulty,
        "performance_trend": "stable",
        "recommended_action": reason,
        "next_assessment_difficulty": difficulty,
        "strength_areas": [],
        "weak_areas": [],
        "_debug": {"engine": "legacy_rule_cascade_initial", "reason": reason},
    }


def _record_decision(
    *,
    method: str,
    study_session: dict,
    assessment_session: dict,
    adaptive_result: dict,
    previous_scores: list[float],
    responses: list[dict],
    previous_difficulty: str | None,
    decision_index: int,
) -> None:
    if method == "MCRF":
        crs = adaptive_result.get("crs")
        if not crs:
            raise RuntimeError("MCRF decision did not return a CRS record")
        selected_difficulty = adaptive_result.get("difficulty", adaptive_result.get("next_assessment_difficulty"))
        save_crs_record({
            "student_id": assessment_session["student_id"],
            "study_session_id": study_session["study_session_id"],
            "participant_id": study_session["participant_id"],
            "condition": "MCRF",
            "assessment_id": assessment_session["id"],
            "timestamp": time.time(),
            "performance": crs["components"]["performance"],
            "behavioral_cue": crs["components"]["behavioral_cue"],
            "integrity": crs["components"]["integrity"],
            "trend": crs["components"]["trend"],
            "complexity": crs["components"]["complexity"],
            "crs": crs["score"],
            "difficulty": selected_difficulty,
            "explanation": crs["explanation"],
        })
        save_research_crs_decision(
            study_session=study_session,
            assessment_session=assessment_session,
            adaptive_result={**adaptive_result, "next_assessment_difficulty": selected_difficulty},
            previous_scores=previous_scores,
            per_question_responses=responses,
            previous_difficulty=previous_difficulty or "",
            attention_score=assessment_session["attention_score_during_video"],
            transcript_text=assessment_session.get("transcript_text"),
            decision_index=decision_index,
            method="MCRF",
        )
        return

    save_research_legacy_decision(
        study_session=study_session,
        assessment_session=assessment_session,
        adaptive_result=adaptive_result,
        previous_scores=previous_scores,
        per_question_responses=responses,
        previous_difficulty=previous_difficulty or "",
        current_score=_assessment_score(responses),
        decision_index=decision_index,
        method="LEGACY",
    )


def _record_block_decision(
    *,
    method: str,
    study_session: dict,
    assessment_session: dict,
    adaptive_result: dict,
    previous_scores: list[float],
    responses: list[dict],
    previous_difficulty: str | None,
    block_start_index: int,
    block_size: int,
) -> None:
    """Protocol correction: difficulty is decided ONCE per block (MCRF for
    Q1-5, LEGACY for Q6-10) before that block's questions are generated —
    never recomputed per question from the previous answer's correctness or
    timing. `adaptive_result`/`previous_scores`/`responses` are therefore
    the single, frozen inputs for the whole block; we still write one
    research decision record per question (decision_index block_start_index+1
    .. +block_size) so the existing CRS/LEGACY export shape (one row per
    question) is unchanged — every record in the block just carries the
    same already-determined difficulty instead of a re-evaluated one.
    """
    for offset in range(block_size):
        _record_decision(
            method=method,
            study_session=study_session,
            assessment_session=assessment_session,
            adaptive_result=adaptive_result,
            previous_scores=previous_scores,
            responses=responses,
            previous_difficulty=previous_difficulty,
            decision_index=block_start_index + offset + 1,
        )


def _result_message(percentage: float) -> tuple[str, list[str]]:
    if percentage >= 90:
        return "Outstanding! You've truly mastered this material!", ["Next: Advanced Topics", "Challenge: Timed Quiz"]
    if percentage >= 70:
        return "Well done! You have a solid understanding.", ["Next: Advanced Topics", "Challenge: Timed Quiz"]
    if percentage >= 50:
        return "Decent effort! Review the videos to strengthen weak areas.", ["Review: Completed Videos", "Practice: Easier Questions"]
    return "Don't worry! Rewatch the completed videos and try again — you'll improve.", ["Review: Completed Videos", "Practice: Easier Questions", "Resource: Study Guide"]


def _finalize_assessment(
    *,
    session: dict,
    study_session: dict,
    state: dict,
    current_user: User,
    db: Session,
    allow_partial: bool = False,
) -> dict:
    questions = session.get("questions", [])
    answers = state.get("answers", {})
    # FIX (item 3, timeout auto-submit): the normal 10/10 completion path
    # (allow_partial=False, unchanged) still requires all ten protocol
    # questions to be answered — that behavior is untouched. allow_partial
    # is only used by the new /submit timeout path below, for the case
    # where the timer expired before the tenth question was reached; any
    # question the student never got to answer scores 0 rather than
    # blocking finalization. No MCRF/LEGACY/CRS records are written here
    # either way — those are only ever recorded per-question, at answer
    # time, exactly as before.
    if not allow_partial and (len(questions) != TOTAL_QUESTIONS or len(answers) != TOTAL_QUESTIONS):
        raise RuntimeError("Cannot complete an assessment before all ten protocol questions are answered")
    save_question_responses(
        study_session=study_session,
        assessment_session=session,
        answers=answers,
        response_events=state.get("responses") or [],
        submitted_at=datetime.utcnow(),
        total_time_spent=0.0,
    )
    correct_count = sum(
        question["id"] in answers and answers[question["id"]] == question.get("correct_answer")
        for question in questions
    )
    total_points = sum(int(question.get("points", 10)) for question in questions)
    earned_points = sum(
        int(question.get("points", 10))
        for question in questions
        if question["id"] in answers and answers[question["id"]] == question.get("correct_answer")
    )
    percentage = round((correct_count / TOTAL_QUESTIONS) * 100, 1)
    xp_earned = int(earned_points * 1.5)
    xp_result = _apply_xp(current_user, xp_earned, db)
    if percentage >= 100:
        advance_challenge_progress(current_user.id, "quiz", set_to=1)
    last_adaptive = state.get("last_adaptive_response") or {
        "performance_trend": "stable",
        "recommended_action": "Assessment completed.",
        "next_assessment_difficulty": questions[-1].get("difficulty", "medium"),
        "strength_areas": [],
        "weak_areas": [],
    }
    message, suggested_topics = _result_message(percentage)
    result = {
        "session_id": session["id"],
        "study_session_id": study_session["study_session_id"],
        "participant_id": study_session["participant_id"],
        "condition": "MIXED",
        "student_id": current_user.id,
        "score": percentage,
        "total_points": total_points,
        "earned_points": earned_points,
        "percentage": percentage,
        "xp_earned": xp_earned,
        "total_xp": xp_result["new_xp"],
        "new_level": xp_result["new_level"],
        "leveled_up": xp_result["leveled_up"],
        "time_spent": int(sum(float(response.get("response_time_seconds") or 0) for response in state.get("responses", []))),
        "correct_answers": correct_count,
        "total_questions": TOTAL_QUESTIONS,
        "difficulty": session.get("difficulty", "medium"),
        "message": message,
        "next_difficulty": last_adaptive["next_assessment_difficulty"],
        "suggested_topics": suggested_topics,
        "timestamp": time.time(),
        # FIX (item 3): lets the frontend/results page and research export
        # distinguish a genuine timeout finalize from a full 10/10 finish.
        "timed_out": allow_partial and len(answers) < TOTAL_QUESTIONS,
        "answered_count": len(answers),
        "adaptive_response": {
            "performance_trend": last_adaptive["performance_trend"],
            "recommended_action": last_adaptive["recommended_action"],
            "next_assessment_difficulty": last_adaptive["next_assessment_difficulty"],
            "strength_areas": last_adaptive["strength_areas"],
            "weak_areas": last_adaptive["weak_areas"],
            # Question 10 is selected by LEGACY; CRS entries for questions
            # 1--5 remain separately and precisely logged.
            "crs": last_adaptive.get("crs"),
        },
        "completion_status": "timed_out" if allow_partial and len(answers) < TOTAL_QUESTIONS else "completed",
    }
    save_assessment_result(result)
    refresh_behavioral_summary(study_session["study_session_id"])
    # FIX (items 8/9): complete_study_session is unchanged and still called
    # exactly once, right here, only once all ten questions are answered or
    # a genuine timeout finalize has happened (allow_partial path) — never
    # when the assessment starts or a video ends.
    complete_study_session(study_session["study_session_id"], "completed")
    # FIX (item 9): stash the final result on the session's adaptive_state so
    # a retried/duplicate finalize call (e.g. a race between the 10th
    # /answer and a near-simultaneous timeout /submit) can return the same
    # result idempotently instead of erroring or re-finalizing.
    update_assessment_adaptive_state(
        session["id"],
        adaptive_state={**state, "final_result": result},
        completion_status=result["completion_status"],
    )
    return result


@router.post("/generate", response_model=AssessmentSession)
async def generate_assessment(
    request: GenerateAssessmentRequest,
    current_user: User = Depends(get_current_user),
):
    """Start or resume the single 10-question mixed-method assessment."""
    request.student_id = current_user.id
    try:
        study_session = get_or_create_active_study_session(
            current_user.id,
            course_id=request.course_id,
            video_id=request.video_id,
            requested_study_session_id=request.study_session_id,
        )
    except (ValueError, StudyConsentRequired) as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    completed_context = get_completed_video_context(study_session["study_session_id"])
    contributing_video_ids = completed_context["contributing_video_ids"]
    if not contributing_video_ids:
        # The frontend completion call (POST .../videos/{id}/complete) may have
        # been lost due to a stale-closure race, a network hiccup, or a rapid
        # video switch that replaced the study session before the ENDED event
        # fired.  Rather than returning 409 and forcing the user to retry,
        # auto-record the completion for the video that was passed with this
        # generate request so the pipeline can proceed.
        #
        # record_completed_video is idempotent: if the frontend DID manage to
        # record it concurrently, this is a no-op (the existing row is kept and
        # optionally enriched with the transcript text if it was previously
        # empty).
        if request.video_id:
            try:
                record_completed_video(
                    study_session_id=study_session["study_session_id"],
                    user_id=current_user.id,
                    video_id=request.video_id,
                    transcript_text=request.transcript_text or "",
                )
                completed_context = get_completed_video_context(study_session["study_session_id"])
                contributing_video_ids = completed_context["contributing_video_ids"]
            except Exception as auto_err:
                logger.warning(
                    f"Auto-record completion failed for session "
                    f"{study_session['study_session_id']} / video {request.video_id}: {auto_err}"
                )
        if not contributing_video_ids:
            raise HTTPException(
                status_code=409,
                detail="Complete at least one video before starting the assessment.",
            )

    # FIX (root-cause pass): this used to treat ANY non-empty question list
    # as "already generated" and return it as-is. Under the synchronous
    # 10-question-upfront architecture that's wrong — a session can only
    # ever be legitimately resumed if it already has all TOTAL_QUESTIONS
    # persisted. A session with fewer (e.g. a single leftover Q1 from an
    # older code path, or one that failed partway through a prior
    # /generate call) is not a valid resumable session; falling through
    # regenerates it correctly below instead of permanently serving a
    # truncated assessment for this study session.
    existing_session = get_canonical_assessment_session_for_study(study_session["study_session_id"])
    if existing_session:
        existing_question_count = len(existing_session.get("questions") or [])
        if existing_question_count == TOTAL_QUESTIONS:
            return existing_session
        if existing_question_count > TOTAL_QUESTIONS:
            raise HTTPException(
                status_code=409,
                detail="Persisted assessment exceeds the 10-question protocol and cannot be resumed.",
            )

    # Protocol correction: MCRF is evaluated ONCE per assessment, from the
    # learner's MCRF/CRS state for the completed study session as a whole —
    # not re-evaluated per question. This single value governs all of
    # Q1-Q5; it is never recomputed from in-assessment answer correctness.
    transcript_text = completed_context["transcript_text"]
    if not isinstance(transcript_text, str) or not transcript_text.strip():
        raise HTTPException(
            status_code=409,
            detail="A usable transcript is required before generating assessment questions.",
        )
    attention_score = get_completed_video_behavioral_score(study_session["study_session_id"])
    durable_scores = get_recent_scores_pct(current_user.id, limit=5)
    initial = _decision_for_question(
        question_index=0,
        student_id=current_user.id,
        session={
            "attention_score_during_video": attention_score,
            "transcript_text": transcript_text,
            "questions": [],
            "difficulty": "medium",
        },
        durable_scores=durable_scores,
        responses=[],
    )
    mcrf_difficulty = initial["difficulty"]

    # Protocol correction (item 2): LEGACY is now also decided ONCE, up
    # front, alongside MCRF — before any question is generated or answered
    # — instead of at the old Q5->Q6 boundary. It uses only durable,
    # pre-assessment historical evidence (never this assessment's own
    # answers, which don't exist yet), so both block difficulties are fixed
    # before generation starts, per the "determine both difficulties, then
    # generate all ten questions" protocol.
    legacy_decision = _decide_legacy_block_difficulty(
        student_id=current_user.id,
        durable_scores=durable_scores,
    )
    legacy_difficulty = legacy_decision["difficulty"]

    # Generate ALL ten questions now, before the assessment is ever
    # returned/shown: Q1-5 at the single frozen MCRF difficulty, Q6-10 at
    # the single frozen LEGACY difficulty. Nothing about how any question is
    # answered changes any other question's difficulty, and /answer below
    # never generates a question — it only ever returns ones generated here.
    mcrf_questions = await _generate_question_block(
        transcript_text=transcript_text,
        difficulty=mcrf_difficulty,
        topic_id=request.course_id,
        method="MCRF",
        start_index=0,
        count=MCRF_QUESTION_COUNT,
        existing_questions=[],
    )
    legacy_questions = await _generate_question_block(
        transcript_text=transcript_text,
        difficulty=legacy_difficulty,
        topic_id=request.course_id,
        method="LEGACY",
        start_index=MCRF_QUESTION_COUNT,
        count=TOTAL_QUESTIONS - MCRF_QUESTION_COUNT,
        existing_questions=mcrf_questions,
    )
    all_questions = mcrf_questions + legacy_questions

    adaptive_metadata = initial["adaptive_metadata"]
    session = {
        "id": f"session_{uuid.uuid4().hex[:12]}",
        "study_session_id": study_session["study_session_id"],
        "participant_id": study_session["participant_id"],
        "condition": "MIXED",
        "course_id": request.course_id,
        "video_id": contributing_video_ids[-1],
        "contributing_video_ids": contributing_video_ids,
        "questions": all_questions,
        "difficulty": mcrf_difficulty,
        "time_limit": TIME_LIMIT_SECONDS,
        "attention_score_during_video": attention_score,
        "adaptive_metadata": adaptive_metadata,
        "student_id": current_user.id,
        "transcript_text": transcript_text,
        "adaptive_state": {
            "target_questions": TOTAL_QUESTIONS,
            "method_sequence": METHOD_SEQUENCE,
            "answered_count": 0,
            "answers": {},
            "responses": [],
            "adaptive_metadata": adaptive_metadata,
            # Frozen, assessment-level block difficulties (no per-question
            # adaptivity): both are decided once, here, before any question
            # is shown or answered. mcrf_block covers Q1-5, legacy_block
            # covers Q6-10.
            "mcrf_block_difficulty": mcrf_difficulty,
            "legacy_block_difficulty": legacy_difficulty,
            "last_adaptive_response": {
                "performance_trend": "stable",
                "recommended_action": adaptive_metadata["reason"],
                "next_assessment_difficulty": mcrf_difficulty,
                "strength_areas": [],
                "weak_areas": [],
                "crs": initial.get("crs"),
            },
        },
    }
    save_assessment_session(session)
    save_generated_questions(session)
    # One research decision record per question (unchanged export shape),
    # but every one of Q1-Q5's records carries this same, single MCRF
    # decision and every one of Q6-Q10's records carries this same, single
    # LEGACY decision — none are re-evaluated from answer correctness.
    _record_block_decision(
        method="MCRF",
        study_session=study_session,
        assessment_session=session,
        adaptive_result=initial,
        previous_scores=durable_scores,
        responses=[],
        previous_difficulty=None,
        block_start_index=0,
        block_size=MCRF_QUESTION_COUNT,
    )
    _record_block_decision(
        method="LEGACY",
        study_session=study_session,
        assessment_session=session,
        adaptive_result=legacy_decision,
        previous_scores=durable_scores,
        responses=[],
        previous_difficulty=mcrf_difficulty,
        block_start_index=MCRF_QUESTION_COUNT,
        block_size=TOTAL_QUESTIONS - MCRF_QUESTION_COUNT,
    )
    return session


@router.post("/answer")
async def submit_adaptive_answer(
    request: SubmitAdaptiveAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate the current question, record the answer, score it, and
    return the already-existing next question.

    Fix (item 2): all ten questions were generated and persisted at
    /generate time, so this endpoint never generates a question — it only
    validates + records + scores + returns what's already there. Difficulty
    for the next question is never recomputed here either (item 1): it is
    just read back from the two block difficulties frozen at /generate.
    """
    session = get_assessment_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Assessment session not found")
    if session.get("student_id") != current_user.id:
        raise HTTPException(status_code=403, detail="Not your session")
    if session.get("completion_status") in {"completed", "timed_out"}:
        return {"completed": True, "session": session, "result": None}
    try:
        study_session = get_or_create_active_study_session(
            current_user.id,
            requested_study_session_id=session["study_session_id"],
        )
    except (ValueError, StudyConsentRequired) as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    state = dict(session.get("adaptive_state") or {})
    answers = dict(state.get("answers") or {})
    questions = list(session.get("questions") or [])
    question_index = next((index for index, question in enumerate(questions) if question.get("id") == request.question_id), -1)
    if question_index < 0:
        raise HTTPException(status_code=404, detail="Question not found in this assessment")
    if request.question_id in answers:
        return {"completed": False, "session": session, "duplicate": True}
    if question_index != len(answers):
        raise HTTPException(status_code=409, detail="Assessment questions must be answered in order")
    if question_index >= TOTAL_QUESTIONS:
        raise HTTPException(status_code=409, detail="This assessment already contains its ten protocol questions")

    question = questions[question_index]
    method = question.get("adaptive_method") or _method_for_question(question_index)
    response_event = dict(request.response_event or {})
    response_event["question_id"] = request.question_id
    response_event["question_index"] = question_index
    answers[request.question_id] = request.answer
    answered_count = len(answers)
    next_difficulty = None
    if answered_count < TOTAL_QUESTIONS:
        next_difficulty = (
            state.get("mcrf_block_difficulty") or session.get("difficulty", "medium")
            if answered_count < MCRF_QUESTION_COUNT
            else state.get("legacy_block_difficulty") or session.get("difficulty", "medium")
        )
    response_state = {}

    def build_response_state(saved_response):
        responses = list(state.get("responses") or []) + [saved_response]
        last_adaptive = state.get("last_adaptive_response") or {}
        if next_difficulty is not None:
            last_adaptive = {
                **last_adaptive,
                "next_assessment_difficulty": next_difficulty,
                "crs": last_adaptive.get("crs"),
            }
        response_state["state"] = {
            **state,
            "answers": answers,
            "responses": responses,
            "answered_count": answered_count,
            "last_adaptive_response": last_adaptive,
        }
        return response_state["state"]

    try:
        response = save_single_question_response(
            study_session=study_session,
            assessment_session=session,
            question=question,
            question_index=question_index,
            answer=request.answer,
            response_event=response_event,
            submitted_at=datetime.utcnow(),
            decision_method=method,
            adaptive_state_factory=build_response_state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    new_state = response_state["state"]

    if answered_count == TOTAL_QUESTIONS:
        result = _finalize_assessment(
            session={**session, "questions": questions},
            study_session=study_session,
            state=new_state,
            current_user=current_user,
            db=db,
        )
        return {"completed": True, "session": get_assessment_session(session["id"]), "result": result}

    next_index = answered_count

    # ── Protocol correction ──────────────────────────────────────────────
    # Difficulty is NEVER re-evaluated per question here. Both block
    # difficulties (MCRF for Q1-5, LEGACY for Q6-10) were already decided
    # once, up front, in /generate — before Q1 was ever shown — and are
    # simply read back from the frozen adaptive_state below. No answer's
    # correctness, response time, or outcome changes any subsequent
    # question's difficulty.
    assert next_difficulty is not None
    crs_for_response = (state.get("last_adaptive_response") or {}).get("crs")

    # All ten questions were generated and persisted together in /generate
    # and are never mutated afterward, so writing `questions=questions` back
    # here is safe (there is no concurrent background block that could add
    # to it) and keeps the returned session's question list authoritative.
    updated = update_assessment_adaptive_state(
        session["id"],
        questions=questions,
        selected_difficulty=next_difficulty,
    )
    # The next question already exists — it was generated up front in
    # /generate, along with all ten questions, before this assessment was
    # ever shown to the student. This endpoint never generates a question.
    return {
        "completed": False,
        "session": updated,
        "next_question_pending": False,
        "adaptive_response": {"next_assessment_difficulty": next_difficulty, "crs": crs_for_response},
    }


@router.post("/submit", response_model=AssessmentResult)
async def submit_assessment(
    request: SubmitAssessmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Timeout auto-submit (item 3): finalize with whatever was answered
    when the assessment clock runs out.

    In-order answering of all ten questions must still go through
    /api/assessment/answer, unchanged — this endpoint never accepts or
    grades a submitted answer itself (SubmitAssessmentRequest.answers is
    intentionally ignored: the ledger of graded answers already recorded
    via /answer is the only source of truth). This endpoint only exists to
    let a real timer expiry force finalization when fewer than ten
    questions were reached, so the study session is never left stranded.
    """
    session = get_assessment_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Assessment session not found")
    if session.get("student_id") != current_user.id:
        raise HTTPException(status_code=403, detail="Not your session")

    state = dict(session.get("adaptive_state") or {})

    if session.get("completion_status") in {"completed", "timed_out"}:
        # Idempotent: either the 10th /answer already finalized this
        # session, or a duplicate timeout call arrived (e.g. a retry). Both
        # cases must not call complete_study_session or re-finalize again.
        final_result = state.get("final_result")
        if final_result:
            return final_result
        raise HTTPException(
            status_code=409,
            detail="This assessment is already completed.",
        )

    answers = dict(state.get("answers") or {})
    if len(answers) >= TOTAL_QUESTIONS:
        raise HTTPException(
            status_code=409,
            detail="This assessment already contains its ten protocol questions.",
        )

    try:
        study_session = get_or_create_active_study_session(
            current_user.id,
            requested_study_session_id=session["study_session_id"],
        )
    except (ValueError, StudyConsentRequired) as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    return _finalize_assessment(
        session=session,
        study_session=study_session,
        state=state,
        current_user=current_user,
        db=db,
        allow_partial=True,
    )


@router.get("/session/{session_id}")
async def get_session(session_id: str, current_user: User = Depends(get_current_user)):
    session = get_assessment_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("student_id") != current_user.id:
        raise HTTPException(status_code=403, detail="Not your session")
    return session


@router.get("/results/{student_id}")
async def get_results_history(student_id: str, current_user: User = Depends(get_current_user)):
    if student_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only view your own results")
    results = get_student_results(student_id)
    return {
        "student_id": student_id,
        "total_assessments": len(results),
        "results": results,
        "average_score": sum(result.get("percentage", 0) for result in results) / max(len(results), 1),
    }
