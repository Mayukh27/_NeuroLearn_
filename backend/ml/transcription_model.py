import os
import base64
import subprocess
import tempfile
import threading
import uuid
from typing import Optional

from loguru import logger

from config.video_config import nginx_url_to_relative_path
from services.video_path_safety import UnsafeVideoPathError, resolve_video_absolute_path


class TranscriptionError(RuntimeError):
    """The source could not be transcribed into genuine, non-empty text."""

try:
    from faster_whisper import WhisperModel

    WHISPER_AVAILABLE = True
    logger.info("Faster-Whisper loaded — transcription model LIVE")
except ImportError:
    WHISPER_AVAILABLE = False
    logger.warning("Faster-Whisper not installed — using DUMMY transcription")


class TranscriptionService:

    def __init__(self, model_size: str = "base"):
        self.model = None
        self.model_size = model_size
        self._segment_counter = 0

        # Cache: video URL -> real Whisper transcript
        self._video_transcripts: dict[str, list[dict]] = {}

        # FIX: per-video-URL locks so concurrent requests for the SAME
        # video (e.g. the full-transcript fetch and the live-segment poll
        # both firing once playback starts) coalesce into one actual
        # yt-dlp+FFmpeg+Whisper job instead of running it twice in
        # parallel. `_locks_guard` only protects creating/looking up the
        # per-video lock itself, not the transcription work.
        self._transcription_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        if WHISPER_AVAILABLE:
            try:
                logger.info(f"Loading Whisper model: {model_size}")

                self.model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                )

                logger.success(f"Whisper {model_size} loaded successfully")

            except Exception as e:
                logger.error(f"Failed to load Whisper: {e}")
                self.model = None

    # ============================================================
    # REAL VIDEO TRANSCRIPTION
    # ============================================================

    def _get_lock_for_video(self, video_url: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._transcription_locks.get(video_url)
            if lock is None:
                lock = threading.Lock()
                self._transcription_locks[video_url] = lock
            return lock

    def _resolve_local_source(self, video_url: str) -> Optional[str]:
        """V2 (Phase 5): if `video_url` is one of our own Nginx-served
        local video URLs (see config/video_config.py), returns the
        absolute path to that file on disk — so transcribe_video_url()
        can read it directly instead of running yt-dlp. Returns None for
        a YouTube URL (or anything else), which keeps using yt-dlp
        exactly as before. This is the only thing that changes based on
        video source; caching/locking/Whisper logic is identical either
        way.
        """
        relative_path = nginx_url_to_relative_path(video_url)
        if relative_path is None:
            return None
        try:
            path = resolve_video_absolute_path(relative_path)
        except UnsafeVideoPathError as e:
            logger.error(f"Refusing to transcribe unsafe local video path {relative_path!r}: {e}")
            return None
        if not path.is_file():
            logger.error(f"Local video file not found for transcription: {path}")
            return None
        return str(path)

    def _load_from_database_cache(self, video_url: str) -> Optional[list[dict]]:
        """Look up a persisted transcript in Postgres (data/database.py's
        video_transcript_cache table). Never raises — a DB hiccup here
        should fall through to a fresh transcription, not break the
        request. Imported lazily to avoid ml/ importing data/ at module
        load time for anything other than this optional lookup.
        """
        try:
            from data.database import get_cached_video_transcript
            return get_cached_video_transcript(video_url)
        except Exception:
            logger.exception(f"Database transcript lookup failed for video: {video_url}")
            return None

    def _save_to_database_cache(self, video_url: str, segments: list[dict]) -> None:
        """Persist a successfully-produced real transcript so it survives a
        backend restart. Never raises — if this fails, the in-memory cache
        set by the caller just before this call still serves this process
        for its remaining lifetime; only cross-restart persistence is lost,
        not the transcript this request already produced.
        """
        try:
            from data.database import save_video_transcript
            save_video_transcript(video_url, segments)
            logger.info(f"Saved transcript to persistent database cache: {video_url}")
        except Exception:
            logger.exception(f"Failed to persist transcript to database for video: {video_url}")

    def get_cached_transcript_only(self, video_url: str) -> Optional[list[dict]]:
        """
        FIX (repeated /live polling): a plain in-memory dict read, nothing
        else — no lock, no database round-trip, no pipeline. Returns None
        if this video hasn't been transcribed yet in this process.

        /live polls arrive roughly once a second for the whole duration of
        playback; once a video's transcript is cached, every one of those
        polls only needs this. Callers should use this directly (it's O(1)
        and safe to call straight from the event loop, no thread offload
        needed) and only fall back to the full transcribe_video_url() path
        — which does need thread offload — on a miss (the video hasn't
        been transcribed yet at all, or this process just restarted and
        the in-memory cache is cold).
        """
        return self._video_transcripts.get(video_url)

    def transcribe_video_url(self, video_url: str) -> list[dict]:
        """
        Download audio from a video URL using yt-dlp + FFmpeg,
        then transcribe it with Faster-Whisper.

        Cache lookup order: in-memory cache -> persistent database cache
        -> actual yt-dlp/FFmpeg/Whisper transcription. A successful real
        transcript is written to both the in-memory cache and the
        database (data/database.py's video_transcript_cache table) so it
        survives a backend restart; failed or empty transcripts are never
        cached.

        FIX: this is a long-running, blocking call, so callers must run it
        via run_in_threadpool (see routers/transcription.py) rather than
        awaiting it directly on the event loop. If a job for this exact
        video_url is already in progress (e.g. the full-transcript request
        and the live-segment poll both fired once playback started), a
        second caller waits on the same per-video lock and then reuses the
        result from cache instead of starting a second yt-dlp/FFmpeg/
        Whisper run.
        """

        if video_url in self._video_transcripts:
            logger.info(f"Using cached transcript from memory for video: {video_url}")
            return self._video_transcripts[video_url]

        lock = self._get_lock_for_video(video_url)
        with lock:
            # Re-check memory cache: another thread may have just finished
            # this exact job (memory + database) while we were waiting for
            # the lock above.
            if video_url in self._video_transcripts:
                logger.info(f"Using cached transcript from memory for video (job completed while waiting): {video_url}")
                return self._video_transcripts[video_url]

            # Database cache: survives backend restarts. Only checked once
            # per video per process, since a hit is loaded straight into
            # the in-memory cache above for every call after this one.
            db_segments = self._load_from_database_cache(video_url)
            if db_segments:
                logger.info(f"Using cached transcript from database for video: {video_url}")
                self._video_transcripts[video_url] = db_segments
                return db_segments

            if not WHISPER_AVAILABLE or self.model is None:
                logger.error("Whisper unavailable; refusing to fabricate transcript text")
                raise TranscriptionError("Transcription service is unavailable")

            audio_path = None

            try:
                with tempfile.TemporaryDirectory() as temp_dir:

                    output_template = os.path.join(
                        temp_dir,
                        "audio.%(ext)s",
                    )

                    local_source = self._resolve_local_source(video_url)
                    if local_source:
                        # V2 (Phase 5): video already lives on this
                        # server's filesystem (Nginx-served) — read it
                        # directly, no download needed at all.
                        logger.info(f"Using local video file directly (no yt-dlp download): {local_source}")
                        source_path = local_source
                    else:
                        logger.info(f"Downloading video audio with yt-dlp: {video_url}")

                        subprocess.run(
                            [
                                "yt-dlp",
                                 "--js-runtimes", "deno",
                                "-f",
                                "140/bestaudio[ext=m4a]/bestaudio",
                                "--no-playlist",
                                "-o",
                                output_template,
                                video_url,
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        )

                        # Locate downloaded media
                        downloaded_files = [
                            os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
                        ]

                        if not downloaded_files:
                            raise RuntimeError("yt-dlp did not produce an audio file")

                        source_path = downloaded_files[0]

                    # Convert to WAV/PCM using FFmpeg
                    audio_path = os.path.join(
                        temp_dir,
                        "audio.wav",
                    )

                    logger.info("Converting downloaded audio to WAV with FFmpeg")

                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            source_path,
                            "-vn",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            "-sample_fmt",
                            "s16",
                            audio_path,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                    logger.info("Running Faster-Whisper transcription")

                    segments_generator, info = self.model.transcribe(
                        audio_path,
                        word_timestamps=True,
                        language="en",
                    )

                    segments = []

                    for seg in segments_generator:

                        words = []

                        if seg.words:
                            for word in seg.words:
                                words.append(
                                    {
                                        "word": word.word.strip(),
                                        "start": round(word.start, 2),
                                        "end": round(word.end, 2),
                                        "confidence": round(
                                            word.probability or 0.0,
                                            3,
                                        ),
                                    }
                                )

                        segments.append(
                            {
                                "id": f"t_{uuid.uuid4().hex[:8]}",
                                "text": seg.text.strip(),
                                "timestamp": self._format_timestamp(seg.start),
                                "start_time": round(seg.start, 2),
                                "end_time": round(seg.end, 2),
                                "confidence": round(
                                    getattr(
                                        info,
                                        "language_probability",
                                        0.0,
                                    ),
                                    3,
                                ),
                                "model_response": {
                                    "language": getattr(
                                        info,
                                        "language",
                                        "en",
                                    ),
                                    "words": words,
                                },
                            }
                        )

                    segments = [segment for segment in segments if segment["text"].strip()]
                    if not segments:
                        raise TranscriptionError("Transcription produced no usable speech")

                    self._video_transcripts[video_url] = segments

                    logger.success(
                        f"Transcription complete: " f"{len(segments)} real segments"
                    )

                    # Persist the successful real transcript so it survives
                    # a backend restart. This only runs after `segments` is
                    # built successfully above — failed transcripts never
                    # reach this line, so they cannot poison the database cache.
                    self._save_to_database_cache(video_url, segments)

                    return segments

            except subprocess.CalledProcessError as e:
                logger.error(f"Media processing failed: {e.stderr}")
                raise TranscriptionError("Unable to extract audio from video") from e

            except Exception as e:
                logger.error(f"Video transcription failed: {e}")
                if isinstance(e, TranscriptionError):
                    raise
                raise TranscriptionError("Video transcription failed") from e

    # ============================================================
    # AUDIO CHUNK TRANSCRIPTION
    # ============================================================

    def transcribe_audio_chunk(
        self,
        audio_base64: str,
    ) -> list[dict]:
        """
        Transcribe a base64-encoded audio chunk with Faster-Whisper.
        """

        if not WHISPER_AVAILABLE or self.model is None:
            logger.error("Whisper unavailable; refusing to fabricate audio transcript text")
            raise TranscriptionError("Transcription service is unavailable")

        temp_path = None

        try:
            if "," in audio_base64:
                audio_base64 = audio_base64.split(",", 1)[1]

            audio_bytes = base64.b64decode(audio_base64)

            with tempfile.NamedTemporaryFile(
                suffix=".wav",
                delete=False,
            ) as f:
                f.write(audio_bytes)
                temp_path = f.name

            segments_generator, info = self.model.transcribe(
                temp_path,
                word_timestamps=True,
                language="en",
            )

            segments = []

            for seg in segments_generator:

                words = []

                if seg.words:
                    for word in seg.words:
                        words.append(
                            {
                                "word": word.word.strip(),
                                "start": round(word.start, 2),
                                "end": round(word.end, 2),
                                "confidence": round(
                                    word.probability or 0.0,
                                    3,
                                ),
                            }
                        )

                segments.append(
                    {
                        "id": f"t_{uuid.uuid4().hex[:8]}",
                        "text": seg.text.strip(),
                        "timestamp": self._format_timestamp(seg.start),
                        "start_time": round(seg.start, 2),
                        "end_time": round(seg.end, 2),
                        "confidence": round(
                            getattr(
                                info,
                                "language_probability",
                                0.0,
                            ),
                            3,
                        ),
                        "model_response": {
                            "language": getattr(
                                info,
                                "language",
                                "en",
                            ),
                            "words": words,
                        },
                    }
                )

            segments = [segment for segment in segments if segment["text"].strip()]
            if not segments:
                raise TranscriptionError("Transcription produced no usable speech")
            return segments

        except Exception as e:
            logger.error(f"Audio chunk transcription failed: {e}")
            if isinstance(e, TranscriptionError):
                raise
            raise TranscriptionError("Audio chunk transcription failed") from e

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # ============================================================
    # LIVE TIMESTAMP LOOKUP
    # ============================================================

    def get_segment_at_time_cached_only(
        self,
        current_time: float,
        video_url: str,
    ) -> tuple[bool, Optional[dict]]:
        """
        FIX (repeated /live polling): cache-only counterpart to
        get_segment_at_time() below — same segment-matching logic, but
        never calls transcribe_video_url() at all, so it can never trigger
        (or wait on the lock for) an actual transcription job. Safe to
        call directly from the event loop (no thread offload needed).

        Returns (cache_hit, segment). cache_hit=False means this video
        hasn't been transcribed yet in this process — the caller should
        fall back to the full (thread-offloaded) path in that case, which
        both transcribes it and returns the matching segment. cache_hit=
        True with segment=None means the transcript is cached but no
        segment covers this exact timestamp (e.g. a silent gap) — that is
        a normal, complete answer, not a miss.
        """
        segments = self.get_cached_transcript_only(video_url)
        if segments is None:
            return False, None
        for seg in segments:
            if seg["start_time"] <= current_time < seg["end_time"]:
                return True, seg
        return True, None

    def get_segment_at_time(
    self,
    current_time: float,
    video_url: Optional[str] = None,
) -> Optional[dict]:
       """
       Return the real Whisper transcript segment active at
       the requested video timestamp.

       No video URL means there is no transcript source.
       Never return dummy/fabricated transcript data.
      """

       if not video_url:
         return None

       segments = self.transcribe_video_url(video_url)

       for seg in segments:
         if seg["start_time"] <= current_time < seg["end_time"]:
            return seg

       return None

    # ============================================================
    # FULL TRANSCRIPT
    # ============================================================

    def get_full_transcript(
        self,
        video_id: str,
        video_url: Optional[str] = None,
    ) -> list[dict]:
        """
        Return the complete transcript.

        No video URL means there is no transcript source.
        Never return dummy/fabricated transcript data.
        """

        if not video_url:
            raise TranscriptionError("A video URL is required")
        return self.transcribe_video_url(video_url)

    # ============================================================
    # HELPERS
    # ============================================================

    def _format_timestamp(
        self,
        seconds: float,
    ) -> str:
        minutes = int(seconds) // 60
        secs = int(seconds) % 60
        return f"{minutes:02d}:{secs:02d}"

    def _generate_word_timestamps(
        self,
        text: str,
        start: float,
        end: float,
    ) -> list[dict]:

        words = text.split()

        if not words:
            return []

        duration = end - start
        word_duration = duration / len(words)

        result = []

        for i, word in enumerate(words):
            w_start = start + i * word_duration
            w_end = w_start + word_duration * 0.9

            result.append(
                {
                    "word": word,
                    "start": round(w_start, 2),
                    "end": round(w_end, 2),
                    "confidence": 0.85,
                }
            )

        return result


# ================================================================
# SINGLETON
# ================================================================

WHISPER_MODEL_SIZE = os.getenv(
    "WHISPER_MODEL_SIZE",
    "base",
)

transcription_service = TranscriptionService(model_size=WHISPER_MODEL_SIZE)
