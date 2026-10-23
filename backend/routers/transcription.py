"""
============================================================
ROUTER: Transcription — Video transcription endpoints
Endpoints:
    GET  /api/transcription/{video_id}        — full transcript
    GET  /api/transcription/{video_id}/live    — segment at timestamp
    POST /api/transcription/chunk              — transcribe audio chunk
============================================================
"""

import functools

import anyio
from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from schemas.models import TranscriptSegment, TranscriptionRequest
from ml import transcription_service
from ml.transcription_model import TranscriptionError

router = APIRouter(prefix="/api/transcription", tags=["Transcription"])


async def _run_detached(func, *args, **kwargs):
    """Run a blocking call (yt-dlp + FFmpeg + Faster-Whisper) in a worker
    thread, the same way run_in_threadpool does, but with `cancellable=True`.

    FIX (shutdown hang): run_in_threadpool's default (`cancellable=False`)
    means that if the request's asyncio task is cancelled — e.g. uvicorn
    cancelling in-flight request tasks on shutdown — the `await` still
    blocks until the worker thread finishes, no matter how long that takes.
    For a multi-minute Whisper job, that's exactly what produced the
    Ctrl+C hang ("Waiting for connections to close." / "ASGI 'lifespan'
    protocol appears unsupported."), since the server could never actually
    finish shutting down while this request stayed in flight.
    `cancellable=True` lets the `await` return immediately on cancellation
    instead, so the request task — and therefore the server shutdown — is
    no longer blocked. The worker thread itself is a daemon thread and
    keeps running the yt-dlp/FFmpeg/Whisper job independently in the
    background (its eventual result is simply not awaited anymore); it
    still populates the existing in-memory transcript cache and releases
    the existing per-video lock exactly as before, so normal (non-shutdown)
    behavior — including per-video locking and CUDA/CPU handling inside
    the model — is completely unchanged.
    """
    call = functools.partial(func, *args, **kwargs)
    return await anyio.to_thread.run_sync(call, cancellable=True)


@router.get("/{video_id}", response_model=list[TranscriptSegment])
async def get_full_transcript(
    video_id: str,
    video_url: str = Query(...),
):
    # run off the event loop (see _run_detached) so this request neither
    # blocks other API requests while running, nor blocks server shutdown
    # if it's still in flight when the server stops.
    try:
        return await _run_detached(
            transcription_service.get_full_transcript,
            video_id,
            video_url=video_url,
        )
    except TranscriptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

@router.get("/{video_id}/live")
async def get_live_segment(
    video_id: str,
    current_time: float = Query(0.0),
    video_url: str = Query(...),
):
    """
    Get the transcript segment at a specific video timestamp.
    Used for live sync — frontend polls this as video plays.
    """
    # FIX (repeated /live polling): try the cheap, in-memory-only path
    # first — no lock, no thread-pool dispatch, no risk of ever
    # re-running the pipeline. This is the common case for essentially
    # every poll after the first: once a video's transcript is cached
    # (usually within the first second or two of playback), the other
    # ~dozens-to-hundreds of polls for the rest of the video only need
    # this. Only fall through to the full (thread-offloaded) path below
    # on an actual cache miss — the video hasn't been transcribed yet at
    # all, or this process just restarted.
    cache_hit, segment = transcription_service.get_segment_at_time_cached_only(
        current_time, video_url
    )
    if not cache_hit:
        # Same reasoning as get_full_transcript above — the first poll for a
        # given video can trigger the same long transcribe job
        # (transcription_service.get_segment_at_time -> transcribe_video_url).
        try:
            segment = await _run_detached(
                transcription_service.get_segment_at_time,
                current_time,
                video_url=video_url,
            )
        except TranscriptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if segment:
        return segment

    return {
        "id": None,
        "text": "",
        "timestamp": "",
        "message": "No segment at this timestamp",
    }


@router.post("/chunk")
async def transcribe_audio_chunk(request: TranscriptionRequest):
    """
    Transcribe a raw audio chunk (base64 encoded).

    Used for real-time transcription:
        1. Frontend captures audio from video
        2. Encodes as base64
        3. Sends to this endpoint
        4. Whisper processes → returns text

    Returns an explicit service error if Whisper is unavailable or produces no text.
    """
    if request.audio_chunk_base64:
        # Bounded, single-chunk work — not the long-running whole-video
        # job, so it keeps the existing run_in_threadpool behavior.
        try:
            segments = await run_in_threadpool(
                transcription_service.transcribe_audio_chunk,
                request.audio_chunk_base64,
            )
        except TranscriptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"video_id": request.video_id, "segments": segments}

    if request.video_url:
        # This path runs the same long-running whole-video job as the
        # endpoints above, so it needs the same detachable execution.
        try:
            segments = await _run_detached(
                transcription_service.transcribe_video_url,
                request.video_url,
            )
        except TranscriptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"video_id": request.video_id, "segments": segments}

    return {"error": "Provide either audio_chunk_base64 or video_url"}
