"use client"

import { useState, useEffect, useRef, useCallback } from "react"
import { motion, AnimatePresence } from "framer-motion"
import { FileText, Wifi, WifiOff, ChevronDown } from "lucide-react"

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api"

interface TranscriptSegment {
  id: string
  text: string
  timestamp: string
  start_time: number
  end_time: number
  confidence: number
  model_response: {
    language: string
    words: { word: string; start: number; end: number; confidence: number }[]
  }
}

interface TranscriptionPanelProps {
  videoId: string
  videoUrl: string
  currentTime: number
  isPlaying: boolean
}

export default function TranscriptionPanel({
  videoId,
  videoUrl,
  currentTime,
  isPlaying,
}: TranscriptionPanelProps) {
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null)
  const [isConnected, setIsConnected] = useState(false)
  const [transcriptionError, setTranscriptionError] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const lastFetchTime = useRef<number>(-1)
  // Tracks which video IDs have SUCCESSFULLY had a transcription request
  // completed during this page/session flow, so playing/pausing/
  // re-playing the same video (or re-rendering) never fires a fresh
  // fetch once real data has been loaded.
  const transcriptionTriggeredFor = useRef<Set<string>>(new Set())
  // FIX (transcription doesn't start on first play): a video ID sits here
  // only while its fetch is actually in flight — this is what prevents
  // duplicate *concurrent* requests for the same video. It is distinct
  // from transcriptionTriggeredFor above: previously, a video was marked
  // "triggered" (permanently, for this component's lifetime) the instant
  // the fetch was fired, before knowing whether it would succeed. A
  // transient failure or slow backend response (e.g. the server being
  // momentarily busy right as playback starts) then permanently blocked
  // any retry — the only way to try again was a full page reload, which
  // creates a fresh component instance with an empty set. Now a video is
  // only added to transcriptionTriggeredFor on genuine success; a
  // failure clears it from this in-flight set and schedules a bounded
  // retry instead.
  const transcriptionInFlightFor = useRef<Set<string>>(new Set())

  // Selecting/loading/navigating to a video must not trigger transcription
  // — only clear the previously-shown transcript so the panel doesn't keep
  // displaying the last video's segments while nothing has been requested
  // for the new one yet.
  useEffect(() => {
    setSegments([])
    setActiveSegmentId(null)
    setIsConnected(false)
    setTranscriptionError(false)
    lastFetchTime.current = -1
  }, [videoId])

  // ── Fetch full transcript only once actual playback starts ──
  // Requirement: transcription must start on the first real play event for
  // this video, not merely because it was selected/loaded/displayed, and
  // (once it succeeds) at most once per video during this page/session flow.
  useEffect(() => {
    if (!isPlaying) return
    if (transcriptionTriggeredFor.current.has(videoId)) return
    if (transcriptionInFlightFor.current.has(videoId)) return
    transcriptionInFlightFor.current.add(videoId)

    let cancelled = false

    async function fetchFullTranscript(attempt: number) {
      try {
        // FIX: no timeout previously existed at all — a slow/stuck
        // backend response left this fetch pending indefinitely. 25s is
        // generous enough for a genuine
        // cold Faster-Whisper CPU transcription of a normal-length video.
        const controller = new AbortController()
        const timeout = setTimeout(() => controller.abort(), 25000)
        const res = await fetch(
          `${API_BASE}/transcription/${videoId}?video_url=${encodeURIComponent(videoUrl)}`,
          { signal: controller.signal }
        )
        clearTimeout(timeout)
        if (cancelled) return
        if (res.ok) {
          const data = await res.json()
          if (cancelled) return
          setSegments(data)
          setIsConnected(true)
          setTranscriptionError(false)
          // Only now — genuine success — is this video considered done.
          transcriptionInFlightFor.current.delete(videoId)
          transcriptionTriggeredFor.current.add(videoId)
          return
        }
        throw new Error(`transcription fetch failed: ${res.status}`)
      } catch {
        if (cancelled) return
        // FIX: previously this video was already marked "triggered"
        // before this point, permanently, so a failure here meant no
        // retry could ever happen short of a full page reload. Bounded
        // retry (3 attempts, short backoff) instead, covering exactly
        // the case of a momentarily busy/slow backend right as playback
        // starts. Give up retrying after those attempts without inventing text.
        if (attempt < 3) {
          setTimeout(() => {
            if (!cancelled) fetchFullTranscript(attempt + 1)
          }, 1500 * attempt)
          return
        }
        transcriptionInFlightFor.current.delete(videoId)
        setSegments([])
        setIsConnected(false)
        setTranscriptionError(true)
      }
    }
    fetchFullTranscript(1)

    return () => {
      cancelled = true
      transcriptionInFlightFor.current.delete(videoId)
    }
  }, [isPlaying, videoId, videoUrl])

  // ── Poll live segment as video plays ──
  useEffect(() => {
    if (!isPlaying) return

    const roundedTime = Math.floor(currentTime)

    // Only fetch when time changes by at least 1 second
    if (roundedTime === lastFetchTime.current) return
    lastFetchTime.current = roundedTime

    // Find active segment from already loaded segments
    const active = segments.find(
      (s) => currentTime >= s.start_time && currentTime < s.end_time
    )
    if (active) {
      setActiveSegmentId(active.id)
    }

    // Also try fetching live from backend (to get new segments)
    async function fetchLive() {
      try {
        const res = await fetch(
          `${API_BASE}/transcription/${videoId}/live?current_time=${currentTime}&video_url=${encodeURIComponent(videoUrl)}`
        )
        if (res.ok) {
          const data = await res.json()
          if (data.id) {
            setSegments((prev) => {
              if (prev.some((s) => s.id === data.id)) {
                return prev
              }

              return [...prev, data].sort(
                (a, b) => a.start_time - b.start_time
              )
            })
          }
          setIsConnected(true)
        }
      } catch {
        setIsConnected(false)
      }
    }

    fetchLive()
  }, [currentTime, isPlaying, videoId, videoUrl, segments])

  // ── Auto-scroll to active segment ──
  useEffect(() => {
    if (!autoScroll || !activeSegmentId || !scrollRef.current) return
    const el = scrollRef.current.querySelector(`[data-segment-id="${activeSegmentId}"]`)
    el?.scrollIntoView({ behavior: "smooth", block: "center" })
  }, [activeSegmentId, autoScroll])

  // ── Get full transcript text (for assessment generation) ──
  const getFullText = useCallback(() => {
    return segments.map((s) => s.text).join(" ")
  }, [segments])

  // Expose on window for parent page to access
  useEffect(() => {
    (window as any).__transcriptText = getFullText
  }, [getFullText])

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.4, delay: 0.2 }}
      className="rounded-2xl overflow-hidden bg-[var(--bg-card)] border border-[var(--border-subtle)] flex flex-col"
    >
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-[var(--border-subtle)] flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <FileText size={14} className="text-[var(--text-muted)]" />
          <span className="text-xs font-semibold text-[var(--text-primary)]">
            Live Transcription
          </span>
        </div>
        <div className="flex items-center gap-2">
          {isConnected ? (
            <Wifi size={10} className="text-emerald-400" />
          ) : (
            <WifiOff size={10} className="text-amber-400" />
          )}
          {isPlaying && (
            <motion.div
              animate={{ scale: [1, 1.3, 1] }}
              transition={{ duration: 1.5, repeat: Infinity }}
              className="w-1.5 h-1.5 rounded-full bg-red-500"
            />
          )}
          <span className="text-[9px] text-[var(--text-muted)] font-mono">
            {segments.length} seg
          </span>
        </div>
      </div>

      {/* Transcript list */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto p-3 space-y-2 min-h-[200px] max-h-[380px]"
        onScroll={() => {
          // Disable auto-scroll if user manually scrolls up
          const el = scrollRef.current
          if (!el) return
          const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50
          setAutoScroll(isAtBottom)
        }}
      >
        <AnimatePresence initial={false}>
          {segments.length > 0 ? (
            segments.map((seg, idx) => {
              const isActive = seg.id === activeSegmentId
              const isPast = currentTime > seg.end_time

              return (
                <motion.div
                  key={seg.id}
                  data-segment-id={seg.id}
                  initial={{ opacity: 0, x: -15 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: idx * 0.03 }}
                  className={`flex gap-2.5 items-start p-2.5 rounded-xl transition-all duration-300 ${isActive
                      ? "bg-violet-500/10 border border-violet-500/20"
                      : isPast
                        ? "bg-[var(--bg-elevated)]/50 opacity-60"
                        : "bg-[var(--bg-elevated)] border border-transparent"
                    }`}
                >
                  {/* Timestamp */}
                  <div className="flex flex-col items-center shrink-0 pt-0.5">
                    <span
                      className={`text-[10px] font-mono ${isActive ? "text-violet-400" : "text-[var(--text-muted)]"
                        }`}
                    >
                      {seg.timestamp}
                    </span>
                    {isActive && (
                      <motion.div
                        animate={{ scale: [1, 1.5, 1] }}
                        transition={{ duration: 1, repeat: Infinity }}
                        className="w-1 h-1 rounded-full bg-violet-400 mt-1"
                      />
                    )}
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <p
                      className={`text-xs leading-relaxed ${isActive
                          ? "text-[var(--text-primary)] font-medium"
                          : "text-[var(--text-secondary)]"
                        }`}
                    >
                      {seg.text}
                    </p>

                    {/* Confidence bar */}
                    <div className="flex items-center gap-2 mt-1.5">
                      <div className="flex-1 h-0.5 rounded-full bg-[var(--bg-primary)] overflow-hidden">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${seg.confidence * 100}%`,
                            background:
                              seg.confidence > 0.9
                                ? "#10b981"
                                : seg.confidence > 0.8
                                  ? "#f59e0b"
                                  : "#ef4444",
                          }}
                        />
                      </div>
                      <span className="text-[8px] text-[var(--text-muted)] font-mono">
                        {(seg.confidence * 100).toFixed(0)}%
                      </span>
                    </div>
                  </div>
                </motion.div>
              )
            })
          ) : (
            <div className="flex items-center justify-center h-32 text-[var(--text-muted)] text-xs">
              {transcriptionError
                ? "Transcription failed. Assessment questions require a genuine transcript."
                : "Waiting for transcription data..."}
            </div>
          )}
        </AnimatePresence>
      </div>

      {/* Footer: scroll indicator */}
      {!autoScroll && (
        <motion.button
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          onClick={() => {
            setAutoScroll(true)
            const el = scrollRef.current
            if (el) el.scrollTop = el.scrollHeight
          }}
          className="mx-3 mb-3 py-1.5 rounded-lg bg-violet-500/10 border border-violet-500/20 text-violet-400 text-[10px] font-medium flex items-center justify-center gap-1 hover:bg-violet-500/15 transition-colors"
        >
          <ChevronDown size={12} />
          Jump to latest
        </motion.button>
      )}

      {/* Footer info */}
      <div className="px-4 py-2 border-t border-[var(--border-subtle)] bg-[var(--bg-elevated)]/30 text-[9px] text-[var(--text-muted)] flex justify-between">
        <span>Model: Whisper {transcriptionError ? "(unavailable)" : isConnected ? "(live)" : "(cached)"}</span>
        <span>Lang: EN</span>
      </div>
    </motion.div>
  )
}
