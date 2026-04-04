# Thread Safety (CRITICAL)

Thread safety in the crawler and scraper is very important. Both `crawler.py` and `niche_scraper.py` are heavily multithreaded. When modifying these files, pay close attention to the concurrency primitives documented here.

## Crawler (`crawler.py`)

The crawler uses multiple concurrency primitives that must be respected:
- **`self.lock`** — protects stats counters. Always acquire before reading/writing stats.
- **`self.collection_write_lock`** — exclusive lock for batched Milvus writes
- **`self.collection_query_sema`** — semaphore bounding parallel Milvus queries (default 3)
- **`self.embed_semaphore`** — bounds concurrent embedding API calls (default 3)
- **`self._host_lock`** — protects per-host rate limiting state and adaptive concurrency. Token bucket delay is computed inside the lock but the sleep happens outside to avoid blocking all hosts.
- **`college_hash_lock`** (local to `crawl_college_site`) — protects per-college content dedup cache. Must be per-college (not instance-level) to avoid races with `INTER_COLLEGE_PARALLELISM > 1`.
- **`self.playwright_semaphore`** — caps concurrent browser instances
- **`self._cookie_storage_lock`**, **`self._pw_profile_cache_lock`** — I/O locks

### Key Patterns

- **`PlaywrightPool`** — thread-local browser instances (`threading.local()`) because Playwright sync API is not thread-safe. Browsers rotate after `PLAYWRIGHT_POOL_ROTATE_AFTER=50` uses for fresh fingerprints. Supports both Chromium and Camoufox.
- **`DeltaCrawlCache`** — thread-local SQLite connections (SQLite connections cannot be shared across threads). WAL journal mode. Stores ETag, Last-Modified, content hash per URL. All connections are tracked in `_all_conns` (protected by `_all_conns_lock`) for proper cleanup on shutdown.
- **`MilvusFlushThread`** — dedicated daemon thread drains insert `queue.Queue` in batches (`MILVUS_INSERT_BUFFER_SIZE=50` every 2s), reducing `collection_write_lock` contention ~50x. Validates column alignment before each insert to prevent corrupted data. On alignment mismatch, attempts per-row recovery before dropping the batch. Final drain retries up to 3 consecutive errors before abandoning remaining items.
- **`EmbeddingBatcher`** — consolidates embedding requests from multiple threads into fewer API calls (up to 100 texts per call, max wait 200ms). On shutdown, drains remaining queue items so pending futures resolve instead of hanging. Shutdown join timeout is 15s. `get_openai_client()` uses double-checked locking for thread-safe singleton initialization.
- **`ProxyPool`** — lock-protected state with per-proxy semaphores, EMA latency tracking, cooldown on failures, sticky session assignment

## Niche Scraper (`niche_scraper.py`)

- **`DBWriterThread`** — all DB writes go through a single `queue.Queue` to a dedicated writer thread. This eliminates cross-thread Turso WebSocket contention. Never write to the DB from worker threads directly. Includes keepalive SELECT every 60s. Atomic school writes (datapoints + NicheGrade committed together). Retries up to 3x with Hrana error detection and engine reset. If the writer thread crashes, it sets `shutdown_event` so workers stop promptly instead of scraping into a dead queue. After the writer exits, `scrape_all()` performs a best-effort drain of any remaining queue items using `drain_queue_best_effort()` — each item gets a single write attempt (no retries) to avoid masking the root crash cause.
- **`GlobalRateLimiter`** — lock-protected slot reservation. Workers compute and reserve their slot under the lock, then sleep *outside* the lock so `record_request()` and other workers aren't blocked. Scales delays by worker count (aggregate rate stays constant regardless of parallelism). `record_request()` only advances the timestamp — never regresses past a future reservation.
- **`JobClaimer`** — lock-protected dynamic work queue distributing schools across workers
- **`cookie_lock`** — protects `cookie_generation` reads/writes (held briefly, never during I/O)
- **`cookie_capture_lock`** — serializes interactive cookie captures (held for the duration of user interaction). Separated from `cookie_lock` so that other workers' per-school generation checks don't block during a capture.
- Worker threads only interact with thread-local Playwright browsers and the shared rate limiter

### Sentinel Guarantee

Every `_worker_loop` invocation calls `db_writer.worker_done()` exactly once, regardless of where a failure occurs — including the `NicheScraper()` constructor. This invariant ensures the DB writer thread always receives `num_workers` sentinels and terminates. The outer `try/finally` in `_worker_loop` wraps the entire function body including object construction. `scrape_all()` also sends compensation sentinels for workers that were never submitted to the executor (e.g., if shutdown interrupted the launch loop).

### Shutdown + PX Recovery Guard

If PerimeterX blocks both scrapes and shutdown prevents retries after browser restart, the worker skips `db_writer.submit()` when both `points` and `grades` are empty. This prevents the school from being permanently marked `no_data` (and skipped on resume) when it was only PX-blocked. The school remains pending for the next run.

## Inter-College Parallelism

When `INTER_COLLEGE_PARALLELISM > 1`, multiple `crawl_college_site()` calls run concurrently. Per-college state (canonical URL sets, content hash caches) **must** be local to `crawl_college_site`, not instance attributes. Instance-level state is only safe if it's read-only or protected by a lock shared across all colleges (e.g., `self.lock` for stats).

## Shutdown Ordering

The `run_full_crawling_pipeline` shutdown sequence is order-dependent:
1. Workers stop (via `stop_event` / `global_shutdown_event`)
2. `EmbeddingBatcher.shutdown()` — drains pending embedding futures (which may queue final inserts)
3. `_insert_flush_stop` + join — flush thread drains remaining insert buffer
4. `PlaywrightPool.shutdown()` — closes all browser instances
5. `DeltaCrawlCache.close()` — closes SQLite connections

Changing this order can cause data loss (e.g., stopping the flush thread before the batcher means late-resolved embeddings never get inserted).

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine`, `_session_factory`, and `ENGINE` during `reset_engine()`. `get_session()` captures *and invokes* `_session_factory` under this lock so that a concurrent `reset_engine()` cannot dispose the engine before the session is created. (The factory must be invoked inside the lock, not just captured — otherwise `engine.dispose()` in `reset_engine()` could invalidate the pool before `factory()` opens a connection.)
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`. External code should call `get_engine()` rather than importing `ENGINE` directly.
- **`ENGINE`** *(deprecated)* — module-level alias retained for backward compatibility. Prefer `get_engine()`.

## General Rules

- Never share Playwright browser instances across threads
- Never share SQLite connections across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
- Per-college mutable state must be local to `crawl_college_site`, not `self.*` attributes
