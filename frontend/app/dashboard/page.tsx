"use client"

import { useEffect, useState } from "react"
import { motion } from "framer-motion"
import TopicCard from "@/components/TopicCard"
import CRSTrendWidget from "@/components/CRSTrendWidget"
import {
  fetchCourses,
  fetchStudentProfile,
  fetchDailyChallenges,
  fetchCrsHistory,
  type Course,
  type StudentProfile,
  type DailyChallenge,
  type CrsHistoryEntry,
} from "@/lib/api"
import { Trophy, Target, Flame, Clock, BookOpen, Zap, CheckCircle2, Circle } from "lucide-react"

export default function DashboardPage() {
  const [courses, setCourses] = useState<Course[]>([])
  const [student, setStudent] = useState<StudentProfile | null>(null)
  const [challenges, setChallenges] = useState<DailyChallenge[]>([])
  const [crsHistory, setCrsHistory] = useState<CrsHistoryEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)


  useEffect(() => {
    Promise.all([
      fetchCourses(),
      fetchStudentProfile(),
      fetchDailyChallenges(),
      // FIX (auth request): this used to hardcode "student_001" directly,
      // bypassing the real-user default now built into fetchCrsHistory's
      // signature. Backend enforces student_id === the authenticated
      // caller anyway (403 otherwise), so passing anything else would
      // just fail — omit the argument entirely and let it resolve to the
      // real logged-in user.
      fetchCrsHistory(undefined, 10).catch(() => []), // isolated: no CRS history yet shouldn't block the dashboard
    ])
      .then(([c, s, ch, crs]) => {
        setCourses(c)
        setStudent(s)
        setChallenges(ch)
        setCrsHistory(crs)
        setIsLoading(false)
      })
      .catch(() => {
        setIsLoading(false)
      })
  }, [])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <motion.div animate={{ rotate: 360 }} transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }} className="w-10 h-10 border-3 border-violet-500 border-t-transparent rounded-full" />
      </div>
    )
  }

  const totalCompleted = courses.filter((c) => c.progress === 100).length
  // FIX (phase-2 request): was estimatedHours * progress% (an estimate,
  // not a measurement). student.totalWatchTime is now a real backend
  // value, persisted minute-by-minute as videos are actually completed
  // (see backend data/database.py: record_completed_video).
  const totalWatchHours = Math.round((student?.totalWatchTime || 0) / 60)

  return (
    <div className="p-6 max-w-[1400px] mx-auto space-y-8">
      {/* Welcome Header */}
      <motion.div initial={{ opacity: 0, y: -20 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-3xl font-bold text-[var(--text-primary)] mb-1">
          Welcome back, <span className="gradient-text">{student?.name?.split(" ")[0]}</span>
        </h1>
        <p className="text-[var(--text-muted)]">Continue your adaptive learning journey</p>
      </motion.div>

      {/* Quick Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { icon: Zap, label: "Total XP", value: student?.xp?.toLocaleString() || "0", color: "text-violet-400", bg: "from-violet-500/10 to-purple-500/10" },
          { icon: Flame, label: "Streak", value: `${student?.streak || 0} days`, color: "text-orange-400", bg: "from-orange-500/10 to-red-500/10" },
          { icon: BookOpen, label: "Completed", value: `${totalCompleted}/${courses.length}`, color: "text-emerald-400", bg: "from-emerald-500/10 to-cyan-500/10" },
          { icon: Clock, label: "Study Time", value: `${totalWatchHours}h`, color: "text-blue-400", bg: "from-blue-500/10 to-indigo-500/10" },
        ].map((stat, i) => (
          <motion.div
            key={stat.label}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.1 }}
            whileHover={{ y: -2 }}
            className={`rounded-xl p-4 bg-gradient-to-br ${stat.bg} border border-[var(--border-subtle)] hover:border-[var(--border-default)] transition-all`}
          >
            <stat.icon size={20} className={stat.color} />
            <p className="text-2xl font-bold text-[var(--text-primary)] mt-2">{stat.value}</p>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">{stat.label}</p>
          </motion.div>
        ))}
      </div>

      {/* Main Grid: Courses + Sidebar */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Courses Grid */}
        <div className="lg:col-span-3">
          <div className="flex items-center justify-between mb-5">
            <h2 className="text-xl font-bold text-[var(--text-primary)]">All Courses</h2>
            <span className="text-xs text-[var(--text-muted)] bg-[var(--bg-elevated)] px-3 py-1 rounded-full">
              {courses.length} courses
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-5">
            {courses.map((course, index) => (
              <TopicCard key={course.id} course={course} index={index} />
            ))}
          </div>
        </div>

        {/* Right Sidebar */}
        <div className="space-y-5">
          {/* Cognitive Readiness — NeuroLearn-MCL (Phase 13) */}
          <CRSTrendWidget history={crsHistory} />

          {/* Daily Challenges */}
          <motion.div
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.3 }}
            className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-5"
          >
            <div className="flex items-center gap-2 mb-4">
              <Target size={18} className="text-cyan-400" />
              <h3 className="text-sm font-bold text-[var(--text-primary)]">Daily Challenges</h3>
            </div>
            <div className="space-y-3">
              {challenges.map((ch) => (
                <motion.div
                  key={ch.id}
                  whileHover={{ x: 2 }}
                  className={`flex items-start gap-3 p-3 rounded-xl transition-all ${ch.completed ? "bg-emerald-500/5 border border-emerald-500/10" : "bg-[var(--bg-elevated)] border border-[var(--border-subtle)]"}`}
                >
                  {ch.completed ? (
                    <CheckCircle2 size={16} className="text-emerald-400 shrink-0 mt-0.5" />
                  ) : (
                    <Circle size={16} className="text-[var(--text-muted)] shrink-0 mt-0.5" />
                  )}
                  <div className="flex-1 min-w-0">
                    <p className={`text-xs font-semibold ${ch.completed ? "text-emerald-400 line-through" : "text-[var(--text-primary)]"}`}>
                      {ch.title}
                    </p>
                    <p className="text-[10px] text-[var(--text-muted)] mt-0.5">{ch.description}</p>
                    {!ch.completed && (
                      <div className="mt-2">
                        <div className="w-full h-1 rounded-full bg-[var(--bg-primary)] overflow-hidden">
                          <div
                            className="h-full rounded-full progress-animated"
                            style={{ width: `${(ch.progress / ch.target) * 100}%` }}
                          />
                        </div>
                        <p className="text-[10px] text-[var(--text-muted)] mt-1">
                          {ch.progress}/{ch.target}
                        </p>
                      </div>
                    )}
                  </div>
                  <span className="text-[10px] font-bold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-full shrink-0">
                    +{ch.xpReward} XP
                  </span>
                </motion.div>
              ))}
            </div>
          </motion.div>

          {/* Achievements Preview */}
          <motion.div
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.4 }}
            className="rounded-2xl bg-[var(--bg-card)] border border-[var(--border-subtle)] p-5"
          >
            <div className="flex items-center gap-2 mb-4">
              <Trophy size={18} className="text-amber-400" />
              <h3 className="text-sm font-bold text-[var(--text-primary)]">Achievements</h3>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {student?.badges.slice(0, 8).map((badge, i) => (
                <motion.div
                  key={badge.id}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.4 + i * 0.05 }}
                  whileHover={badge.earned ? { scale: 1.15, rotate: 5 } : {}}
                  className={`relative aspect-square rounded-xl flex items-center justify-center text-xl cursor-pointer transition-all ${badge.earned
                      ? "bg-gradient-to-br from-violet-500/15 to-purple-500/15 border border-violet-500/30"
                      : "bg-[var(--bg-elevated)] border border-[var(--border-subtle)] opacity-40 grayscale"
                    }`}
                  title={badge.name}
                >
                  {badge.icon}
                  {badge.earned && (
                    <div className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-emerald-500 flex items-center justify-center">
                      <span className="text-[7px] text-white font-bold">✓</span>
                    </div>
                  )}
                </motion.div>
              ))}
            </div>
            <div className="mt-3 pt-3 border-t border-[var(--border-subtle)]">
              <div className="w-full h-1.5 rounded-full bg-[var(--bg-primary)] overflow-hidden">
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${((student?.badges.filter((b) => b.earned).length || 0) / (student?.badges.length || 1)) * 100}%` }}
                  className="h-full rounded-full progress-animated"
                />
              </div>
              <p className="text-[10px] text-[var(--text-muted)] mt-1.5 text-center">
                {student?.badges.filter((b) => b.earned).length}/{student?.badges.length} earned
              </p>
            </div>
          </motion.div>
        </div>
      </div>
    </div>
  )
}
