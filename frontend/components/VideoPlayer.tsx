"use client"
declare global {
  interface Window {
    YT?: {
      Player: new (
        element: HTMLIFrameElement,
        options: {
          events?: {
            onReady?: () => void
            onStateChange?: (event: { data: number }) => void
          }
        }
      ) => {
        getCurrentTime?: () => number
        getDuration?: () => number
        playVideo?: () => void
        pauseVideo?: () => void
      }
      PlayerState?: {
        ENDED: number
        PLAYING: number
        PAUSED: number
        BUFFERING: number
      }
    }
    onYouTubeIframeAPIReady?: () => void
  }
}
import { useRef, useState, useEffect, useCallback, useMemo } from "react"
import { motion, AnimatePresence } from "framer-motion"
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  Maximize,
  Minimize,
  Loader2,
} from "lucide-react"

// ── URL Type Detection ──
// V2: every video comes from the admin-managed catalog (see /admin) as
// either a local file served by Nginx, or an admin-entered YouTube link
// (routers/courses.py's /videos/youtube endpoint — the only place a
// YouTube URL can enter the system; never seeded, never student-pasted).
// "embed" is a defensive fallback for anything else.
type VideoType = "mp4" | "youtube" | "embed"

export function detectVideoType(url: string): VideoType {
  if (!url) return "mp4"
  if (
    url.includes("youtube.com/watch") ||
    url.includes("youtu.be/") ||
    url.includes("youtube.com/embed") ||
    url.includes("youtube.com/shorts")
  )
    return "youtube"
  if (
    url.endsWith(".mp4") ||
    url.endsWith(".webm") ||
    url.endsWith(".ogg") ||
    url.includes(".mp4?") ||
    url.includes(".webm?")
  )
    return "mp4"
  return "embed"
}

function getYouTubeEmbedUrl(url: string): string {
  let videoId = ""
  if (url.includes("youtube.com/watch")) {
    const u = new URL(url)
    videoId = u.searchParams.get("v") || ""
  } else if (url.includes("youtu.be/")) {
    videoId = url.split("youtu.be/")[1]?.split(/[?&#]/)[0] || ""
  } else if (url.includes("youtube.com/embed/")) {
    videoId = url.split("youtube.com/embed/")[1]?.split(/[?&#]/)[0] || ""
  } else if (url.includes("youtube.com/shorts/")) {
    videoId = url.split("youtube.com/shorts/")[1]?.split(/[?&#]/)[0] || ""
  }
  return `https://www.youtube.com/embed/${videoId}?enablejsapi=1&autoplay=0&rel=0&modestbranding=1&origin=${typeof window !== "undefined" ? window.location.origin : ""}`
}

interface VideoPlayerProps {
  videoUrl: string
  title: string
  onTimeUpdate?: (currentTime: number, duration: number) => void
  onPlayStateChange?: (isPlaying: boolean) => void
  onVideoEnd?: () => void
  /** Seconds to seek to once metadata loads (resume support). Applied
   *  once per videoUrl change, not re-applied on every re-render. */
  startAt?: number
}

export default function VideoPlayer({
  videoUrl,
  title,
  onTimeUpdate,
  onPlayStateChange,
  onVideoEnd,
  startAt,
}: VideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const youtubePlayerRef = useRef<{
    getCurrentTime?: () => number
    getDuration?: () => number
    playVideo?: () => void
    pauseVideo?: () => void
  } | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const [isPlaying, setIsPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [duration, setDuration] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const [isMuted, setIsMuted] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [showControls, setShowControls] = useState(true)
  const [isBuffering, setIsBuffering] = useState(false)
  const [hasError, setHasError] = useState(false)

  const onTimeUpdateRef = useRef(onTimeUpdate)
  const onPlayStateChangeRef = useRef(onPlayStateChange)
  const onVideoEndRef = useRef(onVideoEnd)

  useEffect(() => {
    onTimeUpdateRef.current = onTimeUpdate
    onPlayStateChangeRef.current = onPlayStateChange
    onVideoEndRef.current = onVideoEnd
  }, [onTimeUpdate, onPlayStateChange, onVideoEnd])

  const controlsTimeout = useRef<NodeJS.Timeout | undefined>(undefined)
  const videoType = useMemo(() => detectVideoType(videoUrl), [videoUrl])
  const embedUrl = useMemo(
    () => (videoType === "youtube" ? getYouTubeEmbedUrl(videoUrl) : ""),
    [videoUrl, videoType]
  )

  const formatTime = (s: number) => {
    if (!s || isNaN(s)) return "0:00"
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    const sec = Math.floor(s % 60)
    if (h > 0)
      return `${h}:${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`
    return `${m}:${sec.toString().padStart(2, "0")}`
  }

  // ── Native HTML5 video events ──
  useEffect(() => {
    if (videoType !== "mp4") return
    const video = videoRef.current
    if (!video) return
    setHasError(false)

    const onUpdate = () => {
      const ct = video.currentTime
      const dur = video.duration || 1
      setProgress((ct / dur) * 100)
      setCurrentTime(ct)
      onTimeUpdate?.(ct, dur)
    }
    const onMeta = () => {
      setDuration(video.duration)
      if (startAt && startAt > 1 && startAt < video.duration - 2) {
        video.currentTime = startAt
      }
    }
    const onEnd = () => {
      setIsPlaying(false)
      onPlayStateChange?.(false)
      onVideoEnd?.()
    }
    const onWaiting = () => setIsBuffering(true)
    const onCanPlay = () => { setIsBuffering(false); setHasError(false) }
    const onError = () => setHasError(true)

    video.addEventListener("timeupdate", onUpdate)
    video.addEventListener("loadedmetadata", onMeta)
    video.addEventListener("ended", onEnd)
    video.addEventListener("waiting", onWaiting)
    video.addEventListener("canplay", onCanPlay)
    video.addEventListener("error", onError)

    return () => {
      video.removeEventListener("timeupdate", onUpdate)
      video.removeEventListener("loadedmetadata", onMeta)
      video.removeEventListener("ended", onEnd)
      video.removeEventListener("waiting", onWaiting)
      video.removeEventListener("canplay", onCanPlay)
      video.removeEventListener("error", onError)
    }
  }, [videoType, videoUrl, onTimeUpdate, onPlayStateChange, onVideoEnd, startAt])
  useEffect(() => {
    if (videoType !== "youtube") return

    let cancelled = false

    const handleStateChange = (event: { data: number }) => {
      if (cancelled) return
      const state = event.data

      if (state === 0) {
        // ENDED
        setIsPlaying(false)
        setProgress(100)
        const player = youtubePlayerRef.current
        const current = player?.getCurrentTime?.() ?? 0
        const dur = player?.getDuration?.() ?? duration
        setCurrentTime(current)
        setDuration(dur)
        onTimeUpdateRef.current?.(current, dur)
        onPlayStateChangeRef.current?.(false)
        onVideoEndRef.current?.()
        return
      }
      if (state === 1) {
        // PLAYING
        setIsPlaying(true)
        onPlayStateChangeRef.current?.(true)
        return
      }
      if (state === 2) {
        // PAUSED
        setIsPlaying(false)
        onPlayStateChangeRef.current?.(false)
      }
    }

    const createPlayer = () => {
      if (cancelled || !iframeRef.current || !window.YT?.Player) return
      youtubePlayerRef.current = new window.YT.Player(iframeRef.current, {
        events: {
          onReady: () => {
            if (cancelled) return
            const player = youtubePlayerRef.current
            const dur = player?.getDuration?.() ?? 0
            if (dur > 0) {
              setDuration(dur)
              onTimeUpdateRef.current?.(0, dur)
            }
          },
          onStateChange: handleStateChange,
        },
      })
    }

    if (window.YT?.Player) {
      createPlayer()
    } else {
      const previousReady = window.onYouTubeIframeAPIReady
      window.onYouTubeIframeAPIReady = () => {
        previousReady?.()
        createPlayer()
      }
      const existingScript = document.querySelector('script[src="https://www.youtube.com/iframe_api"]')
      if (!existingScript) {
        const script = document.createElement("script")
        script.src = "https://www.youtube.com/iframe_api"
        script.async = true
        document.body.appendChild(script)
      }
    }

    return () => {
      cancelled = true
      youtubePlayerRef.current = null
    }
  }, [videoType, videoUrl])

  // ── YouTube currentTime synchronization ──
  useEffect(() => {
    if (videoType !== "youtube" || !isPlaying) return
    const interval = window.setInterval(() => {
      const player = youtubePlayerRef.current
      const current = player?.getCurrentTime?.()
      const dur = player?.getDuration?.()
      if (
        typeof current !== "number" || !Number.isFinite(current) ||
        typeof dur !== "number" || !Number.isFinite(dur) || dur <= 0
      ) return
      setCurrentTime(current)
      setDuration(dur)
      setProgress((current / dur) * 100)
      onTimeUpdateRef.current?.(current, dur)
    }, 500)
    return () => window.clearInterval(interval)
  }, [videoType, isPlaying])

  const togglePlay = useCallback(() => {
    if (videoType === "mp4") {
      const video = videoRef.current
      if (!video) return
      if (isPlaying) {
        video.pause()
      } else {
        video.play().catch(() => setHasError(true))
      }
      const next = !isPlaying
      setIsPlaying(next)
      onPlayStateChange?.(next)
      return
    }
    if (videoType === "youtube") {
      if (isPlaying) {
        youtubePlayerRef.current?.pauseVideo?.()
      } else {
        youtubePlayerRef.current?.playVideo?.()
      }
    }
  }, [isPlaying, videoType, onPlayStateChange])

  const seek = (offset: number) => {
    if (videoType === "mp4" && videoRef.current)
      videoRef.current.currentTime += offset
  }

  const handleProgressClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (videoType !== "mp4" || !videoRef.current) return
    const rect = e.currentTarget.getBoundingClientRect()
    const pct = (e.clientX - rect.left) / rect.width
    videoRef.current.currentTime = pct * (videoRef.current.duration || 0)
  }

  const toggleMute = () => {
    if (videoType === "mp4" && videoRef.current)
      videoRef.current.muted = !isMuted
    setIsMuted(!isMuted)
  }

  const toggleFullscreen = () => {
    const el = containerRef.current
    if (!el) return
    if (!isFullscreen) el.requestFullscreen?.()
    else document.exitFullscreen?.()
    setIsFullscreen(!isFullscreen)
  }

  const handleMouseMove = () => {
    setShowControls(true)
    if (controlsTimeout.current) clearTimeout(controlsTimeout.current)
    controlsTimeout.current = setTimeout(() => {
      if (isPlaying) setShowControls(false)
    }, 3000)
  }

  return (
    <motion.div
      ref={containerRef}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5 }}
      className="rounded-2xl overflow-hidden bg-[var(--bg-card)] border border-[var(--border-subtle)] shadow-2xl shadow-black/30"
    >
      {/* Video Container */}
      <div
        className="relative bg-black group"
        onMouseMove={handleMouseMove}
        onMouseLeave={() => isPlaying && setShowControls(false)}
      >
        {videoType === "mp4" && (
          <video
            ref={videoRef}
            src={videoUrl}
            className="w-full aspect-video bg-black"
            playsInline
            crossOrigin="anonymous"
            preload="metadata"
            onClick={togglePlay}
          />
        )}

        {videoType === "youtube" && (
          <iframe
            ref={iframeRef}
            src={embedUrl}
            className="w-full aspect-video bg-black"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
            title={title}
          />
        )}

        {videoType === "embed" && (
          <iframe
            src={videoUrl}
            className="w-full aspect-video bg-black"
            allow="autoplay; fullscreen"
            allowFullScreen
            title={title}
          />
        )}

        {/* Error overlay */}
        {hasError && (
          <div className="absolute inset-0 flex flex-col items-center justify-center bg-black/80 z-20">
            <div className="w-14 h-14 rounded-full bg-red-500/20 flex items-center justify-center mb-3">
              <span className="text-2xl">⚠️</span>
            </div>
            <p className="text-red-400 text-sm font-medium mb-1">Video failed to load</p>
            <p className="text-[var(--text-muted)] text-xs max-w-xs text-center">
              This video could not be loaded. Check the local file exists on the server, or that the YouTube video is available/embeddable.
            </p>
          </div>
        )}

        {/* Buffering */}
        <AnimatePresence>
          {isBuffering && !hasError && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 flex items-center justify-center bg-black/40 z-10"
            >
              <Loader2 size={36} className="text-violet-400 animate-spin" />
            </motion.div>
          )}
        </AnimatePresence>

        {/* Center play (mp4 paused) */}
        {videoType === "mp4" && !isPlaying && !hasError && (
          <motion.div
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            className="absolute inset-0 flex items-center justify-center bg-black/20 z-10 cursor-pointer"
            onClick={togglePlay}
          >
            <motion.div
              whileHover={{ scale: 1.15 }}
              whileTap={{ scale: 0.9 }}
              className="rounded-full bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center shadow-2xl shadow-violet-500/40"
              style={{ width: 72, height: 72 }}
            >
              <Play size={28} className="text-white ml-1" fill="white" />
            </motion.div>
          </motion.div>
        )}

        {/* Controls overlay (mp4) */}
        {videoType === "mp4" && (
          <motion.div
            initial={false}
            animate={{ opacity: showControls ? 1 : 0 }}
            transition={{ duration: 0.2 }}
            className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/90 via-black/50 to-transparent px-4 pb-3 pt-16 z-20"
            onClick={(e) => e.stopPropagation()}
          >
            <div
              onClick={handleProgressClick}
              className="w-full h-1.5 bg-white/15 rounded-full cursor-pointer group/prog mb-3 hover:h-2.5 transition-all duration-200"
            >
              <div
                className="h-full rounded-full relative"
                style={{ width: `${progress}%`, background: "linear-gradient(90deg, #8b5cf6, #a855f7, #ec4899)" }}
              >
                <div className="absolute right-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 rounded-full bg-white shadow-lg shadow-black/30 scale-0 group-hover/prog:scale-100 transition-transform duration-150" />
              </div>
            </div>

            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <motion.button whileTap={{ scale: 0.85 }} onClick={() => seek(-10)} className="w-8 h-8 rounded-lg bg-white/10 flex items-center justify-center text-white/80 hover:bg-white/20 hover:text-white transition-all" title="Rewind 10s">
                  <SkipBack size={15} />
                </motion.button>
                <motion.button whileTap={{ scale: 0.85 }} onClick={togglePlay} className="w-10 h-10 rounded-xl bg-white/15 flex items-center justify-center text-white hover:bg-white/25 transition-all">
                  {isPlaying ? <Pause size={18} /> : <Play size={18} className="ml-0.5" />}
                </motion.button>
                <motion.button whileTap={{ scale: 0.85 }} onClick={() => seek(10)} className="w-8 h-8 rounded-lg bg-white/10 flex items-center justify-center text-white/80 hover:bg-white/20 hover:text-white transition-all" title="Forward 10s">
                  <SkipForward size={15} />
                </motion.button>
                <span className="text-[11px] text-white/60 font-mono ml-2 select-none">
                  {formatTime(currentTime)} / {formatTime(duration)}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <motion.button whileTap={{ scale: 0.85 }} onClick={toggleMute} className="w-8 h-8 rounded-lg bg-white/10 flex items-center justify-center text-white/80 hover:bg-white/20 hover:text-white transition-all">
                  {isMuted ? <VolumeX size={15} /> : <Volume2 size={15} />}
                </motion.button>
                <motion.button whileTap={{ scale: 0.85 }} onClick={toggleFullscreen} className="w-8 h-8 rounded-lg bg-white/10 flex items-center justify-center text-white/80 hover:bg-white/20 hover:text-white transition-all">
                  {isFullscreen ? <Minimize size={15} /> : <Maximize size={15} />}
                </motion.button>
              </div>
            </div>
          </motion.div>
        )}
      </div>

      {/* Title bar */}
      <div className="px-5 py-3 border-t border-[var(--border-subtle)] flex items-center justify-between">
        <div className="flex items-center gap-3 min-w-0">
          <div className={`w-2 h-2 rounded-full shrink-0 ${isPlaying ? "bg-green-500 animate-pulse" : "bg-[var(--text-muted)]"}`} />
          <h3 className="text-sm font-semibold text-[var(--text-primary)] truncate">{title}</h3>
        </div>
        <span className="text-[10px] font-mono text-[var(--text-muted)] bg-[var(--bg-elevated)] px-2 py-0.5 rounded-md shrink-0 ml-2">
          {videoType.toUpperCase()}
        </span>
      </div>
    </motion.div>
  )
}