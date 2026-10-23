from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import assessment


def _install_resumable_session(monkeypatch, question_count):
    existing = {"id": "assessment-1", "questions": [{}] * question_count}
    monkeypatch.setattr(
        assessment,
        "get_or_create_active_study_session",
        lambda *args, **kwargs: {"study_session_id": "study-1"},
    )
    monkeypatch.setattr(
        assessment,
        "get_completed_video_context",
        lambda _: {
            "contributing_video_ids": ["video-1"],
            "transcript_text": "A usable transcript.",
        },
    )
    monkeypatch.setattr(
        assessment,
        "get_canonical_assessment_session_for_study",
        lambda _: existing,
    )
    request = SimpleNamespace(
        student_id=None,
        course_id="course-1",
        video_id="video-1",
        study_session_id="study-1",
        transcript_text=None,
    )
    current_user = SimpleNamespace(id="student-1")
    return request, current_user, existing


def test_generate_rejects_persisted_assessment_with_more_than_ten_questions(monkeypatch):
    request, current_user, _ = _install_resumable_session(monkeypatch, 11)

    coroutine = assessment.generate_assessment(request, current_user)
    with pytest.raises(HTTPException) as error:
        coroutine.send(None)

    assert error.value.status_code == 409
    assert "10-question protocol" in error.value.detail


def test_generate_resumes_persisted_assessment_with_exactly_ten_questions(monkeypatch):
    request, current_user, existing = _install_resumable_session(monkeypatch, 10)

    coroutine = assessment.generate_assessment(request, current_user)
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)

    assert result.value.value is existing
