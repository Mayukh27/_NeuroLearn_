"""
============================================================
ROUTER: Content Discovery — Auto Course Generator
File: backend/routers/content.py

Endpoints:
    POST /api/content/discover
        Accept topic → scrape → store course → return structured course
        with video list.

    GET  /api/content/courses/auto
        List all auto-generated courses stored in DB.

    POST /api/content/pipeline/full
        Run full pipeline: discover → queue transcription → queue assessment
        for each discovered video (async background tasks).
============================================================
"""

import time
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from loguru import logger

from scraping.pipeline import discover_content
from data.database import (
    save_auto_course,
    get_auto_course,
    get_all_auto_courses,
    update_video_transcription_status,
    save_auto_course_to_courses,
)
from ml import transcription_service, question_generator, adaptive_engine

router = APIRouter(prefix="/api/content", tags=["Content Discovery"])


# ── Request / Response Schemas ────────────────────────────────

class DiscoverRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=200, example="Operating Systems")
    max_videos: int = Field(default=5, ge=1, le=10)
    auto_transcribe: bool = Field(
        default=False,
        description="If True, immediately queue transcription for each video (slow)"
    )


class VideoSummary(BaseModel):
    id: str
    title: str
    url: str
    duration: int
    thumbnail: str
    channel: str
    assessment_available: bool
    transcription_available: bool
    # FIX (course generator request): which curriculum stage this video
    # was chosen for (Fundamentals/Core Concepts/Intermediate/Advanced/
    # Applied Project) — without this the frontend has no way to show
    # that the course actually spans levels rather than repeating one.
    stage_label: str = ""


class DiscoverResponse(BaseModel):
    course_id: str
    course_title: str
    topic: str
    description: str
    videos: list[VideoSummary]
    total_found: int
    status: str
    elapsed_seconds: float
    # FIX (course generator request): which stages actually got at least
    # one video — lets the UI/caller detect a still-too-narrow course.
    stages_covered: list[str] = []


class FullPipelineRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=200, example="Machine Learning")
    max_videos: int = Field(default=3, ge=1, le=5)
    student_id: str = Field(default="student_001")
    attention_score: float = Field(default=75.0, ge=0, le=100)


class FullPipelineResponse(BaseModel):
    course_id: str
    course_title: str
    status: str
    message: str
    videos_queued: int
    assessment_sessions: list[dict]


# ── Background Task: Full Pipeline ───────────────────────────

def _run_full_pipeline_for_video(
    course_id: str,
    video: dict,
    student_id: str,
    attention_score: float,
) -> dict:
    """
    For a single video:
      1. Transcribe via Whisper
      2. Generate assessment from transcript via FLAN-T5
    Returns assessment session dict.
    """
    video_id = video["id"]
    video_url = video["url"]
    title = video["title"]

    logger.info(f"[FullPipeline] Processing video: {title[:50]}")

    # Step 1: Transcription
    try:
        segments = transcription_service.transcribe_video_url(video_url)
        transcript_text = " ".join(
            seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")
            for seg in segments
        )
        logger.info(f"[FullPipeline] Transcription done for {video_id} ({len(transcript_text)} chars)")
    except Exception as ex:
        update_video_transcription_status(course_id, video_id, available=False)
        logger.exception(f"[FullPipeline] Transcription failed for {video_id}; assessment generation skipped: {ex}")
        raise RuntimeError(f"Transcription failed for video {video_id}; questions were not generated") from ex
    if not transcript_text.strip():
        update_video_transcription_status(course_id, video_id, available=False)
        logger.error(f"[FullPipeline] Empty transcript for {video_id}; assessment generation skipped")
        raise RuntimeError(f"Transcription returned no usable text for video {video_id}")
    update_video_transcription_status(course_id, video_id, available=True)

    # Step 2: Adaptive difficulty
    difficulty_result = adaptive_engine.get_initial_difficulty(
        student_id=student_id,
        attention_score=attention_score,
        previous_score=None,
    )
    difficulty = difficulty_result["difficulty"]

    # Step 3: Question generation
    try:
        questions = question_generator.generate_questions(
            transcript_text=transcript_text,
            difficulty=difficulty,
            num_questions=5,
            topic_id=course_id,
        )
    except Exception as ex:
        logger.warning(f"[FullPipeline] Question generation failed for {video_id}: {ex}")
        questions = []

    # Step 4: Build session
    session_id = f"auto_session_{uuid.uuid4().hex[:10]}"
    time_limits = {"easy": 600, "medium": 420, "hard": 300}

    session = {
        "id": session_id,
        "course_id": course_id,
        "video_id": video_id,
        "video_title": title,
        "questions": questions,
        "difficulty": difficulty,
        "time_limit": time_limits.get(difficulty, 420),
        "attention_score_during_video": attention_score,
        "adaptive_metadata": difficulty_result.get("adaptive_metadata", {}),
        "student_id": student_id,
        "created_at": time.time(),
        "auto_generated": True,
    }

    from data.database import save_assessment_session
    save_assessment_session(session)
    logger.success(f"[FullPipeline] Session {session_id} saved for video {video_id}")

    return {
        "session_id": session_id,
        "video_id": video_id,
        "video_title": title,
        "difficulty": difficulty,
        "question_count": len(questions),
    }


def _background_full_pipeline(
    course_id: str,
    videos: list[dict],
    student_id: str,
    attention_score: float,
):
    """Background task executed by FastAPI BackgroundTasks."""
    logger.info(f"[Background] Starting full pipeline for course {course_id} ({len(videos)} videos)")
    for video in videos:
        try:
            _run_full_pipeline_for_video(course_id, video, student_id, attention_score)
        except Exception as ex:
            logger.error(f"[Background] Pipeline failed for video {video.get('id')}: {ex}")
    logger.success(f"[Background] Full pipeline complete for course {course_id}")


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/discover", response_model=DiscoverResponse)
async def discover_course_content(request: DiscoverRequest):
    """
    **Auto Course Generator** — Discover educational videos for a topic.

    1. Scrapes YouTube search results (no paid API)
    2. Deduplicates and ranks results
    3. Stores as a new auto-generated course in the database
    4. Returns structured video list ready for the learning pipeline

    Each returned video has:
    - `assessment_available: true` (will be generated on-demand)
    - `transcription_available: false` (generated when video is played)

    Set `auto_transcribe: true` to eagerly transcribe all videos
    (significantly slower — recommended only for small max_videos).
    """
    topic = request.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Topic cannot be empty")

    logger.info(f"[/discover] Request: topic='{topic}', max_videos={request.max_videos}")

    # ── Run pipeline ──
    result = discover_content(topic=topic, max_videos=request.max_videos)

    if result["status"] == "failed" or not result["videos"]:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not find videos for topic '{topic}'. "
                "Try a different topic or more specific keywords."
            ),
        )

    # ── Persist to DB ──
    save_auto_course(result)
    logger.info(f"[/discover] Saved course {result['course_id']} to DB")

    return DiscoverResponse(
        course_id=result["course_id"],
        course_title=result["course_title"],
        topic=result["topic"],
        description=result["description"],
        videos=[VideoSummary(**{k: v[k] for k in VideoSummary.__fields__}) for v in result["videos"]],
        total_found=result["total_found"],
        status=result["status"],
        elapsed_seconds=result["elapsed_seconds"],
    )


@router.get("/courses/auto")
async def list_auto_courses():
    """
    List all auto-generated courses stored in the database.
    """
    courses = get_all_auto_courses()
    return {
        "total": len(courses),
        "courses": courses,
    }


@router.get("/courses/auto/{course_id}")
async def get_auto_course_detail(course_id: str):
    """
    Get details of a specific auto-generated course.
    """
    course = get_auto_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail=f"Auto course '{course_id}' not found")
    return course


@router.post("/courses/auto/{course_id}/save")
async def save_auto_course_to_dashboard(course_id: str):
    """
    Save an auto-generated course into the main courses collection
    so it appears in dashboard "All Courses".
    """
    auto_course = get_auto_course(course_id)
    if not auto_course:
        raise HTTPException(status_code=404, detail=f"Auto course '{course_id}' not found")

    saved_course = save_auto_course_to_courses(course_id)
    if not saved_course:
        raise HTTPException(status_code=500, detail="Failed to save auto course")

    return {
        "saved": True,
        "course_id": saved_course.get("id"),
        "title": saved_course.get("title"),
        "message": "Course saved to dashboard successfully",
    }


@router.post("/pipeline/full", response_model=FullPipelineResponse)
async def run_full_pipeline(
    request: FullPipelineRequest,
    background_tasks: BackgroundTasks,
):
    """
    **Full Auto-Course Pipeline** — Discover + Transcribe + Assess.

    Runs the complete NeuroLearn pipeline for a topic:
    1. Discovers videos via web scraping
    2. Stores auto-generated course
    3. Queues background task that:
       - Transcribes each video (Whisper)
       - Generates assessment questions (FLAN-T5)
       - Creates assessment sessions

    The endpoint returns immediately. Transcription + assessment
    generation happens in the background (use GET /courses/auto/{id}
    to poll for completion status).
    """
    topic = request.topic.strip()

    # ── Discover content ──
    result = discover_content(topic=topic, max_videos=request.max_videos)

    if result["status"] == "failed" or not result["videos"]:
        raise HTTPException(
            status_code=502,
            detail=f"No videos found for topic '{topic}'",
        )

    save_auto_course(result)

    course_id = result["course_id"]
    videos = result["videos"]

    # ── Kick off background pipeline ──
    background_tasks.add_task(
        _background_full_pipeline,
        course_id=course_id,
        videos=videos,
        student_id=request.student_id,
        attention_score=request.attention_score,
    )

    logger.info(f"[/pipeline/full] Background pipeline queued for course {course_id}")

    return FullPipelineResponse(
        course_id=course_id,
        course_title=result["course_title"],
        status="queued",
        message=(
            f"Auto course created with {len(videos)} videos. "
            "Transcription and assessment generation are running in the background."
        ),
        videos_queued=len(videos),
        assessment_sessions=[],   # populated in background
    )
