"""
============================================================
ROUTER: Courses — Course listing, details, video links, and
V2 admin content management (create/delete courses, attach/detach
local videos, bulk folder import). Admin endpoints require X-Admin-Key
(see services/admin_auth.py) — there is no student-facing way to add,
remove, or point content at an arbitrary URL.

Endpoints:
    GET    /api/courses
    GET    /api/courses/{course_id}
    GET    /api/courses/{course_id}/videos/{video_id}
    POST   /api/courses                                          [admin]
    POST   /api/courses/import                                   [admin]
    DELETE /api/courses/{course_id}                               [admin]
    POST   /api/courses/{course_id}/videos/youtube                [admin]
    POST   /api/courses/{course_id}/videos/{local_video_id}       [admin]
    DELETE /api/courses/{course_id}/video-links/{video_link_id}   [admin]
============================================================
"""
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Header
from schemas.models import Course, VideoLink, CourseCreate, CourseYoutubeVideoCreate
from data.database import (
    get_all_courses,
    get_course,
    create_course,
    delete_course,
    bulk_import_course,
    attach_local_video_to_course,
    attach_youtube_video_to_course,
    detach_video_from_course,
)
from data.models_orm import User
from auth.security import get_current_user_optional
from services.admin_auth import require_admin, is_valid_admin_key
from services.youtube_validation import is_youtube_url
from services.video_path_safety import (
    UnsafeVideoPathError, validate_relative_video_path, sanitize_path_segment,
    probe_video_duration_seconds,
)
from config.video_config import get_video_storage_root, ALLOWED_VIDEO_EXTENSIONS

router = APIRouter(prefix="/api/courses", tags=["Courses"])


async def student_or_admin(
    current_user: Optional[User] = Depends(get_current_user_optional),
    x_admin_key: str = Header(default=""),
) -> Optional[User]:
    """These GET endpoints have two legitimate callers with two different
    credentials: a logged-in student (JWT — wants their own real
    progress) and the admin content-management UI (X-Admin-Key — the
    same /admin page that manages courses also lists them, but the admin
    operator is never "logged in" as a student and has no JWT to send).

    FIX: these endpoints used to require Depends(get_current_user)
    unconditionally (student JWT only). That broke the admin panel's own
    course listing — adminListCourses()/adminListVideos() call these
    exact routes with only X-Admin-Key, and calls to Promise.all([...])
    it as part of verifying the key correctness on unlock — so a
    CORRECT admin key always 401'd here regardless, because this
    dependency never looked at X-Admin-Key at all.

    Returns the student User for per-student progress, or None for an
    admin caller (get_all_courses/get_course already have a documented
    "no user in scope" fallback for that — the global, non-personalized
    values). Raises 401 only if NEITHER credential is valid.
    """
    if current_user is not None:
        return current_user
    if is_valid_admin_key(x_admin_key):
        return None
    raise HTTPException(status_code=401, detail="Login required, or a valid X-Admin-Key header.")


@router.get("/", response_model=list[Course])
async def list_courses(current_user: Optional[User] = Depends(student_or_admin)):
    """Get all available courses — with this student's actual progress
    if called as a student, or the global/unpersonalized view if called
    from the admin content-management UI."""
    return get_all_courses(user_id=current_user.id if current_user else None)


@router.get("/{course_id}", response_model=Course)
async def get_course_detail(course_id: str, current_user: Optional[User] = Depends(student_or_admin)):
    """Get a specific course, with this student's actual progress, and all video links."""
    course = get_course(course_id, user_id=current_user.id if current_user else None)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    return course


@router.get("/{course_id}/videos/{video_id}")
async def get_video_detail(
    course_id: str, video_id: str, current_user: Optional[User] = Depends(student_or_admin)
):
    """Get a specific video within a course."""
    course = get_course(course_id, user_id=current_user.id if current_user else None)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    video = next(
        (v for v in course.get("video_links", []) if v["id"] == video_id),
        None,
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    return {"course": course, "video": video}


# ── Admin: course management (V2, Phase 4) ─────────────────────

@router.post("/", response_model=Course, status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_course(body: CourseCreate):
    """Create a new (empty) course. Attach local videos to it afterward
    via POST /api/courses/{course_id}/videos/{local_video_id}.
    """
    return create_course(
        title=body.title,
        description=body.description,
        icon=body.icon,
        category=body.category,
        difficulty=body.difficulty,
        estimated_hours=body.estimated_hours,
        tags=body.tags,
    )


@router.post("/import", response_model=Course, status_code=201, dependencies=[Depends(require_admin)])
async def admin_import_course_folder(
    title: str = Form(...),
    description: str = Form(""),
    icon: str = Form("📚"),
    category: str = Form("General"),
    difficulty: str = Form("Beginner"),
    estimated_hours: float = Form(0),
    tags: str = Form("[]"),  # JSON-encoded list[str]
    relative_paths: list[str] = Form(...),  # webkitRelativePath per file, same order as `files`
    files: list[UploadFile] = File(...),
):
    """Bulk-import an entire course folder in one request: every video
    file the admin's browser found under the dropped/selected folder,
    plus the course metadata the admin confirmed in the review step.
    Creates the Course and every LocalVideo/video_link as a single
    all-or-nothing transaction (see data/database.py::bulk_import_course)
    — no half-populated course if something fails partway through.

    `relative_paths[i]` is the path of `files[i]` *within* the dropped
    folder (e.g. "Module 1/01-intro.mp4"), exactly as the browser's
    File.webkitRelativePath reports it — untrusted input, sanitized
    segment-by-segment below before it ever touches the filesystem.
    """
    import json

    if len(files) != len(relative_paths):
        raise HTTPException(
            status_code=400,
            detail=f"files ({len(files)}) and relative_paths ({len(relative_paths)}) count mismatch.",
        )
    if not files:
        raise HTTPException(
            status_code=400,
            detail="The selected folder does not contain any supported video files.",
        )

    try:
        tags_list = json.loads(tags)
        if not isinstance(tags_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="tags must be a JSON array of strings.")

    # A fresh, server-chosen folder name for this import — independent of
    # the eventual Course.id (which doesn't exist until the DB insert
    # below) and never derived from admin-supplied text, so nothing in
    # the folder path is attacker/admin controlled.
    import_folder = uuid.uuid4().hex
    storage_root = get_video_storage_root()
    written_files: list[Path] = []

    try:
        video_specs = []
        skipped: list[str] = []

        for index, (upload, rel_path) in enumerate(zip(files, relative_paths)):
            segments = [sanitize_path_segment(p) for p in rel_path.replace("\\", "/").split("/") if p.strip()]
            if not segments:
                skipped.append(rel_path or f"file #{index}")
                continue

            candidate_relative = f"{import_folder}/" + "/".join(segments)
            try:
                safe_relative = validate_relative_video_path(candidate_relative)
            except UnsafeVideoPathError:
                # Most commonly: extension not in ALLOWED_VIDEO_EXTENSIONS
                # (e.g. a .DS_Store, thumbs.db, or subtitle file the
                # folder picker also picked up) — skip, don't fail the
                # whole import over a non-video file in the folder.
                skipped.append(rel_path)
                continue

            dest = storage_root / safe_relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
            written_files.append(dest)

            duration = probe_video_duration_seconds(dest)
            module = segments[0] if len(segments) > 1 else ""
            video_specs.append({
                "relative_path": safe_relative,
                "title": Path(segments[-1]).stem,
                "module": module,
                "duration_seconds": duration,
                "file_size_bytes": dest.stat().st_size,
            })

        if not video_specs:
            raise HTTPException(
                status_code=400,
                detail="The selected folder does not contain any supported video files.",
            )

        # Deterministic order: by module/subfolder, then filename —
        # matches "detect module/folder structure" from the import spec
        # without needing the admin to hand-order anything.
        video_specs.sort(key=lambda v: (v["module"], v["relative_path"]))
        for i, v in enumerate(video_specs):
            v["order"] = i + 1

        return bulk_import_course(
            title=title,
            description=description,
            icon=icon,
            category=category,
            difficulty=difficulty,
            estimated_hours=estimated_hours,
            tags=tags_list,
            videos=video_specs,
        )
    except HTTPException:
        for f in written_files:
            f.unlink(missing_ok=True)
        raise
    except Exception:
        # Any unexpected failure (disk full, DB error inside
        # bulk_import_course, etc.) — clean up whatever was already
        # written to disk rather than leaving orphaned video files with
        # no DB row pointing at them.
        for f in written_files:
            f.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Import failed; no partial course was created.")


@router.delete("/{course_id}", status_code=204, dependencies=[Depends(require_admin)])
async def admin_delete_course(course_id: str):
    """Deletes the course. Does not delete any LocalVideo rows/files —
    those remain in the library and can be attached to another course.
    """
    if not delete_course(course_id):
        raise HTTPException(status_code=404, detail="Course not found")


@router.post("/{course_id}/videos/youtube", response_model=Course, dependencies=[Depends(require_admin)])
async def admin_attach_youtube_video(course_id: str, body: CourseYoutubeVideoCreate):
    """Attaches a YouTube video to this course's catalog by URL. This is
    the only way a YouTube URL enters V2 — admin-only, never seeded,
    never a student-facing "paste a URL" field. Videos in a course can
    be local (see the endpoint below) or YouTube, mixed freely.
    """
    if not is_youtube_url(body.youtube_url):
        raise HTTPException(
            status_code=400,
            detail="Not a recognizable YouTube URL (expected youtube.com/watch, youtu.be/, youtube.com/embed/, or /shorts/).",
        )
    course = get_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    return attach_youtube_video_to_course(
        course_id, title=body.title, youtube_url=body.youtube_url, order=body.order
    )


@router.post("/{course_id}/videos/{local_video_id}", response_model=Course, dependencies=[Depends(require_admin)])
async def admin_attach_video(course_id: str, local_video_id: str, title: str | None = None, order: int | None = None):
    """Attaches an already-registered local video (see POST /api/videos/)
    to this course's catalog. Builds the video_links[].url from the
    Nginx-served local video URL — never a hand-entered or YouTube URL.
    """
    course = attach_local_video_to_course(course_id, local_video_id, title=title, order=order)
    if not course:
        raise HTTPException(status_code=404, detail="Course or local video not found")
    return course


@router.delete("/{course_id}/video-links/{video_link_id}", response_model=Course, dependencies=[Depends(require_admin)])
async def admin_detach_video(course_id: str, video_link_id: str):
    """Removes a video from this course's catalog (by video_links[].id,
    not the underlying local video id). The LocalVideo row is untouched.
    """
    course = detach_video_from_course(course_id, video_link_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    return course