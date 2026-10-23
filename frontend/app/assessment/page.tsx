"use client"

import { useEffect, useState, useRef, useCallback, Suspense } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { motion, AnimatePresence } from "framer-motion"
import { Brain, Clock, Loader2 } from "lucide-react"
import AssessmentCard from "@/components/AssessmentCard"
import {
  generateAssessment,
  getActiveStudySession,
  getAssessmentSession,
  submitAdaptiveAnswer,
  submitAssessment,
  type AssessmentSession,
  type QuestionResponseEvent,
} from "@/lib/api"

// FIX (perf/timeout request, item 1): the next question's FLAN-T5
// generation now happens in a backend background task instead of blocking
// the /answer response, so a "pending" round needs a short poll instead of
// arriving inline. Interval and timeout are generous relative to a single
// FLAN-T5 forward pass, but bounded so a stuck generation still surfaces
// an error instead of spinning forever.
const NEXT_QUESTION_POLL_INTERVAL_MS = 700
const NEXT_QUESTION_POLL_TIMEOUT_MS = 30000

const TOTAL_ASSESSMENT_QUESTIONS = 10

export default function AssessmentPage() {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center h-[70vh]">
        <div className="w-10 h-10 border-3 border-violet-500 border-t-transparent rounded-full animate-spin" />
      </div>
    }>
      <AssessmentContent />
    </Suspense>
  )
}

function AssessmentContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const courseId = searchParams.get("course")
  const videoId = searchParams.get("video")
  const studySessionId = searchParams.get("studySession")
  const videosParam = searchParams.get("videos")
  const contributingVideoIds = (videosParam || videoId || "").split(",").filter(Boolean)
  const attentionScore = parseFloat(searchParams.get("behavioral_cue") || "70")
  const prevScore = searchParams.get("prev") ? parseFloat(searchParams.get("prev")!) : null
  const attentionDataParam = searchParams.get("attentionData") || ""
  const courseTitleParam = searchParams.get("courseTitle") || "Course"
  const videoTitleParam = searchParams.get("videoTitle") || "Video"
  const transcriptParam = searchParams.get("transcript") || ""
  
  const [session, setSession] = useState<AssessmentSession | null>(null)
  const [currentIdx, setCurrentIdx] = useState(0)
  const [answers, setAnswers] = useState<Record<string, string | number>>({})
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isAdvancing, setIsAdvancing] = useState(false)
  const [timeRemaining, setTimeRemaining] = useState(0)
  // FIX (13D.1): durations use a monotonic clock (performance.now()); wall-clock
  // Date is kept only for human-readable ISO timestamps.
  const [startTime] = useState(() => performance.now())
  const timerRef = useRef<NodeJS.Timeout | undefined>(undefined)
  // FIX (perf/timeout request, item 9): guards against finalizing twice —
  // e.g. the timer firing auto-submit at nearly the same moment the
  // in-flight 10th answer already completed the assessment.
  const hasFinalizedRef = useRef(false)
  const questionPresentedAtRef = useRef<Record<string, { mono: number; wall: number }>>({})
  const responseEventsRef = useRef<QuestionResponseEvent[]>([])
  const generationStartedRef = useRef(false)


  // Fetch transcript text from window (set by TranscriptionPanel)
  const getTranscriptText = (): string => {
    if (transcriptParam.trim()) return transcriptParam
    if (typeof window !== "undefined" && (window as any).__transcriptText) {
      return (window as any).__transcriptText()
    }
    return ""
  }

  // Generate assessment on mount
  useEffect(() => {
  async function generate() {
    if (generationStartedRef.current) return
    generationStartedRef.current = true
    setIsLoading(true)

    try {
      let resolvedCourseId = courseId
      let resolvedVideoId = videoId
      let resolvedStudySessionId = studySessionId
      let resolvedVideoIds = contributingVideoIds

      if (!resolvedStudySessionId) {
        const activeSession = await getActiveStudySession()

        resolvedStudySessionId = activeSession.studySessionId
        resolvedCourseId = activeSession.courseId || resolvedCourseId
        resolvedVideoId = activeSession.videoId || resolvedVideoId

        if (!resolvedVideoIds.length && resolvedVideoId) {
          resolvedVideoIds = [resolvedVideoId]
        }
      }

      if (!resolvedCourseId || !resolvedVideoId || !resolvedStudySessionId) {
        throw new Error("No active study session is available for assessment.")
      }

      const sess = await generateAssessment(
        resolvedCourseId,
        resolvedVideoId,
        attentionScore,
        prevScore,
        getTranscriptText(),
        resolvedStudySessionId,
        resolvedVideoIds
      )

      setSession(sess)
      setTimeRemaining(sess.timeLimit)
    } catch (err) {
      console.error("Failed to generate assessment:", err)
    } finally {
      setIsLoading(false)
    }
  }

  generate()
}, [courseId, videoId, studySessionId, videosParam, attentionScore, prevScore])

  useEffect(() => {
    const question = session?.questions[currentIdx]
    if (question && !questionPresentedAtRef.current[question.id]) {
      questionPresentedAtRef.current[question.id] = { mono: performance.now(), wall: Date.now() }
    }
  }, [session, currentIdx])

  // Timer countdown
  useEffect(() => {
    if (!session || timeRemaining <= 0) return
    timerRef.current = setInterval(() => {
      setTimeRemaining((prev) => {
        if (prev <= 1) {
          clearInterval(timerRef.current)
          handleAutoSubmit()
          return 0
        }
        return prev - 1
      })
    }, 1000)
    return () => clearInterval(timerRef.current)
  }, [session])

  // FIX (perf/timeout request, item 3): timer expiry now actually finalizes
  // the assessment (it previously did nothing at all — see handleAnswer's
  // completion branch below for the reused navigation logic). Whatever was
  // graded via submitAdaptiveAnswer so far counts; anything unanswered is
  // scored as incorrect by the backend's timeout path.
  const handleAutoSubmit = useCallback(async () => {
    if (!session || hasFinalizedRef.current) return
    hasFinalizedRef.current = true
    setIsSubmitting(true)
    try {
      const result = await submitAssessment(
        session.id,
        answers,
        session.questions,
        Math.round((performance.now() - startTime) / 1000),
        responseEventsRef.current
      )
      const resultPayload = {
        data: JSON.stringify(result),
        behavioral_cue: attentionDataParam,
        course: courseTitleParam,
        video: videoTitleParam,
      }
      if (session.studySessionId || studySessionId) {
        window.sessionStorage.setItem("neurolearn_last_result", JSON.stringify(resultPayload))
      }
      const params = new URLSearchParams({
        data: JSON.stringify(result),
        behavioral_cue: attentionDataParam,
        course: courseTitleParam,
        video: videoTitleParam,
      })
      router.push(`/results?${params.toString()}`)
    } catch (err) {
      console.error("Timeout auto-submit failed:", err)
      hasFinalizedRef.current = false
      setIsSubmitting(false)
    }
  }, [session, answers, startTime, attentionDataParam, courseTitleParam, videoTitleParam, studySessionId, router])

  // FIX (perf/timeout request, item 1): generation of the next question now
  // happens in a backend background task, so a "pending" round polls
  // GET /assessment/session/{id} until the question shows up (or times out)
  // instead of expecting it inline in the /answer response.
  const waitForNextQuestion = async (
    sessionId: string,
    expectedQuestionCount: number
  ): Promise<AssessmentSession> => {
    const deadline = Date.now() + NEXT_QUESTION_POLL_TIMEOUT_MS
    // First check without delay — the background task may already be done.
    let latest = await getAssessmentSession(sessionId)
    while (latest.questions.length < expectedQuestionCount) {
      if (Date.now() >= deadline) {
        throw new Error("Timed out waiting for the next question to generate.")
      }
      await new Promise((resolve) => setTimeout(resolve, NEXT_QUESTION_POLL_INTERVAL_MS))
      latest = await getAssessmentSession(sessionId)
    }
    return latest
  }

  const handleAnswer = async (questionId: string, answer: string | number) => {
    if (!session || isSubmitting || isAdvancing || hasFinalizedRef.current) return
    const presentedRec = questionPresentedAtRef.current[questionId] || { mono: performance.now(), wall: Date.now() }
    const presentedAt = presentedRec.wall
    const submittedAt = Date.now()
    const elapsedMs = performance.now() - presentedRec.mono
    const responseEvent: QuestionResponseEvent = {
      questionId,
      questionIndex: currentIdx,
      presentedAt: new Date(presentedAt).toISOString(),
      submittedAt: new Date(submittedAt).toISOString(),
      responseTimeSeconds: Math.max(0, Math.round(elapsedMs / 100) / 10),
      status: "submitted",
    }
    responseEventsRef.current = [
      ...responseEventsRef.current.filter((event) => event.questionId !== questionId),
      responseEvent,
    ]
    const newAnswers = { ...answers, [questionId]: answer }
    setAnswers(newAnswers)

    const isFinalQuestion = currentIdx + 1 >= TOTAL_ASSESSMENT_QUESTIONS
    if (isFinalQuestion) {
      setIsSubmitting(true)
    } else {
      setIsAdvancing(true)
    }
    try {
      const round = await submitAdaptiveAnswer(session.id, questionId, answer, responseEvent)
      if (round.completed && round.result) {
        if (hasFinalizedRef.current) return // timer already finalized this session
        hasFinalizedRef.current = true
        const resultPayload = {
          data: JSON.stringify(round.result),
          behavioral_cue: attentionDataParam,
          course: courseTitleParam,
          video: videoTitleParam,
        }
        if (session.studySessionId || studySessionId) {
          window.sessionStorage.setItem("neurolearn_last_result", JSON.stringify(resultPayload))
        }
        const params = new URLSearchParams({
          data: JSON.stringify(round.result),
          behavioral_cue: attentionDataParam,
          course: courseTitleParam,
          video: videoTitleParam,
        })
        router.push(`/results?${params.toString()}`)
        return
      }
      // FIX (perf/timeout request, item 1): the next question is generated
      // in the background now, so wait for it to show up instead of
      // expecting it inline in `round.session`.
      const readySession = round.nextQuestionPending
        ? await waitForNextQuestion(session.id, currentIdx + 2)
        : round.session
      setSession(readySession)
      setCurrentIdx((i) => i + 1)
    } catch (err) {
      console.error("Adaptive answer failed:", err)
    } finally {
      setIsSubmitting(false)
      setIsAdvancing(false)
    }
  }

  // Loading
  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center h-[70vh] gap-4">
        <motion.div
          animate={{ rotate: 360 }}
          transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
          className="w-10 h-10 border-3 border-violet-500 border-t-transparent rounded-full"
        />
        <div className="text-center">
          <p className="text-sm font-medium text-[var(--text-primary)]">Generating Assessment...</p>
          <p className="text-xs text-[var(--text-muted)] mt-1">
            AI is creating questions adapted to your level
          </p>
        </div>
      </div>
    )
  }

  if (!session || session.questions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-[70vh] gap-3">
        <Brain size={32} className="text-[var(--text-muted)]" />
        <p className="text-sm text-[var(--text-muted)]">No questions available. Try watching a video first.</p>
        <button onClick={() => router.push("/dashboard")} className="text-xs text-violet-400 underline">
          Go to Dashboard
        </button>
      </div>
    )
  }

  const currentQ = session.questions[currentIdx]

  return (
    <div className="p-6 max-w-4xl mx-auto">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -15 }}
        animate={{ opacity: 1, y: 0 }}
        className="text-center mb-10"
      >
        <h1 className="text-2xl font-bold text-[var(--text-primary)] mb-1">Assessment</h1>
        <p className="text-xs text-[var(--text-muted)]">
          Difficulty:{" "}
          <span className="font-semibold capitalize text-violet-400">{session.difficulty}</span>
          {" · "}
          Behavioral Cue during video:{" "}
          <span className="font-semibold text-cyan-400">
            {session.attentionScoreDuringVideo == null
              ? "no data"
              : `${Math.round(session.attentionScoreDuringVideo)}%`}
          </span>
        </p>
        {session.adaptiveMetadata?.reason && (
          <p className="text-[10px] text-[var(--text-muted)] mt-1 italic max-w-md mx-auto">
            {session.adaptiveMetadata.reason}
          </p>
        )}
      </motion.div>

      {/* Submitting overlay */}
      <AnimatePresence>
        {isSubmitting && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex flex-col items-center justify-center gap-3"
          >
            <Loader2 size={32} className="text-violet-400 animate-spin" />
            <p className="text-sm text-white">Analyzing your results...</p>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Question card */}
      <AnimatePresence mode="wait">
        <AssessmentCard
          key={currentQ.id}
          question={currentQ}
          questionNumber={currentIdx + 1}
          totalQuestions={TOTAL_ASSESSMENT_QUESTIONS}
          timeRemaining={timeRemaining}
          initialAnswer={answers[currentQ.id] ?? null}
          isFinalQuestion={currentIdx + 1 >= TOTAL_ASSESSMENT_QUESTIONS}
          isBusy={isAdvancing || isSubmitting}
          canGoPrevious={currentIdx > 0}
          canGoNext={answers[currentQ.id] !== undefined && currentIdx < session.questions.length - 1}
          onSubmit={handleAnswer}
          onPrevious={() => setCurrentIdx((idx) => Math.max(0, idx - 1))}
          onNext={() => setCurrentIdx((idx) => Math.min(session.questions.length - 1, idx + 1))}
        />
      </AnimatePresence>

      {/* Tip */}
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.5 }}
        className="text-center text-[10px] text-[var(--text-muted)] mt-8"
      >
        💡 Questions adapt based on your video behavioral_cue and previous performance
      </motion.p>
    </div>
  )
}