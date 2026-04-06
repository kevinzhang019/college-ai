# Thread Safety — Niche Scraper (CRITICAL)

The Niche scraper (`niche_scraper.py`) is heavily multithreaded. Read this before modifying `niche_scraper.py`.

> **Audit:** A full concurrency audit was performed on 2026-04-05. See [Thread Safety Audit](thread-safety-niche-audit.md) for the complete findings.

## Concurrency Primitives

- **`DBWriterThread`** — all DB writes go through a single `queue.Queue` to a dedicated writer thread (daemon=True). This eliminates cross-thread Turso WebSocket contention. Never write to the DB from worker threads directly. The thread is daemon so a hung DB operation cannot prevent process exit — `scrape_all()` joins it with a 60s timeout, and if it doesn't finish, the process can still exit cleanly. Includes keepalive SELECT every 60s. Atomic school writes (datapoints + NicheGrade committed together). Retries up to 3x with Hrana error detection and engine reset. Grade counter (`total_grades`) is incremented only after successful commit to prevent double-counting on retry. If the writer thread crashes, it sets `shutdown_event` so workers stop promptly instead of scraping into a dead queue. After the writer exits, `scrape_all()` performs a best-effort drain of any remaining queue items using `drain_queue_best_effort()` — each item gets a single write attempt (no retries) to avoid masking the root crash cause. The final sentinel drain uses `get_nowait()` with `queue.Empty` handling to avoid TOCTOU races.
- **`GlobalRateLimiter`** — lock-protected slot reservation. Workers compute and reserve their slot under the lock, then sleep *outside* the lock so `record_request()` and other workers aren't blocked. Scales delays by worker count (aggregate rate stays constant regardless of parallelism). `record_request()` only advances the timestamp — never regresses past a future reservation.
- **`JobClaimer`** — lock-protected dynamic work queue distributing schools across workers
- **`cookie_lock`** — protects `cookie_generation` reads/writes (held briefly, never during I/O)
- **`cookie_capture_lock`** — serializes interactive cookie captures (held for the duration of user interaction, which is unbounded). Separated from `cookie_lock` so that other workers' per-school generation checks don't block during a capture. Workers acquire this lock using a `timeout=2.0` polling loop that checks `shutdown_event` each iteration — never a bare `Lock.acquire()` — so that Ctrl+C during capture does not leave other workers deaf to shutdown.
- **`capture_in_progress`** — `threading.Event` set while any worker is actively in `capture_cookies()`. Other workers check this at **two gate checkpoints** in the worker loop: (A) at the top of each school iteration before any page loads, and (B) between scattergram and grades scraping. When the event is set, the worker closes its browser via `_close_for_capture()`, poll-waits for capture to finish, updates its cookie generation, and restarts with fresh cookies. This prevents workers from making doomed page loads (PX will block them) and from producing noisy browser restart errors during capture. The PX recovery path also closes the browser immediately via `_close_for_capture()` on detection and checks this event before restarting — a defense-in-depth guard in case another worker is mid-capture.
- Worker threads only interact with thread-local Playwright browsers and the shared rate limiter

## Playwright Thread Model

Each worker creates its own `sync_playwright()` instance, browser, context, and page. Playwright's sync API uses greenlets — a hidden dispatcher fiber (greenlet) runs an asyncio event loop within the **same OS thread**. Response event handlers (`page.on("response", ...)`) execute in this dispatcher fiber, not a separate OS thread. This means:
- No cross-thread data race between the handler appending to `_intercepted_data` and the worker reading it — they are sequentially interleaved within one thread
- A greenlet from thread A cannot be resumed from thread B (raises `greenlet.error`), enforcing per-thread isolation
- Re-entrant Playwright API calls from inside event handlers will deadlock (the dispatcher fiber is already running)
- Only one `sync_playwright()` instance can be active per thread — starting a second one while the first is running raises "using Playwright Sync API inside the asyncio loop". `capture_cookies()` uses `_close_for_capture()` to tear down the existing browser **synchronously on the worker thread** before starting the capture browser. This is required because `stop()` must resume the greenlet that owns the asyncio event loop — a daemon thread cannot do this (greenlets are thread-bound). The `finally` block in `capture_cookies()` also runs all cleanup synchronously (page/context/browser close + `pl.stop()`) on the same thread for the same reason. `_launch_chrome()` has a fallback that resets the asyncio loop on failure as defense-in-depth. The PX recovery path uses `_close_for_capture()` (synchronous on the owner thread) followed by `start()` — it never calls `restart()` (which uses daemon-threaded `close()`) because that would violate the greenlet thread-affinity constraint and cause "Playwright Sync API inside asyncio loop" errors.

### `_close_for_capture()` vs `close()`

Two cleanup methods exist because of the greenlet constraint:
- **`close(timeout)`** — general-purpose cleanup that delegates to a daemon thread with a timeout. Safe for the worker `finally` block and `restart()` where the calling thread may not be the Playwright owner (e.g., the outer daemon wrapper in the worker's finally block). The daemon thread may produce harmless greenlet errors if the browser process is already dead.
- **`_close_for_capture()`** — synchronous cleanup that calls `playwright.stop()` on the current thread, then nulls all references. Called by `capture_cookies()` and by the worker-loop gate checkpoints — both run on the worker thread that owns the Playwright instance. `stop()` is called first (not last) because it kills the server subprocess and browser process, making individual `page.close()`/`browser.close()` calls unnecessary. This avoids the hang risk from Playwright issue #1847 where `browser.close()` blocks indefinitely on crashed browsers — after `stop()` the process is gone. No daemon thread is created, so no orphaned threads and no greenlet errors.

## Sentinel Guarantee

Every `_worker_loop` invocation calls `db_writer.worker_done()` exactly once, regardless of where a failure occurs — including the `NicheScraper()` constructor. This invariant ensures the DB writer thread always receives `num_workers` sentinels and terminates. The outer `try/finally` in `_worker_loop` wraps the entire function body including object construction. `scrape_all()` also sends compensation sentinels for workers that were never submitted to the executor (e.g., if shutdown interrupted the launch loop).

`worker_done()` uses `put(timeout=2.0)` in a retry loop (not unbounded `put()`) so that if the queue is full and the writer thread has crashed, the worker's `finally` block does not deadlock. When `shutdown_event` is set and the writer is no longer alive, the sentinel is dropped (not enqueued) — a dead writer will never count sentinels, so the sentinel is meaningless and the only goal is to let the worker exit. If all 15 retries (30s) exhaust without the early-exit condition, the sentinel is dropped and `shutdown_event` is set — this prevents a scenario where the writer loops forever waiting for a sentinel that was silently dropped, which would cause a 60s stall at exit.

## Shutdown + PX Recovery Guard

During shutdown, the worker skips `db_writer.submit()` whenever `grades` is empty — even if `points` is non-empty. This prevents a school from being permanently marked `no_data` (and skipped on resume) when grades are missing only because a PX retry was interrupted by shutdown. Complete data (has grades) is still submitted during shutdown to preserve progress. Schools with incomplete data remain pending for the next run.

`capture_cookies()` returns a boolean indicating whether cookies were actually saved. If capture is cancelled by shutdown (returns `False`), the cookie generation counter is **not** bumped — preventing other workers from needlessly reloading stale cookies. The stdin polling loop has no timeout — the capture window stays open indefinitely until the user presses ENTER or Ctrl+C fires `shutdown_event`. The loop catches `EOFError` (non-interactive mode), `ValueError`, and `OSError` (bad file descriptor) — all fall back to a 60s interruptible timed wait instead of propagating to the worker loop.

Each worker tracks `consecutive_capture_failures`. After `MAX_CAPTURE_FAILURES` (2) consecutive failed captures (cancellation or error), the worker skips further capture attempts and just restarts the browser with existing cookies. A successful capture (by this worker or another) resets the counter.

`restart()` uses an interruptible 0.5s-increment sleep (matching `GlobalRateLimiter.wait()`) and checks `shutdown_event` both before and after the sleep to prevent launching a browser during shutdown.

## Cookie File Atomicity

Both `capture_cookies()` and `_login()` write cookies using the atomic temp-file + `os.replace()` pattern. This prevents other workers from reading a truncated JSON file if the write is interrupted mid-stream.

## Browser Cleanup Timeout

Playwright's `browser.close()` and `pl.stop()` can hang indefinitely if the browser process OOMed or crashed (confirmed in Playwright issue #1847). Cleanup sites use appropriate strategies:

1. **`close()` method** — accepts a `timeout` parameter (default `CLOSE_TIMEOUT` = 15s). Snapshots resource references, nulls out instance fields immediately (so the instance is reusable even if cleanup hangs), then runs the actual close/stop calls in a daemon thread. If the daemon hangs past the timeout, it is abandoned and reaped at process exit.
2. **`_close_for_capture()` method** — synchronous cleanup that calls `playwright.stop()` first (kills the process tree), then nulls references. Used by `capture_cookies()` and the worker-loop gate checkpoints. No daemon thread, no greenlet errors, no hang risk from individual resource closes.
3. **Worker `finally` block** — runs `scraper.close()` in an additional outer daemon thread with a 15-second timeout (defense-in-depth).
4. **`capture_cookies()` `finally` block** — closes the capture browser resources (page, context, browser) and calls `pl.stop()` **synchronously on the same thread**. All Playwright sync API calls are bound to the creating thread's greenlet; closing from a daemon thread causes `greenlet.error: cannot switch to a different thread`. Synchronous `pl.stop()` also ensures the greenlet-based asyncio event loop is properly torn down.

`_launch_chrome()` has a fallback: if `sync_playwright().start()` fails (stale asyncio loop from an incomplete cleanup), it closes the old loop, installs a fresh one, and retries.

## Memory Management

- `_intercepted_data` (list of captured XHR response payloads) is released in a `try/finally` block at the end of `scrape_scattergram()`. This prevents large JSON bodies from persisting in memory between schools. Per-page-load growth is capped at `_MAX_INTERCEPTED` (200) entries to bound peak memory on chatty pages.
- `_response_handler` (Playwright response listener closure) is explicitly removed from the page and nil'd in both `close()` and `_close_for_capture()`. The closure captures `self`, creating a reference cycle (`NicheScraper → _response_handler → closure → NicheScraper`) — clearing it eagerly breaks the cycle so the scraper and its browser handles can be GC'd promptly instead of waiting for Python's cycle collector.
- The write queue is bounded at `maxsize=50`. If the writer crashes and stops consuming, workers block on `put()` until the 2-second timeout fires and they detect `shutdown_event`. Maximum in-queue memory is bounded to 50 items.

## Session Safety Pattern

All `get_session()` call sites use the defensive pattern: `session = None` before the call, and `if session is not None: session.close()` in the `finally` block. This prevents `UnboundLocalError` if `get_session()` raises (e.g., DB unreachable), which would otherwise mask the real error. Applies to `_write_one_with_retry()`, `_keepalive()`, `drain_queue_best_effort()`, `scrape_all()`, and `reset_no_data_schools()`.

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine` and `_session_factory` during `reset_engine()`. `get_session()` captures `_session_factory` under this lock, then calls `factory()` *outside* the lock. This is safe because NullPool means the old engine has no connection cache to invalidate — sessions from the old factory use their own independent connections. Calling `factory()` outside the lock avoids holding `_engine_lock` during Turso WebSocket connection (network I/O), which would stall `reset_engine()` during slow network conditions.
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`.
- **NullPool** — `dispose()` on the old engine is safe even with active sessions: NullPool has no idle connection cache, so dispose is effectively a no-op on live connections. Sessions created from the old factory continue working until closed.

## General Rules

- Never share Playwright browser instances across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
