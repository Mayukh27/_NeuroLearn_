"""
data/database.py — Postgres-backed data access layer.

FIX (auth/postgres/xp/leaderboard request): this file previously wrote to
a TinyDB JSON file (data/database_tinydb_legacy.py, kept for reference)
despite the manuscript claiming "PostgreSQL-backed persistence" — a real
claim-vs-implementation gap. This version is backed by an actual Postgres
database (see data/db.py) via SQLAlchemy.

Function names and return shapes are kept as close as possible to the
legacy TinyDB version so the routers that import from this module did
not all need to be rewritten — only the handful that needed real
authentication or that were silently not persisting XP were changed
(routers/student.py, routers/assessment.py, routers/gamification.py,
routers/auth.py — new).
"""
from typing import Callable, Optional
from datetime import datetime, date, timedelta
import math
import statistics
import subprocess
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from data.db import SessionLocal
from data.models_orm import (
    User, Course, AutoCourse, AssessmentSession, AssessmentResult,
    CRSHistory, Consent, StudyConsent, AttentionLog, DailyChallenge, Notification,
    DailyChallengeProgress, PasswordResetToken, RefreshToken,
    ResearchParticipant, StudySession, QuestionResponse, ResearchCRSDecision,
    ResearchLegacyDecision,
    BehavioralSummary, PrePostResult, GeneratedQuestion, StudyVideoCompletion,
    VideoTranscriptCache, LocalVideo, VideoWatchProgress,
)
from config.video_config import (
    get_nginx_video_url,
)

XP_PER_LEVEL = 100
EXPERIMENT_VERSION = "full-study-v2-mixed-segments"
CRS_CONFIG_VERSION = "crs-equal-weights-v1"
MIXED_METHOD_CONDITION = "MIXED"
FIXED_METHOD_SEQUENCE = "MCRF_THEN_LEGACY"
VALID_STUDY_CONDITIONS = {"MCRF", "LEGACY", MIXED_METHOD_CONDITION}
VALID_SEQUENCE_ORDERS = {"MCRF_THEN_LEGACY", "LEGACY_THEN_MCRF"}


class StudyConsentRequired(Exception):
    """
    FIX (A-2): raised by get_or_create_research_participant() — the root
    creation point for ResearchParticipant/StudySession rows — when no
    granted study_consent record exists for this user. Every research
    write downstream (assessment responses, prepost results, CRS
    decisions, video completions) is keyed to a study_session_id that can
    only exist if a StudySession was created, which can only happen
    through this function, so gating here gates all of them. Routers
    catch this and return 403; they must not swallow it and proceed.
    """
    pass


def level_from_xp(xp: int) -> int:
    return max(1, int(xp or 0) // XP_PER_LEVEL + 1)


def xp_to_next_level(xp: int) -> int:
    return XP_PER_LEVEL - (int(xp or 0) % XP_PER_LEVEL)


def apply_xp(user: User, amount: int) -> dict:
    before_level = level_from_xp(user.xp)
    user.xp = max(0, int(user.xp or 0) + int(amount or 0))
    user.level = level_from_xp(user.xp)
    user.xp_to_next_level = xp_to_next_level(user.xp)
    return {
        "new_xp": user.xp,
        "new_level": user.level,
        "leveled_up": user.level > before_level,
        "xp_to_next_level": user.xp_to_next_level,
    }


def _session() -> Session:
    """Short-lived session for the simple functions below (mirrors the
    original TinyDB functions' style of "just do the operation and
    return" with no explicit session threading through every call site)."""
    return SessionLocal()


def current_application_version() -> Optional[str]:
    try:
        repo_root = Path(__file__).resolve().parents[2]
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _participant_number(participant_id: str) -> int:
    try:
        return int(participant_id.rsplit("_", 1)[1])
    except Exception:
        return 0


def get_study_consent(user_id: str, db: Optional[Session] = None) -> Optional[dict]:
    """FIX (A-2): look up the current study-participation consent record."""
    owns_session = db is None
    db = db or _session()
    try:
        row = db.get(StudyConsent, user_id)
        if row is None:
            return None
        return {
            "student_id": row.student_id,
            "granted": bool(row.granted),
            "granted_at": row.granted_at.isoformat() if row.granted_at else None,
            "version": row.version,
        }
    finally:
        if owns_session:
            db.close()


def set_study_consent(user_id: str, granted: bool, version: str = "1.0") -> dict:
    """FIX (A-2): record the authenticated student's study-participation
    consent decision. This is independent of webcam consent."""
    db = _session()
    try:
        row = db.get(StudyConsent, user_id)
        now = datetime.utcnow()
        if row is None:
            row = StudyConsent(
                student_id=user_id, granted=granted, granted_at=now, version=version,
            )
            db.add(row)
        else:
            row.granted = granted
            row.granted_at = now
            row.version = version
        db.commit()
        return {
            "student_id": user_id,
            "granted": granted,
            "granted_at": now.isoformat(),
            "version": version,
        }
    finally:
        db.close()


def _next_participant_id(db: Session) -> str:
    rows = db.execute(select(ResearchParticipant.participant_id)).all()
    max_seen = max((_participant_number(r[0]) for r in rows), default=0)
    return f"participant_{max_seen + 1:03d}"


def _assigned_condition_for_participant_id(participant_id: str) -> str:
    # The actual study is within-subject: every participant receives both
    # methods in one assessment.  Keep the legacy field for historical rows,
    # but make its value truthful for newly created study records.
    return MIXED_METHOD_CONDITION


def get_or_create_research_participant(user_id: str, db: Optional[Session] = None) -> dict:
    owns_session = db is None
    db = db or _session()
    try:
        row = db.execute(
            select(ResearchParticipant).where(ResearchParticipant.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            # FIX (A-2): this is the root creation point for research
            # records. Independently verify study-participation consent
            # server-side before creating anything — never trust a
            # client-supplied flag. (Reading an *existing* participant
            # below is not gated here; only creation is.)
            consent = get_study_consent(user_id, db)
            if not (consent and consent.get("granted")):
                raise StudyConsentRequired(
                    "Study-participation consent is required before a research "
                    "record can be created for this student."
                )
            participant_id = _next_participant_id(db)
            sequence_order = FIXED_METHOD_SEQUENCE
            row = ResearchParticipant(
                participant_id=participant_id,
                user_id=user_id,
                sequence_order=sequence_order,
                assigned_condition=_assigned_condition_for_participant_id(participant_id),
            )
            db.add(row)
            db.flush()
            if owns_session:
                db.commit()
        else:
            # New sessions always follow the same within-assessment order;
            # participant-level A/B assignment is no longer part of the
            # protocol.  Existing completed study_sessions retain their own
            # historical condition/sequence values.
            row.sequence_order = FIXED_METHOD_SEQUENCE
            row.assigned_condition = MIXED_METHOD_CONDITION
            db.flush()
            if owns_session:
                db.commit()
        return {
            "participant_id": row.participant_id,
            "user_id": row.user_id,
            "sequence_order": row.sequence_order,
            "assigned_condition": row.assigned_condition,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    finally:
        if owns_session:
            db.close()


def create_study_session(
    user_id: str,
    course_id: Optional[str] = None,
    video_id: Optional[str] = None,
    module_id: Optional[str] = None,
) -> dict:
    db = _session()
    try:
        participant_doc = get_or_create_research_participant(user_id, db)
        participant = db.get(ResearchParticipant, participant_doc["participant_id"])
        participant.sequence_order = FIXED_METHOD_SEQUENCE
        participant.assigned_condition = MIXED_METHOD_CONDITION
        condition = MIXED_METHOD_CONDITION
        row = StudySession(
            participant_id=participant.participant_id,
            user_id=user_id,
            condition=condition,
            sequence_order=FIXED_METHOD_SEQUENCE,
            course_id=course_id,
            module_id=module_id or course_id,
            video_id=video_id,
            experiment_version=EXPERIMENT_VERSION,
            application_version=current_application_version(),
            crs_config_version=CRS_CONFIG_VERSION,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return study_session_to_dict(row)
    finally:
        db.close()


def get_or_create_study_session_for_material(
    user_id: str,
    course_id: Optional[str] = None,
    video_id: Optional[str] = None,
    module_id: Optional[str] = None,
) -> dict:
    db = _session()
    try:
        existing = db.execute(
            select(StudySession)
            .where(
                StudySession.user_id == user_id,
                StudySession.course_id == course_id,
                StudySession.experiment_version == EXPERIMENT_VERSION,
                StudySession.completion_status == "started",
            )
            .order_by(StudySession.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing:
            return study_session_to_dict(existing)
    finally:
        db.close()
    return create_study_session(user_id, course_id=course_id, video_id=video_id, module_id=module_id)


def get_study_session(study_session_id: str) -> Optional[dict]:
    db = _session()
    try:
        row = db.get(StudySession, study_session_id)
        return study_session_to_dict(row) if row else None
    finally:
        db.close()


def get_or_create_active_study_session(
    user_id: str,
    course_id: Optional[str] = None,
    video_id: Optional[str] = None,
    requested_study_session_id: Optional[str] = None,
) -> dict:
    db = _session()
    try:
        if requested_study_session_id:
            existing = db.get(StudySession, requested_study_session_id)
            if not existing or existing.user_id != user_id:
                raise ValueError("Invalid study_session_id for current user")
            return study_session_to_dict(existing)

        existing = db.execute(
            select(StudySession)
            .where(
                StudySession.user_id == user_id,
                StudySession.experiment_version == EXPERIMENT_VERSION,
                StudySession.completion_status == "started",
            )
            .order_by(StudySession.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing:
            if course_id and not existing.course_id:
                existing.course_id = course_id
            if video_id and not existing.video_id:
                existing.video_id = video_id
            db.commit()
            return study_session_to_dict(existing)
    finally:
        db.close()
    return create_study_session(user_id, course_id=course_id, video_id=video_id)


def complete_study_session(study_session_id: str, status: str = "completed") -> Optional[dict]:
    db = _session()
    try:
        row = db.get(StudySession, study_session_id)
        if not row:
            return None
        row.completion_status = status
        row.ended_at = datetime.utcnow()
        if row.pretest_score is not None and row.posttest_score is not None:
            row.learning_gain = row.posttest_score - row.pretest_score
        db.commit()
        db.refresh(row)
        return study_session_to_dict(row)
    finally:
        db.close()


def study_session_to_dict(row: StudySession) -> dict:
    return {
        "study_session_id": row.study_session_id,
        "participant_id": row.participant_id,
        "user_id": row.user_id,
        "condition": row.condition,
        "sequence_order": row.sequence_order,
        "course_id": row.course_id,
        "module_id": row.module_id,
        "video_id": row.video_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "completion_status": row.completion_status,
        "experiment_version": row.experiment_version,
        "application_version": row.application_version,
        "crs_config_version": row.crs_config_version,
        "pretest_score": row.pretest_score,
        "posttest_score": row.posttest_score,
        "learning_gain": row.learning_gain,
        "camera_used": row.camera_used,
        "camera_opted_out": row.camera_opted_out,
        "camera_revoked": row.camera_revoked,
    }


def record_completed_video(
    study_session_id: str,
    user_id: str,
    video_id: str,
    transcript_text: Optional[str] = None,
) -> dict:
    """Persist one completed video without treating an opened video as done.

    The client calls this only from a terminal playback event.  Repeating a
    completion event is intentionally idempotent and may enrich a previously
    empty transcript, but never changes the original order or timestamp.
    """
    db = _session()
    try:
        study = db.get(StudySession, study_session_id)
        if not study or study.user_id != user_id:
            raise ValueError("Invalid study_session_id for current user")
        if study.completion_status != "started":
            raise ValueError("Study session is no longer accepting completed videos")
        if not video_id:
            raise ValueError("video_id is required")

        existing = db.execute(
            select(StudyVideoCompletion).where(
                StudyVideoCompletion.study_session_id == study_session_id,
                StudyVideoCompletion.video_id == video_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            next_order = int(
                db.execute(
                    select(func.count(StudyVideoCompletion.id)).where(
                        StudyVideoCompletion.study_session_id == study_session_id
                    )
                ).scalar_one()
                or 0
            ) + 1
            existing = StudyVideoCompletion(
                study_session_id=study_session_id,
                participant_id=study.participant_id,
                video_id=video_id,
                completion_order=next_order,
                transcript_text=(transcript_text or None),
            )
            db.add(existing)
            if not study.video_id:
                study.video_id = video_id

            # FIX (phase-2 request): total_watch_time was defined on the
            # User model and returned in /api/student/profile but never
            # written anywhere, so the dashboard's "Study Time" stat was
            # always 0 from the backend and had to be faked client-side
            # (estimatedHours * progress%). Accrue it here, once per
            # distinct completed video (guarded by the `existing is None`
            # branch above so re-completing the same video is still a
            # no-op), using the video's real recorded duration.
            user = db.get(User, user_id)
            local_video = db.get(LocalVideo, video_id)
            if user is not None and local_video is not None and local_video.duration_seconds:
                user.total_watch_time = (user.total_watch_time or 0) + round(
                    local_video.duration_seconds / 60
                )
        elif transcript_text and not (existing.transcript_text or "").strip():
            existing.transcript_text = transcript_text

        db.commit()
        db.refresh(existing)
        return {
            "study_session_id": study_session_id,
            "video_id": existing.video_id,
            "completion_order": existing.completion_order,
            "completed_at": existing.completed_at.isoformat(),
        }
    finally:
        db.close()


def get_completed_video_context(study_session_id: str) -> dict:
    """Return the ordered, server-recorded material eligible for assessment."""
    db = _session()
    try:
        rows = db.execute(
            select(StudyVideoCompletion)
            .where(StudyVideoCompletion.study_session_id == study_session_id)
            .order_by(StudyVideoCompletion.completion_order.asc())
        ).scalars().all()
        return {
            "contributing_video_ids": [row.video_id for row in rows],
            "transcript_text": "\n\n".join(
                f"[Video {row.video_id}]\n{row.transcript_text.strip()}"
                for row in rows
                if row.transcript_text and row.transcript_text.strip()
            ),
            "videos": [
                {
                    "video_id": row.video_id,
                    "completion_order": row.completion_order,
                    "completed_at": row.completed_at.isoformat(),
                    "transcript_present": bool(row.transcript_text and row.transcript_text.strip()),
                }
                for row in rows
            ],
        }
    finally:
        db.close()


def get_completed_video_behavioral_score(study_session_id: str) -> Optional[float]:
    """Average only observations for videos actually completed in this study.

    FIX (D-4 follow-up): this used to return 50.0 for "no completed videos"
    and "no attention logs" alike — indistinguishable from a genuine 50%
    measurement by the time it reached compute_crs(). That silently
    defeated the behavioral_cue source flag added in ml/crs.py (SAP
    section 9's "source flags" requirement): a student who never granted
    camera consent looked identical, in the stored decision record, to
    one whose camera genuinely measured 50%. Returns None for "no genuine
    data" instead — callers (adaptive_engine / compute_crs) already treat
    None as "apply the documented neutral default and flag it as such."
    """
    db = _session()
    try:
        completed_ids = {
            row[0]
            for row in db.execute(
                select(StudyVideoCompletion.video_id).where(
                    StudyVideoCompletion.study_session_id == study_session_id
                )
            ).all()
        }
        if not completed_ids:
            return None
        logs = db.execute(
            select(AttentionLog).where(AttentionLog.study_session_id == study_session_id)
        ).scalars().all()
        scores = [float(log.score) for log in logs if log.video_id in completed_ids and log.score is not None]
        return sum(scores) / len(scores) if scores else None
    finally:
        db.close()


# ── Persistent video transcript cache ──────────────────────
# Global, video_url-keyed cache of real Whisper transcripts (see
# ml/transcription_model.py), separate from StudyVideoCompletion above:
# StudyVideoCompletion.transcript_text is the session-scoped record of
# what a specific student watched/completed in a specific study session;
# this table is a single, session-independent row per video_url so the
# same educational video isn't re-downloaded/re-transcribed every time a
# different student (or a restarted backend) needs it again.

def get_cached_video_transcript(video_url: str) -> Optional[list[dict]]:
    """Look up a persisted real transcript by video URL.

    Returns the exact segment list previously saved by
    save_video_transcript (same shape produced by Faster-Whisper: id,
    text, timestamp, start_time, end_time, confidence, model_response),
    or None if this video has never been successfully transcribed and
    persisted before.
    """
    db = _session()
    try:
        row = db.get(VideoTranscriptCache, video_url)
        if not row or not row.segments:
            return None
        return row.segments
    finally:
        db.close()


def save_video_transcript(video_url: str, segments: list[dict]) -> None:
    """Persist a successfully-produced real transcript so it survives a
    backend restart. Upserts on video_url (the primary key), so calling
    this more than once for the same video updates the one existing row
    rather than creating a duplicate.
    """
    db = _session()
    try:
        db.merge(VideoTranscriptCache(video_url=video_url, segments=segments))
        db.commit()
    finally:
        db.close()


# ── Users / Students ────────────────────────────────────────
# NOTE: "student" and "user" are the same row (see models_orm.User) —
# every account created via /api/auth/signup is a student profile.

def _user_to_dict(user: User, db: Session) -> dict:
    """Shape a User row into the same dict shape StudentProfile expects,
    including a live-computed `rank` (position in the real leaderboard —
    see get_leaderboard) and `total_courses_completed` (real per-student
    course-completion count — see _count_completed_courses_for_user)
    rather than stored, staleable fields."""
    rank = _compute_rank(db, user.id)
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "avatar": user.avatar,
        "level": level_from_xp(user.xp),
        "xp": user.xp,
        "xp_to_next_level": xp_to_next_level(user.xp),
        "streak": user.streak,
        "best_streak": user.best_streak,
        "total_courses_completed": _count_completed_courses_for_user(db, user.id),
        "total_watch_time": user.total_watch_time,
        "joined_date": user.joined_date.isoformat() if isinstance(user.joined_date, date) else str(user.joined_date),
        "rank": rank,
        "badges": user.badges or [],
    }


def _count_completed_courses_for_user(db: Session, user_id: str) -> int:
    """FIX (phase-2 request): User.total_courses_completed was defined,
    initialized to 0 at signup, and returned in the profile/leaderboard —
    but nothing in the backend ever incremented it, so it was permanently
    0 for every student. Rather than add a fragile stored counter that
    has to be kept in sync with completions across multiple study
    sessions (and could double-count or drift), derive it the same way
    per-course progress is now derived: count courses where this
    student's real completed-video set covers every video in the course.
    """
    completed_ids = _get_completed_video_ids_for_user(db, user_id)
    if not completed_ids:
        return 0
    count = 0
    for course in db.execute(select(Course)).scalars().all():
        links = course.video_links or []
        total = course.total_videos or len(links)
        if not total:
            continue
        course_video_ids = {v.get("local_video_id") for v in links if v.get("local_video_id")}
        if course_video_ids and course_video_ids.issubset(completed_ids):
            count += 1
    return count


def _compute_rank(db: Session, student_id: str) -> int:
    """1-indexed position of student_id in the real, live XP ordering —
    replaces the old static `rank` field that was seeded once and never
    recalculated as XP changed."""
    ids_by_xp = [
        row[0] for row in db.execute(
            select(User.id).order_by(User.xp.desc())
        ).all()
    ]
    try:
        return ids_by_xp.index(student_id) + 1
    except ValueError:
        return len(ids_by_xp) + 1


def get_student(student_id: str) -> Optional[dict]:
    db = _session()
    try:
        user = db.get(User, student_id)
        return _user_to_dict(user, db) if user else None
    finally:
        db.close()


def update_student(student_id: str, updates: dict):
    db = _session()
    try:
        user = db.get(User, student_id)
        if not user:
            return
        for k, v in updates.items():
            setattr(user, k, v)
        db.commit()
    finally:
        db.close()


# ── Courses ──────────────────────────────────────────────────

def get_all_courses(user_id: Optional[str] = None) -> list[dict]:
    db = _session()
    try:
        rows = db.execute(select(Course)).scalars().all()
        completed_ids = _get_completed_video_ids_for_user(db, user_id) if user_id else None
        progress_by_video = _get_watch_progress_for_user(db, user_id) if user_id else None
        return [
            _course_to_dict(c, completed_video_ids=completed_ids, progress_by_video=progress_by_video)
            for c in rows
        ]
    finally:
        db.close()


def get_course(course_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    db = _session()
    try:
        c = db.get(Course, course_id)
        if not c:
            return None
        completed_ids = _get_completed_video_ids_for_user(db, user_id) if user_id else None
        progress_by_video = _get_watch_progress_for_user(db, user_id) if user_id else None
        return _course_to_dict(c, completed_video_ids=completed_ids, progress_by_video=progress_by_video)
    finally:
        db.close()


def _get_completed_video_ids_for_user(db: Session, user_id: str) -> set[str]:
    """Distinct local_video ids this student has actually completed,
    across every one of their study sessions (any condition/sequence).

    FIX (phase-2 request): Course.completed_videos/Course.progress are
    columns on the *global* course row (shared by every student), and
    nothing in the backend ever wrote to them — so every student saw
    0%/0 completed on every course regardless of what they'd actually
    watched. Real per-student completion lives in StudyVideoCompletion
    (joined through StudySession.user_id); this computes that set so
    _course_to_dict can derive real progress per request instead.
    """
    rows = db.execute(
        select(StudyVideoCompletion.video_id)
        .join(StudySession, StudySession.study_session_id == StudyVideoCompletion.study_session_id)
        .where(StudySession.user_id == user_id)
        .distinct()
    ).scalars().all()
    return set(rows)


def upsert_video_watch_progress(
    user_id: str, video_id: str, position_seconds: float, duration_seconds: Optional[float] = None
) -> dict:
    """Persist how far into a video this student has actually watched.

    FIX (phase-4 request): "resume where I left off" needs a real
    per-student position to resume *to* — there was no such column
    anywhere before this. Called frequently (throttled client-side, not
    every timeupdate tick) from the video player, so this is a plain
    upsert, not an audit trail like StudyVideoCompletion.
    """
    db = _session()
    try:
        row = db.execute(
            select(VideoWatchProgress).where(
                VideoWatchProgress.user_id == user_id,
                VideoWatchProgress.video_id == video_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = VideoWatchProgress(user_id=user_id, video_id=video_id)
            db.add(row)
        row.position_seconds = max(0.0, position_seconds)
        if duration_seconds:
            row.duration_seconds = duration_seconds
        db.commit()
        return {"video_id": video_id, "position_seconds": row.position_seconds}
    finally:
        db.close()


def _get_watch_progress_for_user(db: Session, user_id: str) -> dict[str, float]:
    """video_id -> position_seconds for every video this student has any
    saved playback position for (used to resume the right video/spot)."""
    rows = db.execute(
        select(VideoWatchProgress.video_id, VideoWatchProgress.position_seconds).where(
            VideoWatchProgress.user_id == user_id
        )
    ).all()
    return {video_id: (position_seconds or 0) for video_id, position_seconds in rows}


def _course_to_dict(
    c: Course,
    completed_video_ids: Optional[set[str]] = None,
    progress_by_video: Optional[dict[str, float]] = None,
) -> dict:
    db = _session()
    try:
        video_links = []
        completed_count = 0

        for video in c.video_links or []:
            video_copy = dict(video)

            local_video_id = video_copy.get("local_video_id")

            if local_video_id:
                local_video = db.get(LocalVideo, local_video_id)

                if local_video:
                    video_copy["url"] = get_nginx_video_url(
                        local_video.relative_path
                    )

            if completed_video_ids is not None:
                # FIX (phase-4 request): "completed" used to be a static
                # False written once when the video was attached to the
                # course (a global, admin-time value — see
                # attach_local_video_to_course/attach_youtube_video_to_course)
                # and never updated per student, which made the "resume at
                # first incomplete video" logic meaningless. Overwrite with
                # this student's real completion state.
                is_completed = local_video_id in completed_video_ids
                video_copy["completed"] = is_completed
                if is_completed:
                    completed_count += 1
            if progress_by_video is not None and local_video_id:
                position = progress_by_video.get(local_video_id, 0)
                duration = video_copy.get("duration") or 0
                # FIX (phase-4 request): watched_percent was hardcoded to 0
                # at attach-time (see attach_local_video_to_course) and
                # never updated — VideoLinkSelector.tsx already renders a
                # partial-progress bar from it, it just never had real data.
                video_copy["watched_percent"] = (
                    min(100.0, round((position / duration) * 100, 1)) if duration else 0
                )

            video_links.append(video_copy)

        total = c.total_videos or len(video_links)
        if completed_video_ids is not None:
            # Real per-student values, derived above — NOT the shared
            # global c.completed_videos/c.progress columns.
            completed_videos = completed_count
            progress = round((completed_count / total) * 100, 1) if total else 0
        else:
            # No authenticated user in scope (e.g. an internal/admin call
            # with no student context) — fall back to the raw columns
            # rather than silently claiming 0/0 progress for a real user.
            completed_videos = c.completed_videos
            progress = c.progress

        return {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "icon": c.icon,
            "category": c.category,
            "difficulty": c.difficulty,
            "total_videos": c.total_videos,
            "completed_videos": completed_videos,
            "progress": progress,
            "estimated_hours": c.estimated_hours,
            "tags": c.tags or [],
            "video_links": video_links,
        }
    finally:
        db.close()
# ── Course admin management (V2, Phase 4) ───────────────────
# All course/video-catalog content in V2 is created and removed through
# these functions (called only from admin-gated endpoints in
# routers/courses.py — see services/admin_auth.py). There is no hardcoded
# seed catalog in V2.

def bulk_import_course(
    *,
    title: str,
    description: str = "",
    icon: str = "📚",
    category: str = "General",
    difficulty: str = "Beginner",
    estimated_hours: float = 0,
    tags: Optional[list[str]] = None,
    videos: list[dict],
) -> dict:
    """Create one Course plus a LocalVideo row and video_links entry for
    every item in `videos`, as a SINGLE transaction — either the whole
    course and all its videos are created, or none of them are.

    FIX (phase-5 request): the previous flow was create-course, then
    attach-one-video N times (each its own commit) — a failure partway
    through left a half-populated course with no automatic cleanup. This
    is the real "bulk import a whole course folder" endpoint the admin
    UX needs; each item in `videos` is already a validated, on-disk
    relative_path with its ffprobe'd duration (see routers/courses.py
    for the upload/probe step — this function only does the DB part).

    Each dict in `videos` must have: relative_path, title, order, and
    optionally duration_seconds, file_size_bytes.
    """
    from config.video_config import get_nginx_video_url  # local import avoids a module-load-order cycle

    if not videos:
        raise ValueError("bulk_import_course requires at least one video.")

    db = _session()
    try:
        course = Course(
            id=uuid.uuid4().hex,
            title=title,
            description=description,
            icon=icon,
            category=category,
            difficulty=difficulty,
            total_videos=len(videos),
            completed_videos=0,
            progress=0,
            estimated_hours=estimated_hours,
            tags=tags or [],
            video_links=[],
        )
        db.add(course)
        db.flush()  # assign course.id-backed FKs without committing yet

        video_links = []
        for v in videos:
            local_video = LocalVideo(
                id=uuid.uuid4().hex,
                title=v["title"],
                relative_path=v["relative_path"],
                course_id=course.id,
                duration_seconds=v.get("duration_seconds"),
                file_size_bytes=v.get("file_size_bytes"),
                order=v["order"],
            )
            db.add(local_video)
            db.flush()

            video_links.append({
                # FIX (found via live HTTP testing while verifying phase
                # 2/4/5 end-to-end): this id is the ONLY video identifier
                # the student-facing schema exposes (schemas.models.
                # VideoLink has no local_video_id field), and it's the
                # exact value the frontend sends back for completion
                # (completeStudyVideo) and resume (saveVideoProgress).
                # A random id here — as this used to be — means neither
                # of those could ever match a LocalVideo row on the way
                # back in; completion/watch-time/resume would silently
                # no-op forever. Using local_video.id directly makes the
                # round-trip actually work, and stays unique within this
                # course's video_links list (one link per local video,
                # per course).
                "id": local_video.id,
                "local_video_id": local_video.id,
                "title": v["title"],
                "url": get_nginx_video_url(v["relative_path"]),
                "duration": int(v.get("duration_seconds") or 0),
                "thumbnail": "",
                "order": v["order"],
                "completed": False,
                "watched_percent": 0,
            })

        course.video_links = video_links
        db.commit()
        db.refresh(course)
        return _course_to_dict(course)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_course(
    *,
    title: str,
    description: str = "",
    icon: str = "📚",
    category: str = "General",
    difficulty: str = "Beginner",
    estimated_hours: float = 0,
    tags: Optional[list[str]] = None,
) -> dict:
    db = _session()
    try:
        row = Course(
            id=uuid.uuid4().hex,
            title=title,
            description=description,
            icon=icon,
            category=category,
            difficulty=difficulty,
            total_videos=0,
            completed_videos=0,
            progress=0,
            estimated_hours=estimated_hours,
            tags=tags or [],
            video_links=[],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _course_to_dict(row)
    finally:
        db.close()


def delete_course(course_id: str) -> bool:
    """Deletes the course row (and its video_links catalog entries with
    it). Does NOT delete any LocalVideo rows or files — those are
    independent, reusable library entries (a video can be detached and
    re-attached to a different course without re-registering it).
    """
    db = _session()
    try:
        c = db.get(Course, course_id)
        if not c:
            return False
        db.delete(c)
        db.commit()
        return True
    finally:
        db.close()


def attach_local_video_to_course(
    course_id: str,
    local_video_id: str,
    *,
    title: Optional[str] = None,
    order: Optional[int] = None,
) -> Optional[dict]:
    """Appends a VideoLink entry (built from an existing LocalVideo's
    metadata) to course.video_links. The URL is the Nginx-served local
    video URL (NGINX_VIDEO_BASE_URL + relative_path) — never a YouTube
    link, never touches FastAPI for the actual bytes. Returns the updated
    course dict, or None if the course or local video doesn't exist.
    """
    from config.video_config import get_nginx_video_url  # local import avoids a module-load-order cycle

    db = _session()
    try:
        course = db.get(Course, course_id)
        video = db.get(LocalVideo, local_video_id)
        if not course or not video:
            return None

        video_links = list(course.video_links or [])
        if any(v.get("local_video_id") == local_video_id for v in video_links):
            return _course_to_dict(course)  # already attached; idempotent

        link_order = order if order is not None else len(video_links) + 1
        video_links.append({
            # FIX (same root cause as bulk_import_course — see comment
            # there, found via live testing): must equal local_video_id.
            "id": video.id,
            "local_video_id": video.id,
            "title": title or video.title,
            "url": get_nginx_video_url(video.relative_path),
            "duration": int(video.duration_seconds or 0),
            "thumbnail": "",
            "order": link_order,
            "completed": False,
            "watched_percent": 0,
        })
        course.video_links = video_links
        course.total_videos = len(video_links)
        db.commit()
        db.refresh(course)
        return _course_to_dict(course)
    finally:
        db.close()


def attach_youtube_video_to_course(
    course_id: str,
    *,
    title: str,
    youtube_url: str,
    order: Optional[int] = None,
) -> Optional[dict]:
    """Admin-only path (V2, Phase 4 revised): appends a VideoLink built
    directly from an admin-supplied YouTube URL — no LocalVideo row,
    since there's no local file. This is the one and only place a
    YouTube URL enters the catalog; it is never seeded and never
    reachable from a student-facing input (see routers/courses.py).
    """
    db = _session()
    try:
        course = db.get(Course, course_id)
        if not course:
            return None

        video_links = list(course.video_links or [])
        link_order = order if order is not None else len(video_links) + 1
        video_links.append({
            "id": uuid.uuid4().hex,
            "local_video_id": None,
            "title": title,
            "url": youtube_url,
            "duration": 0,
            "thumbnail": "",
            "order": link_order,
            "completed": False,
            "watched_percent": 0,
        })
        course.video_links = video_links
        course.total_videos = len(video_links)
        db.commit()
        db.refresh(course)
        return _course_to_dict(course)
    finally:
        db.close()


def detach_video_from_course(course_id: str, video_link_id: str) -> Optional[dict]:
    """Removes one entry from course.video_links by its VideoLink id
    (not the underlying LocalVideo id). Does not delete the LocalVideo
    row — it remains in the library, available to attach elsewhere.
    """
    db = _session()
    try:
        course = db.get(Course, course_id)
        if not course:
            return None
        video_links = [v for v in (course.video_links or []) if v.get("id") != video_link_id]
        course.video_links = video_links
        course.total_videos = len(video_links)
        db.commit()
        db.refresh(course)
        return _course_to_dict(course)
    finally:
        db.close()


# ── Local videos (V2, Phase 2) ──────────────────────────────
# Metadata + a safe, validated RELATIVE path only — never binaries, never
# an absolute filesystem path. relative_path is validated by
# services/video_path_safety.py before it ever reaches these functions.

def _local_video_to_dict(v: LocalVideo) -> dict:
    return {
        "id": v.id,
        "title": v.title,
        "relative_path": v.relative_path,
        "course_id": v.course_id,
        "duration_seconds": v.duration_seconds,
        "file_size_bytes": v.file_size_bytes,
        "order": v.order,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


def create_local_video(
    *,
    title: str,
    relative_path: str,
    course_id: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    file_size_bytes: Optional[int] = None,
    order: Optional[int] = None,
    video_id: Optional[str] = None,
) -> dict:
    """Insert a LocalVideo row. `relative_path` must already be validated
    (see services/video_path_safety.validate_relative_video_path) by the
    caller — this function does not re-validate, to keep the data layer
    free of filesystem/config concerns; routers/videos.py is responsible
    for calling validation before this.
    """
    db = _session()
    try:
        row = LocalVideo(
            **({"id": video_id} if video_id else {}),
            title=title,
            relative_path=relative_path,
            course_id=course_id,
            duration_seconds=duration_seconds,
            file_size_bytes=file_size_bytes,
            order=order,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _local_video_to_dict(row)
    finally:
        db.close()


def get_local_video(video_id: str) -> Optional[dict]:
    db = _session()
    try:
        v = db.get(LocalVideo, video_id)
        return _local_video_to_dict(v) if v else None
    finally:
        db.close()


def get_local_video_by_path(relative_path: str) -> Optional[dict]:
    db = _session()
    try:
        v = db.execute(
            select(LocalVideo).where(LocalVideo.relative_path == relative_path)
        ).scalar_one_or_none()
        return _local_video_to_dict(v) if v else None
    finally:
        db.close()


def list_local_videos(course_id: Optional[str] = None) -> list[dict]:
    db = _session()
    try:
        stmt = select(LocalVideo).order_by(LocalVideo.order, LocalVideo.created_at)
        if course_id:
            stmt = stmt.where(LocalVideo.course_id == course_id)
        rows = db.execute(stmt).scalars().all()
        return [_local_video_to_dict(v) for v in rows]
    finally:
        db.close()


def delete_local_video(video_id: str) -> bool:
    """Deletes the metadata row only — never touches the underlying file."""
    db = _session()
    try:
        v = db.get(LocalVideo, video_id)
        if not v:
            return False
        db.delete(v)
        db.commit()
        return True
    finally:
        db.close()


# ── Assessment sessions/results ─────────────────────────────

def save_assessment_session(session: dict):
    db = _session()
    try:
        row = AssessmentSession(
            id=session["id"],
            student_id=session["student_id"],
            study_session_id=session.get("study_session_id"),
            participant_id=session.get("participant_id"),
            condition=session.get("condition"),
            course_id=session.get("course_id"),
            video_id=session.get("video_id"),
            contributing_video_ids=session.get("contributing_video_ids", []),
            questions=session.get("questions", []),
            difficulty=session.get("difficulty", "medium"),
            starting_difficulty=session.get("starting_difficulty", session.get("difficulty", "medium")),
            selected_difficulty=session.get("selected_difficulty", session.get("difficulty", "medium")),
            completion_status=session.get("completion_status", "started"),
            number_of_questions=len(session.get("questions", [])),
            time_limit=session.get("time_limit", 420),
            attention_score_during_video=session.get("attention_score_during_video", 50),
            transcript_text=session.get("transcript_text"),
            adaptive_state=session.get("adaptive_state", {}),
        )
        db.merge(row)
        db.commit()
    finally:
        db.close()


def get_assessment_session(session_id: str) -> Optional[dict]:
    db = _session()
    try:
        s = db.get(AssessmentSession, session_id)
        if not s:
            return None
        return {
            "id": s.id, "student_id": s.student_id, "questions": s.questions or [],
            "study_session_id": s.study_session_id,
            "participant_id": s.participant_id,
            "condition": s.condition,
            "course_id": s.course_id,
            "video_id": s.video_id,
            "contributing_video_ids": s.contributing_video_ids or [],
            "difficulty": s.difficulty, "time_limit": s.time_limit,
            "starting_difficulty": s.starting_difficulty or s.difficulty,
            "selected_difficulty": s.selected_difficulty or s.difficulty,
            "completion_status": s.completion_status,
            "attention_score_during_video": s.attention_score_during_video,
            "transcript_text": s.transcript_text,
            "adaptive_state": s.adaptive_state or {},
            "adaptive_metadata": (s.adaptive_state or {}).get("adaptive_metadata", {
                "previous_score": None,
                "adjusted_difficulty": s.difficulty,
                "reason": "Assessment protocol initialized.",
            }),
        }
    finally:
        db.close()


def get_canonical_assessment_session_for_study(study_session_id: str) -> Optional[dict]:
    db = _session()
    try:
        rows = db.execute(
            select(AssessmentSession)
            .where(AssessmentSession.study_session_id == study_session_id)
            .order_by(AssessmentSession.created_at.desc())
        ).scalars().all()
        row = next((candidate for candidate in rows if candidate.questions), None) or (rows[0] if rows else None)
        if not row:
            return None
        return {
            "id": row.id, "student_id": row.student_id, "questions": row.questions or [],
            "study_session_id": row.study_session_id,
            "participant_id": row.participant_id,
            "condition": row.condition,
            "course_id": row.course_id,
            "video_id": row.video_id,
            "contributing_video_ids": row.contributing_video_ids or [],
            "difficulty": row.difficulty, "time_limit": row.time_limit,
            "starting_difficulty": row.starting_difficulty or row.difficulty,
            "selected_difficulty": row.selected_difficulty or row.difficulty,
            "completion_status": row.completion_status,
            "attention_score_during_video": row.attention_score_during_video,
            "transcript_text": row.transcript_text,
            "adaptive_state": row.adaptive_state or {},
            "adaptive_metadata": (row.adaptive_state or {}).get("adaptive_metadata", {
                "previous_score": None,
                "adjusted_difficulty": row.difficulty,
                "reason": "Assessment protocol initialized.",
            }),
        }
    finally:
        db.close()


def save_assessment_result(result: dict):
    db = _session()
    try:
        known = {"id", "student_id", "session_id", "score", "percentage", "xp_earned", "timestamp"}
        row = AssessmentResult(
            id=result.get("id") or f"{result['session_id']}_{result.get('timestamp', '')}",
            student_id=result["student_id"],
            session_id=result.get("session_id"),
            score=result.get("score", result.get("percentage")),
            percentage=result.get("percentage"),
            xp_earned=result.get("xp_earned", 0),
            timestamp=result.get("timestamp", datetime.utcnow().timestamp()),
            payload={k: v for k, v in result.items() if k not in known},
        )
        db.add(row)
        session = db.get(AssessmentSession, result.get("session_id"))
        if session:
            session.completion_status = result.get("completion_status", "completed")
            session.completed_at = datetime.utcfromtimestamp(result["timestamp"]) if result.get("timestamp") else datetime.utcnow()
            session.ending_difficulty = result.get("next_difficulty")
            session.total_score = result.get("earned_points")
            session.percentage = result.get("percentage")
            session.total_duration_seconds = result.get("time_spent")
            session.number_of_questions = result.get("total_questions")
            session.number_correct = result.get("correct_answers")
        db.commit()
    finally:
        db.close()


def get_student_results(student_id: str) -> list[dict]:
    db = _session()
    try:
        rows = db.execute(
            select(AssessmentResult).where(AssessmentResult.student_id == student_id)
        ).scalars().all()
        out = []
        for r in rows:
            d = dict(r.payload or {})
            d.update({
                "student_id": r.student_id, "session_id": r.session_id,
                "score": r.score, "percentage": r.percentage,
                "xp_earned": r.xp_earned, "timestamp": r.timestamp,
            })
            out.append(d)
        return out
    finally:
        db.close()


def get_recent_scores_pct(student_id: str, limit: int = 5) -> list[float]:
    db = _session()
    try:
        rows = db.execute(
            select(AssessmentResult.percentage, AssessmentResult.timestamp)
            .where(AssessmentResult.student_id == student_id)
            .order_by(AssessmentResult.timestamp.asc())
        ).all()
        return [r[0] for r in rows[-limit:] if r[0] is not None]
    finally:
        db.close()


# ── CRS history (unchanged shape from legacy — Phase 10/11) ────

def save_crs_record(record: dict) -> dict:
    required = {
        "student_id", "timestamp", "performance", "behavioral_cue", "integrity",
        "trend", "complexity", "crs", "difficulty", "explanation",
    }
    missing = required - record.keys()
    if missing:
        raise ValueError(f"save_crs_record missing required fields: {sorted(missing)}")

    record = dict(record)
    record.setdefault("assessment_id", None)
    db = _session()
    try:
        row = CRSHistory(**{k: record[k] for k in [
            "student_id", "assessment_id", "timestamp", "performance",
            "behavioral_cue", "integrity", "trend", "complexity", "crs",
            "difficulty", "explanation",
        ]})
        row.study_session_id = record.get("study_session_id")
        row.participant_id = record.get("participant_id")
        row.condition = record.get("condition")
        db.add(row)
        db.commit()
        logger.info(
            f"CRS persisted: student={record['student_id']} crs={record['crs']:.3f} "
            f"difficulty={record['difficulty']}"
        )
    finally:
        db.close()
    return record


def get_crs_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    db = _session()
    try:
        rows = db.execute(
            select(CRSHistory)
            .where(CRSHistory.student_id == student_id)
            .order_by(CRSHistory.timestamp.asc())
        ).scalars().all()
        records = [{
            "student_id": r.student_id, "assessment_id": r.assessment_id,
            "timestamp": r.timestamp, "performance": r.performance,
            "behavioral_cue": r.behavioral_cue, "integrity": r.integrity, "trend": r.trend,
            "complexity": r.complexity, "crs": r.crs, "difficulty": r.difficulty,
            "explanation": r.explanation,
        } for r in rows]
        return records[-limit:] if limit else records
    finally:
        db.close()


def get_current_crs(student_id: str) -> Optional[dict]:
    history = get_crs_history(student_id)
    return history[-1] if history else None


def _parse_dt(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def save_generated_questions(session: dict) -> None:
    db = _session()
    try:
        for q in session.get("questions", []):
            meta = q.get("llm_metadata") or {}
            row = GeneratedQuestion(
                question_id=q["id"],
                study_session_id=session.get("study_session_id"),
                assessment_session_id=session.get("id"),
                model_version=meta.get("model"),
                generated_live_fallback=meta.get("generated_from"),
                source_material=", ".join(session.get("contributing_video_ids") or []) or session.get("video_id") or session.get("course_id"),
                difficulty=q.get("difficulty"),
                bloom_level=meta.get("blooms_level"),
                generation_timestamp=datetime.utcnow(),
                question_metadata=meta,
            )
            db.merge(row)
        db.commit()
    finally:
        db.close()


def save_question_responses(
    *,
    study_session: dict,
    assessment_session: dict,
    answers: dict,
    response_events: Optional[list[dict]],
    submitted_at: datetime,
    total_time_spent: float,
    decision_method: Optional[str] = None,
    adaptive_state_factory: Optional[Callable[[dict], dict]] = None,
) -> list[dict]:
    questions = assessment_session.get("questions", [])
    events = {e.get("question_id"): e for e in (response_events or []) if e.get("question_id")}
    rows_out = []
    db = _session()
    try:
        for idx, q in enumerate(questions):
            question_id = q["id"]
            event = events.get(question_id, {})
            try:
                idx = int(event.get("question_index", idx))
            except (TypeError, ValueError):
                pass
            existing = db.execute(
                select(QuestionResponse).where(
                    QuestionResponse.study_session_id == study_session["study_session_id"],
                    QuestionResponse.assessment_session_id == assessment_session["id"],
                    QuestionResponse.question_id == question_id,
                )
            ).scalar_one_or_none()
            submitted_answer = answers.get(question_id)
            status = "submitted" if submitted_answer is not None else "unanswered"
            presented = _parse_dt(event.get("presented_at"))
            submitted = _parse_dt(event.get("submitted_at")) or (submitted_at if submitted_answer is not None else None)
            response_time = event.get("response_time_seconds")
            # A missing client measurement stays missing; dividing total
            # session duration among questions would fabricate item timing.
            try:
                response_time = None if response_time is None else float(response_time)
            except (TypeError, ValueError):
                raise ValueError("response_time_seconds must be a finite non-negative number")
            if response_time is not None and (not math.isfinite(response_time) or response_time < 0):
                raise ValueError("response_time_seconds must be a finite non-negative number")
            correct = None
            points = None
            if submitted_answer is not None:
                correct = submitted_answer == q.get("correct_answer")
                points = float(q.get("points", 10) if correct else 0)
            meta = q.get("llm_metadata") or {}
            if existing is None:
                existing = QuestionResponse(
                    study_session_id=study_session["study_session_id"],
                    assessment_session_id=assessment_session["id"],
                    participant_id=study_session["participant_id"],
                    condition=study_session["condition"],
                    question_id=question_id,
                    question_index=idx,
                )
                db.add(existing)
            method = decision_method or q.get("adaptive_method") or study_session.get("condition")
            if method not in {"MCRF", "LEGACY"}:
                method = None
            existing.condition = method or existing.condition
            existing.question_difficulty = q.get("difficulty")
            existing.question_source = meta.get("generated_from")
            existing.model_provider = meta.get("model")
            existing.live_fallback_status = meta.get("generated_from")
            existing.bloom_level = meta.get("blooms_level")
            existing.presented_at = presented
            existing.submitted_at = submitted
            existing.response_time_seconds = response_time
            existing.submitted_answer = None if submitted_answer is None else str(submitted_answer)
            existing.correctness = correct
            existing.score_points = points
            existing.status = status
            rows_out.append({
                "question_id": question_id,
                "question_index": idx,
                "condition": method,
                "response_time_seconds": response_time,
                "correctness": correct,
                "status": status,
            })
        if adaptive_state_factory is not None:
            persisted_assessment = db.get(AssessmentSession, assessment_session["id"])
            if not persisted_assessment:
                raise ValueError("Assessment session not found while saving response")
            persisted_assessment.adaptive_state = adaptive_state_factory(rows_out[0])
        db.commit()
    finally:
        db.close()
    return rows_out


def save_single_question_response(
    *,
    study_session: dict,
    assessment_session: dict,
    question: dict,
    question_index: int,
    answer,
    response_event: Optional[dict],
    submitted_at: datetime,
    decision_method: Optional[str] = None,
    adaptive_state_factory: Optional[Callable[[dict], dict]] = None,
) -> dict:
    event = dict(response_event or {})
    event.setdefault("question_id", question["id"])
    event.setdefault("question_index", question_index)
    rows = save_question_responses(
        study_session=study_session,
        assessment_session={**assessment_session, "questions": [question]},
        answers={question["id"]: answer},
        response_events=[event],
        submitted_at=submitted_at,
        total_time_spent=0.0,
        decision_method=decision_method,
        adaptive_state_factory=adaptive_state_factory,
    )
    return rows[0]


def update_assessment_adaptive_state(
    session_id: str,
    *,
    questions: Optional[list[dict]] = None,
    selected_difficulty: Optional[str] = None,
    adaptive_state: Optional[dict] = None,
    completion_status: Optional[str] = None,
) -> Optional[dict]:
    db = _session()
    try:
        row = db.get(AssessmentSession, session_id)
        if not row:
            return None
        if questions is not None:
            row.questions = questions
            row.number_of_questions = len(questions)
        if selected_difficulty is not None:
            row.selected_difficulty = selected_difficulty
        if adaptive_state is not None:
            row.adaptive_state = adaptive_state
        if completion_status is not None:
            row.completion_status = completion_status
        db.commit()
        db.refresh(row)
        return {
            "id": row.id, "student_id": row.student_id, "questions": row.questions or [],
            "study_session_id": row.study_session_id,
            "participant_id": row.participant_id,
            "condition": row.condition,
            "course_id": row.course_id,
            "video_id": row.video_id,
            "contributing_video_ids": row.contributing_video_ids or [],
            "difficulty": row.difficulty, "time_limit": row.time_limit,
            "starting_difficulty": row.starting_difficulty or row.difficulty,
            "selected_difficulty": row.selected_difficulty or row.difficulty,
            "completion_status": row.completion_status,
            "attention_score_during_video": row.attention_score_during_video,
            "transcript_text": row.transcript_text,
            "adaptive_state": row.adaptive_state or {},
            "adaptive_metadata": (row.adaptive_state or {}).get("adaptive_metadata", {
                "previous_score": None,
                "adjusted_difficulty": row.difficulty,
                "reason": "Assessment protocol initialized.",
            }),
        }
    finally:
        db.close()


def save_research_crs_decision(
    *,
    study_session: dict,
    assessment_session: dict,
    adaptive_result: dict,
    previous_scores: list[float],
    per_question_responses: list[dict],
    previous_difficulty: str,
    attention_score: float,
    transcript_text: Optional[str],
    decision_index: Optional[int] = None,
    method: str = "MCRF",
) -> Optional[dict]:
    crs_block = adaptive_result.get("crs")
    if not crs_block or method != "MCRF":
        return None
    db = _session()
    try:
        existing_stmt = select(ResearchCRSDecision).where(
            ResearchCRSDecision.study_session_id == study_session["study_session_id"],
            ResearchCRSDecision.assessment_session_id == assessment_session["id"],
        )
        if decision_index is not None:
            existing_stmt = existing_stmt.where(ResearchCRSDecision.decision_index == decision_index)
        existing = db.execute(existing_stmt).scalar_one_or_none()
        if existing:
            return {
                "id": existing.id,
                "decision_index": existing.decision_index,
                "crs": existing.crs,
            }
        prior_count = db.execute(
            select(func.count(ResearchCRSDecision.id)).where(
                ResearchCRSDecision.study_session_id == study_session["study_session_id"]
            )
        ).scalar_one()
        decision_index = int(decision_index or int(prior_count or 0) + 1)
        weights = crs_block.get("weights_used", {})
        components = crs_block.get("components", {})
        timing_values = [
            r.get("response_time_seconds")
            for r in per_question_responses
            if r.get("response_time_seconds") is not None
        ]
        row = ResearchCRSDecision(
            study_session_id=study_session["study_session_id"],
            assessment_session_id=assessment_session["id"],
            participant_id=study_session["participant_id"],
            condition=method,
            decision_index=decision_index,
            timestamp=datetime.utcnow(),
            performance=components["performance"],
            behavioral_cue=components["behavioral_cue"],
            response_timing=components["integrity"],
            trend=components["trend"],
            complexity=components["complexity"],
            crs=crs_block["score"],
            alpha=weights["alpha"],
            beta=weights["beta"],
            gamma=weights["gamma"],
            delta=weights["delta"],
            epsilon=weights["epsilon"],
            selected_difficulty=adaptive_result["next_assessment_difficulty"],
            previous_difficulty=previous_difficulty,
            explanation=crs_block.get("explanation"),
            performance_inputs={"recent_scores_pct": previous_scores},
            timing_inputs={"response_times_seconds": timing_values},
            trend_inputs={"recent_scores_pct": previous_scores},
            complexity_inputs={
                "source_material": assessment_session.get("video_id") or assessment_session.get("course_id"),
                "transcript_present": bool(transcript_text and transcript_text.strip()),
                "transcript_length": len(transcript_text or ""),
            },
            detail=crs_block.get("detail") or {},
        )
        db.add(row)
        db.commit()
        return {"id": row.id, "decision_index": decision_index, "crs": row.crs}
    finally:
        db.close()


def save_research_legacy_decision(
    *,
    study_session: dict,
    assessment_session: dict,
    adaptive_result: dict,
    previous_scores: list[float],
    per_question_responses: list[dict],
    previous_difficulty: str,
    current_score: float,
    decision_index: Optional[int] = None,
    method: str = "LEGACY",
) -> Optional[dict]:
    if method != "LEGACY":
        return None
    db = _session()
    try:
        existing_stmt = select(ResearchLegacyDecision).where(
            ResearchLegacyDecision.study_session_id == study_session["study_session_id"],
            ResearchLegacyDecision.assessment_session_id == assessment_session["id"],
        )
        if decision_index is not None:
            existing_stmt = existing_stmt.where(ResearchLegacyDecision.decision_index == decision_index)
        existing = db.execute(existing_stmt).scalar_one_or_none()
        if existing:
            return {"id": existing.id, "decision_index": existing.decision_index}
        prior_count = db.execute(
            select(func.count(ResearchLegacyDecision.id)).where(
                ResearchLegacyDecision.study_session_id == study_session["study_session_id"]
            )
        ).scalar_one()
        decision_index = int(decision_index or int(prior_count or 0) + 1)
        row = ResearchLegacyDecision(
            study_session_id=study_session["study_session_id"],
            assessment_session_id=assessment_session["id"],
            participant_id=study_session["participant_id"],
            condition=method,
            decision_index=decision_index,
            timestamp=datetime.utcnow(),
            performance_input=current_score,
            performance_history=previous_scores,
            previous_difficulty=previous_difficulty,
            selected_difficulty=adaptive_result["next_assessment_difficulty"],
            explanation=adaptive_result.get("recommended_action"),
            detail={
                "performance_trend": adaptive_result.get("performance_trend"),
                "strength_areas": adaptive_result.get("strength_areas", []),
                "weak_areas": adaptive_result.get("weak_areas", []),
                "question_responses": per_question_responses,
            },
        )
        db.add(row)
        db.commit()
        return {"id": row.id, "decision_index": decision_index}
    finally:
        db.close()


def refresh_behavioral_summary(study_session_id: str) -> Optional[dict]:
    db = _session()
    try:
        study = db.get(StudySession, study_session_id)
        if not study:
            return None
        logs = db.execute(
            select(AttentionLog).where(AttentionLog.study_session_id == study_session_id)
        ).scalars().all()
        scores = [float(l.score) / 100.0 for l in logs if l.score is not None]
        state_counts: dict[str, int] = {}
        for log in logs:
            state_counts[log.state or "unknown"] = state_counts.get(log.state or "unknown", 0) + 1
        total = max(len(logs), 1)
        proportions = {k: v / total for k, v in state_counts.items()}
        existing = db.execute(
            select(BehavioralSummary).where(BehavioralSummary.study_session_id == study_session_id)
        ).scalar_one_or_none()
        if existing is None:
            existing = BehavioralSummary(
                study_session_id=study_session_id,
                participant_id=study.participant_id,
                condition=study.condition,
            )
            db.add(existing)
        existing.mean_b = statistics.mean(scores) if scores else 0.5
        existing.median_b = statistics.median(scores) if scores else 0.5
        existing.stddev_b = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        existing.min_b = min(scores) if scores else 0.5
        existing.max_b = max(scores) if scores else 0.5
        existing.observation_count = len(scores)
        existing.behavioral_state_proportions = proportions
        existing.camera_used = study.camera_used
        existing.camera_opted_out = study.camera_opted_out
        existing.camera_revoked = study.camera_revoked
        existing.updated_at = datetime.utcnow()
        db.commit()
        return {"study_session_id": study_session_id, "observation_count": existing.observation_count}
    finally:
        db.close()


def save_prepost_results(study_session_id: str, test_type: str, responses: list[dict]) -> dict:
    test_type = test_type.lower()
    if test_type not in {"pre", "post"}:
        raise ValueError("test_type must be 'pre' or 'post'")
    db = _session()
    try:
        study = db.get(StudySession, study_session_id)
        if not study:
            raise ValueError("Study session not found")
        scores = []
        for idx, r in enumerate(responses):
            row = db.execute(
                select(PrePostResult).where(
                    PrePostResult.study_session_id == study_session_id,
                    PrePostResult.test_type == test_type,
                    PrePostResult.question_id == r["question_id"],
                )
            ).scalar_one_or_none()
            if row is None:
                row = PrePostResult(
                    study_session_id=study_session_id,
                    participant_id=study.participant_id,
                    test_type=test_type,
                    question_id=r["question_id"],
                )
                db.add(row)
            row.question_index = int(r.get("question_index", idx))
            row.correctness = r.get("correctness")
            row.response_time_seconds = r.get("response_time_seconds")
            row.score = r.get("score")
            row.started_at = _parse_dt(r.get("started_at"))
            row.completed_at = _parse_dt(r.get("completed_at"))
            if r.get("score") is not None:
                scores.append(float(r["score"]))
        total_score = sum(scores) if scores else None
        if test_type == "pre":
            study.pretest_score = total_score
        else:
            study.posttest_score = total_score
        if study.pretest_score is not None and study.posttest_score is not None:
            study.learning_gain = study.posttest_score - study.pretest_score
        db.commit()
        return {
            "study_session_id": study_session_id,
            "test_type": test_type,
            "score": total_score,
            "learning_gain": study.learning_gain,
        }
    finally:
        db.close()


def _component_history(student_id: str, component: str, limit: Optional[int]) -> list[dict]:
    records = get_crs_history(student_id, limit=limit)
    return [
        {"timestamp": r["timestamp"], "assessment_id": r.get("assessment_id"), "value": r[component]}
        for r in records
    ]


def get_performance_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    return _component_history(student_id, "performance", limit)


def get_behavioral_cue_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    return _component_history(student_id, "behavioral_cue", limit)


def get_integrity_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    return _component_history(student_id, "integrity", limit)


def get_trend_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    return _component_history(student_id, "trend", limit)


def get_complexity_history(student_id: str, limit: Optional[int] = None) -> list[dict]:
    return _component_history(student_id, "complexity", limit)


# ── Consent (CR6 fix, now Postgres-backed) ──────────────────

def get_consent(student_id: str, session_id: Optional[str] = None) -> Optional[dict]:
    db = _session()
    try:
        if session_id is None:
            c = db.execute(
                select(Consent)
                .where(Consent.student_id == student_id)
                .order_by(Consent.granted_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        else:
            c = db.get(Consent, {"student_id": student_id, "session_id": session_id})
        if not c:
            return None
        return {
            "student_id": c.student_id, "session_id": c.session_id, "granted": c.granted,
            "granted_at": c.granted_at.isoformat() if c.granted_at else None,
            "retention_days": c.retention_days,
            "raw_frames_stored": c.raw_frames_stored, "version": c.version,
        }
    finally:
        db.close()


def set_consent(record: dict) -> dict:
    db = _session()
    try:
        granted_at = record.get("granted_at")
        if isinstance(granted_at, str):
            granted_at = datetime.fromisoformat(granted_at.replace("Z", "+00:00"))
        row = Consent(
            student_id=record["student_id"],
            session_id=record["session_id"],
            study_session_id=record.get("study_session_id"),
            granted=record["granted"],
            granted_at=granted_at or datetime.utcnow(),
            retention_days=record.get("retention_days", 30),
            raw_frames_stored=record.get("raw_frames_stored", False),
            version=record.get("version", "1.0"),
        )
        db.merge(row)
        if record.get("study_session_id"):
            study = db.get(StudySession, record["study_session_id"])
            if study:
                if record["granted"]:
                    study.camera_used = True
                else:
                    if study.camera_used:
                        study.camera_revoked = True
                    else:
                        study.camera_opted_out = True
        db.commit()
    finally:
        db.close()
    return record


def purge_expired_attention_logs() -> int:
    """Delete attention_logs rows older than the retention window the
    student consented to (default 30 days)."""
    from datetime import timedelta
    db = _session()
    removed = 0
    try:
        consents = db.execute(select(Consent)).scalars().all()
        for c in consents:
            cutoff = (datetime.utcnow() - timedelta(days=c.retention_days)).isoformat()
            stale = db.execute(
                select(AttentionLog).where(
                    AttentionLog.student_id == c.student_id,
                    AttentionLog.timestamp < cutoff,
                )
            ).scalars().all()
            for row in stale:
                db.delete(row)
                removed += 1
        db.commit()
    finally:
        db.close()
    return removed


def log_attention(log: dict):
    db = _session()
    try:
        participant_id = log.get("participant_id")
        if not participant_id and log.get("study_session_id"):
            study = db.get(StudySession, log["study_session_id"])
            participant_id = study.participant_id if study else None
        row = AttentionLog(
            student_id=log["student_id"], participant_id=participant_id,
            study_session_id=log.get("study_session_id"), session_id=log.get("session_id"),
            video_id=log.get("video_id"),
            timestamp=log.get("timestamp"), score=log.get("score"),
            state=log.get("state"), confidence=log.get("confidence"),
            message=log.get("message"), model_response=log.get("model_response", {}),
            source=log.get("source", "dummy"),
            consent_confirmed=log.get("consent_confirmed", False),
        )
        db.add(row)
        if log.get("study_session_id"):
            study = db.get(StudySession, log["study_session_id"])
            if study:
                study.camera_used = True
        db.commit()
    finally:
        db.close()


def get_attention_logs(video_id: str, student_id: str) -> list[dict]:
    db = _session()
    try:
        rows = db.execute(
            select(AttentionLog).where(
                AttentionLog.video_id == video_id, AttentionLog.student_id == student_id
            )
        ).scalars().all()
        return [{
            "student_id": r.student_id, "video_id": r.video_id, "timestamp": r.timestamp,
            "participant_id": r.participant_id,
            "study_session_id": r.study_session_id,
            "session_id": r.session_id,
            "score": r.score, "state": r.state, "confidence": r.confidence,
            "message": r.message, "model_response": r.model_response,
            "source": r.source, "consent_confirmed": r.consent_confirmed,
        } for r in rows]
    finally:
        db.close()


# ── Leaderboard — now a REAL live query, not a static seeded table ────
# FIX (this request): previously `leaderboard_table` was a hand-seeded
# TinyDB table of fictional students (Priya Sharma, Marcus Chen, ...)
# that never changed no matter what any real student did. It is gone —
# the leaderboard below is computed by ordering the real `users` table.

def get_leaderboard(limit: int = 50) -> list[dict]:
    db = _session()
    try:
        rows = db.execute(
            select(User).order_by(User.xp.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "rank": i + 1,
                "student_id": u.id,
                "name": u.name,
                "avatar": u.avatar or "",
                "xp": u.xp,
                "level": u.level,
                "streak": u.streak,
                "courses_completed": _count_completed_courses_for_user(db, u.id),
            }
            for i, u in enumerate(rows)
        ]
    finally:
        db.close()


# ── Daily challenges — now per-student (FIX, remaining-things request) ──
# Previously get_daily_challenges() returned one global row per
# challenge with a shared `completed`/`progress` — one student finishing
# a challenge marked it finished for everyone. Progress now lives in
# DailyChallengeProgress, scoped to (student_id, challenge_id, today).

def _get_or_create_progress(db: Session, student_id: str, challenge_id: str) -> DailyChallengeProgress:
    today = date.today()
    row = db.execute(
        select(DailyChallengeProgress).where(
            DailyChallengeProgress.student_id == student_id,
            DailyChallengeProgress.challenge_id == challenge_id,
            DailyChallengeProgress.challenge_date == today,
        )
    ).scalar_one_or_none()
    if row is None:
        row = DailyChallengeProgress(
            student_id=student_id, challenge_id=challenge_id, challenge_date=today,
            progress=0, completed=False, xp_awarded=False,
        )
        db.add(row)
        db.flush()
    return row


def get_daily_challenges(student_id: str) -> list[dict]:
    db = _session()
    try:
        templates = db.execute(select(DailyChallenge)).scalars().all()
        out = []
        for t in templates:
            progress_row = _get_or_create_progress(db, student_id, t.id)
            out.append({
                "id": t.id, "title": t.title, "description": t.description,
                "xp_reward": t.xp_reward, "type": t.type, "target": t.target,
                "progress": progress_row.progress, "completed": progress_row.completed,
            })
        db.commit()
        return out
    finally:
        db.close()


def advance_challenge_progress(student_id: str, challenge_type: str, amount: int = 1, set_to: Optional[int] = None) -> list[dict]:
    """
    Bump progress on every challenge of `challenge_type` for this student
    today (usually just one, but supports several sharing a type). Caps
    at the challenge's target and marks completed exactly once; awards
    XP exactly once per (student, challenge, day) via `xp_awarded`, so
    calling this multiple times in one day is always safe/idempotent.
    Returns the list of challenges that just newly completed (empty if
    none did) so the caller can surface a "+XP" toast.
    """
    db = _session()
    newly_completed = []
    try:
        templates = db.execute(
            select(DailyChallenge).where(DailyChallenge.type == challenge_type)
        ).scalars().all()
        for t in templates:
            row = _get_or_create_progress(db, student_id, t.id)
            if row.completed:
                continue
            row.progress = set_to if set_to is not None else min(row.progress + amount, t.target)
            if row.progress >= t.target:
                row.completed = True
                row.completed_at = datetime.utcnow()
                if not row.xp_awarded:
                    user = db.get(User, student_id)
                    if user:
                        apply_xp(user, t.xp_reward)
                    row.xp_awarded = True
                    newly_completed.append({"id": t.id, "title": t.title, "xp_reward": t.xp_reward})
        db.commit()
    finally:
        db.close()
    return newly_completed


def record_daily_activity(student_id: str) -> dict:
    """
    FIX (remaining-things request): `streak`/`best_streak` existed as
    User columns but nothing ever updated them. Called from the login
    endpoint (and could be called from any "the student did something
    today" event): if last_active_date was yesterday, streak += 1; if it
    was already today, no change (idempotent — logging in twice in a day
    doesn't inflate the streak); if it was any earlier date (or never),
    the streak resets to 1. best_streak tracks the historical max.
    Also advances the "Streak Keeper" daily challenge for today.
    """
    db = _session()
    try:
        user = db.get(User, student_id)
        if not user:
            return {}
        today = date.today()
        if user.last_active_date == today:
            pass  # already recorded today — no-op, not a double-count
        elif user.last_active_date == today - timedelta(days=1):
            user.streak += 1
        else:
            user.streak = 1
        user.best_streak = max(user.best_streak, user.streak)
        user.last_active_date = today
        db.commit()
        streak_result = {"streak": user.streak, "best_streak": user.best_streak}
    finally:
        db.close()
    advance_challenge_progress(student_id, "streak", set_to=1)
    return streak_result


def get_notifications(student_id: str = "student_001") -> list[dict]:
    db = _session()
    try:
        rows = db.execute(select(Notification)).scalars().all()
        return [{
            "id": n.id, "type": n.type, "title": n.title, "message": n.message,
            "timestamp": n.timestamp, "read": n.read, "icon": n.icon,
        } for n in rows]
    finally:
        db.close()


# ── Auto Courses ─────────────────────────────────────────────

def save_auto_course(course_data: dict) -> None:
    db = _session()
    try:
        row = AutoCourse(
            course_id=course_data["course_id"],
            course_title=course_data.get("course_title"),
            description=course_data.get("description"),
            icon=course_data.get("icon"),
            category=course_data.get("category"),
            difficulty=course_data.get("difficulty"),
            tags=course_data.get("tags", []),
            videos=course_data.get("videos", []),
            generated_at=course_data.get("generated_at", 0),
        )
        db.merge(row)
        db.commit()
    finally:
        db.close()


def get_auto_course(course_id: str) -> Optional[dict]:
    db = _session()
    try:
        c = db.get(AutoCourse, course_id)
        if not c:
            return None
        return {
            "course_id": c.course_id, "course_title": c.course_title,
            "description": c.description, "icon": c.icon, "category": c.category,
            "difficulty": c.difficulty, "tags": c.tags or [], "videos": c.videos or [],
            "generated_at": c.generated_at,
        }
    finally:
        db.close()


def get_all_auto_courses() -> list[dict]:
    db = _session()
    try:
        rows = db.execute(select(AutoCourse)).scalars().all()
        courses = [{
            "course_id": c.course_id, "course_title": c.course_title,
            "description": c.description, "icon": c.icon, "category": c.category,
            "difficulty": c.difficulty, "tags": c.tags or [], "videos": c.videos or [],
            "generated_at": c.generated_at,
        } for c in rows]
        return sorted(courses, key=lambda c: c.get("generated_at", 0), reverse=True)
    finally:
        db.close()


def update_video_transcription_status(course_id: str, video_id: str, available: bool) -> None:
    db = _session()
    try:
        c = db.get(AutoCourse, course_id)
        if not c:
            return
        videos = c.videos or []
        for v in videos:
            if v.get("id") == video_id:
                v["transcription_available"] = available
        c.videos = videos
        db.commit()
    finally:
        db.close()


def save_auto_course_to_courses(course_id: str) -> Optional[dict]:
    auto_course = get_auto_course(course_id)
    if not auto_course:
        return None

    def _to_title_case_difficulty(value: str) -> str:
        normalized = (value or "Intermediate").strip().lower()
        mapping = {"beginner": "Beginner", "intermediate": "Intermediate", "advanced": "Advanced"}
        return mapping.get(normalized, "Intermediate")

    videos = auto_course.get("videos", [])
    converted_videos = []
    for idx, video in enumerate(videos, start=1):
        converted_videos.append({
            "id": video.get("id", f"auto_v_{idx}"),
            "title": video.get("title", f"Video {idx}"),
            "url": video.get("url", ""),
            "duration": int(video.get("duration", 0) or 0),
            "thumbnail": video.get("thumbnail", ""),
            "order": int(video.get("order", idx)),
            "completed": bool(video.get("completed", False)),
            "watched_percent": float(video.get("watched_percent", 0.0) or 0.0),
            # FIX (course generator request): was silently dropped here —
            # same class of bug as an undeclared response_model field.
            "stage_label": video.get("stage_label", ""),
        })

    total_videos = len(converted_videos)
    completed_videos = sum(1 for v in converted_videos if v.get("completed"))
    total_seconds = sum(int(v.get("duration", 0) or 0) for v in converted_videos)
    estimated_hours = round(total_seconds / 3600, 1) if total_seconds > 0 else round(max(total_videos, 1) * 0.2, 1)
    progress = round((completed_videos / total_videos) * 100, 1) if total_videos > 0 else 0.0

    course_doc = {
        "id": auto_course.get("course_id", course_id),
        "title": auto_course.get("course_title", "Auto Course"),
        "description": auto_course.get("description", "Auto-generated course"),
        "icon": auto_course.get("icon", "🎓"),
        "category": auto_course.get("category", "Auto-Generated"),
        "difficulty": _to_title_case_difficulty(auto_course.get("difficulty", "Intermediate")),
        "total_videos": total_videos,
        "completed_videos": completed_videos,
        "progress": progress,
        "estimated_hours": max(0.1, estimated_hours),
        "tags": auto_course.get("tags", ["auto-generated"]),
        "video_links": converted_videos,
    }

    db = _session()
    try:
        row = Course(**course_doc)
        db.merge(row)
        db.commit()
    finally:
        db.close()

    return course_doc


# ── Refresh tokens (remaining-things request) ────────────────
# FIX: access tokens previously had no revocation mechanism — logging
# out only meant "the browser forgot the token", nothing server-side
# actually invalidated it. Refresh tokens are stored as a SHA-256 hash
# (never the raw value) and can be revoked for real.

def create_refresh_token(student_id: str, token_hash: str, expires_at: datetime) -> None:
    db = _session()
    try:
        db.add(RefreshToken(user_id=student_id, token_hash=token_hash, expires_at=expires_at))
        db.commit()
    finally:
        db.close()


def get_valid_refresh_token(token_hash: str) -> Optional[dict]:
    db = _session()
    try:
        row = db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None or row.revoked or row.expires_at < datetime.utcnow():
            return None
        return {"id": row.id, "user_id": row.user_id, "expires_at": row.expires_at}
    finally:
        db.close()


def revoke_refresh_token(token_hash: str) -> None:
    db = _session()
    try:
        row = db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row:
            row.revoked = True
            db.commit()
    finally:
        db.close()


def revoke_all_refresh_tokens(student_id: str) -> int:
    """Used by 'log out everywhere' / password reset (rotating the
    password should kill every existing session, not just the current
    device's)."""
    db = _session()
    try:
        rows = db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == student_id, RefreshToken.revoked == False  # noqa: E712
            )
        ).scalars().all()
        for row in rows:
            row.revoked = True
        db.commit()
        return len(rows)
    finally:
        db.close()


# ── Password reset (remaining-things request) ────────────────
# FIX: there was no password reset flow at all before this.

def create_password_reset_token(student_id: str, token_hash: str, expires_at: datetime) -> None:
    db = _session()
    try:
        # Invalidate any earlier unused reset tokens for this user first
        # — only the most recently requested link should ever work.
        old = db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == student_id, PasswordResetToken.used == False  # noqa: E712
            )
        ).scalars().all()
        for row in old:
            row.used = True
        db.add(PasswordResetToken(user_id=student_id, token_hash=token_hash, expires_at=expires_at))
        db.commit()
    finally:
        db.close()


def get_valid_password_reset_token(token_hash: str) -> Optional[dict]:
    db = _session()
    try:
        row = db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None or row.used or row.expires_at < datetime.utcnow():
            return None
        return {"id": row.id, "user_id": row.user_id}
    finally:
        db.close()


def mark_password_reset_token_used(token_hash: str) -> None:
    db = _session()
    try:
        row = db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row:
            row.used = True
            db.commit()
    finally:
        db.close()


def set_user_password(student_id: str, new_hashed_password: str) -> None:
    db = _session()
    try:
        user = db.get(User, student_id)
        if user:
            user.hashed_password = new_hashed_password
            db.commit()
    finally:
        db.close()


# ── Seeding ──────────────────────────────────────────────────
# Global demo content (courses, daily challenges, notifications) is
# seeded so the app isn't empty on first run. Student accounts are NOT
# seeded here — they're created for real via POST /api/auth/signup (see
# scripts/seed_demo_accounts.py for a script that does exactly that, so
# the leaderboard has real, auth-created rows to show on a fresh demo).

def seed_database():
    db = _session()
    try:
        # V2 (Phase 4): courses/videos are no longer seeded with hardcoded
        # data — all catalog content (courses, and which local videos
        # belong to them) is created through the admin-only endpoints in
        # routers/courses.py. This also removes the 19 hardcoded YouTube
        # URLs that used to ship in _seed_courses().
        if db.execute(select(DailyChallenge).limit(1)).first() is None:
            _seed_challenges(db)
        if db.execute(select(Notification).limit(1)).first() is None:
            _seed_notifications(db)
        db.commit()
        logger.success("Postgres seed check complete (challenges/notifications; courses are admin-managed in V2)")
    finally:
        db.close()


def _seed_challenges(db: Session):
    challenges = [
        {"id": "dc1", "title": "Watch 30 minutes", "description": "Watch any video for 30 minutes", "xp_reward": 50, "type": "watch", "target": 30},
        {"id": "dc2", "title": "Perfect Quiz", "description": "Score 100% on any quiz", "xp_reward": 100, "type": "quiz", "target": 1},
        {"id": "dc3", "title": "Streak Keeper", "description": "Log in and study today", "xp_reward": 25, "type": "streak", "target": 1},
        {"id": "dc4", "title": "Review Master", "description": "Review 3 completed lessons", "xp_reward": 75, "type": "review", "target": 3},
    ]
    for c in challenges:
        db.merge(DailyChallenge(**c))
    logger.info("Seeded daily challenges")


def _seed_notifications(db: Session):
    notifs = [
        {"id": "n1", "type": "achievement", "title": "Welcome to NeuroLearn!", "message": "Create your account to start earning XP", "timestamp": "just now", "read": False, "icon": "🎉"},
    ]
    for n in notifs:
        db.merge(Notification(**n))
    logger.info("Seeded notifications")
