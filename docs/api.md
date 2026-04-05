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

Static HTML/JS/CSS SPA served by Python `http.server` on port 3000.

**Features:**
- **Mode tabs:** "Ask a Question" (Q&A) and "Essay Helper" toggle
- **Essay sub-modes:** "Get Ideas" or "Review My Draft" radio toggle
- **Essay draft textarea:** Shown when "Review My Draft" selected
- **Searchable college dropdown:** Fuzzy-filtered list loaded from `/options`
- **Query type badge:** Shows detected mode (Q&A, Essay Ideas, Essay Review, Prediction) in response
- **Confidence banner:** Color-coded indicator with source count
- **Markdown rendering:** Headers, bold, italic, bullet lists, `[N]` citation highlighting

## Startup

`./start.sh` launches both backend (:8000) and frontend (:3000) with PID tracking and cleanup on Ctrl+C.
