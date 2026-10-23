import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import database
from data.db import Base
from data.models_orm import (
    AssessmentSession,
    QuestionResponse,
    ResearchCRSDecision,
    ResearchLegacyDecision,
    ResearchParticipant,
    StudySession,
)
from routers import assessment
from scripts import export_research_data
from scripts.validate_research_export import validate


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'research-test.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_session", factory)
    monkeypatch.setattr(export_research_data, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _seed_mixed_assessment(factory):
    study_data = {
        "study_session_id": "study-test",
        "participant_id": "participant-test",
        "user_id": "user-test",
        "condition": "MIXED",
        "sequence_order": "MCRF_THEN_LEGACY",
    }
    questions = [
        {
            "id": f"question-{index}",
            "difficulty": "medium" if index < 5 else "hard",
            "adaptive_method": "MCRF" if index < 5 else "LEGACY",
            "correct_answer": index % 4,
            "points": 10,
            "llm_metadata": {"model": "test-model", "generated_from": "test", "blooms_level": "apply"},
        }
        for index in range(10)
    ]
    state = {
        "target_questions": 10,
        "method_sequence": ["MCRF"] * 5 + ["LEGACY"] * 5,
        "answered_count": 0,
        "answers": {},
        "responses": [],
        "mcrf_block_difficulty": "medium",
        "legacy_block_difficulty": "hard",
    }
    participant = ResearchParticipant(
        participant_id=study_data["participant_id"],
        user_id=study_data["user_id"],
        sequence_order=study_data["sequence_order"],
        assigned_condition="MIXED",
    )
    study = StudySession(**study_data)
    db = factory()
    db.add_all([participant, study])
    db.commit()
    db.close()

    session_data = {
        "id": "assessment-test",
        "student_id": study_data["user_id"],
        "study_session_id": study_data["study_session_id"],
        "participant_id": study_data["participant_id"],
        "condition": "MIXED",
        "course_id": "course-test",
        "video_id": "video-test",
        "contributing_video_ids": [],
        "questions": questions,
        "difficulty": "medium",
        "completion_status": "started",
        "adaptive_state": state,
    }
    database.save_assessment_session(session_data)
    database.save_generated_questions(session_data)

    db = factory()
    db.add_all([
        ResearchCRSDecision(
            study_session_id=study_data["study_session_id"],
            assessment_session_id=session_data["id"],
            participant_id=study_data["participant_id"],
            condition="MCRF",
            decision_index=index,
            performance=0.5,
            behavioral_cue=0.5,
            response_timing=0.5,
            trend=0.5,
            complexity=0.5,
            crs=0.5,
            alpha=0.2,
            beta=0.2,
            gamma=0.2,
            delta=0.2,
            epsilon=0.2,
            selected_difficulty="medium",
        )
        for index in range(1, 6)
    ] + [
        ResearchLegacyDecision(
            study_session_id=study_data["study_session_id"],
            assessment_session_id=session_data["id"],
            participant_id=study_data["participant_id"],
            condition="LEGACY",
            decision_index=index,
            selected_difficulty="hard",
        )
        for index in range(6, 11)
    ])
    db.commit()
    db.close()
    return study_data, session_data, questions


def test_assessment_persistence_exports_complete_mixed_assessment(isolated_database, tmp_path):
    factory = isolated_database
    study, session, questions = _seed_mixed_assessment(factory)
    state = dict(session["adaptive_state"])
    for index, question in enumerate(questions):
        state_holder = {}

        def persist_state(response, *, current=state, q=question):
            answers = {**current["answers"], q["id"]: q["correct_answer"]}
            next_state = {
                **current,
                "answers": answers,
                "responses": [*(current["responses"]), response],
                "answered_count": len(answers),
            }
            state_holder["state"] = next_state
            return next_state

        response = database.save_single_question_response(
            study_session=study,
            assessment_session=session,
            question=question,
            question_index=index,
            answer=question["correct_answer"],
            response_event={"response_time_seconds": 10.5 + index},
            submitted_at=datetime.utcnow(),
            decision_method=question["adaptive_method"],
            adaptive_state_factory=persist_state,
        )
        state = state_holder["state"]

    assessment_row = database.get_assessment_session(session["id"])
    database.save_question_responses(
        study_session=study,
        assessment_session=assessment_row,
        answers=state["answers"],
        response_events=state["responses"],
        submitted_at=datetime.utcnow(),
        total_time_spent=0.0,
    )
    database.update_assessment_adaptive_state(
        session["id"], adaptive_state=state, completion_status="completed"
    )
    db = factory()
    assert db.scalar(select(func.count()).select_from(QuestionResponse)) == 10
    db.close()

    output = tmp_path / "export"
    export_research_data.export_dataset(output)
    passes, warnings, failures = validate(output)
    assert warnings == []
    assert failures == []
    assert any("no failing integrity checks" in result for result in passes)

    with (output / "question_responses.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10
    assert [int(row["question_index"]) for row in rows] == list(range(10))
    for index, (row, question) in enumerate(zip(rows, questions)):
        assert row["assessment_session_id"] == session["id"]
        assert row["study_session_id"] == study["study_session_id"]
        assert row["participant_id"] == study["participant_id"]
        assert row["question_id"] == question["id"]
        assert row["submitted_answer"] == str(question["correct_answer"])
        assert row["correctness"] == "True"
        assert float(row["response_time_seconds"]) == 10.5 + index
        assert row["condition"] == ("MCRF" if index < 5 else "LEGACY")
        assert row["question_difficulty"] == ("medium" if index < 5 else "hard")


def test_response_and_adaptive_state_roll_back_together_on_commit_failure(
    isolated_database
):
    factory = isolated_database
    study, session, questions = _seed_mixed_assessment(factory)
    failure = {"armed": True}

    def fail_first_commit(_session):
        if failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated commit failure")

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    event.listen(Session, "before_commit", fail_first_commit)
    try:
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            database.save_single_question_response(
                study_session=study,
                assessment_session=session,
                question=questions[0],
                question_index=0,
                answer=questions[0]["correct_answer"],
                response_event={"response_time_seconds": 4.0},
                submitted_at=datetime.utcnow(),
                decision_method="MCRF",
                adaptive_state_factory=lambda _response: {"answers": {questions[0]["id"]: 0}},
            )
    finally:
        event.remove(Session, "before_commit", fail_first_commit)

    db = factory()
    assert db.scalar(select(func.count()).select_from(QuestionResponse)) == 0
    persisted = db.get(AssessmentSession, session["id"])
    assert persisted.adaptive_state["answers"] == {}
    db.close()


def test_timeout_exports_explicit_unanswered_rows_without_fabricated_values(
    isolated_database, tmp_path, monkeypatch
):
    factory = isolated_database
    study, session, questions = _seed_mixed_assessment(factory)
    first = database.save_single_question_response(
        study_session=study,
        assessment_session=session,
        question=questions[0],
        question_index=0,
        answer=questions[0]["correct_answer"],
        response_event={"response_time_seconds": 5.0},
        submitted_at=datetime.utcnow(),
        decision_method="MCRF",
    )
    state = {"answers": {questions[0]["id"]: questions[0]["correct_answer"]}, "responses": [first]}
    monkeypatch.setattr(
        assessment,
        "_apply_xp",
        lambda *_args: {"new_xp": 0, "new_level": 1, "leveled_up": False},
    )
    result = assessment._finalize_assessment(
        session=database.get_assessment_session(session["id"]),
        study_session=study,
        state=state,
        current_user=SimpleNamespace(id=study["user_id"]),
        db=None,
        allow_partial=True,
    )
    assert result["completion_status"] == "timed_out"

    output = tmp_path / "timeout-export"
    export_research_data.export_dataset(output)
    _, _, failures = validate(output)
    assert failures == []
    with (output / "question_responses.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10
    unanswered = [row for row in rows if row["status"] == "unanswered"]
    assert len(unanswered) == 9
    assert all(
        row["submitted_answer"] == ""
        and row["correctness"] == ""
        and row["submitted_at"] == ""
        and row["response_time_seconds"] == ""
        and row["score_points"] == ""
        for row in unanswered
    )


def test_missing_response_time_remains_missing_and_invalid_values_are_rejected(
    isolated_database
):
    study, session, questions = _seed_mixed_assessment(isolated_database)
    row = database.save_single_question_response(
        study_session=study,
        assessment_session=session,
        question=questions[0],
        question_index=0,
        answer=questions[0]["correct_answer"],
        response_event={"status": "unanswered"},
        submitted_at=datetime.utcnow(),
        decision_method="MCRF",
    )
    assert row["response_time_seconds"] is None
    assert row["status"] == "submitted"
    with pytest.raises(ValueError, match="finite non-negative"):
        database.save_single_question_response(
            study_session=study,
            assessment_session=session,
            question=questions[1],
            question_index=1,
            answer=questions[1]["correct_answer"],
            response_event={"response_time_seconds": float("nan")},
            submitted_at=datetime.utcnow() + timedelta(seconds=1),
            decision_method="MCRF",
        )
