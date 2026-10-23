# NeuroLearn - Adaptive Learning Platform

NeuroLearn is an adaptive learning platform with a Next.js frontend and a
FastAPI backend. It combines course discovery, video learning, transcription,
question generation, attention monitoring, adaptive assessment, gamification,
and report generation into one full-stack learning workflow.

## Features

- Course dashboard, learning sessions, assessments, results, profile, and leaderboard views
- Authentication flows for signup, login, password reset, and current-user state
- Auto course discovery from topic input using scraping and metadata fallbacks
- Multi-source video learning with YouTube, MP4, and URL support
- Webcam attention monitoring with MediaPipe-backed scoring when ML dependencies are installed
- Whisper transcription and FLAN-T5 question generation with dummy fallbacks for local demos
- Cognitive Readiness Score (CRS) for adaptive difficulty selection
- XP, streaks, badges, challenges, notifications, and leaderboard endpoints
- PDF report generation and optional email delivery
- Frontend fallback data mode when the backend is unavailable

## Demo Screens

### Dashboard

<p align="center">
  <img src="demo_files/dashboard.png" width="800">
</p>

### Video Learning

<p align="center">
  <img src="demo_files/video_learning.jpeg" width="800">
</p>

### Assessment

<p align="center">
  <img src="demo_files/assessment.png" width="800">
</p>

### Results

<p align="center">
  <img src="demo_files/result.png" width="800">
</p>

### Report PDF Preview

<p align="center">
  <img src="demo_files/result_pdf.png" width="800">
</p>

### Leaderboard

<p align="center">
  <img src="demo_files/leaderboards.png" width="800">
</p>

### Profile

<p align="center">
  <img src="demo_files/profile.png" width="800">
</p>

**Assessment Report:** [View PDF](demo_files/NeuroLearn_Report_1772355573144.pdf)

## Tech Stack

- Frontend: Next.js 16, React 19, TypeScript, Tailwind CSS 4, Framer Motion, Lucide icons
- Backend: FastAPI, SQLAlchemy, Alembic, PostgreSQL, Pydantic, JWT auth
- ML and media: MediaPipe, OpenAI Whisper, FLAN-T5, PyTorch, OpenCV
- Reporting and delivery: ReportLab, Jinja2, SMTP

## Repository Layout

```text
NeuroLearn/
|-- backend/                  # FastAPI app, routers, auth, data, ML, reports, tests
|-- frontend/                 # Next.js app router UI and API client
|-- demo_files/               # Screenshots and sample report artifacts
|-- flow_diagram.png          # Project architecture diagram
|-- render.yaml               # Render deployment config
|-- vercel.json               # Vercel frontend config
`-- README.md                 # Root project guide
```

Useful sub-guides:

- `frontend/README.md`
- `backend/README.md`

## Architecture

```text
NeuroLearn/
|-- frontend/
|   |-- app/                  # Next.js App Router pages
|   |   |-- dashboard/        # Course grid, gamification, CRS trend
|   |   |-- discover/         # Topic-based course generation
|   |   |-- video/            # Video player, webcam attention, transcript
|   |   |-- assessment/       # Adaptive quiz
|   |   |-- results/          # Score, XP, feedback, CRS breakdown
|   |   |-- leaderboard/      # Global rankings
|   |   |-- profile/          # Student profile, badges, privacy controls
|   |   |-- login/            # Auth
|   |   |-- signup/           # Auth
|   |   |-- forgot-password/  # Password reset request
|   |   `-- reset-password/   # Password reset completion
|   |-- components/           # Reusable UI components
|   |-- lib/                  # API client, auth state, fallback data, utilities
|   `-- public/               # Static frontend assets
|
|-- backend/
|   |-- main.py               # FastAPI entry point
|   |-- auth/                 # JWT auth and password security
|   |-- config/               # CRS and runtime config
|   |-- data/                 # SQLAlchemy/Postgres and legacy data helpers
|   |-- migrations/           # Alembic migration config
|   |-- ml/                   # Attention, transcription, question generation, CRS
|   |-- routers/              # API route groups
|   |-- schemas/              # Pydantic models
|   |-- scraping/             # Course/video discovery pipeline
|   |-- services/             # Email and report generation
|   |-- scripts/              # Operational/demo scripts
|   `-- tests/                # Backend tests
|
`-- demo_files/               # Screenshots and sample report artifacts
```

### Flowchart

<p align="center">
  <img src="flow_diagram.png" width="800">
</p>

## Prerequisites

- Node.js 20.9 or newer
- Python 3.10 or newer
- PostgreSQL running locally or a reachable PostgreSQL connection string
- Optional: a webcam for live attention monitoring
- Optional: FFmpeg for Whisper/video processing workflows

## Quick Start: Frontend Demo

The frontend can run by itself. If the backend is not available, it uses local
fallback data unless strict API mode is enabled.

```bash
cd frontend
npm install
copy .env.example .env.local
npm run dev
```

Open `http://localhost:3000`.

Frontend environment:

```env
NEXT_PUBLIC_API_URL=http://localhost:8000/api
NEXT_PUBLIC_API_STRICT=false
```

Set `NEXT_PUBLIC_API_STRICT=true` when you want backend connection failures to
surface immediately instead of falling back to demo data.

## Quick Start: Full Stack

### 1. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

The API runs at `http://localhost:8000`.

- Swagger docs: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`
- API overview: `http://localhost:8000/api`

On macOS/Linux, activate the virtual environment with:

```bash
source venv/bin/activate
```

### 2. Frontend

```bash
cd frontend
npm install
copy .env.example .env.local
npm run dev
```

Open `http://localhost:3000`.

## Backend Configuration

The backend reads configuration from `backend/.env`.

Common local variables:

```env
HOST=0.0.0.0
PORT=8000
DEBUG=true
CORS_ORIGINS=http://localhost:3000

PG_HOST=localhost
PG_PORT=5432
PG_DB=neurolearn
PG_USER=neurolearn
PG_PASSWORD=neurolearn_dev_pw

JWT_SECRET=change-this-for-local-dev
ACCESS_TOKEN_EXPIRE_MINUTES=30

WHISPER_MODEL_SIZE=base
FLAN_T5_MODEL=google/flan-t5-base

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM_NAME=NeuroLearn
```

You can also provide a full SQLAlchemy URL:

```env
DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/neurolearn
```

The application creates database tables on startup through SQLAlchemy. Alembic
configuration is also present under `backend/migrations`.

## Optional ML Setup

`backend/requirements.txt` installs the full ML stack. Some packages are large,
especially PyTorch, Whisper, Transformers, MediaPipe, and OpenCV.

After installing Playwright, install Chromium for the content-discovery scraper:

```bash
cd backend
python -m playwright install chromium
```

When ML models are unavailable, the backend keeps the JSON contract stable by
returning realistic fallback data for supported features.

## Video URL Support

The video player auto-detects and handles:

| URL Type | Example | Method |
|---|---|---|
| Direct MP4 | `https://example.com/video.mp4` | Native video element |
| YouTube | `youtube.com/watch?v=...` or `youtu.be/...` | Embedded iframe |
| Other URLs | External video pages | iframe fallback where allowed |

Use the custom URL option on the video page to paste a video link directly.

## Learning Flow

### Video Learning Session

```text
Student opens /video?course=course_001

1. Frontend fetches course content from GET /api/courses/{course_id}.
2. Student plays a selected video.
3. If the student grants consent, CameraFeed sends frames to POST /api/attention/snapshot.
4. Backend analyzes the frame with MediaPipe when available and returns an attention score.
5. TranscriptionPanel reads transcript segments from GET /api/transcription/{video_id}/live.
6. When the video ends, the assessment path opens for that course/video context.
```

### Assessment Flow

```text
Student opens /assessment

1. Frontend requests questions from POST /api/assessment/generate.
2. Backend chooses difficulty and generates or returns questions.
3. Student submits answers to POST /api/assessment/submit.
4. Backend grades the attempt, computes CRS, persists results, awards XP, and returns feedback.
5. Frontend shows score, XP, adaptive recommendations, and CRS breakdown on /results.
```

## API Endpoints Reference

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/auth/signup` | Create an account |
| POST | `/api/auth/login` | Log in and receive tokens |
| POST | `/api/auth/refresh` | Refresh access token |
| POST | `/api/auth/logout` | Log out |
| GET | `/api/auth/me` | Current authenticated student |
| POST | `/api/auth/request-password-reset` | Request password reset |
| POST | `/api/auth/reset-password` | Reset password |
| GET | `/api/student/profile` | Student profile and badges |
| POST | `/api/student/xp` | Award XP |
| GET | `/api/courses` | List courses |
| GET | `/api/courses/{course_id}` | Course details |
| GET | `/api/courses/{course_id}/videos/{video_id}` | Video details |
| POST | `/api/content/discover` | Discover videos and create an auto course |
| GET | `/api/content/courses/auto` | List auto-generated courses |
| GET | `/api/content/courses/auto/{course_id}` | Auto-generated course details |
| POST | `/api/content/courses/auto/{course_id}/save` | Save generated course into the course catalog |
| POST | `/api/content/pipeline/full` | Discover, transcribe, and prepare assessments in one pipeline |
| GET | `/api/attention/consent` | Check webcam consent |
| POST | `/api/attention/consent` | Grant or revoke webcam consent |
| POST | `/api/attention/snapshot` | Analyze a consent-gated camera frame |
| GET | `/api/attention/history` | Attention history |
| GET | `/api/attention/dummy-snapshot` | Attention test response without camera |
| POST | `/api/attention/purge-expired` | Purge expired attention records |
| GET | `/api/transcription/{video_id}` | Full transcript |
| GET | `/api/transcription/{video_id}/live` | Transcript segment at timestamp |
| POST | `/api/transcription/chunk` | Transcribe an audio chunk |
| POST | `/api/assessment/generate` | Generate adaptive quiz |
| POST | `/api/assessment/submit` | Submit answers and receive result |
| GET | `/api/assessment/session/{session_id}` | Assessment session details |
| GET | `/api/assessment/results/{student_id}` | Student assessment results |
| GET | `/api/CRS/{student_id}` | Latest CRS record |
| GET | `/api/CRS/{student_id}/history` | Full CRS history |
| GET | `/api/CRS/{student_id}/performance/history` | Performance component history |
| GET | `/api/CRS/{student_id}/attention/history` | Attention component history |
| GET | `/api/CRS/{student_id}/integrity/history` | Response integrity component history |
| GET | `/api/CRS/{student_id}/trend/history` | Learning trend component history |
| GET | `/api/CRS/{student_id}/complexity/history` | Content complexity component history |
| GET | `/api/CRS/{student_id}/difficulty/reason` | Latest adaptive difficulty explanation |
| GET | `/api/leaderboard` | Global rankings |
| GET | `/api/challenges/daily` | Daily challenges |
| POST | `/api/challenges/daily/{challenge_type}/progress` | Update challenge progress |
| GET | `/api/notifications` | Student notifications |
| POST | `/api/report/generate` | Generate report PDF |
| POST | `/api/report/email` | Email report PDF |
| GET | `/api/report/email-status` | Email configuration status |
| GET | `/health` | Backend and ML health check |

## Main Backend Endpoints

- `POST /api/auth/signup`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `GET /api/student/profile`
- `GET /api/courses`
- `POST /api/content/discover`
- `POST /api/attention/snapshot`
- `GET /api/transcription/{video_id}`
- `POST /api/assessment/generate`
- `POST /api/assessment/submit`
- `GET /api/CRS/{student_id}/history`
- `GET /api/leaderboard`
- `POST /api/report/generate`

See `http://localhost:8000/docs` for the current schema and request bodies.

## Cognitive Readiness Score

CRS is the adaptive scoring layer used by the assessment engine. It combines:

- Performance: recent assessment score history
- Attention: gaze, head pose, and blink-normality signals
- Response integrity: timing behavior independent of correctness
- Learning trend: slope over recent scores
- Content complexity: transcript readability and technical density

The fused CRS value selects the next difficulty tier and is persisted so later
submissions can use the learner's recent history.

Default difficulty thresholds:

```text
CRS > 0.75          hard
0.45 <= CRS <= 0.75 medium
CRS < 0.45          easy
```

The CRS configuration lives in `backend/config/CRS_config.py`.

## Testing And Validation

Frontend:

```bash
cd frontend
npm run lint
npm run build
```

Backend:

```bash
cd backend
pytest
```

Focused CRS tests:

```bash
cd backend
pytest tests/test_CRS.py -v
```

## Frontend Routes

| Route | Page | Description |
|---|---|---|
| `/` | Splash / entry | Redirects into the app experience |
| `/dashboard` | Dashboard | Course grid, XP stats, challenges, badges |
| `/discover` | Discover | Generate a course from a topic |
| `/video` | Video Learning | Video player, camera feed, attention monitor, transcription |
| `/assessment` | Assessment | Adaptive quiz with timer |
| `/results` | Results | Score, XP earned, adaptive feedback, CRS breakdown |
| `/leaderboard` | Leaderboard | Global rankings |
| `/profile` | Profile | Student info, achievements, privacy controls |
| `/login` | Login | Account sign-in |
| `/signup` | Signup | Account creation |
| `/forgot-password` | Forgot Password | Password reset request |
| `/reset-password` | Reset Password | Password reset completion |

## Deployment Notes

- The frontend is configured for Vercel with `vercel.json`.
- The backend includes Render-oriented configuration in `render.yaml`.
- Configure production CORS with `CORS_ORIGINS` and `CORS_ORIGIN_REGEX`.
- Set a real `JWT_SECRET` in every non-local environment.
- Use managed PostgreSQL in production and avoid committing `.env` files.
- Configure SMTP credentials only where report email delivery is required.

## Demo Assets

Sample screenshots and report files live in `demo_files/`.

Screens included:

- Dashboard
- Video learning
- Assessment
- Results
- Report PDF preview
- Leaderboard
- Profile

Files:

- `demo_files/dashboard.png`
- `demo_files/video_learning.jpeg`
- `demo_files/assessment.png`
- `demo_files/result.png`
- `demo_files/result_pdf.png`
- `demo_files/leaderboards.png`
- `demo_files/profile.png`
- `demo_files/NeuroLearn_Report_1772355573144.pdf`

  
