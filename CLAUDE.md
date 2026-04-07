# College AI v2

College admissions prediction + RAG Q&A platform. Scrapes college data from multiple sources, trains ML models, and serves predictions + AI-generated answers via a FastAPI backend.

## Quick Start

```bash
pip install -r requirements.txt && playwright install
python -m college_ai.scraping.scorecard_client    # seed ~6,500 schools
python -m college_ai.scraping.niche_scraper       # scrape Niche scattergrams + grades
python -m college_ai.scraping.crawler             # BFS crawl → Zilliz (hybrid schema)
python -m college_ai.ml.data_pipeline export      # export training parquet
python -m college_ai.ml.train                     # single global model
python -m college_ai.ml.train_bucketed            # per-bucket models
cd frontend && npm install && cd ..                # install frontend deps
./start.sh                                        # FastAPI :8000 + frontend :3000
```

## Architecture Docs

- [ML Pipelines](docs/ml-pipelines.md) — two LightGBM architectures: single global model (84 features, Optuna, calibration) and bucketed models (4 models: reach/competitive/match/safety with focal loss, linear trees, Venn-ABERS)
- [Thread Safety — Crawler](docs/thread-safety-crawler.md) — **CRITICAL**: locks, semaphores, thread-local storage, and shutdown ordering in `crawler.py`
- [Thread Safety — Niche](docs/thread-safety-niche.md) — **CRITICAL**: DBWriterThread, rate limiter, sentinel guarantee in `niche_scraper.py`
- [Thread Safety Audit — Niche](docs/thread-safety-niche-audit.md) — Full concurrency audit (2026-04-05): verified no data races, deadlocks, or memory leaks
- [Thread Safety Audit — Crawler](docs/thread-safety-crawler-audit.md) — Full concurrency audit (2026-04-05): five bugs fixed (pw_done_callback ordering, PlaywrightPool rotation lock scope, prune_dead_slots blocking, delta cache crash-consistency, worker_session cleanup), no data races, deadlocks, or memory leaks
- [Crawler](docs/crawler.md) — BFS crawler (curl_cffi, Playwright, camoufox, delta crawling) → Zilliz hybrid search
- [Niche Scraper](docs/niche-scraper.md) — Niche.com scattergrams + letter grades via Camoufox
- [Scorecard Client](docs/scorecard-client.md) — US DOE College Scorecard API → schools table
- [RAG Pipeline](docs/rag-pipeline.md) — v2: greeting detection (skip pipeline) → school data injection (Turso DB → `[SCHOOL DATA]` block in prompt) → LLM-based ranking intent detection (gpt-4.1-nano → Niche categories) → hybrid search (dense + BM25, nprobe=64, embedding cache) → Cohere reranking (relevance threshold, ranking boost by niche_rank + category grades) → two-tier model routing (simple Q&A → gpt-4.1-nano, everything else → gpt-5.4-mini) → specialized generators (Q&A, Essay, Admission Prediction) with SSE streaming, multi-turn history, multi-query retrieval (essay + admissions), profile context in all modes, sentence-aware chunking, and prompt caching
- [Database](docs/database.md) — three tables (schools, applicant_datapoints, niche_grades), Turso/libSQL connection, inline migrations
- [API & Frontend](docs/api.md) — FastAPI endpoints (/ask, /ask/stream, /predict, /compare), React frontend with 4 modes (Q&A, Essay Helper, Admissions Calculator, My Profile)
- [Frontend](docs/frontend.md) — Cole persona, component architecture, design system (dark gray + forest green), state management, streaming, mobile UX

## Project Structure

```
college_ai/
├── api/app.py              FastAPI server
├── db/                     SQLAlchemy models + Turso connection
├── ml/                     Training, inference, feature engineering
├── rag/                    Vector search + OpenAI generation
└── scraping/               Crawler, Niche scraper, Scorecard client
model/                      Trained model artifacts
frontend/                   React + Vite + TypeScript SPA
  └── src/                  Components, store, API layer, hooks
scripts/                    Zilliz maintenance utilities
tests/                      Thread safety + scraper tests
docs/                       Detailed architecture documentation
```

## Dependencies

**Scraping:** `requests`, `beautifulsoup4`, `playwright`, `playwright-stealth`, `camoufox`, `curl_cffi`, `browserforge`
**Vector DB / RAG:** `pymilvus>=2.5.0`, `openai`, `tiktoken`, `cohere`
**Database:** `sqlalchemy-libsql`
**ML:** `lightgbm`, `optuna`, `scikit-learn`, `shap`, `venn-abers`, `rapidfuzz`
**API:** `fastapi`, `uvicorn`
**Frontend:** `react`, `zustand`, `framer-motion`, `tailwindcss`, `@headlessui/react`, `react-markdown`, `rehype-raw`

## Required Environment Variables

`ZILLIZ_URI`, `ZILLIZ_API_KEY`, `OPENAI_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `SCORECARD_API_KEY`, `COHERE_API_KEY` (optional — reranking degrades gracefully without it)

**Optional model overrides:** `MODEL_SIMPLE` (default: `gpt-4.1-nano`), `MODEL_STANDARD` (default: `gpt-5.4-mini`), `OPENAI_CHAT_MODEL` (legacy override for `MODEL_STANDARD`)

**Optional RAG tuning:** `RETRIEVAL_NPROBE` (default: `64`), `RAG_HISTORY_LIMIT` (default: `6`), `RAG_HISTORY_REWRITE_LIMIT` (default: `3`), `CHUNK_SENTENCE_AWARE` (default: `1`)

## Code Style

- Python 3.9 compatible: use `Optional[X]` and `Union[X, Y]`, NOT `X | Y` syntax
- No temporal features in ML pipeline (no dates, time-based features)
- Niche waitlist data is meaningless — drop waitlisted rows before training
- When making multiple ML changes, batch them and retrain once
- Auth token must be passed via `connect_args`, not URL query string (libSQL driver requirement)
- **Never use `MilvusClient` with Zilliz Serverless** — it hangs indefinitely on connection. Use the ORM API (`connections.connect` + `Collection`) for everything including BM25/hybrid schema creation
- **Never put variable content in system prompts** (school names, timestamps, user data, length budgets). System prompts must be static for OpenAI prompt caching to work. All variable content goes in the user message. The shared `COLE_PREAMBLE` in `prompts.py` is the cacheable prefix. Multi-turn instructions are baked into each system prompt as static text — do NOT conditionally append them. Never use `.format()` on system prompt constants.
- **Zustand persist uses a custom `merge` for `profile`** — when adding new fields to `ProfileData`, defaults are backfilled automatically via the deep merge in `store.ts`. Do not add manual migration logic; just ensure the default in the initializer covers the new field.
