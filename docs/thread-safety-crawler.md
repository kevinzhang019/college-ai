# Thread Safety — Crawler (CRITICAL)

The crawler (`crawler.py`) is heavily multithreaded. Read this before modifying `crawler.py`.

## Concurrency Primitives

- **`self.lock`** — protects stats counters. Always acquire before reading/writing stats.
- **`self.collection_write_lock`** — exclusive lock for batched Milvus writes
- **`self.collection_query_sema`** — semaphore bounding parallel Milvus queries (default 3)
- **`self.embed_semaphore`** — bounds concurrent embedding API calls on the fallback paths only (default 3). The primary `EmbeddingBatcher` path serializes calls internally; the semaphore guards `get_embeddings_batch()` and `get_embedding()` when the batcher fails.
- **`self._host_lock`** — protects per-host rate limiting state and adaptive concurrency. Token bucket delay is computed inside the lock but the sleep happens outside to avoid blocking all hosts.
- **`college_hash_lock`** (local to `crawl_college_site`) — protects per-college content dedup cache. Must be per-college (not instance-level) to avoid races with `INTER_COLLEGE_PARALLELISM > 1`.
- **`self._pending_canonical_lock`** — protects `_pending_canonical_urls` set. Prevents TOCTOU duplicates between the Milvus existence query and the eventual batched insert. URLs are claimed before the query and released by the flush thread after commit (or on permanent failure).
- **`self.playwright_semaphore`** — caps concurrent browser instances
- **`self._cookie_storage_lock`** — serializes both cookie reads (`_load_cookies`) and writes (`_save_cookies`) to prevent torn JSON from concurrent access
- **`self._pw_profile_cache_lock`** — protects Playwright profile cache I/O

## Key Patterns

- **`PlaywrightPool`** — thread-local browser instances (`threading.local()`) because Playwright sync API is not thread-safe. Browsers rotate after `PLAYWRIGHT_POOL_ROTATE_AFTER=50` uses for fresh fingerprints. Supports both Chromium and Camoufox. `shutdown()` sets `_started = False` inside `_all_locals_lock` to create a happens-before with any thread in `acquire()` — this prevents new browser creation after shutdown begins. All three branches in `acquire()` (rotation, creation, **and reuse**) check `_started` under `_all_locals_lock` before returning a browser.
- **`DeltaCrawlCache`** — thread-local SQLite connections (SQLite connections cannot be shared across threads). WAL journal mode. Stores ETag, Last-Modified, content hash per URL. All connections are tracked in `_all_conns` (protected by `_all_conns_lock`) for proper cleanup on shutdown. `put()` wraps `execute()`+`commit()` in try/except with `conn.rollback()` on failure to prevent dirty thread-local connection state.
- **`MilvusFlushThread`** — dedicated non-daemon thread drains a bounded insert `queue.Queue(maxsize=500)` in batches (`MILVUS_INSERT_BUFFER_SIZE=50` every 2s), reducing `collection_write_lock` contention ~50x. The bounded queue provides backpressure — worker threads block on `put()` when the queue is full, preventing unbounded memory growth under Milvus backpressure. Validates column alignment before each insert to prevent corrupted data. On alignment mismatch, attempts per-row recovery before dropping the batch. Both the main insert and per-row recovery retry up to 3 times with exponential backoff (1s, 2s) before dropping rows. Dropped rows are tracked in `stats["rows_dropped_insert_fail"]`. The main flush loop tracks consecutive errors with exponential backoff (1s→30s cap) and aborts after 10 consecutive failures to prevent tight CPU spin on a dead Milvus connection. Final drain uses a bounded loop (max 200 iterations) to avoid TOCTOU on `queue.empty()`. After successful insert, releases pending canonical URL claims so subsequent queries see the committed data.
- **`EmbeddingBatcher`** — consolidates embedding requests from multiple threads into fewer API calls (up to 100 texts per call, max wait 200ms). On shutdown, drains remaining queue items so pending futures resolve instead of hanging. Shutdown join timeout is 15s. `_cancel_remaining()` is guarded by `_cancel_lock` so it runs exactly once even when called from both the background thread and the main thread. `get_openai_client()` uses double-checked locking for thread-safe singleton initialization.
- **`ProxyPool`** — lock-protected state with per-proxy semaphores, EMA latency tracking, cooldown on failures, sticky session assignment

## Inter-College Parallelism

When `INTER_COLLEGE_PARALLELISM > 1`, multiple `crawl_college_site()` calls run concurrently. Per-college state (canonical URL sets, content hash caches) **must** be local to `crawl_college_site`, not instance attributes. Instance-level state is only safe if it's read-only or protected by a lock shared across all colleges (e.g., `self.lock` for stats).

- **`_load_college_canonicals`** retries 3 times with exponential backoff (1s, 2s) on Milvus query failure. If all retries fail, raises the exception so `crawl_college_site` skips the college entirely — this prevents mass duplicate inserts that would occur if crawling proceeded with an empty dedup set.

- **`self._base_headers_snapshot`** — frozen `dict` snapshot of `self.session.headers` taken at `__init__` time (single-threaded). Used to seed per-thread `worker_session` and `_test_session` instances. **Never read `self.session.headers` from worker threads** — `scrape_page()` may mutate `request_session.headers` on 403 retries, making it unsafe for concurrent reads.

## Shutdown Ordering

The `run_full_crawling_pipeline` shutdown sequence is order-dependent:
1. Workers stop (via `stop_event` / `global_shutdown_event`)
2. `EmbeddingBatcher.shutdown()` — drains pending embedding futures (which may queue final inserts)
3. `_insert_flush_stop` + join — flush thread drains remaining insert buffer
4. `PlaywrightPool.shutdown()` — closes all browser instances
5. `DeltaCrawlCache.close()` — closes SQLite connections

Changing this order can cause data loss (e.g., stopping the flush thread before the batcher means late-resolved embeddings never get inserted).

The `close()` method also stops background threads (embedding batcher + flush thread) before disconnecting Milvus, following the same ordering. This ensures `close()` can be called independently of `run_full_crawling_pipeline` without hanging the process.

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine` and `_session_factory` during `reset_engine()`. `get_session()` captures *and invokes* `_session_factory` under this lock so that a concurrent `reset_engine()` cannot dispose the engine before the session is created. (The factory must be invoked inside the lock, not just captured — otherwise `engine.dispose()` in `reset_engine()` could invalidate the pool before `factory()` opens a connection.)
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`.

## General Rules

- Never share Playwright browser instances across threads
- Never share SQLite connections across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
- Per-college mutable state must be local to `crawl_college_site`, not `self.*` attributes
