import base64
import time
import math
import random
from typing import Optional
from io import BytesIO

import numpy as np
from loguru import logger

# Try importing ML libraries — fall back to dummy mode
try:
    import cv2
    import mediapipe as mp
    ML_AVAILABLE = True
    logger.info("OpenCV + MediaPipe loaded — behavioral_cue model LIVE")
except ImportError:
    ML_AVAILABLE = False
    logger.warning("OpenCV/MediaPipe not installed — using DUMMY behavioral_cue model")


class AttentionDetector:
   
    # MediaPipe Face Mesh landmark indices
    # Left eye
    LEFT_EYE = [362, 385, 387, 263, 373, 380]
    # Right eye
    RIGHT_EYE = [33, 160, 158, 133, 153, 144]
    # Nose tip, chin, left eye corner, right eye corner, left mouth, right mouth
    FACE_POSE_POINTS = [1, 152, 33, 263, 61, 291]

    # Behavioral Cue thresholds
    ATTENTIVE_THRESHOLD = 65
    INATTENTIVE_THRESHOLD = 30

    # FIX (B-6/J-1): these used to make direct psychological-state
    # judgments ("You're fully engaged", "You seem a bit distracted") and
    # give unsolicited advice ("Take a 2-minute break"). The mentor
    # guidelines prohibit learner-facing claims/judgments about attention,
    # cognition, emotion, or readiness, and prohibit psychological advice
    # based on the camera signal. These are neutral, factual statements of
    # the operational behavioural-signal band only — no state claim, no
    # advice, no encouragement/discouragement framing.
    MESSAGES = {
        "attentive": [
            "Camera-derived behavioural signal: high band.",
            "Operational signal for this interval: high.",
        ],
        "inattentive": [
            "Camera-derived behavioural signal: medium band.",
            "Operational signal for this interval: medium.",
        ],
        "unfocused": [
            "Camera-derived behavioural signal: low band.",
            "Operational signal for this interval: low.",
        ],
    }

    def __init__(self):
        self.face_mesh = None
        self.blink_counter = 0
        self.blink_timestamps: list[float] = []
        self.frame_count = 0
        self._attention_started_at: Optional[float] = None
        self._last_analysis_at: Optional[float] = None
        self._eyes_closed_since: Optional[float] = None
        self._smoothed_score: Optional[float] = None
        self._prev_ear = 0.3
        self._blink_threshold = 0.21

        if ML_AVAILABLE:
            try:
                self.face_mesh = mp.solutions.face_mesh.FaceMesh(
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
                logger.info("MediaPipe Face Mesh initialized")
            except Exception as e:
                logger.error(f"Failed to init Face Mesh: {e}")
                self.face_mesh = None

    def decode_frame(self, frame_base64: str) -> Optional[np.ndarray]:
        """Decode base64 string to OpenCV BGR image."""
        try:
            # Remove data URI prefix if present
            if "," in frame_base64:
                frame_base64 = frame_base64.split(",")[1]

            img_bytes = base64.b64decode(frame_base64)
            nparr = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            logger.error(f"Frame decode error: {e}")
            return None

    def _eye_aspect_ratio(self, landmarks, eye_indices, w: int, h: int) -> float:
        """
        Compute Eye Aspect Ratio (EAR).
        EAR drops when eye closes → blink detection.
        """
        pts = []
        for idx in eye_indices:
            lm = landmarks[idx]
            pts.append((lm.x * w, lm.y * h))

        # Vertical distances
        v1 = math.dist(pts[1], pts[5])
        v2 = math.dist(pts[2], pts[4])
        # Horizontal distance
        h1 = math.dist(pts[0], pts[3])

        ear = (v1 + v2) / (2.0 * h1 + 1e-6)
        return ear

    def _compute_gaze_score(self, landmarks, w: int, h: int) -> tuple[Optional[float], bool]:
        """
        Compute eye contact / gaze score (0-1) from iris landmark position.
        Higher = looking at screen.

        Returns (score, estimated):
          - (score, False) — a genuine measurement from this frame's iris
            landmarks.
          - (None, True) — iris landmarks were unavailable this frame. No
            gaze measurement was taken; the caller must not substitute a
            constant and treat it as one (B-3). Callers fall back to the
            head-pose / eye-openness calibration only, and must flag the
            resulting eye_contact value as estimated.
        """
        try:
            # Left iris center (index 468-472 from refine_landmarks)
            left_iris = landmarks[468]
            # Right iris center
            right_iris = landmarks[473]

            # Left eye corners
            left_inner = landmarks[362]
            left_outer = landmarks[263]

            # Right eye corners
            right_inner = landmarks[133]
            right_outer = landmarks[33]

            # Compute horizontal iris position ratio for each eye
            def iris_ratio(iris, inner, outer):
                total = abs(outer.x - inner.x) + 1e-6
                pos = abs(iris.x - inner.x)
                score = 1.0 - abs(0.5 - (pos / total)) * 2  # 1.0 = centered
                return max(0.0, min(1.0, score))

            left_score = iris_ratio(left_iris, left_inner, left_outer)
            right_score = iris_ratio(right_iris, right_inner, right_outer)

            # Also check vertical — looking up/down
            left_eye_top = landmarks[386]
            left_eye_bottom = landmarks[374]
            vert_range = abs(left_eye_top.y - left_eye_bottom.y) + 1e-6
            vert_pos = abs(left_iris.y - left_eye_top.y) / vert_range
            vert_score = 1.0 - abs(0.5 - vert_pos) * 2
            vert_score = max(0.0, min(1.0, vert_score))

            gaze = (left_score + right_score + vert_score) / 3.0
            return max(0.0, min(1.0, gaze)), False

        except (IndexError, AttributeError):
            # Iris landmarks unavailable this frame — no measurement (B-3).
            return None, True

    def _compute_head_pose(self, landmarks, w: int, h: int) -> Optional[str]:
        """
        Estimate head pose from key facial landmarks.
        Returns: "forward", "slightly_away", "away"
        """
        try:
            nose = landmarks[1]
            chin = landmarks[152]
            left_eye = landmarks[33]
            right_eye = landmarks[263]

            # Face center X
            face_center_x = (left_eye.x + right_eye.x) / 2

            # How far nose deviates from center horizontally
            nose_deviation = abs(nose.x - face_center_x)

            # Vertical check — head tilt
            face_height = abs(chin.y - landmarks[10].y)
            nose_relative_y = (nose.y - landmarks[10].y) / (face_height + 1e-6)

            if nose_deviation > 0.08 or nose_relative_y > 0.75 or nose_relative_y < 0.35:
                return "away"
            elif nose_deviation > 0.04 or nose_relative_y > 0.65:
                return "slightly_away"
            else:
                return "forward"

        except (IndexError, AttributeError):
            # No measurement this frame — missing, not a substituted "forward" (13C.2).
            return None

    def _update_blink_rate(self, ear: float) -> float:
        """Track blinks and compute rate (blinks per minute)."""
        now = time.time()

        # Detect blink: EAR drops below threshold then recovers
        if self._prev_ear > self._blink_threshold and ear < self._blink_threshold:
            self.blink_timestamps.append(now)

        self._prev_ear = ear

        # Remove blinks older than 60 seconds
        self.blink_timestamps = [t for t in self.blink_timestamps if now - t < 60]

        return float(len(self.blink_timestamps))  # blinks per minute

    def analyze_frame(self, frame_base64: str) -> dict:
        """
        Main analysis pipeline.
        Input: base64 camera frame
        Output: JSON-serializable behavioral_cue snapshot
        """
        self.frame_count += 1
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        now = time.time()
        if self._last_analysis_at is not None and now - self._last_analysis_at > 10:
            self.blink_timestamps = []
            self._attention_started_at = None
            self._eyes_closed_since = None
            self._smoothed_score = None
        self._last_analysis_at = now

        # ── If ML not available, return dummy ──
        if not ML_AVAILABLE or self.face_mesh is None:
            raise RuntimeError(
        "Behavioral-cue detector unavailable; refusing to generate "
        "a synthetic research measurement."
    )

        # ── Decode frame ──
        frame = self.decode_frame(frame_base64)
        if frame is None:
            return self._no_face_snapshot(timestamp)

        h, w, _ = frame.shape

        # ── Run MediaPipe ──
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(rgb)

        if not results.multi_face_landmarks:
            return self._no_face_snapshot(timestamp)

        landmarks = results.multi_face_landmarks[0].landmark
        if self._attention_started_at is None:
            self._attention_started_at = now

        # ── Compute metrics ──
        # 1. Eye Aspect Ratio (for blink detection)
        left_ear = self._eye_aspect_ratio(landmarks, self.LEFT_EYE, w, h)
        right_ear = self._eye_aspect_ratio(landmarks, self.RIGHT_EYE, w, h)
        avg_ear = (left_ear + right_ear) / 2.0

        # 2. Blink rate
        blink_rate = self._update_blink_rate(avg_ear)

        # 3. Gaze / eye contact score
        gaze_score, gaze_estimated = self._compute_gaze_score(landmarks, w, h)

        # 4. Head pose
        head_pose = self._compute_head_pose(landmarks, w, h)

        # ── Aggregate behavioral_cue score ──
        # Head pose is steadier than iris tracking on low-resolution webcam frames.
        head_score = (
            {"forward": 1.0, "slightly_away": 0.65, "away": 0.2}[head_pose]
            if head_pose is not None
            else None
        )
        eye_open_score = max(
            0.0,
            min(1.0, (avg_ear - self._blink_threshold) / (0.28 - self._blink_threshold)),
        )
        eyes_closed_duration = 0.0
        if eye_open_score <= 0.2:
            if self._eyes_closed_since is None:
                self._eyes_closed_since = now
            eyes_closed_duration = now - self._eyes_closed_since
        else:
            self._eyes_closed_since = None
        calibrated_gaze_score = gaze_score if gaze_score is not None else 0.0
        if eye_open_score > 0.2 and head_pose == "forward":
            calibrated_gaze_score = max(calibrated_gaze_score, 0.65)
        elif eye_open_score > 0.2 and head_pose == "slightly_away":
            calibrated_gaze_score = max(calibrated_gaze_score, 0.35)

        # FIX (B-8): blink_rate is derived from analyze_frame() calls, which
        # run at an effective 0.5 Hz (measured: CameraFeed.tsx captures
        # every 500ms but only transmits the latest frame every 2000ms).
        # A physiological blink lasts ~100-400ms, so a sample every 2s
        # cannot reliably catch it — matches the mentor guidelines'
        # "sparse snapshots cannot establish blink/closure". blink_rate is
        # therefore excluded from the scored fusion below; it is still
        # computed and returned in model_response as a diagnostic value
        # only (see return dict) and must not be treated as a validated
        # behavioral measurement. gaze_weight/head_weight/eye_open_weight
        # are unchanged from their prior values — dividing by total_weight
        # renormalizes the remaining three proportionally.
        gaze_weight = 0.20
        head_weight = 0.45
        eye_open_weight = 0.30
        # 13C.2: if head pose is missing, drop its term and renormalise the
        # remaining weights rather than substituting a default pose.
        if head_score is None:
            head_weight = 0.0
            head_score = 0.0
        total_weight = gaze_weight + head_weight + eye_open_weight

        raw_score = (
            (
                calibrated_gaze_score * gaze_weight
                + head_score * head_weight
                + eye_open_score * eye_open_weight
            )
            / total_weight
        ) * 100
        if self._smoothed_score is None:
            self._smoothed_score = raw_score
        elif eyes_closed_duration >= 2.0 or head_pose == "away":
            self._smoothed_score = raw_score
        else:
            self._smoothed_score = (self._smoothed_score * 0.65) + (raw_score * 0.35)

        score = int(max(0, min(100, self._smoothed_score)))
        if eyes_closed_duration >= 6.0:
            score = min(score, 5)
        elif eyes_closed_duration >= 4.0:
            score = min(score, 15)
        elif eyes_closed_duration >= 2.0:
            score = min(score, 25)
        elif eye_open_score <= 0.2:
            score = min(score, 60)
        elif head_pose == "away":
            score = min(score, 25)
        elif head_pose == "slightly_away":
            score = min(score, 60)

        # ── Classify state ──
        if score >= self.ATTENTIVE_THRESHOLD:
            state = "attentive"
        elif score >= self.INATTENTIVE_THRESHOLD:
            state = "inattentive"
        else:
            state = "unfocused"

        # ── Confidence based on face detection quality ──
        # FIX (B-3): don't feed a None gaze_score into the confidence
        # formula, and reflect the lower certainty when gaze is estimated.
        gaze_for_confidence = gaze_score if gaze_score is not None else 0.0
        confidence = min(1.0, 0.7 + gaze_for_confidence * 0.3)
        if gaze_estimated:
            confidence = min(confidence, 0.7)

        message = random.choice(self.MESSAGES[state])

        return {
            "timestamp": timestamp,
            "score": score,
            "state": state,
            "confidence": round(confidence, 2),
            "message": message,
            "model_response": {
                # FIX (B-3 follow-up): report the actual measurement. When
                # iris landmarks weren't available this frame, eye_contact
                # is genuinely missing — None — not a substituted number.
                # (calibrated_gaze_score, which folds in head-pose/eye-open
                # when gaze is unavailable, is still used internally for
                # raw_score above; it just isn't reported as "eye contact".)
                "eye_contact": round(gaze_score, 3) if gaze_score is not None else None,
                "eye_contact_estimated": gaze_estimated,
                "eye_open": round(eye_open_score, 3),
                "eyes_closed_duration": round(eyes_closed_duration, 1),
                "head_pose": head_pose,
                "head_pose_missing": head_pose is None,
                "face_detected": True,
                # Diagnostic only (FIX B-8) — not used in raw_score above,
                # not a validated blink/closure measurement at 0.5 Hz.
                "blink_rate": round(blink_rate, 1),
            },
            # FIX (MJ4, peer review packet): explicit, reader-visible tag —
            # this snapshot came from the live MediaPipe pipeline.
            "source": "live",
        }

    def _no_face_snapshot(self, timestamp: str) -> dict:
        """Return snapshot when no face is detected."""
        return {
            "timestamp": timestamp,
            "score": 0,
            "state": "unfocused",
            "confidence": 0.3,
            "message": "No face detected. Please ensure your camera can see your face.",
            "model_response": {
                "eye_contact": None,
                "eye_contact_estimated": True,
                "eye_open": 0.0,
                "head_pose": "away",
                "face_detected": False,
                "blink_rate": 0.0,
            },
            # Still a live-pipeline result (MediaPipe ran, just found no face) —
            # distinct from the dummy-mode fallback below.
            "source": "live",
        }

    def _generate_dummy_snapshot(self, timestamp: str) -> dict:
        """Generate realistic dummy data when ML models aren't loaded."""
        states = ["attentive", "inattentive", "unfocused"]
        weights = [0.6, 0.25, 0.15]
        r = random.random()
        if r > sum(weights[:2]):
            state = "unfocused"
        elif r > weights[0]:
            state = "inattentive"
        else:
            state = "attentive"

        ranges = {"attentive": (70, 100), "inattentive": (30, 69), "unfocused": (0, 29)}
        lo, hi = ranges[state]
        score = random.randint(lo, hi)

        head_poses = {"attentive": "forward", "inattentive": "slightly_away", "unfocused": "away"}

        return {
            "timestamp": timestamp,
            "score": score,
            "state": state,
            "confidence": round(random.uniform(0.7, 0.98), 2),
            "message": random.choice(self.MESSAGES[state]),
            "model_response": {
                "eye_contact": round(score / 100 * random.uniform(0.8, 1.0), 3),
                "eye_open": round(random.uniform(0.8, 1.0), 3),
                "head_pose": head_poses[state],
                "face_detected": random.random() > 0.05,
                "blink_rate": round(random.uniform(10, 22), 1),
            },
            # FIX (MJ4, peer review packet): explicit, reader-visible tag —
            # MediaPipe/OpenCV weren't available, this is synthetic dummy
            # output and must not be reported as a live-model result.
            "source": "dummy",
        }

    def cleanup(self):
        """Release resources."""
        if self.face_mesh:
            self.face_mesh.close()


# ── FIX (B-4): per-learner/session isolation ──────────────────────────────
# AttentionDetector holds mutable per-stream temporal state (blink
# timestamps, EMA-smoothed score, "eyes closed since" timer, previous EAR).
# A single shared module-level instance meant two participants/sessions
# whose snapshot requests interleaved would corrupt each other's state —
# e.g. one learner's blinks counted toward another's blink rate, or one
# learner's smoothed score carrying over into another's first frame.
#
# `attention_detector` below remains as a single stateless-use instance
# for the dev-only dummy endpoint and the startup health check (neither
# accumulates or shares temporal state across learners). Real research
# scoring must go through `get_session_detector()`, which hands each
# distinct (participant, session) pair its own isolated instance.
_IDLE_EVICTION_SECONDS = 600  # drop detector state for streams idle 10+ min

_session_detectors: dict[tuple[str, str], "AttentionDetector"] = {}


def get_session_detector(participant_id: str, session_id: str) -> "AttentionDetector":
    """
    Return the AttentionDetector instance isolated to this
    (participant_id, session_id) pair, creating one on first use.

    This is the only entry point research code should use to obtain a
    detector for scoring real frames — never the shared `attention_detector`
    singleton, which is reserved for stateless dev/health-check uses.
    """
    _evict_idle_detectors()
    key = (participant_id, session_id)
    detector = _session_detectors.get(key)
    if detector is None:
        detector = AttentionDetector()
        _session_detectors[key] = detector
    return detector


def _evict_idle_detectors() -> None:
    """Release detectors (and their MediaPipe resources) that have been
    idle past the eviction window, so state never silently leaks into a
    later, unrelated session and resources aren't held forever."""
    now = time.time()
    stale_keys = [
        key
        for key, det in _session_detectors.items()
        if det._last_analysis_at is not None
        and now - det._last_analysis_at > _IDLE_EVICTION_SECONDS
    ]
    for key in stale_keys:
        _session_detectors.pop(key).cleanup()


# ── Singleton instance — dev-only dummy endpoint / health check ──
attention_detector = AttentionDetector()