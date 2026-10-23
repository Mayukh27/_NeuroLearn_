// ============================================================
// Admin content-management API client (V2, Phase 4)
//
// Deliberately separate from lib/api.ts: admin auth is a single shared
// X-Admin-Key header (see backend/services/admin_auth.py), not the
// participant JWT session that apiFetch()/lib/auth.ts manage. Routing
// admin calls through the same helper would mean an admin 401 gets
// treated as "student session expired" and triggers a token-refresh /
// redirect flow that has nothing to do with admin auth — kept separate
// to avoid that, and so the admin surface stays easy to reason about
// on its own.
//
// The admin key is kept in sessionStorage only (cleared when the tab
// closes) — never sent anywhere except this LAN server, never persisted
// to localStorage/cookies.
// ============================================================

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api"
const ADMIN_KEY_STORAGE = "neurolearn_admin_key"

export function getAdminKey(): string {
  if (typeof window === "undefined") return ""
  return sessionStorage.getItem(ADMIN_KEY_STORAGE) || ""
}

export function setAdminKey(key: string) {
  if (typeof window === "undefined") return
  sessionStorage.setItem(ADMIN_KEY_STORAGE, key)
}

export function clearAdminKey() {
  if (typeof window === "undefined") return
  sessionStorage.removeItem(ADMIN_KEY_STORAGE)
}

export class AdminAuthError extends Error {
  constructor(msg = "Invalid or missing admin key") {
    super(msg)
    this.name = "AdminAuthError"
  }
}

async function adminFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Admin-Key": getAdminKey(),
      ...options?.headers,
    },
  })

  if (res.status === 401 || res.status === 403) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail || detail
    } catch {}
    throw new AdminAuthError(detail)
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail || detail
    } catch {}
    throw new Error(`Admin API ${res.status}: ${detail}`)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ── Local video library (backs /api/videos) ──

export interface AdminLocalVideo {
  id: string
  title: string
  relative_path: string
  course_id: string | null
  duration_seconds: number | null
  file_size_bytes: number | null
  order: number | null
}

export async function adminListVideos(): Promise<AdminLocalVideo[]> {
  return adminFetch<AdminLocalVideo[]>("/videos/", { method: "GET" })
}

export async function adminRegisterVideo(input: {
  title: string
  relative_path: string
}): Promise<AdminLocalVideo> {
  return adminFetch<AdminLocalVideo>("/videos/", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export async function adminDeleteVideo(id: string): Promise<void> {
  await adminFetch<void>(`/videos/${id}`, { method: "DELETE" })
}

// ── Courses (backs /api/courses) ──

export interface AdminVideoLink {
  id: string
  local_video_id?: string
  title: string
  url: string
  duration: number
  order: number
}

export interface AdminCourse {
  id: string
  title: string
  description: string
  icon: string
  category: string
  difficulty: string
  total_videos: number
  completed_videos: number
  progress: number
  estimated_hours: number
  tags: string[]
  video_links: AdminVideoLink[]
}

export async function adminListCourses(): Promise<AdminCourse[]> {
  return adminFetch<AdminCourse[]>("/courses/", { method: "GET" })
}

export async function adminCreateCourse(input: {
  title: string
  description?: string
  icon?: string
  category?: string
  difficulty?: string
  estimated_hours?: number
  tags?: string[]
}): Promise<AdminCourse> {
  return adminFetch<AdminCourse>("/courses/", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export interface ImportCourseFile {
  file: File
  relativePath: string
}

/** POST /api/courses/import — multipart, so this deliberately does NOT
 *  go through adminFetch (which forces Content-Type: application/json;
 *  a multipart request needs the browser to set its own boundary).
 *  Uses XMLHttpRequest instead of fetch so real upload progress
 *  (course video folders can be large) is available via onProgress. */
export function adminImportCourseFolder(
  meta: {
    title: string
    description?: string
    icon?: string
    category?: string
    difficulty?: string
    estimated_hours?: number
    tags?: string[]
  },
  files: ImportCourseFile[],
  onProgress?: (fractionComplete: number) => void
): Promise<AdminCourse> {
  const form = new FormData()
  form.set("title", meta.title)
  form.set("description", meta.description ?? "")
  form.set("icon", meta.icon ?? "📚")
  form.set("category", meta.category ?? "General")
  form.set("difficulty", meta.difficulty ?? "Beginner")
  form.set("estimated_hours", String(meta.estimated_hours ?? 0))
  form.set("tags", JSON.stringify(meta.tags ?? []))
  for (const { file, relativePath } of files) {
    form.append("files", file, file.name)
    form.append("relative_paths", relativePath)
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${API_BASE}/courses/import`)
    xhr.setRequestHeader("X-Admin-Key", getAdminKey())

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status === 401 || xhr.status === 403) {
        reject(new AdminAuthError())
        return
      }
      let body: any = null
      try {
        body = JSON.parse(xhr.responseText)
      } catch {}
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as AdminCourse)
      } else {
        reject(new Error(body?.detail || `Import failed (${xhr.status})`))
      }
    }
    xhr.onerror = () => reject(new Error("Network error during course import."))
    xhr.send(form)
  })
}

export async function adminDeleteCourse(courseId: string): Promise<void> {
  await adminFetch<void>(`/courses/${courseId}`, { method: "DELETE" })
}

export async function adminAttachVideo(
  courseId: string,
  localVideoId: string
): Promise<AdminCourse> {
  return adminFetch<AdminCourse>(`/courses/${courseId}/videos/${localVideoId}`, {
    method: "POST",
  })
}

export async function adminAttachYoutubeVideo(
  courseId: string,
  input: { title: string; youtube_url: string; order?: number }
): Promise<AdminCourse> {
  return adminFetch<AdminCourse>(`/courses/${courseId}/videos/youtube`, {
    method: "POST",
    body: JSON.stringify(input),
  })
}

export async function adminDetachVideo(
  courseId: string,
  videoLinkId: string
): Promise<AdminCourse> {
  return adminFetch<AdminCourse>(`/courses/${courseId}/video-links/${videoLinkId}`, {
    method: "DELETE",
  })
}
