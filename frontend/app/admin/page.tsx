"use client"

import { useState, useEffect, useCallback, useRef } from "react"
import { motion, AnimatePresence } from "framer-motion"
import {
  Settings,
  Plus,
  Trash2,
  Link2,
  Unlink,
  Film,
  BookOpen,
  KeyRound,
  AlertCircle,
  CheckCircle2,
  FolderUp,
  UploadCloud,
  X,
} from "lucide-react"

import {
  getAdminKey,
  setAdminKey,
  clearAdminKey,
  AdminAuthError,
  adminListCourses,
  adminCreateCourse,
  adminDeleteCourse,
  adminImportCourseFolder,
  type ImportCourseFile,
  adminListVideos,
  adminRegisterVideo,
  adminDeleteVideo,
  adminAttachVideo,
  adminAttachYoutubeVideo,
  adminDetachVideo,
  type AdminCourse,
  type AdminLocalVideo,
} from "@/lib/adminApi"

const ALLOWED_IMPORT_EXTENSIONS = [".mp4", ".webm"]

function isSupportedVideoFile(name: string): boolean {
  const lower = name.toLowerCase()
  return ALLOWED_IMPORT_EXTENSIONS.some((ext) => lower.endsWith(ext))
}

/** Reads every File in a dropped/selected folder, via either the
 *  `<input webkitdirectory>` FileList (has `.webkitRelativePath` on
 *  each File already) or a DataTransfer from drag/drop (needs walking
 *  each DataTransferItem's FileSystemEntry tree to get the same info).
 *  Non-video files (thumbs.db, .DS_Store, subtitle files, etc.) are
 *  filtered out here so the preview only ever shows what will actually
 *  be imported. */
async function readDroppedFolder(dataTransfer: DataTransfer): Promise<ImportCourseFile[]> {
  const out: ImportCourseFile[] = []

  async function walk(entry: any, prefix: string): Promise<void> {
    if (entry.isFile) {
      await new Promise<void>((resolve) => {
        entry.file((file: File) => {
          if (isSupportedVideoFile(file.name)) {
            out.push({ file, relativePath: prefix + file.name })
          }
          resolve()
        }, () => resolve())
      })
    } else if (entry.isDirectory) {
      const reader = entry.createReader()
      const entries: any[] = await new Promise((resolve) => {
        const acc: any[] = []
        const readBatch = () => {
          reader.readEntries((batch: any[]) => {
            if (batch.length === 0) {
              resolve(acc)
            } else {
              acc.push(...batch)
              readBatch()
            }
          }, () => resolve(acc))
        }
        readBatch()
      })
      for (const child of entries) {
        await walk(child, prefix + entry.name + "/")
      }
    }
  }

  const items = Array.from(dataTransfer.items)
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean)
  for (const entry of entries) {
    await walk(entry, "")
  }
  return out
}

function filesFromInput(fileList: FileList): ImportCourseFile[] {
  return Array.from(fileList)
    .filter((f) => isSupportedVideoFile(f.name))
    .map((f) => ({
      file: f,
      // webkitRelativePath is "FolderName/Module 1/video.mp4" — strip the
      // top-level folder name itself, since that's the course folder, not
      // part of the module/file structure inside it.
      relativePath: (f as any).webkitRelativePath?.split("/").slice(1).join("/") || f.name,
    }))
}

/**
 * /admin — Content Manager (V2, Phase 4)
 *
 * V2 removes all YouTube content and every hardcoded/student-pasted
 * video URL. Every course and every video in the catalog is created and
 * removed here, by whoever holds the admin key — nowhere else.
 */
export default function AdminPage() {
  const [keyInput, setKeyInput] = useState("")
  const [unlocked, setUnlocked] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [courses, setCourses] = useState<AdminCourse[]>([])
  const [videos, setVideos] = useState<AdminLocalVideo[]>([])
  const [loading, setLoading] = useState(false)

  const [newCourseTitle, setNewCourseTitle] = useState("")
  const [newCourseCategory, setNewCourseCategory] = useState("")
  const [newVideoTitle, setNewVideoTitle] = useState("")
  const [newVideoPath, setNewVideoPath] = useState("")
  const [attachChoice, setAttachChoice] = useState<Record<string, string>>({})
  const [attachSource, setAttachSource] = useState<Record<string, "local" | "youtube">>({})
  const [ytTitle, setYtTitle] = useState<Record<string, string>>({})
  const [ytUrl, setYtUrl] = useState<Record<string, string>>({})

  // ── Bulk course folder import ──
  const [importFiles, setImportFiles] = useState<ImportCourseFile[]>([])
  const [importTitle, setImportTitle] = useState("")
  const [importCategory, setImportCategory] = useState("")
  const [importDragOver, setImportDragOver] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importProgress, setImportProgress] = useState(0)
  const folderInputRef = useRef<HTMLInputElement>(null)

  const applyPickedFiles = (files: ImportCourseFile[]) => {
    if (files.length === 0) {
      setError("The selected folder does not contain any supported video files (.mp4, .webm).")
      return
    }
    setImportFiles(files)
    if (!importTitle.trim()) {
      // Detect the course folder name from the first file's own
      // (pre-strip) path — prefilled, but the admin can still edit it
      // before confirming the import (spec: "prefill... but allow editing").
      const first = files[0]
      const guess = (first.file as any).webkitRelativePath?.split("/")[0]
      if (guess) setImportTitle(guess)
    }
  }

  const handleFolderInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) applyPickedFiles(filesFromInput(e.target.files))
    e.target.value = "" // allow re-selecting the same folder later
  }

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setImportDragOver(false)
    if (!e.dataTransfer.items?.length) return
    const files = await readDroppedFolder(e.dataTransfer)
    applyPickedFiles(files)
  }

  const cancelImport = () => {
    setImportFiles([])
    setImportTitle("")
    setImportCategory("")
    setImportProgress(0)
  }

  const handleImportCourse = async () => {
    if (!importTitle.trim() || importFiles.length === 0) return
    setImporting(true)
    setImportProgress(0)
    setError(null)
    try {
      await adminImportCourseFolder(
        { title: importTitle.trim(), category: importCategory.trim() || "General" },
        importFiles,
        (fraction) => setImportProgress(fraction)
      )
      flash(`Imported "${importTitle.trim()}" — ${importFiles.length} video${importFiles.length === 1 ? "" : "s"}`)
      cancelImport()
      await refresh()
    } catch (e) {
      handleAuthError(e)
    } finally {
      setImporting(false)
    }
  }

  const flash = (msg: string) => {
    setNotice(msg)
    setTimeout(() => setNotice(null), 3000)
  }

  const handleAuthError = (e: unknown) => {
    if (e instanceof AdminAuthError) {
      setUnlocked(false)
      clearAdminKey()
      setError("Admin key rejected — enter it again.")
      return true
    }
    setError(e instanceof Error ? e.message : String(e))
    return false
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [c, v] = await Promise.all([adminListCourses(), adminListVideos()])
      setCourses(c)
      setVideos(v)
    } catch (e) {
      handleAuthError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const existing = getAdminKey()
    if (existing) {
      setUnlocked(true)
      refresh()
    }
  }, [refresh])

  const handleUnlock = async () => {
    if (!keyInput.trim()) return
    setError(null)
    // Stage the candidate key so the authenticated fetch calls below send it,
    // but do NOT flip `unlocked` (and therefore render admin content) until
    // the backend has actually accepted it. adminListCourses()/adminListVideos()
    // are real admin-protected endpoints (require_admin), so a wrong key
    // surfaces as AdminAuthError here and we stay locked.
    setAdminKey(keyInput.trim())
    setLoading(true)
    try {
      const [c, v] = await Promise.all([adminListCourses(), adminListVideos()])
      setCourses(c)
      setVideos(v)
      setUnlocked(true)
    } catch (e) {
      clearAdminKey()
      setUnlocked(false)
      if (e instanceof AdminAuthError) {
        setError("Invalid admin key.")
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setLoading(false)
    }
  }

  const handleLock = () => {
    clearAdminKey()
    setUnlocked(false)
    setKeyInput("")
  }

  const handleCreateCourse = async () => {
    if (!newCourseTitle.trim()) return
    try {
      await adminCreateCourse({
        title: newCourseTitle.trim(),
        category: newCourseCategory.trim() || "General",
      })
      setNewCourseTitle("")
      setNewCourseCategory("")
      flash("Course created")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleDeleteCourse = async (courseId: string) => {
    try {
      await adminDeleteCourse(courseId)
      flash("Course deleted")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleRegisterVideo = async () => {
    if (!newVideoTitle.trim() || !newVideoPath.trim()) return
    try {
      await adminRegisterVideo({ title: newVideoTitle.trim(), relative_path: newVideoPath.trim() })
      setNewVideoTitle("")
      setNewVideoPath("")
      flash("Video registered")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleDeleteVideo = async (id: string) => {
    try {
      await adminDeleteVideo(id)
      flash("Video removed from library")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleAttach = async (courseId: string) => {
    const localVideoId = attachChoice[courseId]
    if (!localVideoId) return
    try {
      await adminAttachVideo(courseId, localVideoId)
      flash("Video attached to course")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleAttachYoutube = async (courseId: string) => {
    const title = (ytTitle[courseId] || "").trim()
    const url = (ytUrl[courseId] || "").trim()
    if (!title || !url) return
    try {
      await adminAttachYoutubeVideo(courseId, { title, youtube_url: url })
      setYtTitle((prev) => ({ ...prev, [courseId]: "" }))
      setYtUrl((prev) => ({ ...prev, [courseId]: "" }))
      flash("YouTube video attached to course")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const handleDetach = async (courseId: string, videoLinkId: string) => {
    try {
      await adminDetachVideo(courseId, videoLinkId)
      flash("Video detached from course")
      await refresh()
    } catch (e) {
      handleAuthError(e)
    }
  }

  const unattachedVideos = (courseId: string) => {
    const attachedIds = new Set(
      (courses.find((c) => c.id === courseId)?.video_links || []).map((v) => v.local_video_id)
    )
    return videos.filter((v) => !attachedIds.has(v.id))
  }

  // ── Locked state: ask for the admin key ──
  if (!unlocked) {
    return (
      <div className="p-6 max-w-md mx-auto mt-20">
        <div className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-6 space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500/30 to-purple-600/30 border border-violet-500/20 flex items-center justify-center">
              <KeyRound size={18} className="text-violet-400" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-[var(--text-primary)]">Admin Access</h1>
              <p className="text-xs text-[var(--text-muted)]">Content management is gated by the server's admin key.</p>
            </div>
          </div>
          <input
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleUnlock()}
            placeholder="Admin key"
            className="w-full px-4 py-2.5 text-sm rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
          />
          {error && (
            <p className="text-xs text-red-400 flex items-center gap-1.5"><AlertCircle size={13} /> {error}</p>
          )}
          <button
            onClick={handleUnlock}
            disabled={!keyInput.trim()}
            className="w-full px-4 py-2.5 text-sm font-semibold rounded-xl bg-gradient-to-r from-violet-500 to-purple-600 text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Unlock
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-[900px] mx-auto space-y-8">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500/30 to-purple-600/30 border border-violet-500/20 flex items-center justify-center">
            <Settings size={20} className="text-violet-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-[var(--text-primary)]">Content Manager</h1>
            <p className="text-sm text-[var(--text-muted)]">
              All courses and videos come from here — no YouTube, no ad-hoc URLs.
            </p>
          </div>
        </div>
        <button
          onClick={handleLock}
          className="text-xs font-medium text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
        >
          Lock
        </button>
      </div>

      <AnimatePresence>
        {notice && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-green-500/10 border border-green-500/20 text-green-400 text-sm"
          >
            <CheckCircle2 size={15} /> {notice}
          </motion.div>
        )}
      </AnimatePresence>
      {error && (
        <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
          <AlertCircle size={15} /> {error}
        </div>
      )}

      {/* Local video library */}
      <section className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-5 space-y-4">
        <div className="flex items-center gap-2">
          <Film size={16} className="text-violet-400" />
          <h2 className="text-sm font-bold text-[var(--text-primary)]">Video Library</h2>
          <span className="text-xs text-[var(--text-muted)]">({videos.length})</span>
        </div>
        <p className="text-xs text-[var(--text-muted)]">
          Register a video file already placed on the server under <code>VIDEO_STORAGE_DIR</code> — this does not upload anything.
        </p>
        <div className="flex flex-wrap gap-2">
          <input
            value={newVideoTitle}
            onChange={(e) => setNewVideoTitle(e.target.value)}
            placeholder="Title"
            className="flex-1 min-w-[160px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
          />
          <input
            value={newVideoPath}
            onChange={(e) => setNewVideoPath(e.target.value)}
            placeholder="relative/path/lecture-01.mp4"
            className="flex-1 min-w-[220px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
          />
          <button
            onClick={handleRegisterVideo}
            disabled={!newVideoTitle.trim() || !newVideoPath.trim()}
            className="px-3 py-2 text-sm font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
          >
            <Plus size={14} /> Register
          </button>
        </div>
        <div className="space-y-1.5">
          {videos.map((v) => (
            <div key={v.id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-elevated)] text-sm">
              <div className="min-w-0">
                <p className="text-[var(--text-primary)] truncate">{v.title}</p>
                <p className="text-[10px] font-mono text-[var(--text-muted)] truncate">{v.relative_path}</p>
              </div>
              <button onClick={() => handleDeleteVideo(v.id)} className="shrink-0 text-[var(--text-muted)] hover:text-red-400 transition-colors p-1">
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {videos.length === 0 && !loading && (
            <p className="text-xs text-[var(--text-muted)] italic">No videos registered yet.</p>
          )}
        </div>
      </section>

      {/* Import Course Folder */}
      <section className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-5 space-y-4">
        <div className="flex items-center gap-2">
          <FolderUp size={16} className="text-violet-400" />
          <h2 className="text-sm font-bold text-[var(--text-primary)]">Import Course Folder</h2>
        </div>
        <p className="text-xs text-[var(--text-muted)]">
          Drop or browse an entire course folder — every video file inside is uploaded and
          registered, and one new course is created with them all attached, in a single step.
          Individual videos can still be added or removed afterward from the Courses list below.
        </p>

        {importFiles.length === 0 ? (
          <div
            onDragOver={(e) => { e.preventDefault(); setImportDragOver(true) }}
            onDragLeave={() => setImportDragOver(false)}
            onDrop={handleDrop}
            className={`rounded-xl border-2 border-dashed p-8 flex flex-col items-center gap-3 text-center transition-colors ${
              importDragOver
                ? "border-violet-500/60 bg-violet-500/5"
                : "border-[var(--border-subtle)]"
            }`}
          >
            <UploadCloud size={28} className="text-[var(--text-muted)]" />
            <p className="text-sm text-[var(--text-secondary)]">Drop course folder here</p>
            <p className="text-xs text-[var(--text-muted)]">or</p>
            <button
              onClick={() => folderInputRef.current?.click()}
              className="px-3 py-2 text-sm font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 flex items-center gap-1.5"
            >
              <FolderUp size={14} /> Browse Folder
            </button>
            {/* webkitdirectory: the standard (if non-standard-named) way to
                let an <input type="file"> pick a whole folder — the
                fallback path required whenever native drag/drop of a
                folder isn't supported by the browser. */}
            <input
              ref={folderInputRef}
              type="file"
              // @ts-ignore — non-standard but universally supported attribute
              webkitdirectory=""
              directory=""
              multiple
              className="hidden"
              onChange={handleFolderInputChange}
            />
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <input
                value={importTitle}
                onChange={(e) => setImportTitle(e.target.value)}
                placeholder="Course title"
                className="flex-1 min-w-[160px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
              />
              <input
                value={importCategory}
                onChange={(e) => setImportCategory(e.target.value)}
                placeholder="Category (optional)"
                className="flex-1 min-w-[160px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
              />
            </div>

            <div className="rounded-xl bg-[var(--bg-elevated)] p-3 max-h-56 overflow-y-auto space-y-1">
              <p className="text-xs font-semibold text-[var(--text-secondary)] mb-2">
                {importFiles.length} video{importFiles.length === 1 ? "" : "s"} detected
              </p>
              {importFiles.map((f, i) => (
                <div key={i} className="text-xs text-[var(--text-muted)] font-mono truncate">
                  {f.relativePath}
                </div>
              ))}
            </div>

            {importing && (
              <div className="h-1.5 rounded-full bg-[var(--bg-elevated)] overflow-hidden">
                <div
                  className="h-full bg-violet-500 transition-all"
                  style={{ width: `${Math.round(importProgress * 100)}%` }}
                />
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={cancelImport}
                disabled={importing}
                className="px-3 py-2 text-sm font-semibold rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-secondary)] disabled:opacity-40 flex items-center gap-1.5"
              >
                <X size={14} /> Cancel
              </button>
              <button
                onClick={handleImportCourse}
                disabled={importing || !importTitle.trim()}
                className="px-3 py-2 text-sm font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 disabled:opacity-40 flex items-center gap-1.5"
              >
                <FolderUp size={14} />
                {importing ? `Importing… ${Math.round(importProgress * 100)}%` : "Import Course"}
              </button>
            </div>
          </div>
        )}
      </section>

      {/* Courses */}
      <section className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-5 space-y-4">
        <div className="flex items-center gap-2">
          <BookOpen size={16} className="text-violet-400" />
          <h2 className="text-sm font-bold text-[var(--text-primary)]">Courses</h2>
          <span className="text-xs text-[var(--text-muted)]">({courses.length})</span>
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            value={newCourseTitle}
            onChange={(e) => setNewCourseTitle(e.target.value)}
            placeholder="Course title"
            className="flex-1 min-w-[160px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
          />
          <input
            value={newCourseCategory}
            onChange={(e) => setNewCourseCategory(e.target.value)}
            placeholder="Category (optional)"
            className="flex-1 min-w-[160px] px-3 py-2 text-sm rounded-lg bg-[var(--bg-elevated)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-violet-500/50"
          />
          <button
            onClick={handleCreateCourse}
            disabled={!newCourseTitle.trim()}
            className="px-3 py-2 text-sm font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
          >
            <Plus size={14} /> Create
          </button>
        </div>

        <div className="space-y-3">
          {courses.map((c) => (
            <div key={c.id} className="rounded-xl bg-[var(--bg-elevated)] p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-semibold text-[var(--text-primary)]">{c.title}</p>
                  <p className="text-xs text-[var(--text-muted)]">{c.category} · {c.total_videos} video{c.total_videos === 1 ? "" : "s"}</p>
                </div>
                <button onClick={() => handleDeleteCourse(c.id)} className="text-[var(--text-muted)] hover:text-red-400 transition-colors p-1">
                  <Trash2 size={14} />
                </button>
              </div>

              {c.video_links.length > 0 && (
                <div className="space-y-1">
                  {c.video_links.map((vl) => (
                    <div key={vl.id} className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-[var(--bg-card)] text-xs">
                      <span className="text-[var(--text-secondary)] truncate">{vl.title}</span>
                      <button onClick={() => handleDetach(c.id, vl.id)} className="shrink-0 text-[var(--text-muted)] hover:text-amber-400 transition-colors p-1" title="Detach">
                        <Unlink size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex gap-1.5 p-0.5 rounded-lg bg-[var(--bg-card)] w-fit">
                <button
                  onClick={() => setAttachSource((prev) => ({ ...prev, [c.id]: "local" }))}
                  className={`px-2.5 py-1 text-[11px] font-semibold rounded-md transition-colors ${
                    (attachSource[c.id] || "local") === "local"
                      ? "bg-violet-500/25 text-violet-300"
                      : "text-[var(--text-muted)]"
                  }`}
                >
                  Local
                </button>
                <button
                  onClick={() => setAttachSource((prev) => ({ ...prev, [c.id]: "youtube" }))}
                  className={`px-2.5 py-1 text-[11px] font-semibold rounded-md transition-colors ${
                    attachSource[c.id] === "youtube"
                      ? "bg-violet-500/25 text-violet-300"
                      : "text-[var(--text-muted)]"
                  }`}
                >
                  YouTube
                </button>
              </div>

              {(attachSource[c.id] || "local") === "local" ? (
                <div className="flex gap-2">
                  <select
                    value={attachChoice[c.id] || ""}
                    onChange={(e) => setAttachChoice((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    className="flex-1 px-2.5 py-1.5 text-xs rounded-lg bg-[var(--bg-card)] border border-[var(--border-subtle)] text-[var(--text-primary)]"
                  >
                    <option value="">Attach a video from the library…</option>
                    {unattachedVideos(c.id).map((v) => (
                      <option key={v.id} value={v.id}>{v.title}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => handleAttach(c.id)}
                    disabled={!attachChoice[c.id]}
                    className="px-2.5 py-1.5 text-xs font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                  >
                    <Link2 size={12} /> Attach
                  </button>
                </div>
              ) : (
                <div className="flex flex-wrap gap-2">
                  <input
                    value={ytTitle[c.id] || ""}
                    onChange={(e) => setYtTitle((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    placeholder="Video title"
                    className="flex-1 min-w-[120px] px-2.5 py-1.5 text-xs rounded-lg bg-[var(--bg-card)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  />
                  <input
                    value={ytUrl[c.id] || ""}
                    onChange={(e) => setYtUrl((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    placeholder="https://youtube.com/watch?v=…"
                    className="flex-[2] min-w-[180px] px-2.5 py-1.5 text-xs rounded-lg bg-[var(--bg-card)] border border-[var(--border-subtle)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  />
                  <button
                    onClick={() => handleAttachYoutube(c.id)}
                    disabled={!(ytTitle[c.id] || "").trim() || !(ytUrl[c.id] || "").trim()}
                    className="px-2.5 py-1.5 text-xs font-semibold rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                  >
                    <Link2 size={12} /> Attach
                  </button>
                </div>
              )}
            </div>
          ))}
          {courses.length === 0 && !loading && (
            <p className="text-xs text-[var(--text-muted)] italic">No courses yet — create one above.</p>
          )}
        </div>
      </section>
    </div>
  )
}
