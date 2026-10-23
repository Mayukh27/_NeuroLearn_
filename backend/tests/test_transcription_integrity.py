import os
import sys

import pytest
from types import SimpleNamespace
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ml.transcription_model as transcription_model
from ml.transcription_model import TranscriptionError, TranscriptionService
from routers import assessment, content


def test_full_transcript_forwards_provided_video_url():
    service = TranscriptionService.__new__(TranscriptionService)
    expected = [{"id": "real", "text": "Genuine transcript."}]
    calls = []

    def transcribe(video_url):
        calls.append(video_url)
        return expected

    service.transcribe_video_url = transcribe
    assert service.get_full_transcript("video-1", "https://video.example/watch") == expected
    assert calls == ["https://video.example/watch"]


def test_unavailable_whisper_fails_instead_of_returning_fallback(monkeypatch):
    service = TranscriptionService.__new__(TranscriptionService)
    service.model = None
    service._video_transcripts = {}
    service._transcription_locks = {}
    import threading
    service._locks_guard = threading.Lock()
    service._load_from_database_cache = lambda _: None
    monkeypatch.setattr(transcription_model, "WHISPER_AVAILABLE", False)

    with pytest.raises(TranscriptionError, match="unavailable"):
        service.transcribe_video_url("https://video.example/watch")


def test_full_transcript_rejects_missing_video_url():
    service = TranscriptionService.__new__(TranscriptionService)
    with pytest.raises(TranscriptionError, match="URL is required"):
        service.get_full_transcript("video-1")


@pytest.mark.parametrize("transcribe_result", [[], [SimpleNamespace(
    text="  ", start=0.0, end=1.0, words=None,
)]])
def test_audio_transcription_rejects_empty_or_blank_results(monkeypatch, transcribe_result):
    service = TranscriptionService.__new__(TranscriptionService)
    service.model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter(transcribe_result), SimpleNamespace()))
    monkeypatch.setattr(transcription_model, "WHISPER_AVAILABLE", True)

    with pytest.raises(TranscriptionError, match="no usable speech"):
        service.transcribe_audio_chunk("YQ==")


def test_audio_transcription_reports_model_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("decoder failed")

    service = TranscriptionService.__new__(TranscriptionService)
    service.model = SimpleNamespace(transcribe=fail)
    monkeypatch.setattr(transcription_model, "WHISPER_AVAILABLE", True)

    with pytest.raises(TranscriptionError, match="Audio chunk transcription failed"):
        service.transcribe_audio_chunk("YQ==")


@pytest.mark.parametrize("fails", [False, True])
def test_content_pipeline_never_generates_questions_from_failed_or_empty_transcript(monkeypatch, fails):
    def transcribe(_):
        if fails:
            raise TranscriptionError("decoder failed")
        return []

    statuses = []
    generated = []
    monkeypatch.setattr(content.transcription_service, "transcribe_video_url", transcribe)
    monkeypatch.setattr(
        content,
        "update_video_transcription_status",
        lambda course_id, video_id, available: statuses.append(available),
    )
    monkeypatch.setattr(
        content.question_generator,
        "generate_questions",
        lambda **kwargs: generated.append(kwargs),
    )

    with pytest.raises(RuntimeError):
        content._run_full_pipeline_for_video(
            "course-1", {"id": "video-1", "url": "https://video.example", "title": "Lesson"},
            "student-1", 50,
        )
    assert statuses == [False]
    assert generated == []


def test_assessment_generation_stops_before_questions_without_transcript(monkeypatch):
    monkeypatch.setattr(
        assessment,
        "get_or_create_active_study_session",
        lambda *args, **kwargs: {"study_session_id": "study-1"},
    )
    monkeypatch.setattr(
        assessment,
        "get_completed_video_context",
        lambda _: {"contributing_video_ids": ["video-1"], "transcript_text": "  "},
    )
    monkeypatch.setattr(assessment, "get_canonical_assessment_session_for_study", lambda _: None)

    async def unexpected_question_generation(**kwargs):
        raise AssertionError("question generation must not run without transcript text")

    monkeypatch.setattr(assessment, "_generate_question_block", unexpected_question_generation)
    request = SimpleNamespace(
        student_id=None, course_id="course-1", video_id="video-1", study_session_id=None,
    )
    user = SimpleNamespace(id="student-1")

    with pytest.raises(HTTPException) as error:
        assessment.generate_assessment(request, user).send(None)
    assert error.value.status_code == 409
