# Thread Safety — Niche Scraper (CRITICAL)

The Niche scraper (`niche_scraper.py`) is heavily multithreaded. Read this before modifying `niche_scraper.py`.

> **Audits:**
> - 2026-04-05 initial concurrency audit — see [Thread Safety Audit](thread-safety-niche-audit.md).
> - 2026-04-17 memory-leak / orphaned-thread follow-up — see [Thread Safety Audit 2](thread-safety-niche-audit-2.md). Confirmed a worker-exit leak where cleanup ran on the wrong thread via a daemon wrapper; fixed by making cleanup synchronous on the owner thread via `_close_for_capture()`.

## Concurrency Primitives

- **`DBWriterThread`** — all DB writes go through a single `queue.Queue` to a dedicated writer thread (`daemon=False` as of 2026-04-17 audit). This eliminates cross-thread Turso WebSocket contention. Never write to the DB from worker threads directly. The writer is non-daemon so the Turso WebSocket always gets a chance to flush before the process exits; `scrape_all()` joins it with a 60s timeout on the happy path, and on abnormal main exit the non-daemon thread blocks process teardown until the queue drains (bounded in practice by the writer's own `MAX_CONSEC_ERRORS` circuit breaker). Includes keepalive SELECT every 60s. Atomic school writes (datapoints + NicheGrade committed together via explicit `session.begin()` — DELETE + INSERT for datapoints and UPSERT for grades are wrapped in a single transaction that auto-commits on success and auto-rolls-back on any exception). Retries up to 3x with Hrana error detection and engine reset. Turso plan-level blocks (quota exhaustion, `is_blocked_error()`) are detected before the retry branch and return `False` immediately — no retries, no engine reset, since the error is not transient. The keepalive also detects blocked errors and logs a warning instead of resetting the engine. Grade counter (`total_grades`) is incremented only after successful commit to prevent double-counting on retry. If the writer thread crashes, it sets `shutdown_event` so workers stop promptly instead of scraping into a dead queue. After the writer exits, `scrape_all()` performs a best-effort drain of any remaining queue items using `drain_queue_best_effort()` — each item gets a single write attempt (no retries) to avoid masking the root crash cause. The final sentinel drain uses `get_nowait()` with `queue.Empty` handling to avoid TOCTOU races.
- **`GlobalRateLimiter`** — lock-protected slot reservation. Workers compute and reserve their slot under the lock, then sleep *outside* the lock so `record_request()` and other workers aren't blocked. Scales delays by worker count (aggregate rate stays constant regardless of parallelism). `record_request()` only advances the timestamp — never regresses past a future reservation.
- **`JobClaimer`** — lock-protected dynamic work queue distributing schools across workers
- **`cookie_lock`** — protects `cookie_generation` reads/writes (held briefly, never during I/O)
- **`cookie_capture_lock`** — serializes interactive cookie captures (held for the duration of user interaction, which is unbounded). Separated from `cookie_lock` so that other workers' per-school generation checks don't block during a capture. Workers acquire this lock using a `timeout=2.0` polling loop that checks `shutdown_event` each iteration — never a bare `Lock.acquire()` — so that Ctrl+C during capture does not leave other workers deaf to shutdown.
- **`capture_in_progress`** — `threading.Event` set while any worker is actively in `capture_cookies()`. Other workers check this at **two gate checkpoints** in the worker loop: (A) at the top of each school iteration before any page loads, and (B) between scattergram and grades scraping. When the event is set, the worker closes its browser via `_close_for_capture()`, poll-waits for capture to finish, updates its cookie generation, and restarts with fresh cookies. This prevents workers from making doomed page loads (PX will block them) and from producing noisy browser restart errors during capture. Gate checkpoint B also resets `_px_blocked = False` after restart to prevent a stale flag (set by the pre-restart scattergram scrape) from triggering redundant PX recovery on the subsequent grades scrape. The PX recovery path also closes the browser immediately via `_close_for_capture()` on detection and checks this event before restarting — a defense-in-depth guard in case another worker is mid-capture.
- **`stats_lock`** — protects `stats["total_points"]` (incremented by workers) and `stats["total_grades"]` (incremented by the DB writer after successful commit). Independent of all other locks — never nested with `cookie_lock`, `cookie_capture_lock`, etc.
- Worker threads only interact with thread-local Playwright browsers and the shared rate limiter

## Playwright Thread Model

Each worker creates its own `sync_playwright()` instance, browser, context, and page. Playwright's sync API uses greenlets — a hidden dispatcher fiber (greenlet) runs an asyncio event loop within the **same OS thread**. Response event handlers (`page.on("response", ...)`) execute in this dispatcher fiber, not a separate OS thread. This means:
- No cross-thread data race between the handler appending to `_intercepted_data` and the worker reading it — they are sequentially interleaved within one thread
- A greenlet from thread A cannot be resumed from thread B (raises `greenlet.error`), enforcing per-thread isolation
- Re-entrant Playwright API calls from inside event handlers will deadlock (the dispatcher fiber is already running)
- Only one `sync_playwright()` instance can be active per thread — starting a second one while the first is running raises "using Playwright Sync API inside the asyncio loop". `capture_cookies()` uses `_close_for_capture()` to tear down the existing browser **synchronously on the worker thread** before starting the capture browser. This is required because `stop()` must resume the greenlet that owns the asyncio event loop — a daemon thread cannot do this (greenlets are thread-bound). The `finally` block in `capture_cookies()` also runs all cleanup synchronously (page/context/browser close + `pl.stop()`) on the same thread for the same reason. `_launch_chrome()` has a fallback that resets the asyncio loop on failure as defense-in-depth. The PX recovery path uses `_close_for_capture()` (synchronous on the owner thread) followed by `start()` — it never calls `restart()` (which uses daemon-threaded `close()`) because that would violate the greenlet thread-affinity constraint and cause "Playwright Sync API inside asyncio loop" errors.

### `_close_for_capture()` — the single cleanup path

As of the 2026-04-17 audit there is **one** cleanup path:
- **`_close_for_capture()`** — synchronous cleanup on the current (owner) thread. Calls `playwright.stop()` first (kills the Playwright server subprocess and therefore the browser process), then nulls `page`/`context`/`browser`/`_playwright`/`_owner_thread`. No daemon thread, no greenlet errors, no hang risk: once `stop()` returns, the process tree is gone so there is nothing left that could block on an individual `close()`. Called by `capture_cookies()`, the worker-loop gate checkpoints, and the worker's `finally` block at exit.
- **`close(timeout)`** — preserved for API compatibility only. Delegates directly to `_close_for_capture()` and ignores the `timeout` argument. Previously this method wrapped browser teardown in a daemon thread from a non-owner context, which caused `"cannot switch to a different thread"` greenlet errors and silently orphaned a live Playwright runtime on every worker exit (confirmed in the 2026-04-17 audit).

All Playwright-touching methods (`scrape_scattergram`, `scrape_grades`, `reload_cookies_from_disk`, `close`, `_close_for_capture`) call `self._assert_owner()` on entry. `_owner_thread` is stamped at the top of `_launch_chrome()`. A cross-thread call surfaces as a loud `RuntimeError` instead of a silent greenlet hang.

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

Playwright's `browser.close()` and `pl.stop()` can hang indefinitely if the browser process OOMed or crashed (confirmed in Playwright issue #1847). The fix is structural: kill the Playwright server subprocess via `playwright.stop()` **first**, which tears down the whole process tree — nothing can hang afterwards because there is no live handle left to hang on. Every cleanup site follows this rule:

1. **`_close_for_capture()`** — the single cleanup primitive. Synchronous on the owner thread. `playwright.stop()` is called before any `page`/`context`/`browser` reference is touched, then all references are nulled. No daemon thread, no greenlet errors, no timeout needed.
2. **`close(timeout)`** — kept for API compat. Delegates to `_close_for_capture()`; the `timeout` argument is accepted but ignored.
3. **Worker `finally` block** — calls `scraper._close_for_capture()` directly on the worker (owner) thread. Exceptions are logged and swallowed so the worker always exits.
4. **`capture_cookies()` `finally` block** — the capture browser is local to `capture_cookies()` and torn down synchronously on the same thread (`pl.stop()` last, after best-effort per-resource closes — safe because this is always the calling worker thread).

`_launch_chrome()` has a fallback: if `sync_playwright().start()` fails (stale asyncio loop from an incomplete cleanup elsewhere), it closes the old loop, installs a fresh one, and retries.

## Memory Management

- `_intercepted_data` (list of captured XHR response payloads) is released in a `try/finally` block at the end of `scrape_scattergram()`. This prevents large JSON bodies from persisting in memory between schools. Per-page-load growth is capped at `_MAX_INTERCEPTED` (200) entries to bound peak memory on chatty pages.
- `_response_handler` (Playwright response listener closure) is explicitly removed from the page and nil'd in both `close()` and `_close_for_capture()`. The closure captures `self`, creating a reference cycle (`NicheScraper → _response_handler → closure → NicheScraper`) — clearing it eagerly breaks the cycle so the scraper and its browser handles can be GC'd promptly instead of waiting for Python's cycle collector.
- The write queue is bounded at `maxsize=500` (raised from 50 in the 2026-04-17 audit — the tighter cap caused workers to back-pressure into their 60s put-retry loop on transient writer stalls and drop results). If the writer crashes and stops consuming, workers block on `put()` until the 2-second timeout fires and they detect `shutdown_event`. Maximum in-queue memory is bounded to 500 small tuples.
- `f.result()` in `scrape_all()` uses `WORKER_EXIT_TIMEOUT` (90s). If a worker is stuck before its finally block (e.g. still inside `page.goto(timeout=60s)`), main stops waiting and the shortfall-sentinel path keeps shutdown deterministic.

## Session Safety Pattern

All `get_session()` call sites use the defensive pattern: `session = None` before the call, and `if session is not None: session.close()` in the `finally` block. This prevents `UnboundLocalError` if `get_session()` raises (e.g., DB unreachable), which would otherwise mask the real error. Applies to `_write_one_with_retry()`, `_keepalive()`, `drain_queue_best_effort()`, and `scrape_all()`. Write call sites (`_write_one_with_retry`, `drain_queue_best_effort`) use `with session.begin():` for explicit transaction boundaries — the context manager handles commit and rollback automatically, so no manual `session.rollback()` is needed.

## Database Connection (`connection.py`)

- **`_engine_lock`** — protects `_engine` and `_session_factory` during `reset_engine()`. `get_session()` captures `_session_factory` under this lock, then calls `factory()` *outside* the lock. This is safe because NullPool means the old engine has no connection cache to invalidate — sessions from the old factory use their own independent connections. Calling `factory()` outside the lock avoids holding `_engine_lock` during Turso WebSocket connection (network I/O), which would stall `reset_engine()` during slow network conditions.
- **`get_engine()`** — returns the current `_engine` reference under `_engine_lock`. Used by `init_db()` and migration functions. Matches the lock discipline of `get_session()`.
- **NullPool** — `dispose()` on the old engine is safe even with active sessions: NullPool has no idle connection cache, so dispose is effectively a no-op on live connections. Sessions created from the old factory continue working until closed.
- **`is_blocked_error()`** — pure function (no shared state, no locks) that detects Turso plan-level quota blocks by matching "blocked" + "upgrade" in the error message. Used by `_write_one_with_retry()`, `_keepalive()`, and `with_retry()` to fail fast instead of retrying. The `return False` path in `_write_one_with_retry` follows the same `consec_errors` → `RuntimeError` → `_crashed` → `shutdown_event` shutdown flow as any other write failure — no change to thread lifecycle, sentinel guarantee, or shutdown ordering.

## General Rules

- Never share Playwright browser instances across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
