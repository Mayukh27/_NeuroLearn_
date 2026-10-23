"""
data/models_orm.py — SQLAlchemy ORM models backing the Postgres database.

Design note: nested/variable-shape content (badges, video_links, tags,
model_response) is stored as JSONB columns rather than exploded into
further join tables. This keeps the exact same document shapes the
existing Pydantic response models (schemas/models.py) already expect —
so routers barely change — while still getting real Postgres benefits
where they matter most: a unique/indexed `users.email` for auth, and a
real `ORDER BY xp DESC` for the leaderboard instead of a static seeded
table (see data/database.py's get_leaderboard()).
"""
import uuid
from datetime import datetime, date

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Date, ForeignKey, Text,
    UniqueConstraint, CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from data.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    """
    A registered account. Doubles as the "student profile" the rest of the
    app already models (schemas.models.StudentProfile) — auth fields and
    gamification fields live on the same row rather than a separate join,
    since every user in this app *is* a student.
    """
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    name = Column(String, nullable=False)
    avatar = Column(String, default="/placeholder-user.jpg")

    # Gamification (previously a TinyDB dict in data/database.py's seed)
    level = Column(Integer, default=1)
    xp = Column(Integer, default=0)
    xp_to_next_level = Column(Integer, default=100)
    streak = Column(Integer, default=0)
    best_streak = Column(Integer, default=0)
    total_courses_completed = Column(Integer, default=0)
    total_watch_time = Column(Integer, default=0)  # minutes
    badges = Column(JSONB, default=list)

    joined_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.utcnow)
    # FIX (remaining-things request): `streak`/`best_streak` existed as
    # columns but nothing anywhere ever wrote to them after creation —
    # they were permanently stuck at their initial value. This tracks the
    # last calendar date the student did anything streak-worthy (logged
    # in or submitted an assessment), so routers/auth.py's login handler
    # can compute a real consecutive-day streak.
    last_active_date = Column(Date, nullable=True)

    consent = relationship("Consent", back_populates="user")


class Course(Base):
    __tablename__ = "courses"
    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    icon = Column(String)
    category = Column(String)
    difficulty = Column(String)
    total_videos = Column(Integer, default=0)
    completed_videos = Column(Integer, default=0)
    progress = Column(Float, default=0)
    estimated_hours = Column(Float, default=0)
    tags = Column(JSONB, default=list)
    video_links = Column(JSONB, default=list)


class AutoCourse(Base):
    __tablename__ = "auto_courses"
    course_id = Column(String, primary_key=True)
    course_title = Column(String)
    description = Column(Text)
    icon = Column(String)
    category = Column(String)
    difficulty = Column(String)
    tags = Column(JSONB, default=list)
    videos = Column(JSONB, default=list)
    generated_at = Column(Float, default=0)


class AssessmentSession(Base):
    __tablename__ = "assessment_sessions"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, ForeignKey("users.id"), index=True)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=True, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=True, index=True)
    condition = Column(String, nullable=True, index=True)
    course_id = Column(String, nullable=True, index=True)
    video_id = Column(String, nullable=True, index=True)
    contributing_video_ids = Column(JSONB, default=list)
    questions = Column(JSONB, default=list)
    difficulty = Column(String, default="medium")
    starting_difficulty = Column(String, nullable=True)
    selected_difficulty = Column(String, nullable=True)
    ending_difficulty = Column(String, nullable=True)
    completion_status = Column(String, default="started", index=True)
    completed_at = Column(DateTime, nullable=True)
    total_score = Column(Float, nullable=True)
    percentage = Column(Float, nullable=True)
    total_duration_seconds = Column(Float, nullable=True)
    number_of_questions = Column(Integer, nullable=True)
    number_correct = Column(Integer, nullable=True)
    time_limit = Column(Integer, default=420)
    attention_score_during_video = Column(Float, default=50)
    transcript_text = Column(Text, nullable=True)
    adaptive_state = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class AssessmentResult(Base):
    __tablename__ = "assessment_results"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, ForeignKey("users.id"), index=True)
    session_id = Column(String, index=True)
    score = Column(Float)
    percentage = Column(Float)
    xp_earned = Column(Integer)
    timestamp = Column(Float)  # epoch seconds, matches previous TinyDB field
    payload = Column(JSONB, default=dict)  # everything else (message, adaptive_response, etc.)


class CRSHistory(Base):
    __tablename__ = "crs_history"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, ForeignKey("users.id"), index=True)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=True, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=True, index=True)
    condition = Column(String, nullable=True, index=True)
    assessment_id = Column(String, nullable=True)
    timestamp = Column(Float)
    performance = Column(Float)
    behavioral_cue = Column(Float)
    integrity = Column(Float)
    trend = Column(Float)
    complexity = Column(Float)
    crs = Column(Float)
    difficulty = Column(String)
    explanation = Column(Text)


class Consent(Base):
    """CR6 (peer review packet) — webcam-monitoring consent, one row per user."""
    __tablename__ = "consent"
    student_id = Column(String, ForeignKey("users.id"), primary_key=True)
    session_id = Column(String, primary_key=True, default="legacy")
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=True, index=True)
    granted = Column(Boolean, default=False)
    granted_at = Column(DateTime, nullable=True)
    retention_days = Column(Integer, default=30)
    raw_frames_stored = Column(Boolean, default=False)
    version = Column(String, default="1.0")

    user = relationship("User", back_populates="consent")


class StudyConsent(Base):
    """
    FIX (A-2): study-participation consent — the mentor guidelines treat
    this as a decision separate from webcam/camera consent ("Study
    consent and camera choice are separate decisions"). One row per user.
    A granted row here is the single required gate checked before any
    research record (ResearchParticipant / StudySession, and everything
    keyed off a study_session_id downstream of it) is ever created.
    """
    __tablename__ = "study_consent"
    student_id = Column(String, ForeignKey("users.id"), primary_key=True)
    granted = Column(Boolean, default=False)
    granted_at = Column(DateTime, nullable=True)
    version = Column(String, default="1.0")


class AttentionLog(Base):
    __tablename__ = "attention_logs"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, ForeignKey("users.id"), index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=True, index=True)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=True, index=True)
    session_id = Column(String, index=True, nullable=True)
    video_id = Column(String, index=True)
    timestamp = Column(String)
    score = Column(Integer)
    state = Column(String)
    confidence = Column(Float)
    message = Column(String)
    model_response = Column(JSONB, default=dict)
    source = Column(String, default="dummy")  # MJ4 fix: "live" | "dummy"
    consent_confirmed = Column(Boolean, default=False)


class ResearchParticipant(Base):
    __tablename__ = "research_participants"
    participant_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    sequence_order = Column(String, nullable=False)
    assigned_condition = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StudySession(Base):
    __tablename__ = "study_sessions"
    study_session_id = Column(String, primary_key=True, default=lambda: f"study_{uuid.uuid4().hex[:12]}")
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    condition = Column(String, nullable=False, index=True)
    sequence_order = Column(String, nullable=False)
    course_id = Column(String, nullable=True, index=True)
    module_id = Column(String, nullable=True, index=True)
    video_id = Column(String, nullable=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    ended_at = Column(DateTime, nullable=True)
    completion_status = Column(String, default="started", index=True)
    experiment_version = Column(String, default="full-study-v1", index=True)
    application_version = Column(String, nullable=True)
    crs_config_version = Column(String, default="crs-equal-weights-v1")
    pretest_score = Column(Float, nullable=True)
    posttest_score = Column(Float, nullable=True)
    learning_gain = Column(Float, nullable=True)
    camera_used = Column(Boolean, default=False)
    camera_opted_out = Column(Boolean, default=False)
    camera_revoked = Column(Boolean, default=False)


class StudyVideoCompletion(Base):
    """A video that was actually completed within a study session.

    This is deliberately separate from the course catalogue's mutable
    ``completed`` flag.  It is the ordered, session-scoped audit trail used
    to build one assessment from one or more completed videos.
    """
    __tablename__ = "study_video_completions"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    video_id = Column(String, nullable=False, index=True)
    completion_order = Column(Integer, nullable=False)
    completed_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    transcript_text = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "study_session_id", "video_id",
            name="uq_study_video_completion_once",
        ),
        UniqueConstraint(
            "study_session_id", "completion_order",
            name="uq_study_video_completion_order",
        ),
    )


class VideoTranscriptCache(Base):
    """Persistent Whisper-transcript cache, keyed by video URL, so a video
    already transcribed once is never re-downloaded/re-run through
    yt-dlp/FFmpeg/Whisper after a backend restart.

    Deliberately separate from StudyVideoCompletion.transcript_text (the
    per-session, per-student audit trail of what was completed *within one
    study session*): the same educational video can be watched across many
    different study sessions/students, so this table is a single global
    row per video_url, independent of any study session. Nothing about
    StudyVideoCompletion's schema or semantics changes.
    """
    __tablename__ = "video_transcript_cache"
    video_url = Column(String, primary_key=True)
    segments = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class LocalVideo(Base):
    """V2 (Phase 2) — metadata for a locally-stored video file, served over
    the LAN by Nginx directly from disk (see Phase 3). This table stores
    ONLY metadata and a safe, validated, RELATIVE path — never the video
    binary and never an absolute filesystem path. `relative_path` is
    validated by services/video_path_safety.py against VIDEO_STORAGE_DIR
    (an externally-configured env var, see config/video_config.py) before
    it is ever written here, so a row existing in this table is never
    proof by itself that the path is safe to resolve — callers must
    re-validate via video_path_safety on read, exactly as on write. The
    CheckConstraint below is a defense-in-depth backstop against '..'
    traversal making it into the column by some other path (e.g. a direct
    DB write), not a substitute for that validation.

    Deliberately independent of Course.video_links (JSONB) rather than a
    replacement for it: a Course's video_links entry points at a
    LocalVideo by URL/id (wired up in Phase 4), the same way it previously
    pointed at a YouTube URL. No existing Course/VideoLink schema changes.
    """
    __tablename__ = "local_videos"
    id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, nullable=False)
    # Relative to VIDEO_STORAGE_DIR, forward-slash, no leading '/', no '..'.
    # e.g. "neuroscience-101/lecture-03.mp4"
    relative_path = Column(String, nullable=False, unique=True)
    course_id = Column(String, ForeignKey("courses.id"), nullable=True, index=True)
    duration_seconds = Column(Float, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    order = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("relative_path NOT LIKE '%..%'", name="ck_local_videos_no_traversal"),
        CheckConstraint("relative_path NOT LIKE '/%'", name="ck_local_videos_not_absolute"),
    )


class QuestionResponse(Base):
    __tablename__ = "question_responses"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, index=True)
    assessment_session_id = Column(String, ForeignKey("assessment_sessions.id"), nullable=False, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    condition = Column(String, nullable=False, index=True)
    question_id = Column(String, nullable=False, index=True)
    question_index = Column(Integer, nullable=False)
    question_difficulty = Column(String, nullable=True)
    question_source = Column(String, nullable=True)
    model_provider = Column(String, nullable=True)
    live_fallback_status = Column(String, nullable=True)
    bloom_level = Column(String, nullable=True)
    presented_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    response_time_seconds = Column(Float, nullable=True)
    submitted_answer = Column(Text, nullable=True)
    correctness = Column(Boolean, nullable=True)
    score_points = Column(Float, nullable=True)
    status = Column(String, default="submitted", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "study_session_id", "assessment_session_id", "question_id",
            name="uq_question_response_once",
        ),
    )


class ResearchCRSDecision(Base):
    __tablename__ = "research_crs_decisions"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, index=True)
    assessment_session_id = Column(String, ForeignKey("assessment_sessions.id"), nullable=False, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    condition = Column(String, nullable=False, index=True)
    decision_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    performance = Column(Float, nullable=False)
    behavioral_cue = Column(Float, nullable=False)
    response_timing = Column(Float, nullable=False)
    trend = Column(Float, nullable=False)
    complexity = Column(Float, nullable=False)
    crs = Column(Float, nullable=False)
    alpha = Column(Float, nullable=False)
    beta = Column(Float, nullable=False)
    gamma = Column(Float, nullable=False)
    delta = Column(Float, nullable=False)
    epsilon = Column(Float, nullable=False)
    selected_difficulty = Column(String, nullable=False)
    previous_difficulty = Column(String, nullable=True)
    explanation = Column(Text, nullable=True)
    performance_inputs = Column(JSONB, default=dict)
    timing_inputs = Column(JSONB, default=dict)
    trend_inputs = Column(JSONB, default=dict)
    complexity_inputs = Column(JSONB, default=dict)
    detail = Column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "study_session_id", "assessment_session_id", "decision_index",
            name="uq_research_crs_decision_index",
        ),
    )


class ResearchLegacyDecision(Base):
    __tablename__ = "research_legacy_decisions"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, index=True)
    assessment_session_id = Column(String, ForeignKey("assessment_sessions.id"), nullable=False, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    condition = Column(String, nullable=False, index=True)
    decision_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    performance_input = Column(Float, nullable=True)
    performance_history = Column(JSONB, default=list)
    previous_difficulty = Column(String, nullable=True)
    selected_difficulty = Column(String, nullable=False)
    explanation = Column(Text, nullable=True)
    detail = Column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "study_session_id", "assessment_session_id", "decision_index",
            name="uq_research_legacy_decision_index",
        ),
    )


class BehavioralSummary(Base):
    __tablename__ = "behavioral_summaries"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, unique=True, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    condition = Column(String, nullable=False, index=True)
    mean_b = Column(Float, nullable=True)
    median_b = Column(Float, nullable=True)
    stddev_b = Column(Float, nullable=True)
    min_b = Column(Float, nullable=True)
    max_b = Column(Float, nullable=True)
    observation_count = Column(Integer, default=0)
    behavioral_state_proportions = Column(JSONB, default=dict)
    camera_used = Column(Boolean, default=False)
    camera_opted_out = Column(Boolean, default=False)
    camera_revoked = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PrePostResult(Base):
    __tablename__ = "prepost_results"
    id = Column(String, primary_key=True, default=_uuid)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=False, index=True)
    participant_id = Column(String, ForeignKey("research_participants.participant_id"), nullable=False, index=True)
    test_type = Column(String, nullable=False, index=True)
    question_id = Column(String, nullable=False)
    question_index = Column(Integer, nullable=False)
    correctness = Column(Boolean, nullable=True)
    response_time_seconds = Column(Float, nullable=True)
    score = Column(Float, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "study_session_id", "test_type", "question_id",
            name="uq_prepost_question_once",
        ),
    )


class GeneratedQuestion(Base):
    __tablename__ = "generated_questions"
    question_id = Column(String, primary_key=True)
    study_session_id = Column(String, ForeignKey("study_sessions.study_session_id"), nullable=True, index=True)
    assessment_session_id = Column(String, ForeignKey("assessment_sessions.id"), nullable=True, index=True)
    model_version = Column(String, nullable=True)
    generated_live_fallback = Column(String, nullable=True)
    source_material = Column(String, nullable=True)
    difficulty = Column(String, nullable=True)
    bloom_level = Column(String, nullable=True)
    generation_timestamp = Column(DateTime, default=datetime.utcnow)
    question_metadata = Column(JSONB, default=dict)


class DailyChallenge(Base):
    """
    Global challenge TEMPLATE — title/description/xp_reward/target only.
    FIX (remaining-things request): this table previously held
    `completed`/`progress` directly, meaning one student completing
    "Watch 30 minutes" marked it completed for every student, since
    there was only ever one row per challenge. Per-student state now
    lives in DailyChallengeProgress below.
    """
    __tablename__ = "daily_challenges"
    id = Column(String, primary_key=True)
    title = Column(String)
    description = Column(String)
    xp_reward = Column(Integer)
    type = Column(String)
    target = Column(Integer, default=1)


class DailyChallengeProgress(Base):
    """Per-student, per-day progress against a DailyChallenge template."""
    __tablename__ = "daily_challenge_progress"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, ForeignKey("users.id"), index=True)
    challenge_id = Column(String, ForeignKey("daily_challenges.id"), index=True)
    challenge_date = Column(Date, default=date.today, index=True)
    progress = Column(Integer, default=0)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    xp_awarded = Column(Boolean, default=False)  # guards against double XP for the same day


class VideoWatchProgress(Base):
    """Per-student playback position for a video, so the learning flow can
    resume a partially-watched video instead of always restarting at 0:00.

    Deliberately separate from StudyVideoCompletion (a terminal, ordered
    research-evidence record) — this is just "where was I" convenience
    state, upserted frequently (throttled client-side) while a video
    plays, independent of whether a study session exists for it.
    """
    __tablename__ = "video_watch_progress"
    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    video_id = Column(String, ForeignKey("local_videos.id"), nullable=False, index=True)
    position_seconds = Column(Float, default=0)
    duration_seconds = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_video_watch_progress_user_video"),
    )


class PasswordResetToken(Base):
    """
    FIX (remaining-things request): password reset didn't exist at all.
    Tokens are stored as a SHA-256 hash, not the raw value — mirrors how
    the raw token is never persisted anywhere a DB read could leak it,
    same principle as not storing plaintext passwords.
    """
    __tablename__ = "password_reset_tokens"
    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), index=True)
    token_hash = Column(String, unique=True, index=True)
    expires_at = Column(DateTime)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class RefreshToken(Base):
    """
    FIX (remaining-things request): access tokens were long-lived (10h)
    stateless JWTs with no revocation mechanism at all — logging out only
    ever meant "the browser forgot the token", the token itself stayed
    valid until it expired no matter what. Refresh tokens are stored
    hashed (same rationale as PasswordResetToken) and can be revoked
    server-side, which is what makes logout and "log out of all
    sessions" real actions rather than client-only gestures.
    """
    __tablename__ = "refresh_tokens"
    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), index=True)
    token_hash = Column(String, unique=True, index=True)
    expires_at = Column(DateTime)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(String, primary_key=True, default=_uuid)
    student_id = Column(String, nullable=True, index=True)
    type = Column(String)
    title = Column(String)
    message = Column(String)
    timestamp = Column(String)
    read = Column(Boolean, default=False)
    icon = Column(String)