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
| POST | `/ask` | `{question, top_k, college, essay_text}` | RAG Q&A or Essay Helper → `{answer, sources, confidence, source_count, query_type}` |
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

## Frontend (`frontend/`)

React + Vite + TypeScript SPA. Dev server on port 3000, production builds to `frontend/dist/`.

**Tech stack:** React 18, TypeScript, Tailwind CSS, Zustand (state), Framer Motion (animations), Headless UI (combobox), react-markdown + remark-gfm + rehype-raw.

**Two main modes:**

### Q&A Mode
- Single question/answer interface (not conversational chat)
- Auto-resize textarea with Enter-to-submit
- Welcome state with suggested question chips
- Answer rendered as markdown (with rehype-raw for inline HTML) with `[N]` citation badges
- Expandable source cards with college name, page type, and content preview
- Confidence badge (high/medium/low) with color coding

### Essay Helper Mode (two sub-tabs)

**Brainstorm (Chat tab):**
- Conversational message bubbles for back-and-forth essay brainstorming
- User messages (right, blue) and assistant responses (left, dark card) with markdown
- Suggestion chips for empty state
- Calls `POST /ask` with `{question, college}` — no `essay_text`

**Review Draft (Editor tab):**
- Side-by-side split panel: essay textarea (left) + AI feedback (right)
- "Get Ideas" button: calls `/ask` with brainstorming prompt
- "Get Feedback" button: calls `/ask` with `{question, essay_text, college}`
- Word count display, responsive stacking on mobile

**Shared features:**
- Searchable college combobox (Headless UI) loaded from `/options` with 31-school fallback list
- Results count selector (5, 8, 12, 20)
- Animated pill mode toggle (Q&A / Essay Helper) with Framer Motion
- Help modal with example questions across 3 categories
- Floating help button (bottom-right)
- Dark navy blue color palette with blue accents, subtle shadows, rounded corners
- Mobile responsive with tablet/mobile breakpoints

**Design tokens:** Primary blue-500/600, background navy-950 (#0a0e1a), surface navy-900 (#111827) with navy-700 (#1e3a5f) borders, text slate-100/300/400, Inter font, rounded-2xl cards, rounded-full buttons.

## Startup

`./start.sh` launches both backend (:8000) and frontend (:3000) with PID tracking and cleanup on Ctrl+C.

For frontend development:
```bash
cd frontend && npm run dev    # Vite dev server on :3000
cd frontend && npm run build  # Production build to dist/
```
