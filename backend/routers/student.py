"""
============================================================
ROUTER: Student — Profile, XP, Badges
Endpoints:
    GET  /api/student/profile   (auth required — returns YOUR profile)
    POST /api/student/xp        (auth required — awards XP to YOU)
============================================================
FIX (auth request): both endpoints previously trusted a client-supplied
student_id with no verification — any caller could read or credit XP to
any other student by simply passing a different id. Both now derive
identity from the JWT (Depends(get_current_user)) instead.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from schemas.models import (
    StudentProfile, XPAwardRequest, XPAwardResponse,
    VideoProgressRequest, VideoProgressResponse,
)
from data.db import get_db
from data.database import _user_to_dict, apply_xp, upsert_video_watch_progress
from data.models_orm import User
from auth.security import get_current_user

router = APIRouter(prefix="/api/student", tags=["Student"])


@router.get("/profile", response_model=StudentProfile)
async def get_profile(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get the authenticated student's profile (badges, XP, streak, live rank)."""
    return StudentProfile(**_user_to_dict(current_user, db))


@router.post("/video-progress", response_model=VideoProgressResponse)
async def save_video_progress(
    request: VideoProgressRequest,
    current_user: User = Depends(get_current_user),
):
    """Persist how far the authenticated student has watched into a video,
    so the course-centric learning flow (see frontend app/video/page.tsx)
    can resume a partially-watched video instead of always restarting at
    0:00. Called frequently but throttled client-side — this is a plain
    upsert, not an audit/evidence record."""
    result = upsert_video_watch_progress(
        user_id=current_user.id,
        video_id=request.video_id,
        position_seconds=request.position_seconds,
        duration_seconds=request.duration_seconds,
    )
    return VideoProgressResponse(**result)


@router.post("/xp", response_model=XPAwardResponse)
async def award_xp(
    request: XPAwardRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Award XP to the authenticated student. Handles level-up logic.
    `request.student_id` is accepted for backward compatibility with the
    existing frontend/schema but is IGNORED for authorization purposes —
    XP is always credited to whoever the JWT identifies, never to an
    arbitrary id an unauthenticated or malicious caller could supply.
    """
    xp_result = apply_xp(current_user, request.amount)
    db.commit()

    return XPAwardResponse(
        new_xp=xp_result["new_xp"],
        new_level=xp_result["new_level"],
        leveled_up=xp_result["leveled_up"],
        xp_to_next_level=xp_result["xp_to_next_level"],
    )
