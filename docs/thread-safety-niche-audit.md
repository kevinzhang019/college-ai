# Thread Safety Audit — Niche Scraper (2026-04-05)

Full concurrency audit of `niche_scraper.py` and supporting modules (`connection.py`, `shutdown.py`). Cross-referenced with Python `threading` and Playwright documentation via Context7.

**Verdict: Architecture is sound.** Two bugs fixed (one concurrency, one logic), no data races, no deadlocks, no memory leaks, DB writes are atomic.

## Bug Fixed: `worker_done()` Sentinel Drop

**File:** `niche_scraper.py`, `worker_done()` method

`worker_done()` tries to enqueue a sentinel for 30s (15 retries x 2s). The early-exit only fires when both `shutdown_event.is_set()` AND `not self.is_alive()`. If retries exhaust without that condition, the sentinel was silently dropped — leaving the writer permanently short one sentinel. It would loop forever on `Queue.get(timeout=2.0)`, causing a 60-second stall at exit (the `join(timeout=60)` backstop in `scrape_all()`).

**When it can happen:** The queue must stay full for 30 continuous seconds while the writer is alive — possible during sustained Hrana retry cycles (1.5s per failed item) with other workers still submitting.

**Fix:** Added `shutdown_event.set()` before dropping the sentinel. This signals workers to stop submitting (reducing queue pressure) and ensures the system knows something is wrong.

## Bug Fixed: `get_session()` Holding `_engine_lock` During Network I/O

**File:** `connection.py`, `get_session()` function

`get_session()` was calling `factory()` (which opens a Turso WebSocket connection — network I/O) inside `_engine_lock`. If the network is slow, this holds the lock for seconds, stalling any concurrent `reset_engine()` call that is supposed to be a fast recovery action during Hrana retry cycles.

**When it can happen:** Turso endpoint is slow or network is degraded while `DBWriterThread` is trying to `reset_engine()` after a Hrana error. The reset blocks on `_engine_lock` held by a slow `get_session()` in another thread.

**Fix:** Capture `_session_factory` reference under the lock, call `factory()` outside. Safe because: the local reference stays valid even if `reset_engine()` swaps globals immediately after. With NullPool, sessions from the old engine use their own independent connections.

## Bug Fixed: Grade Caching Optimization Was Dead Code

**File:** `niche_scraper.py`, `scrape_scattergram()` method

`scrape_scattergram()`'s `finally` block unconditionally cleared `_cached_grades = None`. Since Python's `finally` runs before the return reaches the caller, `_worker_loop`'s check at `scraper._cached_grades` was always `False` — every school did an extra grades page load even when grades were already extracted from the admissions page's `__NEXT_DATA__`.

**Not a concurrency bug** — both accesses are in the same worker thread. This was a logic error causing unnecessary page loads.

**Fix:** Only clear `_cached_grades` in an `except` block (on failure). Keep `_intercepted_data` clearing in `finally` (unconditional memory management).

## Concurrency Primitives — All Verified Correct

| Primitive | Purpose | Verification |
|-----------|---------|--------------|
| `shutdown_event` | `threading.Event` — global shutdown signal | Thread-safe by design. Signal handler runs on main thread only (Python guarantee), so `_ctrl_c_count += 1` is atomic |
| `cookie_lock` | Protects `cookie_generation[0]` reads/writes | Brief holds only. Never nested as outer lock with any other lock |
| `cookie_capture_lock` | Serializes interactive cookie captures (up to 300s) | 2s timeout polling loop checks `shutdown_event`. `finally` always releases. Lock ordering consistent: `cookie_lock` always inside `cookie_capture_lock`, never reversed |
| `capture_in_progress` | `threading.Event` — set during active cookie capture | Other workers poll-wait before `restart()` to suppress window churn during interactive capture |
| `stats_lock` | Protects `stats["total_points"]` and `stats["total_grades"]` | Independent of all other locks. Acquired in workers and writer, never simultaneously with other locks |
| `write_queue` | `Queue(maxsize=50)` — producer-consumer between workers and writer | `queue.Queue` provides all synchronization. `submit()` and `worker_done()` use timeout-based puts with shutdown checks |
| `JobClaimer._lock` | Protects index increment + list read | Minimal critical section |
| `GlobalRateLimiter._lock` | Protects slot reservation | Slot reserved under lock, sleep outside lock. `record_request()` only advances timestamp (never regresses). 0.5s interruptible sleep |
| `_engine_lock` (connection.py) | Protects `_engine` and `_session_factory` globals | `get_session()` captures factory ref under lock, calls outside. `reset_engine()` builds both into locals before atomic swap. `NullPool` means `dispose()` is safe with active sessions |

## Sentinel Invariant — Holds in All Paths

The DB writer expects exactly `num_workers` sentinels before exiting.

| Path | How sentinel is delivered |
|------|--------------------------|
| Normal worker completion | `worker_done()` in outer `finally` of `_worker_loop` |
| `NicheScraper()` constructor crash | Outer except catches, `finally` sends sentinel (`scraper is None`, close skipped) |
| `scraper.start()` crash | Same path (scraper created but start failed) |
| Workers never launched (shutdown during stagger) | `scrape_all()` sends `num_workers - len(futures)` compensation sentinels |
| Ordering guarantee | `submit()` always called before `worker_done()` per worker — FIFO queue preserves this |

## Shutdown + Cookie Capture — All Edge Cases Handled

| Edge Case | Resolution |
|-----------|------------|
| Shutdown during `cookie_capture_lock.acquire()` polling | Loop checks `shutdown_event`, `if not acquired: break` exits cleanly |
| Shutdown after lock acquired but before capture | `capture_cookies()` checks `shutdown_event` at entry and throughout stdin polling |
| Shutdown between lock acquire and `capture_in_progress.set()` | `capture_cookies()` returns False immediately; `finally: capture_in_progress.clear()` fires; lock released in outer `finally` |
| `capture_cookies()` raises exception | `finally: cookie_capture_lock.release()` fires (call is inside the `try` block) |
| Capture browser hangs | Entry close: daemon handles page/ctx/browser, `_playwright.stop()` runs synchronously on worker thread. Finally block: all cleanup (page/ctx/browser close + `pl.stop()`) runs synchronously on the same thread — Playwright sync API is not thread-safe. Stale asyncio loop handled by `_launch_chrome()` retry as defense-in-depth |
| Capture cancelled by shutdown | Cookie generation NOT bumped, preventing stale cookie proliferation |
| `consecutive_capture_failures` >= 2 | Worker skips capture and just restarts browser, preventing infinite 300s timeout cycles |

## Database Integrity — Atomic and Idempotent

- `_write_school_data()` writes datapoints + NicheGrade in single session, committed atomically
- Hrana retries create fresh session per attempt; delete-before-insert makes retries idempotent (no duplicate rows)
- `total_grades` counter incremented only after successful `session.commit()` (no double-count on retry)
- Session safety pattern: `session = None` before `get_session()`, `if session is not None: session.close()` in `finally` — prevents `UnboundLocalError` if `get_session()` raises
- Shutdown PX recovery guard: `grades` empty + shutdown = skip submit, preventing false `no_data` marking
- `drain_queue_best_effort`: runs only after all producers/consumers joined; no duplication of committed items

## Memory Management — No Leaks

| Resource | Bound | Cleanup |
|----------|-------|---------|
| `_intercepted_data` | Capped at 200 entries (`_MAX_INTERCEPTED`) | Cleared in `scrape_scattergram()` finally and `close()` |
| `_response_handler` closure | Creates reference cycle (`NicheScraper -> closure -> NicheScraper`) | Broken eagerly in `close()` by removing listener and nil'ing reference |
| Browser cleanup threads | Daemon, 15s join timeout | Instance fields nil'd before cleanup starts; daemon flag ensures thread reaped at process exit |
| Write queue | `maxsize=50` | Workers use timeout-based `put()`, drop during shutdown |

## Playwright Lifecycle — Correct

- Each worker owns its own `sync_playwright()` instance (required: Playwright API is not thread-safe)
- Response handler runs in greenlet within same OS thread — no cross-thread data race on `_intercepted_data`
- `capture_cookies()` extracts `_playwright` ref, nulls it, calls `self.close()` (daemon skips playwright), then calls `_playwright.stop()` synchronously — required because greenlets are thread-bound
- `capture_cookies()` finally block runs all cleanup (page/ctx/browser close + `pl.stop()`) synchronously — Playwright sync API calls from daemon threads cause `greenlet.error`
- `_launch_chrome()` fallback handles stale asyncio loop by closing and replacing it (thread-local in Python 3.10+, no cross-thread effect) as defense-in-depth

## `connection.py` Engine Lock — Correct

- `get_session()` captures `_session_factory` under `_engine_lock`, calls `factory()` outside — prevents holding the lock during Turso WebSocket connection (network I/O)
- `reset_engine()` builds both `new_engine` and `new_factory` into locals before swapping both globals atomically — no partial state window
- `NullPool` means `old.dispose()` is safe even with active sessions (no idle connection cache to invalidate)
- `old.dispose()` failure swallowed safely — old engine collected by GC when reference goes out of scope

## Theoretical Edge Cases (Not Fixed — Acceptable Risk)

| Edge Case | Analysis |
|-----------|----------|
| `KeyboardInterrupt` between `executor.submit()` and `futures.append(f)` in `scrape_all()` | Would cause one extra compensation sentinel (harmless — writer exits after N, extra sits unconsumed). Window is ~3 bytecodes wide |
| `sys.exit(1)` force-exit vs non-daemon ThreadPoolExecutor workers | Workers are not daemon threads; Python waits for them before exit. Worst case: worker stuck in `page.goto(timeout=60s)` delays force-exit. Workers check `shutdown_event` frequently and self-terminate within seconds |
