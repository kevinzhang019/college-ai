# API & Frontend

## FastAPI Backend (`college_ai/api/app.py`)

Served by uvicorn on port 8000.

**CORS origins:** `localhost:3000`, `127.0.0.1:3000`, `localhost:8080`, `127.0.0.1:8080`, plus any via `CORS_ORIGINS` env var (comma-separated).

### Endpoints

| Method | Path | Request Body | Purpose |
|---|---|---|---|
| GET | `/health` | — | Liveness probe → `{"status": "ok"}` |
| GET | `/config` | — | Returns current Zilliz collection name |
| GET | `/options` | — | Sorted college names + school→state mapping `{colleges[], school_states{}}` (fuzzy-matched via rapidfuzz) |
| POST | `/ask` | `{question, top_k, college, essay_text}` | Non-streaming RAG Q&A (CLI/testing) |
| POST | `/ask/stream` | `{question, top_k, college, essay_text, essay_prompt, history, experiences, profile}` | SSE streaming RAG (primary frontend endpoint) |
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
| `query_type` | string | `"qa"`, `"essay_ideas"`, `"essay_review"`, `"admission_prediction"`, `"ranking"`, or `"comparison"` |

The backend auto-classifies the query type. If `essay_text` is provided, it's always `essay_review`. The school can come from the `college` param (dropdown) or be extracted from the question text via fuzzy matching. If the dropdown is set, text extraction is skipped entirely. When no dropdown is set, the system extracts all mentioned schools (up to 5) and filters retrieval for each.

**Lazy loading:** ML predictor loaded on first request (`_get_predictor()`). Returns error message if no model artifacts exist.

### `/ask/stream` Details

Primary endpoint used by the frontend. Runs the same pipeline as `/ask` (route → rewrite → retrieve → rerank) but streams generation via OpenAI `stream=True`.

**Request:**
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `question` | string | Yes | — | User question or essay request (max 2000 chars) |
| `top_k` | int | No | 8 | 1–20, number of sources for generation |
| `response_length` | string | No | — | XS/S/M/L/XL — overrides auto-detected length budget |
| `college` | string | No | — | School from dropdown (hard filter, max 200 chars) |
| `essay_text` | string | No | — | Essay draft (forces essay_review mode, max 10000 chars) |
| `essay_prompt` | string | No | — | Essay assignment prompt (essay mode, max 1000 chars) |
| `history` | array | No | — | Previous messages `[{role, content}]`, last 6 used (content max 5000 chars each) |
| `experiences` | array | No | — | User's extracurriculars `[{title, organization, type, description, startDate, endDate}]` |
| `profile` | object | No | — | Profile `{gpa, testScoreType, testScore, country, countryLabel, state, preferredMajors, savedSchools}` — injected into all prompt modes (QA, essay ideas, essay review) for stats contextualization, residency determination, major-specific advice, school preference awareness, and admission prediction fallback |

Field aliasing: `startDate`/`endDate` accepted via Pydantic `populate_by_name` (maps to `start_date`/`end_date`).

**Response:** Server-Sent Events (`text/event-stream`), one JSON object per `data:` line:

```
data: {"type": "token", "content": "..."}         // streamed text fragment
data: {"type": "answer_replaced", "content": "..."}  // citation-verified replacement (only if changed)
data: {"type": "sources", "sources": [...], "confidence": "...", "query_type": "...", "reranked": true}
data: {"type": "done"}                             // stream complete
data: {"type": "error", "message": "..."}          // on exception
```

Citation verification and confidence scoring happen after generation completes. Verification strips invalid `[N]` citations (N > source count) and normalizes `[SD]` markers (case-insensitive → `[SD]`). `[SD]` markers are passed through to the frontend for rendering as official source badges. If verification modifies the answer (strips invalid citations or appends a grounding warning), an `answer_replaced` event is sent before `sources` so the frontend can replace the streamed text.

## Frontend (`frontend/`)

> **Detailed frontend documentation:** See [frontend.md](frontend.md) for Cole persona, component architecture, design system, state management, and mobile UX.

React + Vite + TypeScript SPA. Dev server on port 3000, production builds to `frontend/dist/`.

**Tech stack:** React 18, TypeScript, Tailwind CSS, Zustand (state with localStorage persistence), Framer Motion (animations), Headless UI (combobox), react-markdown + remark-gfm + rehype-raw.

### Architecture

ChatGPT-style layout with a persistent sidebar (280px) and main content area. Three modes:

| Mode | Key | View | Conversation-based? |
|---|---|---|---|
| Chat | `qa` | ChatView + InputArea | Yes |
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
- All chat conversations shown together in sidebar (legacy `'essay'` mode conversations from before the consolidation are also displayed)
- Last 6 messages sent as `history` to `/ask/stream` for multi-turn context
- Each conversation stores: `college` (selected school) and `essayPrompt` (essay assignment)

### Chat Mode

- Unified chat interface combining Q&A and essay assistance (previously separate modes)
- Multi-message persistent conversations with SSE streaming via `useStreaming` hook
- **Review Draft panel:** Collapsible panel above the input area with essay prompt input and essay draft textarea (with word count). When essay draft is provided, backend auto-classifies as `essay_review`. When only essay prompt is provided (no draft), auto-classifies as `essay_ideas`. Validation: essay text requires a prompt.
- Settings popover (gear icon) in bottom-right of textarea with two controls: **Context Size** (XS/S/M/L/XL → `top_k` 3/5/8/12/16) and **Response Length** (XS/S/M/L/XL → overrides length budget). Both default to M and persist across sessions
- Welcome state shows 4 randomized suggestions from combined QA (~100) and essay (~50) question pools
- Searchable college combobox (Headless UI, loaded from `/options` with 31-school fallback)
- "See my chances" button appears inline with school dropdown when a college is selected — opens QuickPredictModal
- Answers rendered as markdown with `[N]` citation badges, expandable source cards, confidence badge

### Admissions Calculator

- Standalone view (not conversation-based)
- Academic profile form: GPA (0–4.0), SAT/ACT toggle + score, default major (searchable `MajorCombobox` with preferred majors section), default residency (Use Location / Not specified / In-State / Out-of-State / International)
- Multi-school selection via combobox (max 10 schools, already-selected schools filtered out of dropdown), each with per-school searchable major combobox and always-editable residency dropdown (No residency / In-State / Out-of-State / International)
- Auto-residency: when location-eligible (non-US country, or US with state selected), Default Residency defaults to "Use Location" which auto-computes per school via `computeResidency()` (fuzzy-matched school→state mapping from `/options`). Non-US → International, US matching state → In-State, US different state → Out-of-State. All dropdowns remain editable — user overrides are respected on submit
- Results displayed as PredictionCard components: probability percentage (color-coded), 95% confidence interval, safety/match/reach classification badge, contributing factors (positive/negative)

### My Profile / Experiences

- **Academic Info** card: GPA + SAT/ACT type and score + country/state location dropdowns, persisted in Zustand `profile`
  - Country dropdown (all countries, US first). State dropdown appears only when US is selected
  - Auto-determines residency (in-state/out-of-state/international) by fuzzy-matching the selected school against the Turso DB via `determine_residency()` on the backend
- **Major Preferences** card: searchable Headless UI Combobox to add majors from `ALLOWED_MAJORS`, drag-to-reorder ranked list using Framer Motion `Reorder`. Ranked list passed to LLM as `"Preferred majors (ranked): #1 X, #2 Y"`
- **Experiences** list with full CRUD via modal ExperienceForm
- Experience types: `extracurricular`, `project`, `work`, `volunteer` (each color-coded)
- Each experience: title, organization, type, description, start/end date (month picker, optional "Present" toggle)
- Profile data auto-populates Admissions Calculator and Quick Predict modal
- Experiences auto-included as context in all chat requests (formatted by `format_experiences()` on backend)
- Profile data (GPA, test scores, location, preferred majors) sent on every request when GPA, country, or preferred majors are set, enabling stats contextualization, residency-aware tuition advice, and major-specific guidance

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

## EC2 Deployment

The API runs on a t3.micro (Amazon Linux 2023) as a systemd service. See `deploy/api-setup.sh` for the full bootstrap script.

**Port configuration:** The API listens on port 8000. Cloudflare proxies external traffic to port 8000 on the EC2 instance, so the EC2 security group must allow inbound TCP on port 8000. The service does **not** bind to port 80 — running on a high port avoids needing root privileges and matches what Cloudflare expects.

**Service management:**
```bash
sudo systemctl restart college-ai-api     # restart after deploy
sudo systemctl status college-ai-api      # check status
journalctl -u college-ai-api -f           # tail logs
```

**Deploy flow:**
```bash
cd /home/ec2-user/college-ai && git pull
sudo cp deploy/college-ai-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart college-ai-api
```
