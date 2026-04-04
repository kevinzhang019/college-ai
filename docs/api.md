# API & Frontend

## FastAPI Backend (`college_ai/api/app.py`)

Served by uvicorn on port 8000.

**CORS origins:** `localhost:3000`, `127.0.0.1:3000`, `localhost:8080`, `127.0.0.1:8080`

### Endpoints

| Method | Path | Request Body | Purpose |
|---|---|---|---|
| GET | `/health` | — | Liveness probe → `{"status": "ok"}` |
| GET | `/config` | — | Returns current Zilliz collection name |
| GET | `/options` | — | Sorted list of all college names (from CSV seeds) |
| POST | `/ask` | `{question, top_k, college}` | RAG Q&A → `{answer, sources, confidence, ...}` |
| POST | `/predict` | `{gpa, school_name, sat, act, residency, major}` | Admission prediction → `{probability, confidence_interval, classification, factors}` |
| POST | `/compare` | `{gpa, sat, act, schools[], residency, major}` | Multi-school comparison → `{results[]}` sorted by probability |
| GET | `/scattergram/{school_name}` | — | All applicant datapoints for scatter plot visualization |

**Lazy loading:** ML predictor loaded on first request (`_get_predictor()`). Returns error message if no model artifacts exist.

## Frontend (`frontend/`)

Static HTML/JS/CSS SPA served by Python `http.server` on port 3000. Calls the FastAPI backend.

## Startup

`./start.sh` launches both backend (:8000) and frontend (:3000) with PID tracking and cleanup on Ctrl+C.
