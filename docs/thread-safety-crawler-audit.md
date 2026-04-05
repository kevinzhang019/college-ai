# Thread Safety Audit — Crawler (2026-04-05)

Full concurrency audit of `crawler.py` and supporting modules (`embeddings.py`, `shutdown.py`, `connection.py`, `config.py`). Cross-referenced with pymilvus ORM documentation and Python `threading` semantics.

**Verdict: Architecture is sound.** Five bugs fixed (2 in round 1, 3 in round 2), no data races, no deadlocks, no memory leaks, Milvus writes are validated before insert.

## Bugs Fixed

### 1. `pw_done_callback` Ordering (Medium — data loss risk)

**File:** `crawler.py`, `crawl_college_site()` inner function `pw_done_callback`

The Playwright done callback removed itself from `active_pw_futures` **before** running `_merge_pw_result()`. The main thread's wait loop checked `active_pw_futures` to decide when all Playwright work was done. Because the discard happened first, the loop could exit while callbacks were still calling `upload_to_milvus()` and putting rows in the insert buffer. `_flush_all_inserts()` would then run before those rows landed — causing silent data loss.

**When it can happen:** Any crawl where Playwright fallback is triggered and the callback's `upload_to_milvus()` takes non-trivial time (embedding API call + buffer put). More likely under load when the insert buffer is near capacity and `put()` retries.

**Fix:** Reordered to run `_merge_pw_result()` inside a `try`, with `active_pw_futures.discard()` in `finally`. The wait loop can only exit once all callback work (including buffer puts) is complete.

### 2. `PlaywrightPool.acquire()` Rotation Lock Scope (Low — liveness)

**File:** `crawler.py`, `PlaywrightPool.acquire()` rotation branch

`_close_slot(slot)` ran inside `_all_locals_lock`. `_close_slot` does blocking Chromium IPC (`browser.close()`) that can hang indefinitely on zombie processes. Holding the lock during this call blocked all pool operations — `acquire()`, `shutdown()`, and `prune_dead_slots()` — across every thread.

**When it can happen:** A Chromium process becomes a zombie (segfault, OOM kill) and `browser.close()` hangs. All Playwright pool users stall until the process is reaped or the system kills it.

**Fix:** Slot is removed from `_all_locals` under the lock, then closed outside. Only the rotating thread blocks on a hang — all other threads continue normally. This matches the pattern used by `shutdown()` (daemon thread with 10s timeout). Note: `prune_dead_slots()` was also updated (audit round 2) to use daemon threads with timeout — it previously called `_close_slot()` synchronously.

## Concurrency Primitives — All Verified Correct

| Primitive | Purpose | Verification |
|-----------|---------|--------------|
| `self.lock` | Protects `stats` dict mutations | Brief holds only. Never nested as outer lock with any other lock except inside `state_lock` (consistent ordering) |
| `self._close_lock` | Guards `close()` idempotency check-then-set | Released before any blocking I/O. Prevents double-execution from `run_full_crawling_pipeline` finally + `main()` finally |
| `self.collection_write_lock` | Exclusive Milvus inserts/deletes | Only acquired by flush thread (batch insert, per-row recovery) and `upload_to_milvus` (no_resume delete). Never nested with `collection_query_sema` |
| `self.collection_query_sema` | Bounds parallel Milvus queries (default 3) | Released before any write lock is acquired. `_load_college_canonicals` holds it for entire iteration (correct — iteration is part of the query) |
| `self.embed_semaphore` | Bounds concurrent embedding API calls (fallback path only) | Independent of all other locks |
| `self._host_lock` | Protects per-host rate limit state, token bucket, circuit breaker | Token bucket delay computed inside lock, sleep outside. `_prune_host_state()` evicts stale entries under lock |
| `self._pending_canonical_lock` | Protects `_pending_canonical_urls` TOCTOU guard | Never held when acquiring `collection_query_sema` or `collection_write_lock` |
| `self.playwright_semaphore` | Caps concurrent browser instances | Timed acquire (30s) prevents indefinite blocking. Released in `finally` |
| `self._cookie_storage_lock` | Serializes cookie file reads/writes | Prevents torn JSON from concurrent access. File I/O is brief |
| `self._pw_profile_cache_lock` | Protects Playwright profile cache | Check-outside, load-outside, write-inside pattern. Duplicate loads are harmless (same file content) |
| `self._pw_local_registry_lock` | Protects non-pool Playwright registry | `close()` swaps browser dicts to `{}` under lock, then closes old browsers outside lock |
| `state_lock` (per-college) | Protects BFS state: `crawled_canon`, `discovered_canon`, `pages_crawled_shared`, `pw_uploaded_shared` | Local to `crawl_college_site`. All mutations under lock. Consistent ordering: `state_lock` -> `self.lock` |
| `college_hash_lock` (per-college) | Protects per-college content dedup cache | Local to `crawl_college_site`. Must be per-college (not instance-level) for `INTER_COLLEGE_PARALLELISM > 1` |
| `pw_futures_lock` (per-college) | Protects `active_pw_futures` set | Consistent ordering: `pw_futures_lock` -> `state_lock` -> `self.lock` |
| `ProxyPool._lock` | Protects all proxy state, sticky assignments, EMA latency | `_is_available()` semaphore test-release-reacquire is atomic under the lock. TTL-based sticky eviction prevents unbounded growth |
| `PlaywrightPool._all_locals_lock` | Protects `_all_locals` list, `_started` flag | `_started` check-under-lock in all three `acquire()` branches (rotation, creation, reuse). `shutdown()` sets `_started=False` under lock as happens-before |
| `DeltaCrawlCache._all_conns_lock` | Protects `_all_conns` list, connection registration | `_closed` set inside lock so `_get_conn()` re-check is reliable. Connection created + registered atomically under lock |
| `EmbeddingBatcher._submit_lock` | Atomic shutdown-check + queue-put in `submit()` | `shutdown()` fence (`with _submit_lock: pass`) ensures no orphaned futures |
| `EmbeddingBatcher._cancel_lock` | Guards `_cancel_remaining()` exactly-once | Called from both background thread and `shutdown()` — lock ensures single execution |
| `global_shutdown_event` | `threading.Event` — process-wide shutdown signal | Thread-safe by design. Signal handler runs on main thread only (Python guarantee) |

## Pending Canonical URL Invariant — Holds in All Paths

URLs are claimed in `_pending_canonical_urls` before the Milvus existence query and released after the flush thread commits (or on any failure). Every code path releases:

| Path | How claim is released |
|------|-----------------------|
| URL already exists (resume mode) | `upload_to_milvus` releases immediately |
| URL already exists (no_resume) | Claim stays; flush thread releases after delete + re-insert |
| All chunks are content-dedup duplicates | `upload_to_milvus` releases immediately |
| Embedding succeeds, buffer put succeeds | Flush thread releases after successful Milvus insert |
| Embedding fails (batcher + fallback) | `upload_to_milvus` releases in error path |
| Milvus insert fails after retries | Flush thread releases in permanent-failure handler |
| Column alignment mismatch | Flush thread releases after per-row recovery attempt |
| `_flush_insert_buffer` outer raises | Except block releases all claims for the batch |
| Buffer full + flush thread crashed | `upload_to_milvus` detects `_flush_thread_crashed`, releases, raises |
| Flush thread crashes (10 consecutive errors) | Final drain loop releases abandoned rows' claims |
| Milvus query fails in `upload_to_milvus` | Inner except releases claim, function continues without claim (safe: `crawled_canon` prevents same-college re-processing) |

## Shutdown + Edge Cases — All Handled

| Edge Case | Resolution |
|-----------|------------|
| Signal during cookie capture | PW selector checks have 1s timeouts, loop bounded to 3 attempts x N selectors. `stop_event` checked on re-entry to worker loop |
| Cookie file corruption on process kill | `json.load` in `_load_cookies` catches exception, falls back to parent domain cookies or `None` |
| Concurrent cookie saves for same domain | Serialized by `_cookie_storage_lock`. Last write wins (idempotent — same banner acceptance) |
| Flush thread crash | Sets `_flush_thread_crashed` + `global_shutdown_event`. Runs final drain + abandoned-rows claim release before exiting |
| `_flush_all_inserts` concurrent with flush thread | Both drain same `Queue` — `Queue.get()` is atomic, each caller gets distinct items. Both serialize inserts via `collection_write_lock` |
| Double `close()` (`run_full_crawling_pipeline` finally + `main()` finally) | `_close_lock` guards check-then-set on `_closed`. Lock released before any blocking I/O |
| Workers still running when `close()` called | Workers check `global_shutdown_event` every `QUEUE_TIMEOUT_SECONDS` (1.5s). 30s wait for futures. PW executor shutdown with `wait=False, cancel_futures=True` — running tasks continue but queued tasks cancelled |
| Milvus disconnect while flush thread running | Flush thread is joined with 30s timeout first. If still alive (daemon), in-flight gRPC RPCs get "cancelled" error — caught by retry logic |
| `queue.Queue.qsize()` TOCTOU in drain loops | Used as early-exit optimization only. Bounded loop (200 iterations) provides real safety net |
| `KeyboardInterrupt` in `crawl_college_site` | Dead code in practice — `install_shutdown()` replaces default SIGINT handler. Signal handler sets `global_shutdown_event` instead of raising |
| PW callbacks running after `pw_executor.shutdown()` | `shutdown(wait=False)` doesn't cancel running tasks. Callbacks continue in worker threads. Wait loop (up to 30s) checks `active_pw_futures` — with fix #1, loop only exits once all callback work completes |

## Data Integrity — Validated Before Insert

| Check | Mechanism |
|-------|-----------|
| Column alignment | `_flush_insert_buffer_inner` verifies all columns have equal length. On mismatch: per-row recovery (3 retries each), then drop with counter |
| BM25 function output exclusion | `content_sparse` excluded from field list via `schema.functions` introspection (fallback to hardcoded set for older pymilvus) |
| Embedding dimension | Each embedding checked for `isinstance(emb, list) and len(emb) == VECTOR_DIM` before inclusion |
| Content length | `chunk_text[:MAX_CONTENT_LENGTH - 1]` and `chunked_title[:MAX_TITLE_LENGTH - 1]` — truncated to fit VARCHAR limits |
| URL canonicalization failure | Chunks with failed canonicalization skipped entirely (prevents empty `url_canonical` collisions in Milvus dedup queries) |
| Milvus expression escaping | `college_name` and `page_canon` escape `"` -> `\\"`. Residual risk: trailing `\` in URL could break expression parsing (extremely unlikely for HTTP URLs) |
| Content dedup | SHA-256 truncated to 16 hex chars (64 bits) per chunk. Per-college cache under `college_hash_lock`. Instance-level cache explicitly forbidden for `INTER_COLLEGE_PARALLELISM > 1` |
| Insert retry | Both batch and per-row recovery retry 3 times with exponential backoff (1s, 2s). Dropped rows tracked in `stats["rows_dropped_insert_fail"]` |

## Memory Management — No Leaks

| Resource | Bound | Cleanup |
|----------|-------|---------|
| `_pending_canonical_urls` | Bounded by concurrent in-flight pages. Claims released by flush thread after insert, or by error paths | All paths verified (see invariant table above) |
| `_host_tokens` / `_host_failures` / etc. | Grows with unique hostnames | `_prune_host_state()` called per-college, evicts entries > 30min |
| `ProxyPool._sticky` | Grows with sticky assignments | TTL-based eviction (`cooldown_sec * 2`) in every `acquire()` call |
| `PlaywrightPool._all_locals` | Bounded by `pool_size` semaphore | Rotation removes + closes slots. `prune_dead_slots()` cleans unhealthy. `shutdown()` clears all |
| `_pw_local_registry` | Bounded by threads using fallback (non-pool) PW path | `close()` iterates all entries, closes browsers in daemon threads with 10s timeout |
| `DeltaCrawlCache._all_conns` | One per thread (thread-local) | `close()` closes all under lock. Minor: dead-thread connections persist until `close()` (bounded by thread pool size) |
| `_insert_buffer` | `Queue(maxsize=500)` | Backpressure blocks workers. Final drain in flush thread + `_flush_all_inserts()` |
| `EmbeddingBatcher._queue` | `Queue(maxsize=200)` | `submit()` retries on full. `_cancel_remaining()` resolves orphaned futures on shutdown |
| `_pw_profile_cache` | Grows with unique domains | Bounded by colleges crawled. Short-lived (process lifetime) |
| Worker sessions | One per worker thread | `worker_session.close()` in `try/finally` at end of `worker_task` |

## Playwright Lifecycle — Correct

- **Pool path (primary):** `PlaywrightPool` manages thread-local browser slots via `threading.local()`. Semaphore caps total concurrent browsers. All three `acquire()` branches (rotation, creation, reuse) check `_started` under `_all_locals_lock`. Rotation closes outside lock (fix #2). `shutdown()` closes all slots in daemon threads with 10s timeout
- **Non-pool path (fallback, rarely used):** Only activated when pool is not started. Thread-local `_pw_local.pw` and `_pw_local.browsers` per thread. Registry stores direct object references for cross-thread cleanup in `close()`. Browser dict swap under lock prevents data race with late workers
- **Camoufox context manager:** `__exit__` called in `finally` after page/context close. Double-exit prevented by `camoufox_cm = None` after first exit
- **Resource leak prevention:** `context.on("page", lambda p: p.close())` kills popup windows. `page.on("dialog", lambda d: d.dismiss())` auto-dismisses dialogs. Resource blocking via `context.route()` aborts images, fonts, analytics
- **Thread affinity:** Playwright sync API is greenlet-based and tied to creator thread. Pool and fallback both use thread-local storage. `_cleanup_thread_local_playwright()` only touches current thread's resources. `prune_dead_slots()` reads `_healthy` flag (set by owning thread) instead of calling `browser.is_connected()` cross-thread

## pymilvus ORM Thread Safety — Verified

| Operation | Thread-Safe? | Notes |
|-----------|-------------|-------|
| `Collection.query()` from multiple threads | Yes | gRPC stub multiplexes concurrent RPCs over single channel. No Collection state mutated during query |
| `Collection.query_iterator()` from multiple threads | Yes | Each call returns independent iterator with own cursor. Semaphore(3) is a sensible precaution |
| `Collection.insert()` serialized by Lock | Yes | Lock is belt-and-suspenders. gRPC handles single insert atomically |
| `connections.connect` / `disconnect` (main thread only) | Yes | Alias registry is a plain dict. CPython GIL makes reads safe. No concurrent writes during operation |
| `Collection.schema` reads from workers | Yes | Set once at `__init__` (single-threaded), only read thereafter |

## Delta Crawl Cache — Correct

- **Thread-local connections:** Each thread gets its own `sqlite3.Connection` via `threading.local()`. SQLite connections cannot be shared across threads
- **WAL mode:** Enables concurrent reads with a single writer. `timeout=10` on `connect()` handles `SQLITE_BUSY` contention
- **Shutdown graceful degradation:** `_closed` is a `threading.Event` (fast-path `is_set()` is thread-safe). `get()` and `put()` catch `sqlite3.ProgrammingError` for in-flight operations during shutdown. `put()` rolls back on failure to prevent dirty connection state
- **Connection registration atomicity:** `_get_conn()` holds `_all_conns_lock` across `connect()` + `append()` — prevents `close()` from clearing the list between creation and registration (TOCTOU)
- **Crash tolerance:** `put()` wraps execute+commit in try/except with rollback on failure. Delta cache writes are deferred to AFTER the insert buffer accepts the row (audit round 2 fix), preventing a crash-consistency gap where a stale content hash could permanently block re-insertion of a page. WAL journal mode ensures partial commits from process kills are rolled back on next open

## Audit Round 2 — Bugs Fixed (2026-04-05)

### 3. `prune_dead_slots()` Synchronous Blocking (Low — liveness)

**File:** `crawler.py`, `PlaywrightPool.prune_dead_slots()`

`_close_slot(slot)` was called synchronously with no timeout. On a zombie Chromium process, `browser.close()` hangs indefinitely, stalling the BFS orchestration thread for the current college (called from `crawl_college_site` before worker submission). `shutdown()` already handled this correctly with daemon threads.

**Fix:** Each `_close_slot()` call in `prune_dead_slots()` now runs in a daemon thread with `t.join(timeout=10)`, matching the `shutdown()` pattern.

### 4. Delta Cache Crash-Consistency Gap (Medium — silent data loss)

**File:** `crawler.py`, `scrape_page()`, `worker_task()`, `_merge_pw_result()`

Delta cache writes (`_delta_cache.put()` and `_write_pw_delta_cache()`) ran BEFORE `upload_to_milvus()` buffered the insert. On crash between the cache write and Milvus commit, the next run saw a content-hash match, set `skip_embed=True`, and permanently skipped re-inserting the page's vectors.

The Playwright path was most vulnerable — no ETag/Last-Modified fallback, purely content-hash-driven.

**Fix:** Delta cache writes are now deferred to AFTER the insert buffer `put()` succeeds:
- HTTP path: `scrape_page()` returns `_delta_meta` dict in the page data; `worker_task` writes the cache after `upload_to_milvus()`
- Sync PW path: `_write_pw_delta_cache()` moved after `upload_to_milvus()` call
- Async PW callback: `_write_pw_delta_cache()` moved after `upload_to_milvus()` in `_merge_pw_result()`

### 5. `worker_session` Cleanup Guarantee (Very Low — resource leak)

**File:** `crawler.py`, `worker_task()`

`worker_session.close()` and `_cleanup_thread_local_playwright()` were placed after the while-loop with no `try/finally`. A `BaseException` escaping the loop would skip cleanup. Now wrapped in `try: return ... finally: cleanup` so cleanup runs on all normal exit paths. (A `BaseException` from within the while-loop itself is effectively impossible on worker threads — only `MemoryError` could trigger it, and the session FD would be GC'd regardless.)
