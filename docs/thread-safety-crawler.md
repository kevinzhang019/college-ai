# Thread Safety — Crawler (CRITICAL)

The crawler (`crawler.py`) is heavily multithreaded. Read this before modifying `crawler.py`. See also the [full concurrency audit](thread-safety-crawler-audit.md) for verified invariants and fixed bugs.

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
- **`self._close_lock`** — guards `close()` idempotency check to prevent double-execution when two threads (e.g., `run_full_crawling_pipeline`'s finally + `main()`'s finally) race into `close()` simultaneously

## Key Patterns

- **`PlaywrightPool`** — thread-local browser instances (`threading.local()`) because Playwright sync API is not thread-safe. Browsers rotate after `PLAYWRIGHT_POOL_ROTATE_AFTER=50` uses for fresh fingerprints. Supports both Chromium and Camoufox. `shutdown()` sets `_started = False` inside `_all_locals_lock` to create a happens-before with any thread in `acquire()` — this prevents new browser creation after shutdown begins. All three branches in `acquire()` (rotation, creation, **and reuse**) check `_started` under `_all_locals_lock` before returning a browser. The reuse branch also verifies `slot._healthy` to guard against a narrow race where `shutdown()` closes a slot between semaphore acquire and the `_started` re-check. Both `prune_dead_slots()` **and the rotation branch in `acquire()`** collect/remove dead slots under `_all_locals_lock` but close them **outside** the lock — `_close_slot()` does blocking I/O (Chromium IPC) that could hang, and holding the lock during that call would block all `acquire()` and `shutdown()` callers. `shutdown()` and `prune_dead_slots()` close slots in **daemon threads with a 10s timeout** so the calling thread is never blocked indefinitely. The rotation branch in `acquire()` closes synchronously on the rotating worker thread (intentional — the semaphore is still held, so a daemon thread would leak the slot). The same daemon-thread timeout pattern is used for non-pool (fallback) Playwright cleanup in `close()`.
- **`DeltaCrawlCache`** — thread-local SQLite connections (SQLite connections cannot be shared across threads). WAL journal mode. Stores ETag, Last-Modified, content hash per URL. All connections are tracked in `_all_conns` (protected by `_all_conns_lock`) for proper cleanup on shutdown. `put()` wraps `execute()`+`commit()` in try/except with `conn.rollback()` on failure to prevent dirty thread-local connection state. **Cache writes are deferred** — `scrape_page()` returns delta metadata (`_delta_meta`) in the page dict instead of writing the cache directly, and the caller (`worker_task` / `_merge_pw_result`) writes the cache AFTER `upload_to_milvus()` has accepted the row into the insert buffer. This prevents a crash-consistency gap where a stale content hash could permanently block re-insertion on the next run.
- **`MilvusFlushThread`** — dedicated **daemon** thread drains a bounded insert `queue.Queue(maxsize=500)` in batches (`MILVUS_INSERT_BUFFER_SIZE=50` every 2s), reducing `collection_write_lock` contention ~50x. Daemon so a hung Milvus connection cannot prevent process exit after the 30s join timeout. The bounded queue provides backpressure — worker threads block on `put()` when the queue is full, preventing unbounded memory growth under Milvus backpressure. Validates column alignment before each insert to prevent corrupted data; BM25 function output fields (`content_sparse`) are excluded from the field list since Milvus auto-generates them. On alignment mismatch, attempts per-row recovery before dropping the batch. Both the main insert and per-row recovery retry up to 3 times with exponential backoff (1s, 2s) before dropping rows. Dropped rows are tracked in `stats["rows_dropped_insert_fail"]`. The main flush loop tracks consecutive errors with exponential backoff (1s→30s cap) and aborts after 10 consecutive failures to prevent tight CPU spin on a dead Milvus connection. Final drain uses a bounded loop (max 200 iterations) to cap iterations in case late callbacks keep adding rows. After successful insert, releases pending canonical URL claims so subsequent queries see the committed data.
- **`pw_done_callback` ordering** — the Playwright done callback runs `_merge_pw_result()` (which uploads data to the insert buffer) **before** removing the future from `active_pw_futures`. This guarantees the main thread's wait loop does not exit prematurely while callbacks are still uploading — the set only empties once all callback work (including buffer puts) is complete.
- **`EmbeddingBatcher`** — consolidates embedding requests from multiple threads into fewer API calls (up to 100 texts per call, max wait 200ms). On shutdown, drains remaining queue items so pending futures resolve instead of hanging. Shutdown join timeout is 15s. `_cancel_remaining()` is guarded by `_cancel_lock` so it runs exactly once even when called from both the background thread and the main thread. `get_openai_client()` uses double-checked locking for thread-safe singleton initialization.
- **`ProxyPool`** — lock-protected state with per-proxy semaphores, EMA latency tracking, cooldown on failures, sticky session assignment

## Inter-College Parallelism

When `INTER_COLLEGE_PARALLELISM > 1`, multiple `crawl_college_site()` calls run concurrently. Per-college state (canonical URL sets, content hash caches) **must** be local to `crawl_college_site`, not instance attributes. Instance-level state is only safe if it's read-only or protected by a lock shared across all colleges (e.g., `self.lock` for stats).

- **`_load_college_canonicals`** retries 3 times with exponential backoff (1s, 2s) on Milvus query failure. If all retries fail, raises the exception so `crawl_college_site` skips the college entirely — this prevents mass duplicate inserts that would occur if crawling proceeded with an empty dedup set.

- **`self._base_headers_snapshot`** — frozen `dict` snapshot of `self.session.headers` taken at `__init__` time (single-threaded). Used to seed per-thread `worker_session` and `_test_session` instances. **Never read `self.session.headers` from worker threads** — `scrape_page()` may mutate `request_session.headers` on 403 retries, making it unsafe for concurrent reads.

## Shutdown Ordering

All shutdown flows through the idempotent `close()` method. `close()` uses `self._close_lock` to guard a check-then-set on `self._closed`, preventing double-execution when two threads race (e.g., `run_full_crawling_pipeline`'s `finally` block + `main()`'s `finally` block after a signal). The lock is released before any I/O to avoid holding it during blocking operations. `run_full_crawling_pipeline` calls `close()` in a `finally` block, and `main()` adds a second `finally` as a safety net — double-calls are now properly guarded.

The `close()` shutdown sequence is order-dependent:
1. Workers stop (via `stop_event` / `global_shutdown_event`)
2. `EmbeddingBatcher.shutdown()` — drains pending embedding futures (which may queue final inserts)
3. `_insert_flush_stop` + join(30s) — flush thread drains remaining insert buffer (daemon thread reaped at exit if hung)
4. Milvus `connections.disconnect("crawler")` — closes the crawler's Zilliz connection (uses alias `"crawler"` to avoid killing the retriever's `"default"` connection)
5. `PlaywrightPool.shutdown()` — closes all browser instances (timeout-protected per slot)
6. Non-pool Playwright cleanup — closes fallback thread-local browsers (timeout-protected per entry)
7. `DeltaCrawlCache.close()` — closes SQLite connections
8. `self.session.close()` — releases HTTP session socket file descriptors

Changing this order can cause data loss (e.g., stopping the flush thread before the batcher means late-resolved embeddings never get inserted).

**KeyboardInterrupt:** `crawl_college_site` catches `KeyboardInterrupt` and explicitly shuts down `pw_executor` (Playwright worker pool) before exiting, preventing orphaned browser processes. The `global_shutdown_event` propagates to all worker threads.

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine` and `_session_factory` during `reset_engine()`. `get_session()` captures *and invokes* `_session_factory` under this lock so that a concurrent `reset_engine()` cannot dispose the engine before the session is created. (The factory must be invoked inside the lock, not just captured — otherwise `engine.dispose()` in `reset_engine()` could invalidate the pool before `factory()` opens a connection.)
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`.

## Milvus Connection Aliases

The crawler and retriever use **separate** pymilvus connection aliases to avoid interference:
- **Crawler:** `alias="crawler"` (`MultithreadedCollegeCrawler._MILVUS_ALIAS`). All ORM calls (`Collection`, `utility.has_collection`, etc.) pass `using="crawler"`.
- **Retriever:** `alias="default"` (`HybridRetriever._get_collection`). Uses double-checked locking (`_client_lock`) for thread-safe lazy initialization since FastAPI serves concurrent requests.

This separation ensures `close()` disconnecting the crawler alias does not kill the retriever's connection when both run in the same process.

## General Rules

- Never share Playwright browser instances across threads
- Never share SQLite connections across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
- Per-college mutable state must be local to `crawl_college_site`, not `self.*` attributes
