"use client"

import { useEffect, useState, useCallback, useRef, Suspense } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { motion, AnimatePresence } from "framer-motion"
import {
  ArrowLeft,
  BookOpen,
  ClipboardCheck,
  Sparkles,
  AlertCircle,
} from "lucide-react"

import VideoPlayer, { detectVideoType } from "@/components/VideoPlayer"
import CameraFeed, { type AttentionSnapshotResponse } from "@/components/CameraFeed"
import AttentionPanel from "@/components/AttentionPanel"
import TranscriptionPanel from "@/components/TranscriptionPanel"
import VideoLinkSelector from "@/components/VideoLinkSelector"
import StudyConsentModal from "@/components/StudyConsentModal"
import {
  fetchCourseById,
  fetchCourses,
  completeStudyVideo,
  startStudySession,
  getStudyConsent,
  saveVideoProgress,
  type Course,
  type VideoLink,
  type StudySession,
} from "@/lib/api"
import { getOrCreateWebcamSessionId } from "@/lib/consent"

/**
 * FIX (phase-4 request): resume priority — a video actually in progress
 * beats one never started, which beats falling back to the first video
 * for review once everything is done. `watchedPercent`/`completed` are
 * now real per-student values from the backend (see
 * backend/data/database.py: _course_to_dict), not the old static
 * attach-time placeholders.
 */
function pickResumeVideo(vids: VideoLink[]): VideoLink | undefined {
  const partial = vids.find((v) => !v.completed && v.watchedPercent > 0 && v.watchedPercent < 100)
  if (partial) return partial
  const incomplete = vids.find((v) => !v.completed)
  if (incomplete) return incomplete
  return vids[0] // everything complete — first video, for review
}

/** Seconds into `video` to resume at, derived from its real watchedPercent. */
function resumeSecondsFor(video: VideoLink | null): number {
  if (!video || !video.duration || !video.watchedPercent) return 0
  return (video.watchedPercent / 100) * video.duration
}

export default function VideoPage() {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center h-[70vh]">
        <div className="w-10 h-10 border-3 border-violet-500 border-t-transparent rounded-full animate-spin" />
      </div>
    }>
      <VideoContent />
    </Suspense>
  )
}

function VideoContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const courseIdParam = searchParams.get("course")
  const videoIdParam = searchParams.get("video")
  const studySessionParam = searchParams.get("studySession")

  const [course, setCourse] = useState<Course | null>(null)
  const [selectedVideo, setSelectedVideo] = useState<VideoLink | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isPlaying, setIsPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [videoDuration, setVideoDuration] = useState(0)
  const [videoEnded, setVideoEnded] = useState(false)
  // FIX (A-1 / consent-on-navigation): persisted in sessionStorage via
  // lib/consent.ts so this stays stable across video switches AND across
  // full page navigation (e.g. to /profile or /dashboard and back) within
  // one browser session — see getOrCreateWebcamSessionId() for why.
  const [webcamSessionId] = useState(() => getOrCreateWebcamSessionId())
  const [studySession, setStudySession] = useState<StudySession | null>(null)
  // FIX (A-2 follow-up): the backend requires study-participation consent
  // before it will create a StudySession at all. null = not checked yet,
  // false = checked and not granted (show the modal), true = granted.
  const [studyConsentGranted, setStudyConsentGranted] = useState<boolean | null>(null)
  const [behavioralCueGranted, setBehavioralCueGranted] = useState<boolean | null>(null)
  // FIX (auto-revoke on session completion): flipped true right before
  // navigating to the assessment. CameraFeed watches this prop and
  // auto-revokes camera consent once the study session's instructional
  // segment is over (see lib/consent.ts).
  const [sessionComplete, setSessionComplete] = useState(false)
  const [completedVideoTranscripts, setCompletedVideoTranscripts] = useState<Record<string, string>>({})
  const [isPreparingAssessment, setIsPreparingAssessment] = useState(false)

  const studySessionIdRef = useRef<string | null>(null)
  useEffect(() => {
    studySessionIdRef.current = studySession?.studySessionId ?? null
  }, [studySession?.studySessionId])

  const selectedVideoIdRef = useRef<string | null>(null)
  useEffect(() => {
    selectedVideoIdRef.current = selectedVideo?.id ?? null
  }, [selectedVideo?.id])

  const sessionCreatedForCourseRef = useRef<string | null>(null)

  const [latestAttention, setLatestAttention] = useState<AttentionSnapshotResponse | null>(null)
  const [attentionHistory, setAttentionHistory] = useState<number[]>([])
  const [sessionAvgAttention, setSessionAvgAttention] = useState(0)

  // V2: video URLs come only from the admin-managed course catalog
  // (Nginx-served local files) — no ad-hoc/pasted URLs, no YouTube.
  const effectiveUrl = selectedVideo?.url || ""
  const effectiveTitle = selectedVideo?.title || "Select a video"
  const effectiveBehavioralCue = behavioralCueGranted === false || attentionHistory.length === 0
    ? 50
    : sessionAvgAttention

  useEffect(() => {
    async function load() {
      setIsLoading(true)
      try {
        if (courseIdParam) {
          const c = await fetchCourseById(courseIdParam)
          if (c) {
            setCourse(c)
            const vids = c.videoLinks || []
            const target = videoIdParam
              ? vids.find((v) => v.id === videoIdParam)
              : pickResumeVideo(vids)
            if (target) setSelectedVideo(target)
          }
        } else {
          const courses = await fetchCourses()
          if (courses.length > 0) {
            setCourse(courses[0])
            const vids = courses[0].videoLinks || []
            const first = pickResumeVideo(vids)
            if (first) setSelectedVideo(first)
          }
        }
      } catch (err) {
        console.error("Failed to load course:", err)
      } finally {
        setIsLoading(false)
      }
    }
    load()
  }, [courseIdParam, videoIdParam])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const status = await getStudyConsent()
        if (!cancelled) setStudyConsentGranted(status.granted)
      } catch (err) {
        console.error("Failed to check study consent status:", err)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!course || !selectedVideo) return
    if (studySessionParam) {
      setStudySession({
        studySessionId: studySessionParam,
        participantId: "",
        condition: "MIXED",
        sequenceOrder: "MCRF_THEN_LEGACY",
        completionStatus: "started",
        experimentVersion: "full-study-v1",
      })
      return
    }
    // FIX (A-2 follow-up): don't attempt to create a study session until
    // we know study consent is granted — the backend 403s otherwise, and
    // retrying the same request in a loop wouldn't help. Once
    // studyConsentGranted flips true (see StudyConsentModal below), this
    // effect re-runs and proceeds normally.
    if (studyConsentGranted !== true) return
    if (sessionCreatedForCourseRef.current === course.id) return
    sessionCreatedForCourseRef.current = course.id

    let cancelled = false
    ;(async () => {
      try {
        const session = await startStudySession(course.id, selectedVideo.id, course.id)
        if (!cancelled) setStudySession(session)
      } catch (err) {
        console.error("Failed to start study session:", err)
        sessionCreatedForCourseRef.current = null
        if (!cancelled) setStudySession(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [course?.id, selectedVideo?.id, studySessionParam, studyConsentGranted])

  const lastProgressSaveRef = useRef<{ videoId: string; wallClockMs: number } | null>(null)

  const handleTimeUpdate = useCallback((ct: number, dur: number) => {
    setCurrentTime(ct)
    setVideoDuration(dur)

    // FIX (phase-4 request): persist real watch position so the next
    // visit can resume here — throttled to ~every 10s (not every
    // timeupdate tick, which fires several times a second) and
    // best-effort: a failed save shouldn't interrupt playback.
    const videoId = selectedVideoIdRef.current
    if (!videoId || !dur) return
    const now = Date.now()
    const last = lastProgressSaveRef.current
    if (last && last.videoId === videoId && now - last.wallClockMs < 10000) return
    lastProgressSaveRef.current = { videoId, wallClockMs: now }
    void saveVideoProgress(videoId, ct, dur).catch((err) =>
      console.error("Failed to save video progress:", err)
    )
  }, [])

  const handlePlayStateChange = useCallback((playing: boolean) => {
    setIsPlaying(playing)
    if (playing) setVideoEnded(false)
  }, [])

  const handleVideoEnd = useCallback(() => {
    setVideoEnded(true)
    setIsPlaying(false)
    const completedVideoId = selectedVideo?.id || null
    if (completedVideoId) {
      const transcriptText = typeof window !== "undefined" && (window as any).__transcriptText
        ? (window as any).__transcriptText()
        : ""
      setCompletedVideoTranscripts((prev) => ({
        ...prev,
        [completedVideoId]: transcriptText,
      }))
      const sessionId = studySessionIdRef.current
      if (sessionId) {
        void completeStudyVideo(sessionId, completedVideoId, transcriptText)
          .catch((err) => console.error("Failed to record completed video:", err))
      } else {
        console.warn("[VideoEnd] study session not yet ready; completion will be recovered by the backend auto-record on assessment generation.", completedVideoId)
      }
    }
  }, [selectedVideo?.id])

  const handleAttentionUpdate = useCallback((snapshot: AttentionSnapshotResponse) => {
    setLatestAttention(snapshot)
    setAttentionHistory((prev) => {
      const next = [...prev, snapshot.score].slice(-60)
      const avg = next.reduce((a, b) => a + b, 0) / next.length
      setSessionAvgAttention(avg)
      return next
    })
  }, [])

  const handleSelectVideo = useCallback((video: VideoLink) => {
    setSelectedVideo(video)
    setIsPlaying(false)
    setCurrentTime(0)
    setVideoEnded(false)
    setAttentionHistory([])
    setLatestAttention(null)
    setSessionAvgAttention(0)
    setBehavioralCueGranted(null)
    // FIX (A-1): Do not regenerate webcamSessionId here. Camera consent is
    // visit/session-level and must survive video switches.
  }, [])

  const goToAssessment = async () => {
    if (isPreparingAssessment) return
    const currentCompletedVideoId = selectedVideo?.id || null
    const currentTranscriptText = typeof window !== "undefined" && (window as any).__transcriptText
      ? (window as any).__transcriptText()
      : ""
    setIsPreparingAssessment(true)
    try {
      if (!studySession?.studySessionId) {
        throw new Error("Study session is not ready yet.")
      }

      const transcriptsToPersist = {
        ...completedVideoTranscripts,
        ...(currentCompletedVideoId ? { [currentCompletedVideoId]: currentTranscriptText } : {}),
      }
      if (Object.keys(transcriptsToPersist).length === 0) {
        throw new Error("Complete a video before starting the assessment.")
      }

      await Promise.all(
        Object.entries(transcriptsToPersist).map(([videoId, transcriptText]) =>
          completeStudyVideo(studySession.studySessionId, videoId, transcriptText)
        )
      )

      const attentionSummary = {
        avgScore: Math.round(effectiveBehavioralCue * 10) / 10,
        scoreHistory: attentionHistory.slice(-40),
        totalSnapshots: attentionHistory.length,
        attentivePercent: Math.round((attentionHistory.filter(s => s >= 65).length / Math.max(attentionHistory.length, 1)) * 100),
        inattentivePercent: Math.round((attentionHistory.filter(s => s >= 30 && s < 65).length / Math.max(attentionHistory.length, 1)) * 100),
        unfocusedPercent: Math.round((attentionHistory.filter(s => s < 30).length / Math.max(attentionHistory.length, 1)) * 100),
        avgEyeContact: behavioralCueGranted === false ? 0.5 : latestAttention?.modelResponse?.eyeContact ?? 0.8,
        avgBlinkRate: behavioralCueGranted === false ? 0 : latestAttention?.modelResponse?.blinkRate ?? 16,
      }
      const params = new URLSearchParams({
        videos: Object.keys(transcriptsToPersist).join(","),
        course: course?.id || "",
        video: selectedVideo?.id || "",
        studySession: studySession.studySessionId,
        courseTitle: course?.title || "Video Session",
        videoTitle: selectedVideo?.title || "Video Session",
        behavioral_cue: Math.round(effectiveBehavioralCue).toString(),
        attentionData: JSON.stringify(attentionSummary),
      })
      setSessionComplete(true)
      router.push(`/assessment?${params.toString()}`)
    } catch (err) {
      console.error("Assessment preparation failed:", err)
    } finally {
      setIsPreparingAssessment(false)
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-[70vh]">
        <motion.div animate={{ rotate: 360 }} transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }} className="w-10 h-10 border-3 border-violet-500 border-t-transparent rounded-full" />
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 max-w-[1500px] mx-auto">
      {/* FIX (A-2 follow-up): resuming an existing session (studySessionParam)
          already implies consent was granted when that session was first
          created — don't re-prompt in that case. */}
      {studyConsentGranted === false && !studySessionParam && (
        <StudyConsentModal
          onDecision={() => setStudyConsentGranted(true)}
          onDecline={() => router.push("/dashboard")}
        />
      )}
      <motion.div initial={{ opacity: 0, y: -15 }} animate={{ opacity: 1, y: 0 }} className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <motion.button whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={() => router.push("/dashboard")}
            className="w-9 h-9 rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] flex items-center justify-center text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:border-[var(--border-default)] transition-all">
            <ArrowLeft size={16} />
          </motion.button>
          <div>
            <h1 className="text-xl font-bold text-[var(--text-primary)]">Video Learning</h1>
            <p className="text-xs text-[var(--text-muted)]">AI-powered behavioral-cue monitoring &amp; live transcription</p>
          </div>
        </div>
      </motion.div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        <div className="lg:col-span-3 order-2 lg:order-1">
          {course && (
            <VideoLinkSelector videos={course.videoLinks || []} activeVideoId={selectedVideo?.id || null} onSelect={handleSelectVideo} courseTitle={course.title} />
          )}
        </div>

        <div className="lg:col-span-6 space-y-5 order-1 lg:order-2">
          {effectiveUrl ? (
            <VideoPlayer videoUrl={effectiveUrl} title={effectiveTitle} startAt={resumeSecondsFor(selectedVideo)} onTimeUpdate={handleTimeUpdate} onPlayStateChange={handlePlayStateChange} onVideoEnd={handleVideoEnd} />
          ) : (
            <div className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] aspect-video flex flex-col items-center justify-center gap-3">
              <BookOpen size={32} className="text-[var(--text-muted)]" />
              <p className="text-sm text-[var(--text-muted)]">Select a video from the list or paste a URL</p>
            </div>
          )}

          <AnimatePresence>
            {videoEnded && (
              <motion.div initial={{ opacity: 0, y: 20, scale: 0.95 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -10 }}
                className="rounded-2xl p-5 border border-violet-500/30 bg-gradient-to-br from-violet-500/10 to-purple-500/5">
                <div className="flex items-start gap-4">
                  <div className="w-12 h-12 rounded-xl bg-violet-500/20 flex items-center justify-center shrink-0">
                    <Sparkles size={24} className="text-violet-400" />
                  </div>
                  <div className="flex-1">
                    <h3 className="text-base font-bold text-[var(--text-primary)] mb-1">Ready for Assessment!</h3>
                    <p className="text-xs text-[var(--text-muted)] mb-3">
                      Your average behavioral-cue score was <span className="font-bold text-violet-400">{Math.round(effectiveBehavioralCue)}%</span>.
                      Based on this and the video content, we&apos;ll generate a personalized quiz.
                    </p>
                    <div className="flex items-center gap-3">
                      <motion.button whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }} onClick={goToAssessment} disabled={isPreparingAssessment}
                        className="px-5 py-2.5 text-sm font-semibold rounded-xl bg-gradient-to-r from-violet-500 to-purple-600 text-white hover:shadow-lg hover:shadow-violet-500/20 transition-all flex items-center gap-2 disabled:opacity-60">
                        <ClipboardCheck size={16} /> {isPreparingAssessment ? "Preparing…" : "Take Assessment"}
                      </motion.button>
                      <button onClick={() => setVideoEnded(false)}
                        className="px-4 py-2.5 text-sm font-medium rounded-xl bg-[var(--bg-elevated)] text-[var(--text-secondary)] border border-[var(--border-subtle)] hover:border-[var(--border-default)] transition-all">
                        Rewatch
                      </button>
                    </div>
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3 mt-4 pt-4 border-t border-violet-500/15">
                  <div className="text-center">
                    <p className="text-lg font-bold text-[var(--text-primary)]">{Math.round(effectiveBehavioralCue)}%</p>
                    <p className="text-[10px] text-[var(--text-muted)]">Avg Behavioral Cue</p>
                  </div>
                  <div className="text-center">
                    <p className="text-lg font-bold text-[var(--text-primary)]">{attentionHistory.length}</p>
                    <p className="text-[10px] text-[var(--text-muted)]">Snapshots</p>
                  </div>
                  <div className="text-center">
                    <p className="text-lg font-bold text-[var(--text-primary)]">{Math.floor(videoDuration / 60)}m {Math.floor(videoDuration % 60)}s</p>
                    <p className="text-[10px] text-[var(--text-muted)]">Duration</p>
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {isPlaying && latestAttention && latestAttention.state !== "attentive" && (
              <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}
                className={`rounded-xl p-3 flex items-center gap-3 border ${
                  latestAttention.state === "inattentive" ? "bg-amber-500/8 border-amber-500/20" : "bg-red-500/8 border-red-500/20"
                }`}>
                <AlertCircle size={18} className={latestAttention.state === "inattentive" ? "text-amber-400" : "text-red-400"} />
                <p className="text-xs text-[var(--text-secondary)]">{latestAttention.message}</p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <div className="lg:col-span-3 space-y-4 order-3">
          <CameraFeed
            isVideoPlaying={isPlaying}
            // FIX (B-7): mp4 and YouTube embeds give a real pause/ended
            // signal (see VideoPlayer.tsx); only a generic opaque iframe
            // ("embed") can never report play state to the parent. Tell
            // CameraFeed which case this is so it only pauses capture
            // when the signal is actually trustworthy.
            videoPlayStateKnown={detectVideoType(effectiveUrl) !== "embed"}
            videoId={selectedVideo?.id || "custom"}
            studentId="student_001"
            sessionId={webcamSessionId}
            studySessionId={studySession?.studySessionId}
            sessionComplete={sessionComplete}
            onConsentChange={(granted) => {
              setBehavioralCueGranted(granted)
              if (!granted) {
                setLatestAttention(null)
                setAttentionHistory([])
                setSessionAvgAttention(0)
              }
            }}
            onAttentionUpdate={handleAttentionUpdate}
          />
          <AttentionPanel
            latestSnapshot={latestAttention}
            sessionAverage={sessionAvgAttention}
            scoreHistory={attentionHistory}
          />
          <TranscriptionPanel
            videoId={selectedVideo?.id || "custom"}
            videoUrl={effectiveUrl}
            currentTime={currentTime}
            isPlaying={isPlaying}
          />
        </div>
      </div>
    </div>
  )
}