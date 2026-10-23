"""
============================================================
PYDANTIC SCHEMAS — JSON Models for all API endpoints
These map 1:1 with the frontend TypeScript interfaces
============================================================
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ── Student ─────────────────────────────────────────────────

class Badge(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    earned: bool
    earned_date: Optional[str] = None
    rarity: Literal["common", "rare", "epic", "legendary"]


class StudentProfile(BaseModel):
    id: str
    name: str
    email: str
    avatar: str
    level: int
    xp: int
    xp_to_next_level: int
    streak: int
    best_streak: int
    total_courses_completed: int
    total_watch_time: int  # minutes
    joined_date: str
    rank: int
    badges: list[Badge]


class XPAwardRequest(BaseModel):
    student_id: str
    amount: int
    reason: str


class VideoProgressRequest(BaseModel):
    video_id: str
    position_seconds: float
    duration_seconds: Optional[float] = None


class VideoProgressResponse(BaseModel):
    video_id: str
    position_seconds: float


class XPAwardResponse(BaseModel):
    new_xp: int
    new_level: int
    leveled_up: bool
    xp_to_next_level: int


# ── Auth ──────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # FIX (remaining-things request): access tokens are now short-lived
    # (30 min) — the refresh_token is what actually keeps a session
    # alive, and unlike the access token it can be revoked server-side.
    refresh_token: str
    user: StudentProfile


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    refresh_token: str  # rotated — the old one is revoked


class LogoutRequest(BaseModel):
    refresh_token: str


class RequestPasswordResetRequest(BaseModel):
    email: str


class RequestPasswordResetResponse(BaseModel):
    message: str
    # FIX (remaining-things request): no SMTP is configured in most dev/
    # demo environments. Rather than silently doing nothing (or crashing)
    # when email can't actually be sent, the raw reset token is returned
    # directly in the response ONLY when SMTP isn't configured — clearly
    # a dev-mode fallback, never in a real deployment with email set up.
    dev_reset_token: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ── Courses ─────────────────────────────────────────────────

class VideoLink(BaseModel):
    id: str
    title: str
    url: str
    duration: int  # seconds
    thumbnail: str
    order: int
    completed: bool
    watched_percent: float


class Course(BaseModel):
    id: str
    title: str
    description: str
    icon: str
    category: str
    difficulty: Literal["Beginner", "Intermediate", "Advanced"]
    total_videos: int
    completed_videos: int
    progress: float
    estimated_hours: float
    tags: list[str]
    video_links: list[VideoLink]


# ── Course admin management (V2, Phase 4) ──────────────────────
# All course/video-catalog content in V2 is created and removed through
# these admin-only endpoints — there is no hardcoded/seeded catalog and no
# student-facing "paste any URL" entry point (see routers/courses.py).

class CourseCreate(BaseModel):
    title: str
    description: str = ""
    icon: str = "📚"
    category: str = "General"
    difficulty: Literal["Beginner", "Intermediate", "Advanced"] = "Beginner"
    estimated_hours: float = 0
    tags: list[str] = []


class CourseYoutubeVideoCreate(BaseModel):
    """Admin-only: attach a YouTube video to a course by URL. This is the
    one and only place a YouTube URL can enter the system in V2 — never
    seeded, never student-pasted (see routers/courses.py)."""
    title: str
    youtube_url: str
    order: Optional[int] = None


# ── Local Videos (V2, Phase 2) ────────────────────────────────
# Metadata only — relative_path is a validated path relative to the
# server's VIDEO_STORAGE_DIR, never an absolute path and never a binary.
# See services/video_path_safety.py.

class LocalVideoCreate(BaseModel):
    title: str
    relative_path: str  # e.g. "neuroscience-101/lecture-03.mp4"; validated server-side
    course_id: Optional[str] = None
    duration_seconds: Optional[float] = None
    file_size_bytes: Optional[int] = None
    order: Optional[int] = None


class LocalVideoOut(BaseModel):
    id: str
    title: str
    relative_path: str
    course_id: Optional[str] = None
    duration_seconds: Optional[float] = None
    file_size_bytes: Optional[int] = None
    order: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── Behavioral Cue / Camera ──────────────────────────────────────

class AttentionModelOutput(BaseModel):
    """Raw JSON output from the behavioral_cue detection ML model"""
    eye_contact: Optional[float] = Field(default=None, ge=0, le=1)
    # FIX (B-3): when iris landmarks aren't available this frame, eye_contact
    # is not a genuine gaze measurement — it falls back to the head-pose /
    # eye-openness calibration instead. This flag says so, rather than
    # letting a substituted number pass silently as a real measurement.
    eye_contact_estimated: bool = False
    eye_open: Optional[float] = Field(default=None, ge=0, le=1)
    eyes_closed_duration: Optional[float] = Field(default=None, ge=0)
    head_pose: Literal["forward", "slightly_away", "away"]
    face_detected: bool
    blink_rate: float


class AttentionSnapshot(BaseModel):
    timestamp: str
    score: int = Field(ge=0, le=100)
    state: Literal["attentive", "inattentive", "unfocused"]
    confidence: float = Field(ge=0, le=1)
    message: str
    model_response: AttentionModelOutput
    # FIX (MJ4, peer review packet): reader/caller-visible signal for
    # whether this snapshot came from a live ML model or a dummy fallback,
    # so reports and figures generated from these responses can be honestly
    # labeled rather than silently mixing live and dummy output.
    source: Literal["live", "dummy"] = "dummy"
    # FIX (CR6, peer review packet): every stored snapshot carries the
    # consent state it was captured under, so a later audit or data export
    # can distinguish consented sessions from anything that shouldn't have
    # been recorded.
    consent_confirmed: bool = False


class AttentionFrameRequest(BaseModel):
    """Request with base64 camera frame"""
    frame_base64: str
    video_id: str
    student_id: str
    session_id: str
    study_session_id: Optional[str] = None
    # FIX (CR6, peer review packet): the backend must not analyze or store
    # a frame without an on-file consent grant for this student. The
    # frontend gates camera start behind the consent modal (see
    # ConsentModal.tsx) — this flag is a second, server-side check so
    # consent can't be bypassed by calling the API directly.
    consent_confirmed: bool = False


# ── Consent (CR6 fix) ─────────────────────────────────────────

class ConsentGrant(BaseModel):
    """A student's response to the webcam-monitoring consent prompt."""
    student_id: str
    session_id: str
    study_session_id: Optional[str] = None
    granted: bool
    # What the student was told at the time of consent — kept alongside the
    # grant so a later policy change doesn't retroactively reinterpret an
    # old consent. Mirrors the retention/opt-out language in ConsentModal.
    retention_days: int = 30
    raw_frames_stored: bool = False  # NeuroLearn never persists raw images — only derived scores
    version: str = "1.0"  # consent-copy version, bump if the disclosure text changes


class ConsentStatus(BaseModel):
    student_id: str
    session_id: Optional[str] = None
    granted: bool
    granted_at: Optional[str] = None
    retention_days: int = 30
    raw_frames_stored: bool = False
    version: str = "1.0"


# ── Study-participation consent (A-2) ──────────────────────────
# Separate from webcam consent above — the mentor guidelines treat study
# consent and camera choice as separate decisions. This is the consent
# checked before any research record is created.

class StudyConsentGrant(BaseModel):
    """A student's response to the study-participation consent prompt."""
    granted: bool
    version: str = "1.0"


class StudyConsentStatus(BaseModel):
    student_id: str
    granted: bool
    granted_at: Optional[str] = None
    version: str = "1.0"


# ── Transcription ───────────────────────────────────────────

class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float
    confidence: float


class TranscriptionModelOutput(BaseModel):
    """Raw JSON from Whisper / transcription model"""
    language: str
    words: list[WordTimestamp]


class TranscriptSegment(BaseModel):
    id: str
    text: str
    timestamp: str
    start_time: float
    end_time: float
    confidence: float
    model_response: TranscriptionModelOutput


class TranscriptionRequest(BaseModel):
    video_id: str
    audio_chunk_base64: Optional[str] = None
    video_url: Optional[str] = None


# ── Assessment / Quiz ───────────────────────────────────────

class LLMQuestionMetadata(BaseModel):
    """JSON metadata from the LLM question generator (FLAN-T5)"""
    model: str
    generated_from: str
    difficulty_score: float = Field(ge=0, le=1)
    blooms_level: Literal[
        "remember", "understand", "apply", "analyze", "evaluate", "create"
    ]


class AssessmentQuestion(BaseModel):
    id: str
    type: Literal["mcq", "true-false", "short-answer"]
    question: str
    options: Optional[list[str]] = None
    correct_answer: int | str
    difficulty: Literal["easy", "medium", "hard"]
    points: int
    explanation: str
    topic_id: str
    llm_metadata: LLMQuestionMetadata
    # Assigned by the assessment orchestrator, not the client.  This makes
    # the fixed MCRF (1--5) / LEGACY (6--10) sequence visible in each item
    # while remaining additive for older generated-question payloads.
    adaptive_method: Optional[Literal["MCRF", "LEGACY"]] = None
    decision_index: Optional[int] = None


class AdaptiveMetadata(BaseModel):
    """JSON from the adaptive difficulty engine"""
    previous_score: Optional[float] = None
    adjusted_difficulty: str
    reason: str


class AssessmentSession(BaseModel):
    id: str
    study_session_id: Optional[str] = None
    participant_id: Optional[str] = None
    condition: Optional[Literal["MCRF", "LEGACY", "MIXED"]] = None
    course_id: str
    video_id: str
    contributing_video_ids: list[str] = []
    questions: list[AssessmentQuestion]
    difficulty: Literal["easy", "medium", "hard"]
    time_limit: int  # seconds
    attention_score_during_video: Optional[float] = None
    adaptive_metadata: AdaptiveMetadata


class GenerateAssessmentRequest(BaseModel):
    course_id: str
    video_id: str
    student_id: str
    study_session_id: Optional[str] = None
    attention_score: float = Field(ge=0, le=100)
    previous_score: Optional[float] = None
    transcript_text: Optional[str] = None
    contributing_video_ids: list[str] = []


class SubmitAdaptiveAnswerRequest(BaseModel):
    session_id: str
    student_id: str
    question_id: str
    answer: int | str
    response_event: Optional[dict] = None


class SubmitAssessmentRequest(BaseModel):
    session_id: str
    student_id: str
    answers: dict[str, int | str]  # question_id -> selected answer
    time_spent: int  # seconds
    response_events: Optional[list[dict]] = None


class AdaptiveResponse(BaseModel):
    """JSON from the adaptive engine after assessment"""
    performance_trend: Literal["improving", "stable", "declining"]
    recommended_action: str
    next_assessment_difficulty: Literal["easy", "medium", "hard"]
    strength_areas: list[str]
    weak_areas: list[str]
    # Phase 11 addition (additive, Optional — old clients unaffected):
    # full Cognitive Readiness Score breakdown, so the frontend can render
    # the CRS gauge/component bars in Phase 13 without a second API call.
    crs: Optional[dict] = None


class AssessmentResult(BaseModel):
    session_id: str
    study_session_id: Optional[str] = None
    participant_id: Optional[str] = None
    condition: Optional[Literal["MCRF", "LEGACY", "MIXED"]] = None
    score: float
    total_points: int
    earned_points: int
    percentage: float
    xp_earned: int
    time_spent: int
    correct_answers: int
    total_questions: int
    difficulty: str
    message: str
    next_difficulty: Literal["easy", "medium", "hard"]
    suggested_topics: list[str]
    adaptive_response: AdaptiveResponse
    # Phase 10/12 additions (additive, Optional — old clients unaffected).
    # `student_id` was already silently present in the dict returned by
    # submit_assessment() before this change but was being dropped by
    # FastAPI's response_model filtering since it was never declared here;
    # `timestamp` is new, needed for get_recent_scores_pct() DB ordering.
    student_id: Optional[str] = None
    # Auth/real-XP fix additions: reflects the *actual* persisted XP state
    # after this submission (previously xp_earned was computed but never
    # written to the student's record — see routers/assessment.py's
    # _apply_xp()). The frontend should use these, not just xp_earned, to
    # display the student's running total.
    total_xp: Optional[int] = None
    new_level: Optional[int] = None
    leveled_up: Optional[bool] = None
    timestamp: Optional[float] = None


# ── Leaderboard ─────────────────────────────────────────────

class LeaderboardEntry(BaseModel):
    rank: int
    student_id: str
    name: str
    avatar: str
    xp: int
    level: int
    streak: int
    courses_completed: int


# ── Daily Challenges ────────────────────────────────────────

class DailyChallenge(BaseModel):
    id: str
    title: str
    description: str
    xp_reward: int
    type: Literal["watch", "quiz", "streak", "review"]
    completed: bool
    progress: int
    target: int


# ── Notifications ───────────────────────────────────────────

class Notification(BaseModel):
    id: str
    type: Literal["achievement", "reminder", "milestone", "challenge"]
    title: str
    message: str
    timestamp: str
    read: bool
    icon: str


# ── Health ──────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    models_loaded: dict[str, bool]