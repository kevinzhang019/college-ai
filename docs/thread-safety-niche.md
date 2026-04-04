# Thread Safety — Niche Scraper (CRITICAL)

The Niche scraper (`niche_scraper.py`) is heavily multithreaded. Read this before modifying `niche_scraper.py`.

## Concurrency Primitives

- **`DBWriterThread`** — all DB writes go through a single `queue.Queue` to a dedicated writer thread. This eliminates cross-thread Turso WebSocket contention. Never write to the DB from worker threads directly. Includes keepalive SELECT every 60s. Atomic school writes (datapoints + NicheGrade committed together). Retries up to 3x with Hrana error detection and engine reset. Grade counter (`total_grades`) is incremented only after successful commit to prevent double-counting on retry. If the writer thread crashes, it sets `shutdown_event` so workers stop promptly instead of scraping into a dead queue. After the writer exits, `scrape_all()` performs a best-effort drain of any remaining queue items using `drain_queue_best_effort()` — each item gets a single write attempt (no retries) to avoid masking the root crash cause. The final sentinel drain uses `get_nowait()` with `queue.Empty` handling to avoid TOCTOU races.
- **`GlobalRateLimiter`** — lock-protected slot reservation. Workers compute and reserve their slot under the lock, then sleep *outside* the lock so `record_request()` and other workers aren't blocked. Scales delays by worker count (aggregate rate stays constant regardless of parallelism). `record_request()` only advances the timestamp — never regresses past a future reservation.
- **`JobClaimer`** — lock-protected dynamic work queue distributing schools across workers
- **`cookie_lock`** — protects `cookie_generation` reads/writes (held briefly, never during I/O)
- **`cookie_capture_lock`** — serializes interactive cookie captures (held for the duration of user interaction). Separated from `cookie_lock` so that other workers' per-school generation checks don't block during a capture.
- Worker threads only interact with thread-local Playwright browsers and the shared rate limiter

## Playwright Thread Model

Each worker creates its own `sync_playwright()` instance, browser, context, and page. Playwright's sync API uses greenlets — a hidden dispatcher fiber (greenlet) runs an asyncio event loop within the **same OS thread**. Response event handlers (`page.on("response", ...)`) execute in this dispatcher fiber, not a separate OS thread. This means:
- No cross-thread data race between the handler appending to `_intercepted_data` and the worker reading it — they are sequentially interleaved within one thread
- A greenlet from thread A cannot be resumed from thread B (raises `greenlet.error`), enforcing per-thread isolation
- Re-entrant Playwright API calls from inside event handlers will deadlock (the dispatcher fiber is already running)

## Sentinel Guarantee

Every `_worker_loop` invocation calls `db_writer.worker_done()` exactly once, regardless of where a failure occurs — including the `NicheScraper()` constructor. This invariant ensures the DB writer thread always receives `num_workers` sentinels and terminates. The outer `try/finally` in `_worker_loop` wraps the entire function body including object construction. `scrape_all()` also sends compensation sentinels for workers that were never submitted to the executor (e.g., if shutdown interrupted the launch loop).

`worker_done()` uses `put(timeout=2.0)` in a retry loop (not unbounded `put()`) so that if the queue is full and the writer thread has crashed, the worker's `finally` block does not deadlock. When `shutdown_event` is set and the writer is no longer alive, the sentinel is dropped (not enqueued) — a dead writer will never count sentinels, so the sentinel is meaningless and the only goal is to let the worker exit.

## Shutdown + PX Recovery Guard

During shutdown, the worker skips `db_writer.submit()` whenever `grades` is empty — even if `points` is non-empty. This prevents a school from being permanently marked `no_data` (and skipped on resume) when grades are missing only because a PX retry was interrupted by shutdown. Complete data (has grades) is still submitted during shutdown to preserve progress. Schools with incomplete data remain pending for the next run.

`capture_cookies()` returns a boolean indicating whether cookies were actually saved. If capture is cancelled by shutdown (returns `False`), the cookie generation counter is **not** bumped — preventing other workers from needlessly reloading stale cookies. The worker also skips `scraper.restart()` during shutdown to avoid launching a browser that will be immediately torn down.

## Cookie File Atomicity

Both `capture_cookies()` and `_login()` write cookies using the atomic temp-file + `os.replace()` pattern. This prevents other workers from reading a truncated JSON file if the write is interrupted mid-stream.

## Browser Cleanup Timeout

Playwright's `browser.close()` can hang indefinitely if the browser process OOMed or crashed (confirmed in Playwright issue #1847). The worker's `finally` block runs `scraper.close()` in a daemon thread with a 15-second timeout. If cleanup hangs, the daemon thread is abandoned — it will be reaped at process exit. This ensures the worker always completes so `scrape_all()` can proceed to its own shutdown logic.

## Memory Management

- `_intercepted_data` (list of captured XHR response payloads) is released in a `try/finally` block at the end of `scrape_scattergram()`. This prevents large JSON bodies from persisting in memory between schools.
- The write queue is bounded at `maxsize=50`. If the writer crashes and stops consuming, workers block on `put()` until the 2-second timeout fires and they detect `shutdown_event`. Maximum in-queue memory is bounded to 50 items.

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine`, `_session_factory`, and `ENGINE` during `reset_engine()`. `get_session()` captures *and invokes* `_session_factory` under this lock so that a concurrent `reset_engine()` cannot dispose the engine before the session is created. (The factory must be invoked inside the lock, not just captured — otherwise `engine.dispose()` in `reset_engine()` could invalidate the pool before `factory()` opens a connection.)
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`. External code should call `get_engine()` rather than importing `ENGINE` directly.
- **`ENGINE`** *(deprecated)* — module-level alias retained for backward compatibility. Prefer `get_engine()`.
- **NullPool** — `dispose()` on the old engine is safe even with active sessions: NullPool has no idle connection cache, so dispose is effectively a no-op on live connections. Sessions created from the old factory continue working until closed.

## General Rules

- Never share Playwright browser instances across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
