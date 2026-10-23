// ============================================================
// API SERVICE LAYER — FastAPI Backend (+ dev-only dummy fallback)
//
// USAGE:
//   1. Set NEXT_PUBLIC_API_URL=http://localhost:8000/api in .env.local
//   2. Start FastAPI backend: cd backend && python main.py
//   3. Course catalog (fetchCourses/fetchCourseById/fetchVideoById) is
//      ALWAYS backend-only — Postgres -> FastAPI -> Frontend, no dummy
//      fallback and no localStorage merge, in every mode. Old/demo
//      courses must never appear, per the research app requirements.
//   4. Everything else (profile, XP, leaderboard, daily challenges,
//      notifications, transcript) tries the backend first, and only
//      falls back to local dummy fixtures on failure — UNLESS
//      NEXT_PUBLIC_API_STRICT=true, in which case those also surface a
//      real error instead of silently showing dummy data. Set this for
//      the production/research deployment.
// ============================================================

import {
  DUMMY_STUDENT,
  DUMMY_LEADERBOARD,
  DUMMY_DAILY_CHALLENGES,
  DUMMY_NOTIFICATIONS,
  DUMMY_TRANSCRIPT_SEGMENTS,
  QUESTION_BANK,
  generateAttentionSnapshot,
  type StudentProfile,
  type Course,
  type VideoLink,
  type AttentionSnapshot,
  type TranscriptSegment,
  type AssessmentQuestion,
  type LeaderboardEntry,
  type DailyChallenge,
  type Notification,
} from "./dummyDb"

// ── Config ──
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api"
const STRICT_MODE = process.env.NEXT_PUBLIC_API_STRICT === "true"

// FIX (auth request): every request now carries the logged-in student's
// JWT, read lazily here to avoid a circular import at module-load time
// (lib/auth.ts doesn't import from lib/api.ts, but importing getToken
// directly up top is still safe — kept as a function call for clarity
// about where the token actually comes from).
import { getToken, getCachedUserId, tryRefreshAccessToken } from "./auth"

export interface DiscoveredVideo {
  id: string
  title: string
  url: string
  duration: number
  thumbnail: string
  channel: string
  assessmentAvailable: boolean
  transcriptionAvailable: boolean
  // FIX (course generator request): which curriculum stage this video
  // covers (Fundamentals/Core Concepts/Intermediate/Advanced/Applied
  // Project) — the generator used to return several videos that all
  // covered the same beginner ground; this field is what lets the UI
  // show (and lets you verify) that a course now actually has range.
  stageLabel?: string
}

export interface AutoCourse {
  courseId: string
  courseTitle: string
  topic: string
  description: string
  icon: string
  category: string
  difficulty: string
  tags: string[]
  videos: DiscoveredVideo[]
  totalFound: number
  generatedAt: number
  elapsedSeconds: number
  status: "success" | "partial" | "failed"
}

export interface DiscoverRequest {
  topic: string
  maxVideos?: number
  autoTranscribe?: boolean
}

export interface FullPipelineRequest {
  topic: string
  maxVideos?: number
  studentId?: string
  attentionScore?: number
}

export interface FullPipelineResponse {
  courseId: string
  courseTitle: string
  status: string
  message: string
  videosQueued: number
  assessmentSessions: object[]
}

export interface SaveAutoCourseResponse {
  saved: boolean
  courseId: string
  title: string
  message: string
}

export interface StudySession {
  studySessionId: string
  participantId: string
  condition: "MCRF" | "LEGACY" | "MIXED"
  sequenceOrder: "MCRF_THEN_LEGACY" | "LEGACY_THEN_MCRF"
  courseId?: string | null
  moduleId?: string | null
  videoId?: string | null
  completionStatus: string
  experimentVersion: string
  applicationVersion?: string | null
}

export interface QuestionResponseEvent {
  questionId: string
  questionIndex: number
  presentedAt: string
  submittedAt?: string
  responseTimeSeconds?: number
  status: "submitted" | "unanswered" | "timeout" | "refresh" | "retry"
}

// ── Helpers ──
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))


/**
 * Recursively convert snake_case keys to camelCase.
 * Handles the mismatch between FastAPI (snake_case) and frontend (camelCase).
 */
function snakeToCamel(str: string): string {
  return str.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
}

function normalizeKeys(obj: any): any {
  if (Array.isArray(obj)) return obj.map(normalizeKeys)
  if (obj !== null && typeof obj === "object") {
    return Object.keys(obj).reduce((acc: any, key) => {
      acc[snakeToCamel(key)] = normalizeKeys(obj[key])
      return acc
    }, {})
  }
  return obj
}

async function apiFetch<T>(
  path: string,
  options?: RequestInit,
  fallback?: () => T | Promise<T>
): Promise<T> {
  try {
    const doRequest = async () => {
      const token = getToken()
      return fetch(`${API_BASE}${path}`, {
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...options?.headers,
        },
        ...options,
      })
    }

    let res = await doRequest()

    // FIX (remaining-things request): access tokens now expire in 30
    // minutes (was a stateless 10h token with no refresh mechanism at
    // all). A 401 here first tries one silent refresh — this is what
    // stops "session expired" from happening every 30 minutes during
    // normal use instead of only at the very end of a long refresh-token
    // lifetime (30 days).
    if (res.status === 401) {
      const refreshed = await tryRefreshAccessToken()
      if (refreshed) {
        res = await doRequest()
      }
    }

    if (res.status === 401) {
      const { AuthError } = await import("./auth")
      throw new AuthError()
    }
    if (!res.ok) {
      let detail = res.statusText
      try {
        const errorBody = await res.json()
        detail = errorBody?.detail || errorBody?.message || detail
      } catch {}
      throw new Error(`API ${res.status}: ${detail}`)
    }
    const data = await res.json()
    // Normalize snake_case keys from backend to camelCase for frontend
    return normalizeKeys(data) as T
  } catch (err) {
    if (err instanceof Error && err.name === "AuthError") throw err

    const errMsg = err instanceof Error ? err.message : String(err)
    const isNetworkIssue = /Failed to fetch|NetworkError|Load failed/i.test(errMsg)

    if (isNetworkIssue && !fallback) {
      throw new Error(
        `Cannot reach backend at ${API_BASE}. Check NEXT_PUBLIC_API_URL and backend CORS settings.`
      )
    }

    if (STRICT_MODE) throw err
    console.warn(`[API] ${path} failed, using fallback:`, (err as Error).message)
    if (fallback) return await fallback()
    throw err
  }
}

/** POST /api/content/discover */
export async function discoverCourseContent(
  request: DiscoverRequest
): Promise<AutoCourse> {
  return apiFetch<AutoCourse>("/content/discover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      topic: request.topic,
      max_videos: request.maxVideos ?? 5,
      auto_transcribe: request.autoTranscribe ?? false,
    }),
  })
}

/** POST /api/content/pipeline/full */
export async function runFullCoursePipeline(
  request: FullPipelineRequest
): Promise<FullPipelineResponse> {
  return apiFetch<FullPipelineResponse>("/content/pipeline/full", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      topic: request.topic,
      max_videos: request.maxVideos ?? 3,
      student_id: request.studentId ?? "student_001",
      attention_score: request.attentionScore ?? 75,
    }),
  })
}

/** POST /api/content/courses/auto/:id/save */
export async function saveAutoCourseToDashboard(
  courseId: string
): Promise<SaveAutoCourseResponse> {
  return apiFetch<SaveAutoCourseResponse>(`/content/courses/auto/${courseId}/save`, {
    method: "POST",
  })
}

// ============================================================
// STUDENT
// ============================================================

/** GET /api/student/profile */
export async function fetchStudentProfile(): Promise<StudentProfile> {
  return apiFetch<StudentProfile>(
    "/student/profile",
    { method: "GET" },
    async () => {
      await delay(200)
      return JSON.parse(JSON.stringify(DUMMY_STUDENT))
    }
  )
}

/** POST /api/student/xp */
export async function awardXP(
  amount: number,
  studentId: string = getCachedUserId() ?? "student_001"
): Promise<{ newXP: number; newLevel: number; leveledUp: boolean; xpToNextLevel: number }> {
  return apiFetch(
    "/student/xp",
    {
      method: "POST",
      body: JSON.stringify({ student_id: studentId, amount, reason: "assessment" }),
    },
    async () => {
      await delay(100)
      const s = DUMMY_STUDENT
      s.xp += amount
      const leveledUp = s.xp >= s.xpToNextLevel
      if (leveledUp) { s.level++; s.xp -= s.xpToNextLevel; s.xpToNextLevel = Math.floor(s.xpToNextLevel * 1.2) }
      return { newXP: s.xp, newLevel: s.level, leveledUp, xpToNextLevel: s.xpToNextLevel }
    }
  )
}

// ============================================================
// VIDEO PROGRESS (resume support)
// ============================================================

/** POST /api/student/video-progress — no dummy fallback; this is
 *  per-student resume state, not something safe to fake. A failure here
 *  should be silent/best-effort to the caller (see app/video/page.tsx),
 *  never block playback. */
export async function saveVideoProgress(
  videoId: string,
  positionSeconds: number,
  durationSeconds?: number
): Promise<{ videoId: string; positionSeconds: number }> {
  return apiFetch(
    "/student/video-progress",
    {
      method: "POST",
      body: JSON.stringify({
        video_id: videoId,
        position_seconds: positionSeconds,
        duration_seconds: durationSeconds,
      }),
    }
  )
}

// ============================================================
// COURSES
// ============================================================

/** GET /api/courses */
export async function fetchCourses(): Promise<Course[]> {
  return apiFetch<Course[]>(
    "/courses/",
    { method: "GET" }
  )
}

/** GET /api/courses/:id */
export async function fetchCourseById(courseId: string): Promise<Course | null> {
  return apiFetch<Course | null>(
    `/courses/${courseId}`,
    { method: "GET" }
  )
}

/** GET /api/courses/:courseId/videos/:videoId */
export async function fetchVideoById(
  courseId: string,
  videoId: string
): Promise<{ course: Course; video: VideoLink } | null> {
  return apiFetch<{ course: Course; video: VideoLink } | null>(
    `/courses/${courseId}/videos/${videoId}/`,
    { method: "GET" }
  )
}

// ============================================================
// ATTENTION — Camera frame → ML model → score
// ============================================================

/** POST /api/attention/snapshot — Send camera frame, get behavioral-cue score */
export async function sendAttentionFrame(
  frameBase64: string,
  videoId: string,
  sessionId: string,
  studySessionId?: string | null,
  studentId: string = getCachedUserId() ?? "student_001"
): Promise<AttentionSnapshot> {
  return apiFetch<AttentionSnapshot>(
    "/attention/snapshot",
    {
      method: "POST",
      // consent_confirmed must be explicitly true or the backend's
      // consent gate (CR6, peer review packet) 403s — see ConsentModal.tsx,
      // which is what actually gates whether this function should even
      // be called.
      body: JSON.stringify({
        frame_base64: frameBase64,
        video_id: videoId,
        session_id: sessionId,
        study_session_id: studySessionId || undefined,
        student_id: studentId,
        consent_confirmed: true,
      }),
    },
    async () => generateAttentionSnapshot()
  )
}

/** GET /api/attention/dummy-snapshot — No camera needed */
export async function fetchDummyAttention(): Promise<AttentionSnapshot> {
  return apiFetch<AttentionSnapshot>(
    "/attention/dummy-snapshot",
    { method: "GET" },
    async () => generateAttentionSnapshot()
  )
}

/** GET /api/attention/history */
export async function fetchAttentionHistory(
  videoId: string,
  studentId: string = getCachedUserId() ?? "student_001"
): Promise<{ logs: AttentionSnapshot[]; average_score: number }> {
  return apiFetch(
    `/attention/history?video_id=${videoId}&student_id=${studentId}`,
    { method: "GET" },
    async () => ({ logs: [], average_score: 0 })
  )
}

// ============================================================
// TRANSCRIPTION — Whisper model
// ============================================================

/** GET /api/transcription/:videoId — Full transcript */
export async function fetchTranscription(videoId: string): Promise<TranscriptSegment[]> {
  return apiFetch<TranscriptSegment[]>(
    `/transcription/${videoId}`,
    { method: "GET" },
    async () => {
      await delay(200)
      return JSON.parse(JSON.stringify(DUMMY_TRANSCRIPT_SEGMENTS))
    }
  )
}

/** GET /api/transcription/:videoId/live?current_time=X&video_url=Y — Segment at timestamp */
export async function fetchLiveTranscriptSegment(
  videoId: string,
  currentTime: number,
  videoUrl: string
): Promise<TranscriptSegment | null> {
  return apiFetch<TranscriptSegment | null>(
    `/transcription/${videoId}/live?current_time=${currentTime}&video_url=${encodeURIComponent(videoUrl)}`,
    { method: "GET" },
    async () => {
      const seg = DUMMY_TRANSCRIPT_SEGMENTS.find(
        (t) => currentTime >= t.startTime && currentTime < t.endTime
      )
      return seg ? JSON.parse(JSON.stringify(seg)) : null
    }
  )
}

/** POST /api/transcription/chunk — Transcribe audio chunk */
export async function transcribeAudioChunk(
  videoId: string,
  audioBase64: string
): Promise<{ segments: TranscriptSegment[] }> {
  return apiFetch(
    "/transcription/chunk",
    {
      method: "POST",
      body: JSON.stringify({ video_id: videoId, audio_chunk_base64: audioBase64 }),
    },
    async () => ({ segments: JSON.parse(JSON.stringify(DUMMY_TRANSCRIPT_SEGMENTS)) })
  )
}

// ============================================================
// ASSESSMENT — Adaptive quiz generation + submission
// ============================================================

export interface StudyConsentStatus {
  studentId: string
  granted: boolean
  grantedAt?: string | null
  version: string
}

/**
 * FIX (A-2 follow-up): the backend now refuses to create any research
 * record (study session, assessment response, etc.) until this
 * study-participation consent — separate from webcam consent — is on
 * file. Check status before calling startStudySession.
 */
export async function getStudyConsent(): Promise<StudyConsentStatus> {
  const data = await apiFetch<any>("/research/consent")
  return {
    studentId: data.student_id,
    granted: data.granted,
    grantedAt: data.granted_at,
    version: data.version,
  }
}

export async function setStudyConsent(granted: boolean, version = "1.0"): Promise<StudyConsentStatus> {
  const data = await apiFetch<any>("/research/consent", {
    method: "POST",
    body: JSON.stringify({ granted, version }),
  })
  return {
    studentId: data.student_id,
    granted: data.granted,
    grantedAt: data.granted_at,
    version: data.version,
  }
}

export async function startStudySession(
  courseId?: string,
  videoId?: string,
  moduleId?: string
): Promise<StudySession> {
  return apiFetch<StudySession>("/research/study-sessions", {
    method: "POST",
    body: JSON.stringify({
      course_id: courseId,
      video_id: videoId,
      module_id: moduleId || courseId,
    }),
  })
}

/** Persist a video that reached its completion event in this study session. */
export async function completeStudyVideo(
  studySessionId: string,
  videoId: string,
  transcriptText: string = ""
): Promise<{ studySessionId: string; videoId: string; completionOrder: number; completedAt: string }> {
  return apiFetch(
    `/research/study-sessions/${encodeURIComponent(studySessionId)}/videos/${encodeURIComponent(videoId)}/complete`,
    {
      method: "POST",
      body: JSON.stringify({ transcript_text: transcriptText }),
    }
  )
}
export async function getActiveStudySession(): Promise<StudySession> {
  return apiFetch<StudySession>("/research/study-sessions/active")
}

export interface AssessmentSession {
  id: string
  studySessionId?: string
  participantId?: string
  condition?: "MCRF" | "LEGACY" | "MIXED"
  courseId: string
  videoId: string
  contributingVideoIds?: string[]
  questions: AssessmentQuestion[]
  difficulty: "easy" | "medium" | "hard"
  timeLimit: number
  attentionScoreDuringVideo: number | null
  adaptiveMetadata: {
    previousScore: number | null
    adjustedDifficulty: string
    reason: string
  }
}

export interface CrsComponents {
  performance: number
  behavioralCue: number
  integrity: number
  trend: number
  complexity: number
}

export interface CrsBlock {
  score: number
  scorePct: number
  components: CrsComponents
  weightsUsed: Record<string, number>
  explanation: string
}

export interface CrsHistoryEntry {
  timestamp: number
  assessmentId: string | null
  performance: number
  behavioralCue: number
  integrity: number
  trend: number
  complexity: number
  crs: number
  difficulty: "easy" | "medium" | "hard"
  explanation: string
}

export interface AssessmentResult {
  sessionId: string
  score: number
  totalPoints: number
  earnedPoints: number
  percentage: number
  xpEarned: number
  // FIX (real XP request): reflects the *actual* persisted XP/level after
  // this submission (see backend routers/assessment.py's _apply_xp()) —
  // previously xpEarned was computed but never written to the student's
  // record, so there was nothing truthful to show here. Optional so
  // older cached results / the dummy fallback path don't break existing UI.
  totalXp?: number
  newLevel?: number
  leveledUp?: boolean
  timeSpent: number
  correctAnswers: number
  totalQuestions: number
  // FIX (perf/timeout request, item 3): present when this result came from
  // the timeout auto-submit path rather than a full 10/10 finish.
  timedOut?: boolean
  answeredCount?: number
  difficulty: string
  message: string
  nextDifficulty: "easy" | "medium" | "hard"
  suggestedTopics: string[]
  adaptiveResponse: {
    performanceTrend: "improving" | "stable" | "declining"
    recommendedAction: string
    nextAssessmentDifficulty: "easy" | "medium" | "hard"
    strengthAreas: string[]
    weakAreas: string[]
    // Phase 11/13 addition (NeuroLearn-MCL): present once the backend has
    // CRS_CONFIG.crs_enabled=True (the default). Optional so older cached
    // results / the legacy-engine fallback path don't break existing UI.
    crs?: CrsBlock
  }
}

/**
 * POST /api/assessment/generate
 * Sends behavioral-cue score + transcript to backend.
 * Backend adaptive engine picks difficulty, FLAN-T5 generates questions.
 */
export async function generateAssessment(
  courseId: string,
  videoId: string,
  attentionScore: number,
  previousScore: number | null,
  transcriptText: string = "",
  studySessionId?: string | null,
  contributingVideoIds: string[] = []
): Promise<AssessmentSession> {
  return apiFetch<AssessmentSession>(
    "/assessment/generate",
    {
      method: "POST",
      body: JSON.stringify({
        course_id: courseId,
        video_id: videoId,
        student_id: getCachedUserId() ?? "student_001",
        study_session_id: studySessionId || undefined,
        attention_score: attentionScore,
        previous_score: previousScore,
        transcript_text: transcriptText,
        contributing_video_ids: contributingVideoIds,
      }),
    }
  )
}

export async function submitAdaptiveAnswer(
  sessionId: string,
  questionId: string,
  answer: string | number,
  responseEvent: QuestionResponseEvent
): Promise<{
  completed: boolean
  session: AssessmentSession
  result?: AssessmentResult | null
  // FIX (perf/timeout request, item 1): true when the next question's
  // FLAN-T5 generation was handed off to a background task instead of
  // blocking this response — the caller should poll getAssessmentSession
  // until session.questions grows before advancing.
  nextQuestionPending?: boolean
  adaptiveResponse?: {
    nextAssessmentDifficulty: "easy" | "medium" | "hard"
    crs?: CrsBlock
  }
  duplicate?: boolean
}> {
  return apiFetch("/assessment/answer", {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      student_id: getCachedUserId() ?? "student_001",
      question_id: questionId,
      answer,
      response_event: {
        question_id: responseEvent.questionId,
        question_index: responseEvent.questionIndex,
        presented_at: responseEvent.presentedAt,
        submitted_at: responseEvent.submittedAt,
        response_time_seconds: responseEvent.responseTimeSeconds,
        status: responseEvent.status,
      },
    }),
  })
}

/**
 * GET /api/assessment/session/{id}
 * FIX (perf/timeout request, item 1): used to poll for the next question
 * once submitAdaptiveAnswer reports nextQuestionPending, since generation
 * now finishes in the backend after the /answer response has already
 * returned.
 */
export async function getAssessmentSession(sessionId: string): Promise<AssessmentSession> {
  return apiFetch<AssessmentSession>(`/assessment/session/${sessionId}`)
}

/**
 * POST /api/assessment/submit
 * FIX (perf/timeout request, item 3): now used specifically as the timeout
 * auto-submit path — finalizes with whatever was already graded via
 * submitAdaptiveAnswer when the assessment clock runs out. It does not
 * grade `answers` itself; the per-question ledger from /answer is the
 * source of truth.
 */
export async function submitAssessment(
  sessionId: string,
  answers: Record<string, string | number>,
  questions: AssessmentQuestion[],
  timeSpent: number,
  responseEvents: QuestionResponseEvent[] = []
): Promise<AssessmentResult> {
  return apiFetch<AssessmentResult>(
    "/assessment/submit",
    {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        student_id: getCachedUserId() ?? "student_001",
        answers,
        time_spent: timeSpent,
        response_events: responseEvents.map((event) => ({
          question_id: event.questionId,
          question_index: event.questionIndex,
          presented_at: event.presentedAt,
          submitted_at: event.submittedAt,
          response_time_seconds: event.responseTimeSeconds,
          status: event.status,
        })),
      }),
    },
    async () => {
      // Local fallback grading
      await delay(400)
      let correct = 0
      let earned = 0
      const total = questions.reduce((s, q) => s + q.points, 0)

      questions.forEach((q) => {
        if (answers[q.id] === q.correctAnswer) { correct++; earned += q.points }
      })

      const pct = Math.round((correct / questions.length) * 100)
      const xp = Math.floor(earned * 1.5)

      let nextDiff: "easy" | "medium" | "hard" = "medium"
      let trend: "improving" | "stable" | "declining" = "stable"
      if (pct >= 80) { nextDiff = "hard"; trend = "improving" }
      else if (pct < 50) { nextDiff = "easy"; trend = "declining" }

      const msg = pct >= 90 ? "Outstanding! You've mastered this." : pct >= 70 ? "Well done! Solid understanding." : pct >= 50 ? "Decent effort. Review weak areas." : "Keep practicing! Rewatch the video."

      // Dummy CRS (NeuroLearn-MCL): mirrors the backend's CRS shape so
      // CRSPanel/CRSTrendWidget render identically whether the backend is
      // reachable or not — same dual-fallback philosophy as every other
      // function in this file, not a separate code path to maintain.
      const timeRatio = timeSpent / 420 // matches the medium-difficulty default in generateAssessment's fallback; submitAssessment isn't passed the actual session time_limit
      const integrityDummy = timeRatio < 0.15 || timeRatio > 1 ? 0.15 : timeRatio < 0.45 || timeRatio > 0.85 ? 0.6 : 1.0
      const crsComponents: CrsComponents = {
        performance: pct / 100,
        behavioralCue: 0.7,
        integrity: integrityDummy,
        trend: trend === "improving" ? 0.75 : trend === "declining" ? 0.25 : 0.5,
        complexity: 0.5,
      }
      const crsScore =
        0.2 * crsComponents.performance +
        0.2 * crsComponents.behavioralCue +
        0.2 * crsComponents.integrity +
        0.2 * crsComponents.trend +
        0.2 * crsComponents.complexity

      return {
        sessionId,
        score: pct,
        totalPoints: total,
        earnedPoints: earned,
        percentage: pct,
        xpEarned: xp,
        timeSpent,
        correctAnswers: correct,
        totalQuestions: questions.length,
        difficulty: questions[0]?.difficulty || "medium",
        message: msg,
        nextDifficulty: nextDiff,
        suggestedTopics: pct >= 70 ? ["Next: Advanced Topics"] : ["Review: Rewatch Video", "Practice: Easier Questions"],
        adaptiveResponse: {
          performanceTrend: trend,
          recommendedAction: pct >= 80 ? "Moving to harder content." : pct >= 50 ? "Keep practicing at this level." : "Review material and try again.",
          nextAssessmentDifficulty: nextDiff,
          strengthAreas: correct >= 3 ? ["Core Concepts", "Syntax"] : ["Basic Recognition"],
          weakAreas: correct < 3 ? ["Applied Knowledge", "Deep Understanding"] : [],
          crs: {
            score: crsScore,
            scorePct: Math.round(crsScore * 1000) / 10,
            components: crsComponents,
            weightsUsed: { alpha: 0.2, beta: 0.2, gamma: 0.2, delta: 0.2, epsilon: 0.2 },
            explanation: "Offline estimate — backend unreachable, showing locally-computed CRS.",
          },
        },
      }
    }
  )
}

// ============================================================
// COGNITIVE READINESS SCORE (CRS) — NeuroLearn-MCL, Phase 13
// ============================================================

function dummyCrsHistory(studentId: string): CrsHistoryEntry[] {
  // Plausible, clearly-offline-looking demo history — same purpose as the
  // other DUMMY_* fixtures in dummyDb.ts: lets every CRS-aware component
  // render meaningfully with no backend running at all.
  const now = Date.now() / 1000
  const base = [0.42, 0.51, 0.58, 0.63, 0.7]
  return base.map((crs, i) => ({
    timestamp: now - (base.length - i) * 86400,
    assessmentId: `demo_session_${i}`,
    performance: Math.min(1, crs + 0.05),
    behavioralCue: 0.7,
    integrity: i === 1 ? 0.4 : 0.9, // one dip, so the trend widget shows it's a real signal, not a flat line
    trend: crs,
    complexity: 0.5,
    crs,
    difficulty: crs > 0.75 ? "hard" : crs >= 0.45 ? "medium" : "easy",
    explanation: "Offline demo history — backend unreachable.",
  }))
}

/** GET /api/crs/{student_id} */
export async function fetchCurrentCrs(
  studentId: string = getCachedUserId() ?? "student_001"
): Promise<CrsHistoryEntry | null> {
  return apiFetch<CrsHistoryEntry | null>(
    `/crs/${studentId}`,
    { method: "GET" },
    async () => {
      await delay(150)
      const history = dummyCrsHistory(studentId)
      return history[history.length - 1] ?? null
    }
  )
}

/** GET /api/crs/{student_id}/history */
export async function fetchCrsHistory(
  studentId: string = getCachedUserId() ?? "student_001",
  limit?: number
): Promise<CrsHistoryEntry[]> {
  const query = limit ? `?limit=${limit}` : ""
  return apiFetch<{ history: CrsHistoryEntry[] }>(
    `/crs/${studentId}/history${query}`,
    { method: "GET" },
    async () => {
      await delay(200)
      return { history: dummyCrsHistory(studentId) } as any
    }
  ).then((res: any) => res.history ?? res)
}

// ============================================================
// GAMIFICATION — Leaderboard, Challenges, Notifications
// ============================================================

/** GET /api/leaderboard */
export async function fetchLeaderboard(): Promise<LeaderboardEntry[]> {
  return apiFetch<LeaderboardEntry[]>(
    "/leaderboard",
    { method: "GET" },
    async () => { await delay(250); return JSON.parse(JSON.stringify(DUMMY_LEADERBOARD)) }
  )
}

/** GET /api/challenges/daily */
export async function fetchDailyChallenges(): Promise<DailyChallenge[]> {
  return apiFetch<DailyChallenge[]>(
    "/challenges/daily",
    { method: "GET" },
    async () => { await delay(150); return JSON.parse(JSON.stringify(DUMMY_DAILY_CHALLENGES)) }
  )
}

/** GET /api/notifications */
export async function fetchNotifications(): Promise<Notification[]> {
  return apiFetch<Notification[]>(
    "/notifications",
    { method: "GET" },
    async () => { await delay(100); return JSON.parse(JSON.stringify(DUMMY_NOTIFICATIONS)) }
  )
}

// ============================================================
// REPORT CARD
// ============================================================

export interface ReportPayload {
  student: { name: string; email: string; id: string; level: number; xp: number }
  course: { title: string; id: string }
  video: { title: string; id: string; duration: number }
  assessment: AssessmentResult
  behavioral_cue: {
    avgScore: number
    scoreHistory: number[]
    totalSnapshots: number
    attentivePercent: number
    inattentivePercent: number
    unfocusedPercent: number
    avgEyeContact: number
    avgBlinkRate: number
    headPoseDistribution?: Record<string, number>
  }
  transcription: { totalSegments: number; avgConfidence: number }
}

/**
 * POST /api/report/generate
 * Returns the PDF as a Blob for browser download.
 */
export async function generateReportPdf(payload: ReportPayload): Promise<Blob> {
  const res = await fetch(`${API_BASE}/report/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(`Report generation failed: ${res.status}`)
  return res.blob()
}

/**
 * POST /api/report/email
 * Generates PDF and emails it to the given address.
 */
export async function emailReport(
  toEmail: string,
  payload: ReportPayload
): Promise<{ success: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/report/email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ toEmail, reportData: payload }),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || "Email failed")
  }
  return res.json()
}

/**
 * GET /api/report/email-status
 * Check if server SMTP is configured.
 */
export async function checkEmailStatus(): Promise<{ configured: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/report/email-status`)
  if (!res.ok) return { configured: false, message: "Server unreachable" }
  return res.json()
}

// ============================================================
// RE-EXPORT TYPES
// ============================================================
export type {
  StudentProfile,
  Course,
  VideoLink,
  AttentionSnapshot,
  TranscriptSegment,
  AssessmentQuestion,
  LeaderboardEntry,
  DailyChallenge,
  Notification,
  Badge,
} from "./dummyDb"