# API & Frontend

## FastAPI Backend (`college_ai/api/app.py`)

Served by uvicorn on port 8000.

**CORS origins:** `localhost:3000`, `127.0.0.1:3000`, `localhost:8080`, `127.0.0.1:8080`, plus any via `CORS_ORIGINS` env var (comma-separated).

### Endpoints

| Method | Path | Request Body | Purpose |
|---|---|---|---|
| GET | `/health` | — | Liveness probe → `{"status": "ok"}` |
| GET | `/config` | — | Returns current Zilliz collection name |
| GET | `/options` | — | Sorted list of all college names (from CSV seeds) |
| POST | `/ask` | `{question, top_k, college, essay_text}` | Non-streaming RAG Q&A (CLI/testing) |
| POST | `/ask/stream` | `{question, top_k, college, essay_text, essay_prompt, history, experiences}` | SSE streaming RAG (primary frontend endpoint) |
| POST | `/predict` | `{gpa, school_name, sat, act, residency, major}` | Admission prediction → `{probability, confidence_interval, classification, factors}` |
| POST | `/compare` | `{gpa, sat, act, schools[], residency, major}` | Multi-school comparison → `{results[]}` sorted by probability |
| GET | `/scattergram/{school_name}` | — | All applicant datapoints for scatter plot visualization |

### `/ask` Details

**Request:**
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `question` | string | Yes | — | User question or essay request |
| `top_k` | int | No | 8 | 1–20, number of sources for generation |
| `college` | string | No | — | School from dropdown (hard filter) |
| `essay_text` | string | No | — | Pasted essay draft (forces essay_review mode) |

**Response:**
| Field | Type | Notes |
|---|---|---|
| `answer` | string | Markdown with `[N]` citations |
| `sources` | array | `{college_name, url, title, content, crawled_at, distance}` per source |
| `confidence` | string | `"high"`, `"medium"`, or `"low"` |
| `source_count` | int | Number of sources used |
| `query_type` | string | `"qa"`, `"essay_ideas"`, `"essay_review"`, or `"admission_prediction"` |

The backend auto-classifies the query type. If `essay_text` is provided, it's always `essay_review`. The school can come from the `college` param (dropdown) or be extracted from the question text via fuzzy matching — either way, results are boosted the same.

**Lazy loading:** ML predictor loaded on first request (`_get_predictor()`). Returns error message if no model artifacts exist.

### `/ask/stream` Details

Primary endpoint used by the frontend. Runs the same pipeline as `/ask` (route → rewrite → retrieve → rerank) but streams generation via OpenAI `stream=True`.

**Request:**
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `question` | string | Yes | — | User question or essay request |
| `top_k` | int | No | 8 | 1–20, number of sources for generation |
| `response_length` | string | No | — | XS/S/M/L/XL — overrides auto-detected length budget |
| `college` | string | No | — | School from dropdown (hard filter) |
| `essay_text` | string | No | — | Essay draft (forces essay_review mode) |
| `essay_prompt` | string | No | — | Essay assignment prompt (essay mode) |
| `history` | array | No | — | Previous messages `[{role, content}]`, last 6 used |
| `experiences` | array | No | — | User's extracurriculars `[{title, organization, type, description, startDate, endDate}]` |

Field aliasing: `startDate`/`endDate` accepted via Pydantic `populate_by_name` (maps to `start_date`/`end_date`).

**Response:** Server-Sent Events (`text/event-stream`), one JSON object per `data:` line:

```
data: {"type": "token", "content": "..."}         // streamed text fragment
data: {"type": "sources", "sources": [...], "confidence": "...", "query_type": "..."}
data: {"type": "done"}                             // stream complete
data: {"type": "error", "message": "..."}          // on exception
```

Citation verification and confidence scoring happen after generation completes, sent in the `sources` event.

## Frontend (`frontend/`)

> **Detailed frontend documentation:** See [frontend.md](frontend.md) for Cole persona, component architecture, design system, state management, and mobile UX.

React + Vite + TypeScript SPA. Dev server on port 3000, production builds to `frontend/dist/`.

**Tech stack:** React 18, TypeScript, Tailwind CSS, Zustand (state with localStorage persistence), Framer Motion (animations), Headless UI (combobox), react-markdown + remark-gfm + rehype-raw.

### Architecture

ChatGPT-style layout with a persistent sidebar (280px) and main content area. Four modes:

| Mode | Key | View | Conversation-based? |
|---|---|---|---|
| Q&A | `qa` | ChatView + InputArea | Yes |
| Essay Helper | `essay` | ChatView + InputArea | Yes |
| Admissions Calculator | `admissions` | AdmissionsView | No (standalone) |
| My Profile | `experiences` | ExperiencesView | No (standalone) |

**Zustand store** (`store.ts`) persists to `localStorage` key `college-ai-store`:

- **Persisted:** `conversations`, `conversationOrder`, `experiences`, `profile`, `activeConversationId`, `contextSize`
- **Ephemeral:** `mode`, `isConnected`, `collegeOptions`, `streamingContent`, `streamingLoading`, `sidebarOpen`

**Component tree:**

```
App
  Sidebar                    mode buttons, new chat, ConversationList
  Main
    ChatView                 Q&A + Essay modes
      WelcomeState           4 randomized suggestions
      MessageBubble          markdown, SourceCards, confidence badge
      StreamingMessage       live token display with loading dots
    InputArea                textarea, CollegeCombobox, ReviewPanel, QuickPredictModal
    ExperiencesView          experiences mode (profile + CRUD)
      ExperienceForm         modal for add/edit
    AdmissionsView           admissions mode (batch compare)
      PredictionCard         probability, CI, classification, factors
```

### Conversations

- Max 50 conversations with LRU eviction (oldest removed when limit exceeded)
- Auto-titled from first user message (truncated at 60 characters)
- Mode-scoped: conversations belong to `'qa'` or `'essay'`, sidebar filters by current mode
- Last 6 messages sent as `history` to `/ask/stream` for multi-turn context
- Each conversation stores: `college` (selected school) and `essayPrompt` (essay assignment)

### Q&A Mode

- Multi-message persistent conversations with SSE streaming via `useStreaming` hook
- Settings popover (gear icon) in bottom-right of textarea with two controls: **Context Size** (XS/S/M/L/XL → `top_k` 3/5/8/12/16) and **Response Length** (XS/S/M/L/XL → overrides length budget). Both default to M and persist across sessions
- Welcome state shows 4 randomized suggestions from ~100 QA questions across 10 categories
- Searchable college combobox (Headless UI, loaded from `/options` with 31-school fallback)
- "See my chances" button appears inline with school dropdown when a college is selected — opens QuickPredictModal with GPA (row 1), SAT/ACT toggle + score (row 2), and major/residency (row 3), calls `POST /predict`, displays PredictionCard
- Answers rendered as markdown with `[N]` citation badges, expandable source cards (first 3 shown, rest collapsible), confidence badge

### Essay Helper Mode

- Same multi-message conversation UI as Q&A
- Required `essay_prompt` field — the essay assignment prompt, sent to backend with every message
- Collapsible ReviewPanel slides up above the input area for pasting essay drafts (with word count)
- User's `experiences` (from My Profile) automatically included in every request for personalized suggestions
- Welcome state shows 4 randomized suggestions from ~50 essay prompts (brainstorm + review)

### Admissions Calculator

- Standalone view (not conversation-based)
- Academic profile form: GPA (0–5.0), SAT/ACT toggle + score, major (83 options from `ALLOWED_MAJORS`), residency (in-state/out-of-state)
- Multi-school selection via combobox with removable chips (max 10 schools)
- Submits batch request to `POST /compare`
- Results displayed as PredictionCard components: probability percentage (color-coded), 95% confidence interval, safety/match/reach classification badge, contributing factors (positive/negative)

### My Profile / Experiences

- **Academic Info** card: GPA + SAT/ACT type and score, persisted in Zustand `profile`
- **Experiences** list with full CRUD via modal ExperienceForm
- Experience types: `extracurricular`, `project`, `work`, `volunteer` (each color-coded)
- Each experience: title, organization, type, description, start/end date (month picker, optional "Present" toggle)
- Profile data auto-populates Admissions Calculator and Quick Predict modal
- Experiences auto-included as context in Essay mode requests (formatted by `format_experiences()` on backend)

### Design System

- **Background:** dark-950 `#0f0f12`, surface dark-900 `#18181b`, elevated dark-800 `#222226`, borders dark-700 `#2e2e33`
- **Accent:** forest-600 `#096E3D` (primary buttons, active states), forest-400 `#34B874` (text accents, streaming), forest-300 `#6ED2A0` (active mode labels)
- **Text:** slate-100 (primary), slate-300 (secondary), slate-400 (muted), slate-500 (hint/timestamp)
- **Font:** Inter, system-ui fallback
- **Shapes:** rounded-2xl cards, rounded-xl inputs, rounded-full buttons/chips/badges
- **Shadows:** custom dark shadow scale (dark-sm through dark-lg) tuned for dark UI

### Mobile UX

- Sidebar is `fixed` on mobile (below `lg` breakpoint), slides in/out with spring animation
- Hamburger icon in mobile header (only visible when sidebar closed)
- Semi-transparent backdrop overlay behind sidebar on mobile
- All content areas scroll independently

## Startup

`./start.sh` launches both backend (:8000) and frontend (:3000) with PID tracking and cleanup on Ctrl+C.

For frontend development:
```bash
cd frontend && npm run dev    # Vite dev server on :3000
cd frontend && npm run build  # Production build to dist/
```
