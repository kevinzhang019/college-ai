# college-ai

A college application assistant combining RAG (retrieval-augmented generation) over crawled college websites with ML-based admissions probability prediction.

## Prerequisites

Environment variables in `.env` at the project root:

| Variable | Required | Default | Used by |
|---|---|---|---|
| `ZILLIZ_URI` | Yes | — | RAG, crawler |
| `ZILLIZ_API_KEY` | Yes | — | RAG, crawler |
| `ZILLIZ_COLLECTION_NAME` | No | `colleges` | RAG, crawler (hybrid search collection) |
| `OPENAI_API_KEY` | Yes | — | RAG, crawler (embeddings) |
| `OPENAI_CHAT_MODEL` | No | `gpt-4.1-mini` | RAG answer generation |
| `COHERE_API_KEY` | No | — | Cross-encoder reranking (optional, degrades gracefully) |
| `CONTEXTUAL_PREFIXES` | No | `0` | Set to `1` to enable LLM contextual chunk prefixes during crawl |
| `TURSO_DATABASE_URL` | No | local SQLite | Admissions DB (Turso cloud) |
| `TURSO_AUTH_TOKEN` | No | — | Admissions DB (Turso cloud) |
| `SCORECARD_API_KEY` | Yes (for scraping) | — | College Scorecard API |

Install dependencies:

```bash
pip install -r requirements.txt
```

## Quick Start

Start both the backend API and web frontend:

```bash
./start.sh
```

- Backend API: `http://localhost:8000`
- Frontend UI: `http://localhost:3000`
- API docs: `http://localhost:8000/docs`

Or manually:

```bash
uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000  # backend
cd frontend && npm run dev                                    # frontend (Vite dev server)
```

To build the frontend for production:

```bash
cd frontend && npm install && npm run build  # outputs to frontend/dist/
```

## Project Structure

```
college_ai/              Main Python package
  api/app.py             FastAPI server (RAG + predictions)
  db/connection.py       SQLAlchemy engine (Turso or local SQLite)
  db/models.py           ORM models (School, ApplicantDatapoint, NicheGrade)
  ml/                    ML training + inference
  rag/                   RAG v2 (hybrid search, query routing, essay helper)
  scraping/              Web crawler, Niche scraper, Scorecard client
scripts/                 One-off DB maintenance tools
tests/                   All tests
model/                   Trained model artifacts (tracked in git)
data/                    Runtime data — training parquet, SQLite DBs (gitignored)
frontend/                React + Vite + TypeScript SPA (builds to dist/)
docs/                    Architecture documentation (ML, RAG, threading, DB, API)
```

## Data Pipeline

Run these in order to build the system from scratch:

### 1. Seed school data from College Scorecard API

```bash
python -m college_ai.scraping.scorecard_client
```

No flags. Fetches ~6,500 schools and upserts into the DB. Requires `SCORECARD_API_KEY` env var.
Optional: `SCORECARD_WORKERS` env var (default `3`) controls concurrent page-fetching threads.

### 2. Scrape admissions data from Niche

```bash
python -m college_ai.scraping.niche_scraper
```

Scrapes scattergram datapoints (GPA/SAT/outcome) and letter grades for each school.

| Flag | Default | Description |
|---|---|---|
| `--school SLUG` | all | Scrape one school (e.g. `stanford-university`) |
| `--grades-only` | off | Only scrape letter grades, skip scattergrams |
| `--no-resume` | off | Re-scrape everything, ignore previous progress |
| `--reset-empty` | off | Delete `no_data` rows so those schools get retried, then exit |
| `--debug` | off | Verbose selector/extraction logging |
| `--headful` | default | Run browser visibly (already default — PerimeterX blocks headless) |
| `--headless` | off | Force headless mode (will likely get blocked) |
| `--capture-cookies` | off | Open browser for manual login/challenge, save cookies for future runs |
| `--workers N` | 3 | Parallel browser workers (max 5) |

### 3. Crawl college websites into Zilliz

```bash
python -m college_ai.scraping.crawler
```

Reads college URLs from CSVs in `college_ai/scraping/colleges/`, BFS-crawls each site, chunks text (512 tokens, 50-token overlap), classifies page type from URL, embeds with OpenAI, and inserts into Zilliz with hybrid search support (dense + BM25). The collection is auto-created with the correct schema on first run.

| Flag | Default | Description |
|---|---|---|
| `--workers N` | `CRAWLER_MAX_WORKERS` (6) | Worker threads per college |
| `--colleges N` | `INTER_COLLEGE_PARALLELISM` (4) | Colleges to crawl in parallel |
| `--max-pages N` | `MAX_PAGES_PER_COLLEGE` (500) | Max pages per college |
| `--no-resume` | off | Force full re-crawl: disables delta cache and replaces existing Milvus vectors (delete + re-insert) |

To recreate the collection from scratch (drops all data): `python scripts/recreate_collection.py`

### 4. Export training data

```bash
python -m college_ai.ml.data_pipeline export
```

Pulls raw data from the DB, normalizes scores, engineers features, and writes `data/training_data.parquet`.

| Argument | Default | Description |
|---|---|---|
| `stats` (positional) | — | Print DB summary (counts, top schools) |
| `export` (positional) | — | Run pipeline and export training data |
| `--format parquet\|csv` | parquet | Output format |

### 5a. Train single global model

```bash
python -m college_ai.ml.train
```

Trains one LightGBM model on all data. Outputs to `model/`.

| Flag | Default | Description |
|---|---|---|
| `--skip-tuning` | off | Skip Optuna hyperparameter search, use defaults |
| `--data-path PATH` | `data/training_data.parquet` | Input training data |
| `--model-dir DIR` | `model` | Output directory for model.pkl + config.json |
| `--n-trials N` | 50 | Number of Optuna trials |
| `--force-imbalance-correction` | off | Enable `is_unbalance` (hurts calibration) |
| `--prune-features` | off | Drop near-zero importance features and retrain |
| `--model-type lightgbm\|catboost` | lightgbm | Boosting framework |

### 5b. Train bucketed models (recommended)

```bash
python -m college_ai.ml.train_bucketed
```

Trains 4 separate models by selectivity bucket (reach/competitive/match/safety). Outputs to `model/bucketed/`.

| Flag | Default | Description |
|---|---|---|
| `--skip-tuning` | off | Skip Optuna, use default hyperparams |
| `--data-path PATH` | `data/training_data.parquet` | Input training data |
| `--model-dir DIR` | `model` | Output directory |
| `--n-trials N` | 50 | Optuna trials per bucket |
| `--bucket NAME` | all | Train only one bucket: `reach`, `competitive`, `match`, or `safety` |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/config` | Current collection name |
| `GET` | `/options` | Available college/major filter lists |
| `POST` | `/ask` | RAG Q&A or Essay Helper |
| `POST` | `/predict` | Admission probability for one school |
| `POST` | `/compare` | Admission probability across multiple schools |
| `GET` | `/scattergram/{school_name}` | Scattergram datapoints for visualization |

### Example: Ask a question

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Scholarships for business majors at UCLA?", "top_k": 8}'
```

### Example: Essay brainstorming

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Help me brainstorm ideas for my Why Stanford essay", "college": "Stanford University"}'
```

### Example: Essay draft review

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Review my Why UPenn essay", "college": "University of Pennsylvania", "essay_text": "Ever since I visited campus..."}'
```

### Example: Predict admission chances

```bash
curl -X POST http://localhost:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{"school_name": "MIT", "gpa": 3.9, "sat": 1550}'
```

## RAG CLI

```bash
python -m college_ai.rag.service --question "How do I apply for CS at MIT?" --top_k 8
python -m college_ai.rag.service --question "Help me brainstorm my Why Stanford essay" --college "Stanford University"
python -m college_ai.rag.service --question "Review my essay" --college "MIT" --essay_text "Ever since..."
```

## Maintenance Scripts

One-off tools in `scripts/` for Zilliz DB maintenance:

| Script | Description |
|---|---|
| `remove_duplicates.py` | Delete duplicate chunks by title+content |
| `count_duplicates.py` | Count duplicates without deleting (read-only) |
| `clean_non_university_urls.py` | Remove off-domain URLs from the collection |
| `consolidate_college_alias.py` | Merge records for college name aliases |
| `recreate_collection.py` | Drop and recreate the Zilliz collection with hybrid schema |
| `migrate_zilliz.py` | Copy data between Zilliz instances |
| `milvus_monitor.py` | Live terminal dashboard of collection stats |
| `run_monitor.py` | CLI wrapper for the monitor |

Run with: `python scripts/<script>.py`

## Architecture Documentation

Detailed architecture docs are in `docs/`:

| Document | Description |
|---|---|
| [ML Pipelines](docs/ml-pipelines.md) | Single global model + per-selectivity-bucket models (LightGBM, focal loss, Venn-ABERS) |
| [Thread Safety — Crawler](docs/thread-safety-crawler.md) | Concurrency primitives in the BFS crawler (locks, semaphores, thread-local storage, shutdown ordering) |
| [Thread Safety — Niche](docs/thread-safety-niche.md) | Concurrency primitives in the Niche scraper (DBWriterThread, rate limiter, sentinel guarantee) |
| [Crawler](docs/crawler.md) | BFS crawler — anti-bot measures, delta crawling, hybrid search schema |
| [Niche Scraper](docs/niche-scraper.md) | Niche.com scattergrams + letter grades via Camoufox |
| [Scorecard Client](docs/scorecard-client.md) | US DOE College Scorecard API → schools table |
| [RAG Pipeline](docs/rag-pipeline.md) | v2: hybrid search (dense + BM25), query routing, Cohere reranking, specialized generators (Q&A + Essay Helper) |
| [Database](docs/database.md) | Three tables, Turso/libSQL connection resilience, inline migrations |
| [API & Frontend](docs/api.md) | Endpoint details, request/response shapes, React frontend (Q&A + Essay Helper modes) |
