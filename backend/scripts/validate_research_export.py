"""Validate exported NeuroLearn research CSVs.

Run:
    cd backend
    python scripts/validate_research_export.py --dir exports/research_full-study-v1
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.export_research_data import EXPORTS

REQUIRED_FILES = [
    "participants.csv",
    "study_sessions.csv",
    "study_video_completions.csv",
    "assessments.csv",
    "question_responses.csv",
    "crs_decisions.csv",
    "legacy_decisions.csv",
    "behavioral_summaries.csv",
    "prepost_results.csv",
    "generated_questions.csv",
]
DIFFICULTIES = {"easy", "medium", "hard", ""}
CONDITIONS = {"MCRF", "LEGACY", "MIXED"}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate(export_dir: Path) -> tuple[list[str], list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    passes: list[str] = []

    data = {name: _read(export_dir / name) for name in REQUIRED_FILES}
    for name, rows in data.items():
        if not (export_dir / name).exists():
            failures.append(f"FAIL missing file: {name}")
        else:
            passes.append(f"PASS loaded {name}: {len(rows)} rows")
            with (export_dir / name).open(newline="", encoding="utf-8") as f:
                headers = set(csv.DictReader(f).fieldnames or [])
            required = set(EXPORTS[Path(name).stem][1])
            missing = sorted(required - headers)
            if missing:
                failures.append(f"FAIL {name} missing required columns: {missing}")

    participants = {r.get("participant_id") for r in data["participants.csv"]}
    sessions = {r.get("study_session_id"): r for r in data["study_sessions.csv"]}
    assessments = {r.get("id"): r for r in data["assessments.csv"]}

    for table_name in [
        "study_sessions.csv", "study_video_completions.csv", "assessments.csv", "question_responses.csv",
        "crs_decisions.csv", "legacy_decisions.csv", "behavioral_summaries.csv", "prepost_results.csv",
        "generated_questions.csv",
    ]:
        for i, row in enumerate(data[table_name], start=2):
            study_session_id = row.get("study_session_id", "")
            if table_name != "generated_questions.csv" or study_session_id:
                if not study_session_id:
                    failures.append(f"FAIL {table_name}:{i} missing study_session_id")
                elif study_session_id not in sessions:
                    failures.append(f"FAIL {table_name}:{i} orphan study_session_id={study_session_id}")
            participant_id = row.get("participant_id", "")
            if participant_id and participant_id not in participants:
                failures.append(f"FAIL {table_name}:{i} orphan participant_id={participant_id}")
            condition = row.get("condition", "")
            if condition and condition not in CONDITIONS:
                failures.append(f"FAIL {table_name}:{i} invalid condition={condition}")

    seen_responses = Counter(
        (
            r.get("study_session_id"),
            r.get("assessment_session_id"),
            r.get("question_id"),
        )
        for r in data["question_responses.csv"]
    )
    for key, count in seen_responses.items():
        if count > 1:
            failures.append(f"FAIL duplicate question response: {key}")

    seen_prepost = Counter(
        (
            r.get("study_session_id"),
            r.get("test_type"),
            r.get("question_id"),
        )
        for r in data["prepost_results.csv"]
    )
    for key, count in seen_prepost.items():
        if count > 1:
            failures.append(f"FAIL duplicate pre/post response: {key}")

    for i, row in enumerate(data["question_responses.csv"], start=2):
        assessment_id = row.get("assessment_session_id")
        if assessment_id not in assessments:
            failures.append(f"FAIL question_responses.csv:{i} orphan assessment_session_id={assessment_id}")
        response_time = _float(row.get("response_time_seconds", ""))
        if row.get("response_time_seconds", "") and response_time is None:
            failures.append(f"FAIL question_responses.csv:{i} invalid response_time_seconds={row.get('response_time_seconds')}")
        if response_time is not None and (
            not math.isfinite(response_time) or response_time < 0 or response_time > 86400
        ):
            failures.append(f"FAIL question_responses.csv:{i} impossible response_time_seconds={response_time}")
        if not row.get("condition"):
            failures.append(f"FAIL question_responses.csv:{i} missing condition")
        elif row.get("condition") not in {"MCRF", "LEGACY"}:
            failures.append(f"FAIL question_responses.csv:{i} invalid decision method={row.get('condition')}")
        assessment = assessments.get(assessment_id)
        if assessment:
            if row.get("study_session_id") != assessment.get("study_session_id"):
                failures.append(f"FAIL question_responses.csv:{i} study_session_id does not match parent assessment")
            if row.get("participant_id") != assessment.get("participant_id"):
                failures.append(f"FAIL question_responses.csv:{i} participant_id does not match parent assessment")
            try:
                assessment_questions = json.loads(assessment.get("questions") or "[]")
            except (TypeError, json.JSONDecodeError):
                assessment_questions = []
            if not isinstance(assessment_questions, list) or any(
                not isinstance(item, dict) for item in assessment_questions
            ):
                assessment_questions = []
            question = next(
                (q for q in assessment_questions if q.get("id") == row.get("question_id")),
                None,
            )
            if not question:
                failures.append(f"FAIL question_responses.csv:{i} question_id is absent from parent assessment")
            else:
                question_position = next(
                    (position for position, item in enumerate(assessment_questions) if item.get("id") == row.get("question_id")),
                    None,
                )
                if _int(row.get("question_index")) != question_position:
                    failures.append(f"FAIL question_responses.csv:{i} question_index does not match parent question order")
                if str(question.get("difficulty", "")) != row.get("question_difficulty", ""):
                    failures.append(f"FAIL question_responses.csv:{i} difficulty does not match parent question")
                expected_method = question.get("adaptive_method")
                if expected_method and expected_method != row.get("condition"):
                    failures.append(f"FAIL question_responses.csv:{i} method does not match parent question")
        status = row.get("status")
        if status == "submitted":
            if not row.get("submitted_answer") or row.get("correctness", "") not in {"True", "False", "true", "false"}:
                failures.append(f"FAIL question_responses.csv:{i} submitted response lacks answer or correctness")
            if not row.get("submitted_at"):
                failures.append(f"FAIL question_responses.csv:{i} submitted response lacks submitted_at")
            else:
                try:
                    datetime.fromisoformat(row["submitted_at"].replace("Z", "+00:00"))
                except ValueError:
                    failures.append(f"FAIL question_responses.csv:{i} submitted_at is not a valid ISO timestamp")
        elif status == "unanswered":
            if any(row.get(field, "") for field in (
                "submitted_answer", "correctness", "submitted_at", "response_time_seconds", "score_points"
            )):
                failures.append(f"FAIL question_responses.csv:{i} unanswered response contains submitted data")
        else:
            failures.append(f"FAIL question_responses.csv:{i} invalid response status={status}")

    for i, row in enumerate(data["crs_decisions.csv"], start=2):
        missing = [
            field for field in ["performance", "behavioral_cue", "response_timing", "trend", "complexity", "crs"]
            if row.get(field, "") == ""
        ]
        if missing:
            failures.append(f"FAIL crs_decisions.csv:{i} missing CRS components: {missing}")
        crs = _float(row.get("crs", ""))
        if crs is not None and not (0.0 <= crs <= 1.0):
            failures.append(f"FAIL crs_decisions.csv:{i} CRS outside [0,1]: {crs}")
        if row.get("selected_difficulty", "") not in DIFFICULTIES:
            failures.append(f"FAIL crs_decisions.csv:{i} invalid selected_difficulty={row.get('selected_difficulty')}")
        if row.get("condition") != "MCRF":
            failures.append(f"FAIL crs_decisions.csv:{i} CRS decision stored for non-MCRF condition")

    for i, row in enumerate(data["legacy_decisions.csv"], start=2):
        assessment_id = row.get("assessment_session_id")
        if assessment_id not in assessments:
            failures.append(f"FAIL legacy_decisions.csv:{i} orphan assessment_session_id={assessment_id}")
        if row.get("condition") != "LEGACY":
            failures.append(f"FAIL legacy_decisions.csv:{i} legacy decision stored for condition={row.get('condition')}")
        if row.get("selected_difficulty", "") not in DIFFICULTIES:
            failures.append(f"FAIL legacy_decisions.csv:{i} invalid selected_difficulty={row.get('selected_difficulty')}")

    for i, row in enumerate(data["assessments.csv"], start=2):
        if row.get("difficulty", "") not in DIFFICULTIES:
            failures.append(f"FAIL assessments.csv:{i} invalid difficulty={row.get('difficulty')}")
        study = sessions.get(row.get("study_session_id"))
        if study and row.get("condition") and row["condition"] != study.get("condition"):
            failures.append(f"FAIL assessments.csv:{i} inconsistent condition")
        if not row.get("created_at"):
            warnings.append(f"WARN assessments.csv:{i} missing started timestamp")

    for study_id, row in sessions.items():
        if not row.get("condition"):
            failures.append(f"FAIL study_sessions.csv missing condition for {study_id}")
        if not row.get("started_at"):
            failures.append(f"FAIL study_sessions.csv missing started_at for {study_id}")

    completions_by_study: dict[str, list[dict]] = {}
    for row in data["study_video_completions.csv"]:
        completions_by_study.setdefault(row.get("study_session_id", ""), []).append(row)
    for study_id, completions in completions_by_study.items():
        order = [int(row["completion_order"]) for row in completions if row.get("completion_order", "").isdigit()]
        if sorted(order) != list(range(1, len(completions) + 1)):
            failures.append(f"FAIL study_video_completions.csv invalid completion order for {study_id}")
        ids = [row.get("video_id") for row in sorted(completions, key=lambda value: _int(value.get("completion_order")) or 0)]
        if len(ids) != len(set(ids)):
            failures.append(f"FAIL study_video_completions.csv duplicate completed video for {study_id}")

    crs_by_assessment: dict[str, list[dict]] = {}
    for row in data["crs_decisions.csv"]:
        crs_by_assessment.setdefault(row.get("assessment_session_id", ""), []).append(row)
    legacy_by_assessment: dict[str, list[dict]] = {}
    for row in data["legacy_decisions.csv"]:
        legacy_by_assessment.setdefault(row.get("assessment_session_id", ""), []).append(row)
    responses_by_assessment: dict[str, list[dict]] = {}
    for row in data["question_responses.csv"]:
        responses_by_assessment.setdefault(row.get("assessment_session_id", ""), []).append(row)

    for assessment_id, assessment in assessments.items():
        if assessment.get("condition") != "MIXED":
            continue  # historical, one-method exports remain valid.
        completion_status = assessment.get("completion_status")
        if completion_status not in {"started", "completed", "timed_out"}:
            failures.append(f"FAIL mixed assessment {assessment_id} has invalid completion_status={completion_status}")
        try:
            questions = json.loads(assessment.get("questions") or "[]")
        except (TypeError, json.JSONDecodeError):
            failures.append(f"FAIL assessments.csv invalid questions JSON for {assessment_id}")
            questions = []
        if not isinstance(questions, list) or any(not isinstance(question, dict) for question in questions):
            failures.append(f"FAIL assessments.csv questions must be a JSON list of objects for {assessment_id}")
            questions = []
        if len(questions) != 10:
            failures.append(f"FAIL mixed assessment {assessment_id} has {len(questions)} questions; expected 10")
        if _int(assessment.get("number_of_questions")) != 10:
            failures.append(f"FAIL mixed assessment {assessment_id} number_of_questions must be 10")
        question_ids = [q.get("id") for q in questions]
        if len(question_ids) != 10 or any(not isinstance(qid, str) or not qid for qid in question_ids) or len(set(question_ids)) != 10:
            failures.append(f"FAIL mixed assessment {assessment_id} question IDs must be ten unique non-empty IDs")
        question_methods = [q.get("adaptive_method") for q in questions]
        if question_methods != ["MCRF"] * 5 + ["LEGACY"] * 5:
            failures.append(f"FAIL mixed assessment {assessment_id} question methods must be MCRF then LEGACY")
        question_difficulties = [q.get("difficulty") for q in questions]
        if any(not isinstance(difficulty, str) or not difficulty for difficulty in question_difficulties):
            failures.append(f"FAIL mixed assessment {assessment_id} has a question without difficulty")
        elif len(set(question_difficulties[:5])) != 1 or len(set(question_difficulties[5:])) != 1:
            failures.append(f"FAIL mixed assessment {assessment_id} difficulty changes within a protocol block")

        generated = {
            row.get("question_id"): row
            for row in data["generated_questions.csv"]
            if row.get("assessment_session_id") == assessment_id
        }
        if set(generated) != set(question_ids):
            failures.append(f"FAIL mixed assessment {assessment_id} generated-question records do not match assessment questions")
        for question in questions:
            generated_row = generated.get(question.get("id"))
            if generated_row and generated_row.get("difficulty") != question.get("difficulty"):
                failures.append(f"FAIL mixed assessment {assessment_id} generated-question difficulty mismatch for {question.get('id')}")
            if generated_row and generated_row.get("study_session_id") != assessment.get("study_session_id"):
                failures.append(f"FAIL mixed assessment {assessment_id} generated-question study session mismatch for {question.get('id')}")
        study_id = assessment.get("study_session_id", "")
        completed_videos = sorted(
            completions_by_study.get(study_id, []),
            key=lambda value: _int(value.get("completion_order")) or 0,
        )
        try:
            contributors = json.loads(assessment.get("contributing_video_ids") or "[]")
        except json.JSONDecodeError:
            failures.append(f"FAIL assessments.csv invalid contributing_video_ids for {assessment_id}")
            contributors = []
        if contributors != [row.get("video_id") for row in completed_videos]:
            failures.append(f"FAIL assessments.csv contributing videos do not match completed order for {assessment_id}")

        assessment_responses = responses_by_assessment.get(assessment_id, [])
        if completion_status == "started":
            partial_responses = sorted(
                assessment_responses,
                key=lambda value: _int(value.get("question_index")) if _int(value.get("question_index")) is not None else -1,
            )
            indexes = [_int(row.get("question_index")) for row in partial_responses]
            if indexes != list(range(len(partial_responses))):
                failures.append(f"FAIL incomplete mixed assessment {assessment_id} responses are not a sequential prefix")
            if any(row.get("status") != "submitted" for row in partial_responses):
                failures.append(f"FAIL incomplete mixed assessment {assessment_id} contains unanswered rows before timeout")
            if len(partial_responses) == 10:
                failures.append(f"FAIL mixed assessment {assessment_id} has ten submitted responses but is still started")
            continue
        if completion_status == "timed_out":
            continue
        is_completed = assessment.get("completion_status") == "completed"
        if not is_completed:
            continue
        responses = sorted(assessment_responses, key=lambda value: _int(value.get("question_index")) if _int(value.get("question_index")) is not None else -1)
        if len(responses) != 10:
            failures.append(f"FAIL mixed assessment {assessment_id} has {len(responses)} responses; expected 10")
            continue
        if [row.get("question_index") for row in responses] != [str(index) for index in range(10)]:
            failures.append(f"FAIL mixed assessment {assessment_id} has non-sequential question indexes")
        if [row.get("condition") for row in responses] != ["MCRF"] * 5 + ["LEGACY"] * 5:
            failures.append(f"FAIL mixed assessment {assessment_id} does not use MCRF then LEGACY response segments")
        if [row.get("question_id") for row in responses] != question_ids:
            failures.append(f"FAIL mixed assessment {assessment_id} response questions do not match assessment order")
        if any(row.get("status") != "submitted" for row in responses):
            failures.append(f"FAIL completed mixed assessment {assessment_id} contains unanswered questions")
        crs_rows = crs_by_assessment.get(assessment_id, [])
        legacy_rows = legacy_by_assessment.get(assessment_id, [])
        crs_indexes = sorted(_int(row.get("decision_index")) for row in crs_rows if _int(row.get("decision_index")) is not None)
        legacy_indexes = sorted(_int(row.get("decision_index")) for row in legacy_rows if _int(row.get("decision_index")) is not None)
        if crs_indexes != [1, 2, 3, 4, 5]:
            failures.append(f"FAIL mixed assessment {assessment_id} has MCRF decisions {crs_indexes}; expected 1-5")
        if legacy_indexes != [6, 7, 8, 9, 10]:
            failures.append(f"FAIL mixed assessment {assessment_id} has LEGACY decisions {legacy_indexes}; expected 6-10")
        if crs_rows and (
            not question_difficulties
            or any(row.get("selected_difficulty") != question_difficulties[0] for row in crs_rows)
        ):
            failures.append(f"FAIL mixed assessment {assessment_id} MCRF decision difficulty disagrees with Q1-Q5")
        if legacy_rows and (
            len(question_difficulties) <= 5
            or any(row.get("selected_difficulty") != question_difficulties[5] for row in legacy_rows)
        ):
            failures.append(f"FAIL mixed assessment {assessment_id} LEGACY decision difficulty disagrees with Q6-Q10")
        for decision_row in crs_rows + legacy_rows:
            if decision_row.get("study_session_id") != assessment.get("study_session_id"):
                failures.append(f"FAIL mixed assessment {assessment_id} decision study_session_id mismatch")
            if decision_row.get("participant_id") != assessment.get("participant_id"):
                failures.append(f"FAIL mixed assessment {assessment_id} decision participant_id mismatch")

    for assessment_id, assessment in assessments.items():
        if assessment.get("condition") != "MIXED" or assessment.get("completion_status") != "timed_out":
            continue
        try:
            timed_out_questions = json.loads(assessment.get("questions") or "[]")
        except (TypeError, json.JSONDecodeError):
            timed_out_questions = []
        if not isinstance(timed_out_questions, list) or any(
            not isinstance(question, dict) for question in timed_out_questions
        ):
            timed_out_questions = []
        timed_out_question_ids = [question.get("id") for question in timed_out_questions]
        responses = sorted(
            responses_by_assessment.get(assessment_id, []),
            key=lambda value: _int(value.get("question_index")) if _int(value.get("question_index")) is not None else -1,
        )
        if len(responses) != 10 or [row.get("question_index") for row in responses] != [str(i) for i in range(10)]:
            failures.append(f"FAIL timed-out mixed assessment {assessment_id} must export ten ordered response records")
        elif not any(row.get("status") == "unanswered" for row in responses):
            failures.append(f"FAIL timed-out mixed assessment {assessment_id} has no unanswered response record")
        if [row.get("condition") for row in responses] != ["MCRF"] * 5 + ["LEGACY"] * 5:
            failures.append(f"FAIL timed-out mixed assessment {assessment_id} does not use MCRF then LEGACY response segments")
        if [row.get("question_id") for row in responses] != timed_out_question_ids:
            failures.append(f"FAIL timed-out mixed assessment {assessment_id} response questions do not match assessment order")

    if not failures:
        passes.append("PASS no failing integrity checks")
    return passes, warnings, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory created by export_research_data.py")
    args = parser.parse_args()
    passes, warnings, failures = validate(Path(args.dir))
    for line in passes + warnings + failures:
        print(line)
    if failures:
        return 2
    if warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
