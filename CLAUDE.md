# College AI v2

College admissions prediction + RAG Q&A platform. Scrapes college data from multiple sources, trains ML models, and serves predictions + AI-generated answers via a FastAPI backend.

## Quick Start

```bash
pip install -r requirements.txt && playwright install
python -m college_ai.scraping.scorecard_client    # seed ~6,500 schools
python -m college_ai.scraping.niche_scraper       # scrape Niche scattergrams + grades
python scripts/build_crawler_seeds.py             # (re)generate general2.csv seed list from Turso (top N by student_size)
python -m college_ai.scraping.crawler             # BFS crawl → Zilliz (hybrid schema)
python scripts/count_legacy_chunked_urls.py       # audit Zilliz for URLs still using the legacy 512-token chunker (per school)
python -m college_ai.ml.data_pipeline export      # export training parquet
python -m college_ai.ml.train                     # single global model
python -m college_ai.ml.train_bucketed            # per-bucket models
cd frontend && npm install && cd ..                # install frontend deps
./start.sh                                        # FastAPI :8000 + frontend :3000
```

## Architecture Docs

- [ML Pipelines](docs/ml-pipelines.md) — two LightGBM architectures: single global model (36 features, Optuna, calibration) and bucketed models (4 models: reach/competitive/match/safety with focal loss, linear trees, Venn-ABERS)
- [Thread Safety — Crawler](docs/thread-safety-crawler.md) — **CRITICAL**: locks, semaphores, thread-local storage, and shutdown ordering in `crawler.py`
- [Thread Safety — Niche](docs/thread-safety-niche.md) — **CRITICAL**: DBWriterThread, rate limiter, sentinel guarantee in `niche_scraper.py`
- [Thread Safety Audit — Niche](docs/thread-safety-niche-audit.md) — Full concurrency audit (2026-04-05): verified no data races, deadlocks, or memory leaks
- [Thread Safety Audit 2 — Niche](docs/thread-safety-niche-audit-2.md) — Memory-leak follow-up (2026-04-17): fixed worker-exit orphan (daemon cleanup on non-owner thread leaked Playwright + Chromium), `DBWriterThread` daemon flag, queue sizing, bounded `f.result` timeout, owner-thread guards on all Playwright-touching methods
- [Thread Safety Audit — Crawler](docs/thread-safety-crawler-audit.md) — Full concurrency audit (2026-04-05): six bugs fixed (pw_done_callback ordering, PlaywrightPool rotation lock scope, prune_dead_slots blocking, delta cache crash-consistency, worker_session cleanup, dead-slot lockout), Playwright resilience fixes (2026-04-16: retry swallowing, pool liveness via is_connected(), orphan slot cleanup, EXCLUDED_URL_PATTERNS wiring, _close_slot_safe hang prevention, bounded pw_executor shutdown), no data races, deadlocks, or memory leaks
- [Crawler](docs/crawler.md) — BFS crawler (curl_cffi, Playwright, camoufox, delta crawling) → Zilliz hybrid search
- [Niche Scraper](docs/niche-scraper.md) — Niche.com scattergrams + letter grades via Camoufox
- [Scorecard Client](docs/scorecard-client.md) — US DOE College Scorecard API → schools table
- [RAG Pipeline](docs/rag-pipeline.md) — v2: multi-school extraction (up to 5, span-tracking dedup) → unified LLM classifier (gpt-4.1-nano → query_type + complexity + categories + niche_categories) → greeting/essay short-circuits → category-aware school data injection (Turso DB → selective `[SCHOOL DATA]` blocks per detected school, parallel fetch for all query types when schools known upfront, post-retrieval discovery fallback for ranking/comparison without detected schools) → hybrid search (dense + BM25, configurable RRF/WeightedRanker, retrieval result cache, 50 candidates) → Cohere rerank-v4.0-pro (32K context, metadata-enriched docs, relevance threshold, ranking boost by niche_rank + niche_categories) → two-tier model routing (simple Q&A → gpt-4.1-nano, everything else → gpt-5.4-mini) → specialized generators (Q&A, Ranking, Comparison, Essay Ideas, Essay Review, Admission Prediction) with dedicated prompt instructions (RANKING_INSTRUCTIONS, COMPARISON_INSTRUCTIONS), SSE streaming, multi-turn history, multi-query retrieval (essay + admissions), profile context, sentence-aware chunking, contextual chunk prefixes, and RAGAS evaluation framework
- [Multi-School Extraction](docs/multi-school-extraction.md) — two-pass school detection: first pass on raw query (~107 hardcoded + 13 flagship + ~800 DB aliases → substring → fuzzy, span-tracking dedup, capped at 5), second pass on punctuation-stripped + shorthand-expanded text (two-phase expansion: word shorthands like bama/mich/ariz then structural like u/state/uni/univ, plus 50 uppercase + 43 lowercase state codes). Flagship aliases map bare "University of X" → flagship campus (e.g. Michigan → Ann Arbor), multi-school IN filter for retrieval, per-school `[SCHOOL DATA]` blocks
- [Database](docs/database.md) — three tables (schools with 51 category-prefixed columns, applicant_datapoints, niche_grades), Turso/libSQL connection, inline migrations
- [API & Frontend](docs/api.md) — FastAPI endpoints (/ask, /ask/stream, /predict, /compare), React frontend with 3 modes (Chat, Admissions Calculator, My Profile)
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
**Vector DB / RAG:** `pymilvus>=2.5.0`, `openai`, `tiktoken`, `cohere`, `rapidfuzz`
**Database:** `sqlalchemy-libsql`
**ML:** `lightgbm`, `optuna`, `scikit-learn`, `shap`, `venn-abers`
**API:** `fastapi`, `uvicorn`
**Frontend:** `react`, `zustand`, `framer-motion`, `tailwindcss`, `@headlessui/react`, `react-markdown`, `rehype-raw`

## Required Environment Variables

`ZILLIZ_URI`, `ZILLIZ_API_KEY`, `OPENAI_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `SCORECARD_API_KEY`, `COHERE_API_KEY` (optional — reranking degrades gracefully without it)

**Optional model overrides:** `MODEL_SIMPLE` (default: `gpt-4.1-nano`), `MODEL_STANDARD` (default: `gpt-5.4-mini`)

**Optional RAG tuning:** `RETRIEVAL_NPROBE` (default: `64`), `RAG_RETRIEVAL_TOP_K` (default: `50`), `RAG_RANKER_TYPE` (default: `rrf`, alt: `weighted`), `RAG_RANKER_RRF_K` (default: `60`), `RAG_DENSE_WEIGHT` (default: `0.7`), `RAG_SPARSE_WEIGHT` (default: `0.3`), `RAG_HISTORY_LIMIT` (default: `6`), `RAG_HISTORY_REWRITE_LIMIT` (default: `3`), `RAG_HISTORY_REWRITE_CHARS` (default: `400`), `CHUNK_SENTENCE_AWARE` (default: `1`)

## Code Style

- Python 3.9 compatible: use `Optional[X]` and `Union[X, Y]`, NOT `X | Y` syntax
- No temporal features in ML pipeline (no dates, time-based features)
- Niche waitlist data is meaningless — drop waitlisted rows before training
- When making multiple ML changes, batch them and retrain once
- Auth token must be passed via `connect_args`, not URL query string (libSQL driver requirement)
- **Never use `MilvusClient` with Zilliz Serverless** — it hangs indefinitely on connection. Use the ORM API (`connections.connect` + `Collection`) for everything including BM25/hybrid schema creation
- **Never put variable content in system prompts** (school names, timestamps, user data, length budgets). System prompts must be static for OpenAI prompt caching to work. All variable content goes in the user message. The shared `COLE_PREAMBLE` in `prompts.py` is the cacheable prefix. Multi-turn instructions are baked into each system prompt as static text — do NOT conditionally append them. Never use `.format()` on system prompt constants.
- **sklearn-compatible wrapper classes must inherit from `sklearn.base.BaseEstimator`** — do not duck-type. `CalibratedClassifierCV` and `FrozenEstimator` require `__sklearn_tags__()` / `get_params()` which `BaseEstimator` provides. Models must be retrained after changing wrapper class inheritance. **Classifier wrappers (sklearn ≥1.6) must ALSO inherit from `ClassifierMixin`, with the mixin listed first in the MRO: `class Wrapper(ClassifierMixin, BaseEstimator)`.** Setting `_estimator_type = "classifier"` as a class attribute is no longer sufficient — sklearn 1.6+ resolves estimator type via `__sklearn_tags__()`, and without `ClassifierMixin` the wrapper is treated as a regressor (breaks `permutation_importance(scoring='neg_log_loss')` with a `predict_proba`/regressor error).
- **Schools table columns are category-prefixed** (`identity_`, `admissions_`, `student_`, `cost_`, `aid_`, `outcome_`, `institution_`). ML code (`data_pipeline.py`, `predict.py`, `feature_utils.py`, `train.py`, `bucket_configs.py`) references these prefixed names directly — there is no prefix→legacy shim. When adding a new School column to the model, reference its prefixed name in `data_pipeline.py`'s training SELECT and in `_get_school_features()` inside `predict.py`; the name flows through the shared feature-engineering layer unchanged. Always use the appropriate category prefix when adding new School columns.
- **Zustand persist uses a custom `merge` for `profile`** — when adding new fields to `ProfileData`, defaults are backfilled automatically via the deep merge in `store.ts`. Do not add manual migration logic; just ensure the default in the initializer covers the new field.
- **Re-run `playwright install` after upgrading the `playwright` package.** Each Playwright release pins a specific Chromium build (e.g. v1208 wants `chromium_headless_shell-1208`). If only the older build is cached, `pw.chromium.launch()` raises `Executable doesn't exist`, and the failed launch leaks the started Playwright sync runtime — poisoning the worker thread's asyncio loop and cascading "Sync API inside the asyncio loop" errors across every subsequent fallback. The crawler's `_create_browser()` now cleans up the leaked runtime on failure (see `docs/thread-safety-crawler-audit.md` Bug #22), but you still need fresh browser binaries.
