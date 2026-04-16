# Thread Safety Audit — Crawler (2026-04-05)

Full concurrency audit of `crawler.py` and supporting modules (`embeddings.py`, `shutdown.py`, `connection.py`, `config.py`). Cross-referenced with pymilvus ORM documentation and Python `threading` semantics.

**Verdict: Architecture is sound.** Six concurrency bugs fixed (2 in round 1, 3 in round 2, 1 in re-audit), 7 memory leak fixes + 8 memory optimization fixes, no data races, no deadlocks, Milvus writes are validated before insert.

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
| `state_lock` (per-college) | Protects BFS state: `crawled_canon`, `discovered_canon`, `pages_crawled_shared`, `pw_uploaded["count"]` | Local to `crawl_college_site`. All mutations under lock. Consistent ordering: `state_lock` -> `self.lock` |
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
| Sub-batch partial insert failure | Merged batch split into sub-batches of 50; if some succeed and others fail permanently, all claims released together after loop completes (partial Milvus data acceptable — URLs re-crawlable on next run) |
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
| Workers still running when `close()` called | Workers check `global_shutdown_event` every `QUEUE_TIMEOUT_SECONDS` (1.5s). 30s wait for futures. Normal path: `pw_executor.shutdown(wait=False)` + 45s bounded poll on `active_pw_futures` (fix #9). Shutdown path: `wait=False` with 5s poll |
| Milvus disconnect while flush thread running | Flush thread is joined with 30s timeout first. If still alive (daemon), in-flight gRPC RPCs get "cancelled" error — caught by retry logic |
| `queue.Queue.qsize()` TOCTOU in drain loops | Used as early-exit optimization only. Bounded loop (200 iterations) provides real safety net |
| `KeyboardInterrupt` in `crawl_college_site` | Dead code in practice — `install_shutdown()` replaces default SIGINT handler. Signal handler sets `global_shutdown_event` instead of raising |
| PW callbacks running after `pw_executor.shutdown()` | Normal path: `shutdown(wait=False)` + 45s bounded poll (fix #9). `close_orphaned_slots()` then reclaims dead-thread browser slots. Shutdown path: `shutdown(wait=False)` with 5s poll on `active_pw_futures`. With fix #1 ordering, set only empties once all callback work completes |

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
| Sub-batch splitting | Merged batch split into sub-batches of 50 rows before Milvus insert to stay under the 4MB gRPC message limit. Each sub-batch retried independently. Partial success tracked separately (`total_inserted`, `total_dropped`). All pending canonical claims released after the loop completes (not per sub-batch) |

## Memory Management — No Leaks

| Resource | Bound | Cleanup |
|----------|-------|---------|
| `_pending_canonical_urls` | Bounded by concurrent in-flight pages. Claims released by flush thread after insert, or by error paths | All paths verified (see invariant table above). Diagnostic warning at 500+ entries |
| `_host_tokens` / `_host_failures` / etc. | Hard cap: `_HOST_STATE_MAX_ENTRIES=200` | `_prune_host_state()` called per-college + every ~100 `scrape_page` calls per thread. TTL eviction (600s periodic, 1800s at college boundary) + hard cap evicts oldest by last-seen |
| `ProxyPool._sticky` | Grows with sticky assignments | TTL-based eviction (`cooldown_sec * 2`) in every `acquire()` call |
| `PlaywrightPool._all_locals` | Bounded by `pool_size` semaphore | Rotation removes + closes slots. `prune_dead_slots()` cleans unhealthy. `close_orphaned_slots()` reclaims slots from dead `pw_executor` threads after each college. `shutdown()` clears all |
| `_pw_local_registry` | Bounded by threads using fallback (non-pool) PW path | `close()` snapshots + clears registry, then closes browsers in daemon threads with 10s timeout |
| `DeltaCrawlCache._all_conns` | One per thread (thread-local) | `close()` closes all under lock. Minor: dead-thread connections persist until `close()` (bounded by thread pool size) |
| `_insert_buffer` | `Queue(maxsize=200)` | Backpressure blocks workers. In-place merge halves flush memory. Final drain in flush thread + `_flush_all_inserts()` |
| `EmbeddingBatcher._queue` | `Queue(maxsize=200)` | `submit()` retries on full. `_cancel_remaining()` resolves orphaned futures on shutdown |
| `_pw_profile_cache` | `OrderedDict` with LRU, cap 50 entries | Read hits: `move_to_end()`. Writes: `popitem(last=False)` when over cap. All under `_pw_profile_cache_lock` |
| `_embedding_cache` | `OrderedDict` with LRU, cap 1024 entries | Hits: `move_to_end()`. Inserts: `popitem(last=False)` when over cap. All under `_embedding_cache_lock` |
| Per-college sets | Cleared at end of `crawl_college_site` | `.clear()` under `state_lock` / `college_hash_lock` breaks closure refs from orphaned PW callbacks |
| Worker sessions | One per worker thread (curl_cffi Session when available) | `worker_session.close()` in `try/finally` at end of `worker_task`. curl_cffi sessions reuse libcurl handle |
| Playwright pool size | `min(PLAYWRIGHT_POOL_SIZE, PLAYWRIGHT_MAX_CONCURRENCY)` | Prevents idle browser processes from wasting ~300-500 MB each |
| Chromium flags | `_CHROMIUM_FLAGS_SAFE` (pool) + `_CHROMIUM_FLAGS_FALLBACK_EXTRA` (non-pool only) | Safe flags save memory; fingerprint-affecting flags restricted to fallback path |
| PW BeautifulSoup | Single soup from longer HTML snapshot | Title from other snapshot via regex. Links extracted before nav decomposition |
| Response body in `scrape_page` | Freed via `del response` after extracting url/text/headers | Saves ~1 MB per concurrent worker |

## Playwright Lifecycle — Correct

- **Pool path (primary):** `PlaywrightPool` manages thread-local browser slots via `threading.local()`. Semaphore caps total concurrent browsers. Slots track `_owner_tid` for orphan detection. All three `acquire()` branches (rotation, creation, reuse) check `_started` under `_all_locals_lock`. Rotation closes outside lock (fix #2). `acquire()` verifies `browser.is_connected()` before returning — dead browsers are closed and recreated inline (fix #7). `shutdown()` closes all slots in daemon threads with 10s timeout. `close_orphaned_slots()` reclaims slots from dead `pw_executor` threads after each college completes (fix #7)
- **Non-pool path (fallback, rarely used):** Only activated when pool is not started. Thread-local `_pw_local.pw` and `_pw_local.browsers` per thread. Registry stores direct object references for cross-thread cleanup in `close()`. Browser dict swap under lock prevents data race with late workers
- **`_pw_task_with_cleanup` guard:** Only calls `_cleanup_thread_local_playwright()` when the pool is not started (fix #7). Prevents accidental cleanup of pool-managed resources on pool-path threads
- **Camoufox context manager:** `__exit__` called in `finally` after page/context close. Double-exit prevented by `camoufox_cm = None` after first exit
- **Resource leak prevention:** `context.on("page", lambda p: p.close())` kills popup windows. `page.on("dialog", lambda d: d.dismiss())` auto-dismisses dialogs. Resource blocking via `context.route()` aborts images, fonts, analytics
- **Thread affinity:** Playwright sync API is greenlet-based and tied to creator thread. Pool and fallback both use thread-local storage. `_cleanup_thread_local_playwright()` only touches current thread's resources (and is guarded to only run when pool is not started). `prune_dead_slots()` reads `_healthy` flag (set by owning thread) instead of calling `browser.is_connected()` cross-thread. `acquire()` calls `browser.is_connected()` on the **owning thread** (safe — called from the same thread that created the browser via thread-local slot). `close_orphaned_slots()` does NOT call `is_connected()` — it uses `_owner_tid` vs `threading.enumerate()` to detect dead threads, then closes slots in daemon threads
- **Failed-launch cleanup:** When `pw.chromium.launch()` raises in `_create_browser()`, the except branch tears down the started `pw` (and `camoufox_cm`) on the same creator thread. Prevents a leaked Playwright sync instance from holding a greenlet asyncio loop on the worker thread, which would poison every subsequent `sync_playwright().start()` with "Sync API inside the asyncio loop" (see Bug #22). Cleanup runs outside `_all_locals_lock` because `_create_browser()` itself runs outside the lock.
- **Asyncio-loop poison recovery:** `_safe_sync_playwright_start()` (used by both pool and non-pool start paths) catches the "Sync API inside the asyncio loop" failure on the calling thread, resets that thread's default asyncio loop, and retries `sync_playwright().start()` once. Touches only the current thread's loop; no shared state.

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

## Re-audit (2026-04-05) — Bugs 1–5 Verified, Bug 6 Fixed

Full re-audit of `crawler.py` (4271 lines), cross-referenced with pymilvus ORM documentation via Context7 and Python `threading` semantics. All 5 previously fixed bugs verified correct. No deadlocks, no memory leaks, no corrupted insert paths. pymilvus write thread-safety confirmed as non-issue due to single-writer `MilvusFlushThread` design.

### 6. `PlaywrightPool.acquire()` Dead-Slot Lockout (Medium — permanent capability loss)

**File:** `crawler.py`, `PlaywrightPool.acquire()` health check branch

When a browser dies mid-use, `release()` correctly marks `slot["_healthy"] = False` and releases the semaphore. `prune_dead_slots()` correctly removes the slot from `_all_locals` and closes it in a daemon thread. However, `self._local.slot` (thread-local storage) still held the stale dead reference. The next `acquire()` call from the same thread found the unhealthy slot, released the semaphore, and returned `(None, -1)` — permanently. The `slot is None` branch that creates new browsers via `_create_browser()` was never reached because `slot` was the stale dead reference, not `None`.

**When it can happen:** Any crawl where a Playwright browser dies mid-use (Chromium segfault, OOM kill, network disconnect). The owning thread permanently loses Playwright fallback for the rest of the crawl. Under `PLAYWRIGHT_MAX_CONCURRENCY=3`, if 2 browsers die, only 1 worker thread can use Playwright.

**Fix (round 1):** Clear the thread-local slot reference when the health check fails, so the next `acquire()` enters the `slot is None` creation branch and gets a fresh browser.

**Fix (round 2 — 2026-04-16):** Round 1 fix was incomplete. Clearing the ref without calling `_close_slot(slot)` left the old Playwright runtime's greenlet-based asyncio loop alive on the thread. When `_create_browser()` called `_safe_sync_playwright_start()`, it hit the existing loop → "Sync API inside the asyncio loop". The `_safe_sync_playwright_start()` recovery (`asyncio.get_event_loop().close()`) doesn't work because Playwright's greenlet holds its own internal loop, not the thread's default loop. Fixed by calling `_close_slot(slot)` (which runs `pw.stop()`) on the owning thread before clearing the ref:

```python
if not slot.get("_healthy", True):
    with self._all_locals_lock:
        try:
            self._all_locals.remove(slot)
        except ValueError:
            pass
    self._close_slot(slot)  # pw.stop() on owning thread — kills greenlet + asyncio loop
    self._local.slot = None
    self._semaphore.release()
    return None, -1
```

**Thread safety of the fix:**
- `self._local` is `threading.local()` — writes only affect the current thread, no cross-thread races possible
- Slot is removed from `_all_locals` under lock before `_close_slot()`, preventing double-close from concurrent `prune_dead_slots()`
- `_close_slot()` runs on the owning thread (correct thread affinity for `pw.stop()`)
- `_close_slot()` is called outside `_all_locals_lock` — consistent with all other close paths (avoids blocking on hung Chromium IPC)
- Next `acquire()` creates a fresh browser via `_create_browser()`, which registers the new slot in `_all_locals` under `_all_locals_lock` — standard creation path

### Structural improvement: `pw_uploaded_shared` dict pattern

**File:** `crawler.py`, `crawl_college_site()`

`pw_uploaded_shared` used the `nonlocal int` pattern — all writes were correctly under `state_lock`, but a future edit adding a write without the lock would silently introduce a data race. Refactored to `pw_uploaded = {"count": 0}` dict pattern, matching the existing `college_counter` pattern (line 4011). No behavioral change — all mutations remain under `state_lock`, but `nonlocal` is no longer needed and the lock requirement is visually obvious.

## Playwright Resilience Fixes (2026-04-16)

Three fixes targeting 100% Playwright fallback failure rate ("Page.goto: Target page, context or browser has been closed").

### 7. Playwright retry swallowing + pool liveness (Medium — complete Playwright failure)

**File:** `crawler.py`, `_scrape_with_playwright_single_attempt()` outer except, `_scrape_with_playwright()` retry loop, `PlaywrightPool.acquire()`

**Three interlocking bugs:**

1. **Exception swallowing:** The outer `except` in `_scrape_with_playwright_single_attempt` caught "browser has been closed" errors, printed them, and returned `None`. Since it returned instead of raising, the retry loop in `_scrape_with_playwright` never saw an exception — zero retries happened. **Fix:** Re-raise retryable errors (browser closed, connection refused/reset) so the retry loop can attempt again.

2. **No slot invalidation on retry:** When a browser died, the retry loop started a new attempt but `acquire()` returned the same dead browser (slot was still `_healthy=True` until `release()` ran). **Fix:** Mark pool slot `_healthy=False` in the retry loop's except clause when the error indicates a dead browser.

3. **No liveness check in `acquire()`:** `acquire()` trusted the `_healthy` flag, which was only set in `release()`. If a browser died between `release()` and the next `acquire()` (OOM kill, crash), `acquire()` returned a dead browser. **Fix:** Added `browser.is_connected()` check in `acquire()` after the `_healthy` flag check. On dead browser, closes the slot and creates a fresh browser inline. Safe because `is_connected()` is called on the owning thread (thread-local slot).

4. **Orphaned slots from dead `pw_executor` threads:** When a school finishes and `pw_executor.shutdown()` kills its threads, the pool's `_all_locals` retains slots with live Chromium processes that can never be reused. Accumulates across schools. **Fix:** Slots now track `_owner_tid`. New `close_orphaned_slots()` method finds slots whose owning thread is dead (via `threading.enumerate()`) and closes them in daemon threads. Called after each `pw_executor.shutdown(wait=True)`.

5. **`_pw_task_with_cleanup` unnecessary cleanup:** Called `_cleanup_thread_local_playwright()` after every Playwright task. For pool-path tasks this was a no-op, but a maintenance hazard. **Fix:** Guard cleanup with `if not self.pw_pool._started`.

### 8. `EXCLUDED_URL_PATTERNS` wired into `is_internal_link()` (Low — crawl efficiency)

**File:** `crawler.py`, `is_internal_link()`, `config.py`

`EXCLUDED_URL_PATTERNS` was defined in `config.py` but never imported or used. Contains useful patterns for social media, login, admin, calendar paths. Now compiled once at module level (`_EXCLUDED_URL_RE`) and checked in `is_internal_link()` alongside `SKIP_PATHS`.

Additionally, `BFS_SKIP_URL_PATTERNS` framework added (empty by default) with `_is_bfs_junk_url()` filter at all 3 BFS enqueue points — ready for future use.

## Memory Leak Fixes (2026-04-06)

Seven fixes targeting OOM crashes caused by unbounded memory growth during long crawler runs.

### 7. Per-College Set Closure Leak (Critical — OOM)

**File:** `crawler.py`, `crawl_college_site()` return path

`_merge_pw_result` callbacks capture per-college sets (`crawled_canon`, `discovered_canon`, `discovered_urls`, `college_hash_cache`, `college_canonical_urls`) by closure. In the global-shutdown path (`wait=False`), orphaned callbacks keep these sets alive, preventing GC. With 4 colleges × 6 sets × 25K URLs each, memory accumulates across colleges. (In the normal path, `shutdown(wait=True)` guarantees all callbacks complete before return, so no orphans exist — but clearing still frees memory promptly.)

**Fix:** Explicitly `.clear()` all per-college sets at the end of `crawl_college_site()`, after `_flush_all_inserts()` and before the return. Clears under `state_lock` (for BFS sets) and `college_hash_lock` (for dedup cache) to synchronize with any late PW callbacks in the shutdown path. After clear, a late callback finds empty sets — harmless since `stop_event` is set and nobody consumes the queue.

**Thread safety:** Lock ordering preserved (`state_lock` → `self.lock`; `college_hash_lock` is independent). `.clear()` empties in-place under the same locks callbacks use. **Safe.**

### 8. Insert Buffer Merge Memory Doubling (Critical — OOM)

**File:** `crawler.py`, `_flush_insert_buffer_inner()`

The old code created a separate `merged` dict and `.extend()`ed each column. Both `rows` and `merged` held all embedding vectors simultaneously, doubling peak memory (~48 MB per flush).

**Fix:** In-place merge into the first row's lists. Consumed rows nulled immediately (`rows[i] = None`). On alignment mismatch, drops the batch (per-row recovery no longer possible since original rows are consumed).

**Thread safety:** `_flush_insert_buffer_inner` runs exclusively on `MilvusFlushThread` (single consumer). No concurrent access. **Safe.**

### 9. Unbounded Host State Dicts (High — sustained growth)

**File:** `crawler.py`, `_prune_host_state()`, `scrape_page()`

`_host_tokens` et al. grew with every unique hostname. Pruning only happened at college boundaries with a 30-min TTL, but active hosts were never evicted.

**Fix:** Hard cap of `_HOST_STATE_MAX_ENTRIES=200` evicts oldest entries by last-seen time after TTL eviction. Periodic pruning every ~100 `scrape_page` calls per thread via thread-local counter (600s TTL for periodic, 1800s at college boundary).

**Thread safety:** All mutations under `_host_lock` (existing). Thread-local counter uses `threading.local()` — no lock needed. **Safe.**

### 10. Unbounded Playwright Profile Cache (High — sustained growth)

**File:** `crawler.py`, `_load_playwright_profile()`

`_pw_profile_cache` stored parsed YAML profiles per domain with no eviction.

**Fix:** `OrderedDict` with LRU eviction (cap 50). Read hits: `move_to_end()`. Writes: `popitem(last=False)` when over cap.

**Thread safety:** All operations under existing `_pw_profile_cache_lock`. **Safe.**

### 11. `_pw_local_registry` Stale References (High — post-close leak)

**File:** `crawler.py`, `close()`

Registry snapshotted but never cleared. Stale entries held Playwright runtime references.

**Fix:** `self._pw_local_registry.clear()` after snapshotting, under `_pw_local_registry_lock`.

**Thread safety:** `close()` is idempotent via `_close_lock`. Workers stopped before `close()` runs. No new appends possible. **Safe.**

### 12. Embedding Cache Stale Entries (Medium — correctness)

**File:** `embeddings.py`, `get_embedding()`

`_embedding_cache` grew to 1024 entries then stopped accepting new entries. Old stale entries never replaced.

**Fix:** `OrderedDict` with LRU eviction. Always inserts; `popitem(last=False)` when over cap. Hits: `move_to_end()`.

**Thread safety:** All operations under existing `_embedding_cache_lock`. **Safe.**

### 13. Pending Canonical URL Diagnostic (Medium — observability)

**File:** `crawler.py`, `_flush_insert_buffer_inner()` after successful insert

No visibility into `_pending_canonical_urls` growth during high throughput.

**Fix:** Diagnostic `print()` when set exceeds 500 entries. Uses `len()` (GIL-atomic, approximate). **Safe.**

## Memory Optimization Fixes (2026-04-06, round 2)

Eight fixes targeting OOM crashes caused by Playwright browser over-provisioning, BeautifulSoup memory amplification, and per-request C-level allocations.

### 14. Playwright Pool Over-Provisioned (High — ~600MB-1GB wasted)

**File:** `crawler.py`, `__init__` pool instantiation

`PLAYWRIGHT_POOL_SIZE=5` but `PLAYWRIGHT_MAX_CONCURRENCY=3`. Two idle browser processes consumed ~300-500 MB each for nothing.

**Fix:** Pool size capped at `min(PLAYWRIGHT_POOL_SIZE, PLAYWRIGHT_MAX_CONCURRENCY)`, so every browser slot is actively used. Pool browsers also now use comprehensive memory-saving Chromium flags (`_CHROMIUM_FLAGS_SAFE`) — previously only 9 flags vs 20 in the non-pool fallback path.

**Thread safety:** Pool initialization is single-threaded (`__init__`). Flag constants are module-level immutable. **Safe.**

### 15. BeautifulSoup 4x Parse in Playwright Path (High — ~30MB peak)

**File:** `crawler.py`, `_scrape_with_playwright_single_attempt()` content extraction

Previously built 4 BeautifulSoup objects simultaneously: `soup_dom`, `soup_idle`, and 2x `soup_copy` via `str()` re-serialization inside `extract_text_and_links()`. Each soup is ~5x the HTML size.

**Fix:** Single-soup strategy — pick the longer HTML snapshot, build one soup. Extract title from the other via lightweight regex. Extract links before decomposing nav/footer elements (links live in nav). Free unused HTML strings and soup via `del` immediately after use.

**Thread safety:** All variables are local to the function. No shared state. **Safe.**

### 16. Response Body Held Alongside Soup (Medium — ~30MB across 24 workers)

**File:** `crawler.py`, `scrape_page()` after soup creation

`response.content` (~1MB) and `soup` (~4MB) both in memory for the duration of `scrape_page()`. Extracted `response.url`, `response.text`, `response.headers.get("ETag")`, and `response.headers.get("Last-Modified")` into locals, then `del response`.

**Thread safety:** `response` is a local variable. `del` only affects the current thread. **Safe.**

### 17. Per-Request curl_cffi Session Creation (Medium — C-level memory fragmentation)

**File:** `crawler.py`, `worker_task()` session creation, `scrape_page()` request path

`curl_requests.get()` (module-level) created a new internal `Session` + libcurl handle per request. Each handle allocates C-level memory (DNS cache, SSL session cache) that may not be freed promptly by Python GC.

**Fix:** Worker threads create `curl_cffi.requests.Session()` when `USE_CURL_CFFI` is enabled. `scrape_page()` detects curl_cffi sessions via `hasattr(session, "curl")` and calls `session.get()` (reusing the handle) instead of the module-level function. Test sessions in `crawl_college_site()` also use curl_cffi when available.

**Thread safety:** Each worker thread owns its own session (no sharing). Session closed in `worker_task()`'s `try/finally`. **Safe.**

### 18. Insert Buffer Maxsize Reduced (Low — ~35MB saved at capacity)

**File:** `crawler.py`, `__init__` insert buffer

`Queue(maxsize=500)` reduced to `Queue(maxsize=200)`. With flush every 2s in batches of 50-200, the buffer rarely exceeds 100 items. Still provides ample headroom.

**Thread safety:** `Queue` is thread-safe by design. Size change has no concurrency impact. **Safe.**

### 19. Work Queue Drain After Workers Stop (Low — faster GC)

**File:** `crawler.py`, `crawl_college_site()` after `with ThreadPoolExecutor` block

Added drain loop to free queued `(depth, url)` tuples before flush/cleanup, rather than waiting for function return and GC.

**Thread safety:** Workers have stopped (executor `__exit__` called). No concurrent producers. **Safe.**

### 20. Pop `internal_links` After Enqueuing (Very Low — removes dangling reference)

**File:** `crawler.py`, `worker_task()` after BFS link enqueue loop

`page_data.pop("internal_links", None)` after links are enqueued. Removes ~10KB of URL strings that would otherwise persist until the next loop iteration.

**Thread safety:** `page_data` is a local variable. No shared access. **Safe.**

### 21. Shared Chromium Flag Constants (No memory impact — maintainability)

**File:** `crawler.py`, module-level constants

Extracted `_CHROMIUM_FLAGS_SAFE` (safe flags) and `_CHROMIUM_FLAGS_FALLBACK_EXTRA` (fingerprint-affecting flags) as module-level constants. Pool browsers use `_CHROMIUM_FLAGS_SAFE` only. Non-pool fallback browsers use both. Eliminates flag duplication and ensures pool browsers get all safe memory-saving flags.

**Safe flags** (no fingerprint impact): `--no-zygote`, `--disable-background-timer-throttling`, `--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows`, etc.

**Fingerprint-affecting flags** (non-pool only): `--disable-accelerated-2d-canvas`, `--disable-permissions-api`, `--force-device-scale-factor=1`.

## Bug Fix — Playwright Asyncio-Loop Poisoning (2026-04-09)

### 22. `_create_browser()` Leaked Playwright Runtime on Launch Failure (High — cascading capability loss)

**File:** `crawler.py`, `PlaywrightPool._create_browser()` (~line 377)

**Symptom:** Crawler logs filled with cascading errors after a single failed browser launch:

```
⚠️  Failed to create Playwright browser: BrowserType.launch:
    Executable doesn't exist at .../chromium_headless_shell-1208/...
⚠️  Failed to create Playwright browser: It looks like you are using
    Playwright Sync API inside the asyncio loop. Please use the Async API instead.
⚠️  Failed to create Playwright browser: It looks like you are using
    Playwright Sync API inside the asyncio loop. ...
```

The first error was the trigger (a stale Playwright cache after the package upgraded from `chromium-1181` to `chromium-1208` — fixed by `python -m playwright install chromium`). Every subsequent error on the same worker thread was the **leaked Playwright sync instance** poisoning that thread's asyncio loop.

**Root cause:**

```python
# BEFORE
def _create_browser(self):
    try:
        ...
        pw = sync_playwright().start()       # ← installs greenlet asyncio loop on this thread
        browser = pw.chromium.launch(...)    # ← raises if binary missing
        ...
    except Exception as e:
        print(f"    ⚠️  Failed to create Playwright browser: {e}")
        return None                          # ← `pw` is leaked, loop stays alive
```

After `chromium.launch()` raised, `pw` was never `.stop()`'d. The Playwright sync runtime kept its greenlet-based asyncio loop bound to the worker thread. The next call to `sync_playwright().start()` on that thread (rotation, fallback, retry) raised "Sync API inside the asyncio loop" because Playwright detected a running loop and refused to start another. Once a thread was poisoned, every subsequent Playwright fallback on that thread failed — silently degrading crawler coverage.

**Fix:**

```python
# AFTER
def _create_browser(self):
    pw = None
    camoufox_cm = None
    try:
        if self.use_camoufox:
            camoufox_cm = Camoufox(headless=self.headless)
            browser = camoufox_cm.__enter__()
            ...
        else:
            pw = _safe_sync_playwright_start()
            browser = pw.chromium.launch(...)
            ...
        return slot
    except Exception as e:
        print(f"    ⚠️  Failed to create Playwright browser: {e}")
        if pw is not None:
            try: pw.stop()
            except Exception: pass
        if camoufox_cm is not None:
            try: camoufox_cm.__exit__(None, None, None)
            except Exception: pass
        return None
```

**Thread safety:**

- Cleanup runs on the same worker thread that started `pw` — correct affinity for Playwright sync API.
- `_create_browser()` is invoked outside `_all_locals_lock` (caller `acquire()` releases the lock at crawler.py:466 before the call), so the cleanup never runs under that lock — no deadlock risk and no widening of the lock-held window.
- `pw.stop()` here is safer than `_close_slot()`'s `pw.stop()` because the chromium subprocess never started — `pw.stop()` only tears down the driver IPC, with no zombie-Chromium hang risk. Daemon-thread wrapping (used by `shutdown()` and `prune_dead_slots()`) is unnecessary on this path.

### 23. Helper: `_safe_sync_playwright_start()` (Defensive — asyncio-loop recovery)

**File:** `crawler.py`, module-level function (~line 43)

A second defensive guard added alongside Bug #22. Both `PlaywrightPool._create_browser()` (Chromium branch) and the non-pool fallback `_pw_local.pw = _safe_sync_playwright_start()` (in `_scrape_with_playwright_single_attempt`) route through this helper instead of calling `sync_playwright().start()` directly.

```python
def _safe_sync_playwright_start():
    try:
        return sync_playwright().start()
    except Exception as e:
        if "Sync API inside the asyncio loop" not in str(e):
            raise
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.close()
        except RuntimeError:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())
        return sync_playwright().start()
```

Mirrors the workaround in `niche_scraper._launch_chrome` (niche_scraper.py:584-602), which has lived with the same failure mode for months. With Bug #22 fixed the helper rarely triggers in practice, but it remains as a backstop against any future leak source (a half-cleaned non-pool fallback, an external library installing a stray loop, etc.).

**Thread safety:**

- Operates only on the **calling thread's** default asyncio loop (`asyncio.get_event_loop()` / `set_event_loop()`). No cross-thread loop access.
- No shared state, no locks. Safe to call from any worker thread.
- Never installs a loop on a thread that doesn't already have a poisoned one — only the except branch swaps the loop, and only after a real failure.

**Verification:** `python -m college_ai.scraping.crawler --colleges 1 --workers 4 --max-pages 80` against `tarleton.edu` (a heavy Playwright-fallback target) over 90s. Before fix: 11 `Failed to create Playwright browser` lines, 9 `Sync API inside the asyncio loop` lines. After fix (with `playwright install chromium` run first): 0 of each. Playwright fallbacks triggered: 16. Fallback failures: 1 (an unrelated 403/timeout, not the asyncio bug).

## Bug Fix — Acquire-Path Close + Normal-Shutdown Bounded Poll (2026-04-16)

### 24. `acquire()` Close Paths Could Hang on Zombie Chromium (Medium — liveness)

**File:** `crawler.py`, `PlaywrightPool.acquire()` (rotation / reuse / health-check branches) and `PlaywrightPool._close_slot_safe()` (new helper)

**Symptom:** With the Bug #6 round-2 fix, `acquire()` now calls `_close_slot(slot)` on the owning thread in three places (rotation, the post-`_started`-recheck reuse fail, and the `_healthy=False` health check) before creating a fresh browser. `_close_slot` calls `browser.close()` + `camoufox_cm.__exit__()` + `pw.stop()` synchronously. If the Chromium process is a zombie, `browser.close()` hangs indefinitely — stalling the worker that just needs to rotate in a fresh browser. `shutdown()` and `prune_dead_slots()` had already solved this by wrapping `_close_slot` in daemon threads with `t.join(timeout=10)`, but the `acquire()` paths were still synchronous.

**Fix:** New helper `_close_slot_safe()` isolates the hang-risky steps (`browser.close()`, `camoufox_cm.__exit__()`) in daemon threads with 10s timeouts, while still running `pw.stop()` **synchronously on the calling thread**. `pw.stop()` must stay on the current thread because it tears down the thread-local greenlet + asyncio loop that Playwright sync API installs — without that teardown, the next `_create_browser()` on the same thread hits "Sync API inside the asyncio loop" (same failure mode as Bug #22, same root cause as Bug #6 round 2). All three `acquire()` close sites now call `_close_slot_safe()` instead of `_close_slot()`.

**Thread safety:**
- Daemon threads for `browser.close()` / `camoufox_cm.__exit__()` follow the same pattern as `shutdown()` / `prune_dead_slots()` — a hung Chromium process cannot block the caller.
- `pw.stop()` runs on the owning worker thread (correct affinity). It's the cheap, non-network step in the sequence (driver IPC teardown, no subprocess wait), so keeping it synchronous does not reintroduce the hang risk.
- Called outside `_all_locals_lock` in all three `acquire()` sites (slot is removed from `_all_locals` under the lock first, close happens after).
- `slot["_healthy"] = False` is set first so any racing `prune_dead_slots()` sees it as already-unhealthy and skips re-closing.

### 25. Normal `pw_executor` Shutdown Could Hang on Stuck Navigation (Low — liveness)

**File:** `crawler.py`, `MultithreadedCollegeCrawler.crawl_college_site()` (per-college `pw_executor` teardown, normal-completion branch)

Normal-completion previously called `pw_executor.shutdown(wait=True, cancel_futures=True)`, which joins all executor threads. If a Playwright page navigation stalled past its 30s timeout (e.g. a browser that died but hasn't surfaced the error yet, or a `browser.close()` hang during task cleanup), the join never returns and the main BFS thread hangs forever between colleges. The global-shutdown branch already used `wait=False` + polling on `active_pw_futures` for exactly this reason.

**Fix:** Mirror the global-shutdown pattern in the normal-completion branch: `shutdown(wait=False, cancel_futures=True)` followed by a 45s bounded poll (30s Playwright nav timeout + 15s retry/cleanup overhead) on `active_pw_futures`. If the set hasn't drained, print a warning and proceed to the `close_orphaned_slots()` step — which already handles slots left behind by dead executor threads.

**Thread safety:**
- Callback ordering invariant (Bug #1) is preserved: `_merge_pw_result()` runs before `active_pw_futures.discard()`, so the set empties only after all buffer puts complete. Proceeding past the 45s cap risks callbacks running without their data landing — same trade-off as the global-shutdown path, accepted because the alternative is a dead-locked crawler.
- `close_orphaned_slots()` downstream reclaims any browser slots whose executor thread died, so the pool does not leak slots on the rare 45s-timeout path.
- `pw_futures_lock` guards both the poll read and the size check, consistent with the global path.

## Rechunk Mode — Thread Safety (2026-04-07)

`--rechunk` adds a temporary crawl mode that re-crawls pages with old 512-token mechanical chunks, replacing them with sentence-aware chunks.

### Detection Logic

`_load_college_canonicals(rechunk=True)` fetches `content`, `url`, and `url_canonical` with a reduced `batch_size=256` (vs. 2048 for non-rechunk) to stay under the 4MB gRPC response limit when content fields are included. Counts tokens per chunk and groups by URL. Old chunker pattern: multi-chunk pages where every chunk except the last is exactly 512 tokens. Single-chunk pages are skipped (no benefit to rechunking). Identified URLs are excluded from `college_canonical_urls` and returned as `rechunk_urls`. Also returns `rechunk_full_urls` (canonical key → full URL with scheme) for BFS seeding.

### BFS Seeding

Rechunk URLs are seeded directly into the BFS `work_queue` at depth 0 under `state_lock` before workers are submitted, guaranteeing they will be re-crawled regardless of link discovery. `discovered_urls` and `discovered_canon` are updated under `state_lock` for consistency with the documented invariant.

### Thread Safety of New Code

| Component | Safety |
|-----------|--------|
| `rechunk_urls` set | Local to `crawl_college_site`, built before workers start, read-only via closures. No lock needed for reads |
| `rechunk_full_urls` dict | Local to `crawl_college_site`, used only for BFS seeding before workers start. Not accessed by workers. `.clear()` under `state_lock` at end |
| `rechunk_urls` cleanup | `.clear()` under `state_lock` at end of `crawl_college_site` alongside other per-college sets |
| Rechunk URL seeding | `discovered_urls`, `discovered_canon`, `work_queue` mutated under `state_lock` before worker submission. No concurrent access |
| `force_replace` in `upload_to_milvus` | Extends existing `no_resume` delete condition. Same `collection_write_lock` acquisition. `_pending_canonical_urls` invariant holds identically |
| `_load_college_canonicals` token counting | CPU-only work inside existing `collection_query_sema` hold. No shared state mutation |
| `_load_college_canonicals` batch size | Reduced to 256 in rechunk mode to prevent gRPC RESOURCE_EXHAUSTED on response. Semaphore held for entire iteration (same pattern, more round-trips) |
| Delta cache | Disabled in rechunk mode (same as `no_resume`). No crash-consistency concern |
| Lock ordering | No new locks. No changes to existing ordering |
| Memory | `content` fetched in batches of 256, discarded after token counting. `url_chunk_tokens` dict holds `List[int]` per URL, `canon_to_full` dict holds one URL string per canonical key; both freed after set comprehension |
