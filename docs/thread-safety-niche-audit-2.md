# Niche Scraper — Memory Leak & Orphan-Thread Audit (2026-04-17)

Follow-up to [Thread Safety Audit](thread-safety-niche-audit.md) (2026-04-05). The first audit verified the happy path; this one traced every worker-exit and fallback path and found a real, recurring orphan: **a daemon cleanup thread ran Playwright's sync API from the wrong thread on every worker exit**, silently leaking a live Playwright runtime and its browser subprocess.

## TL;DR

| # | Bug | Root cause | Status |
|---|-----|-----------|--------|
| 1 | Every worker exit leaked a Playwright runtime + Chromium process | `_worker_loop` finally wrapped `scraper.close()` in a daemon thread; `scraper.close()` spawned **another** daemon. Both daemons ran Playwright's greenlet-bound sync API from a non-owner thread, hit `"cannot switch to different thread"` errors that were caught silently, and never completed cleanup. | **Fixed** — Fix A/B |
| 2 | `NicheScraper.close()` could not be called safely from any thread | Same root cause as #1 — the inner daemon wrap broke greenlet affinity. | **Fixed** — Fix B |
| 3 | `DBWriterThread(daemon=True)` contradicted `thread-safety-niche.md` ("daemon=False" was documented as an explicit invariant) | Drift between code and docs. On abnormal main exit the writer was silently abandoned with its Turso WebSocket open. | **Fixed** — Fix C (code now matches docs: `daemon=False`) |
| 4 | `f.result()` unbounded in `scrape_all` | A worker stuck before its finally (e.g. mid-`page.goto(timeout=60s)`) could block main indefinitely. | **Fixed** — Fix D (`WORKER_EXIT_TIMEOUT=90s`) |
| 5 | `write_queue(maxsize=50)` caused workers to drop results on momentary writer stalls | 50 items was too tight for N workers producing at ~1 result/30s against a writer that may stall 10-30s on reconnect. | **Fixed** — Fix E (`maxsize=500`) |
| 6 | Cross-thread Playwright calls failed silently instead of loudly | No assertion caught "called from non-owner thread" cases — they manifested as hangs. | **Fixed** — Fix F (`_assert_owner()` on all Playwright-touching methods) |

## The orphan in detail

Before the fix, `_worker_loop`'s finally (`niche_scraper.py:~2347`) looked like:

```python
cleanup = threading.Thread(target=scraper.close, daemon=True)  # W1
cleanup.start()
cleanup.join(timeout=15)
```

And `NicheScraper.close()` (`~line 1997`, default `timeout=CLOSE_TIMEOUT=15`) itself ran:

```python
cleanup = threading.Thread(target=_close_resources, daemon=True)  # W2
cleanup.start()
cleanup.join(timeout=timeout)
```

`_close_resources` called `page.close(); context.close(); browser.close(); playwright.stop()` on each handle. But **W2 was not the thread that called `sync_playwright().start()`** — the worker thread was. Playwright's sync API is greenlet-bound to the owner thread; any cross-thread call raises `greenlet.error: cannot switch to a different thread`. The error was caught by the per-resource `except Exception` and logged at DEBUG, so nothing surfaced.

The real Playwright runtime (with its `playwright-driver` subprocess and the launched Chromium) stayed alive. W1's 15s `join` expired, W1 logged "Browser cleanup hung", the worker returned, and the `ThreadPoolExecutor` moved on — but W2 remained, holding the entire browser state. At N workers × M shutdowns in a long run, this is the memory leak the user was seeing.

`_close_for_capture()` had already been solving the same problem correctly (synchronous, owner-thread, `playwright.stop()` first to kill the subprocess tree) — it was just not wired into the worker-exit path. The fix was one line of wiring plus collapsing the now-redundant daemon wrapper inside `close()`.

## Fallback-case coverage

Every path where a worker or writer can exit is re-verified below. "Safe" means cleanup runs on the owner thread with no daemon wrapping.

| # | Trigger | Path | State |
|---|---------|------|-------|
| 1 | Normal SIGINT (`shutdown.py`) | `shutdown_event.set()` → worker loop exits → finally → `_close_for_capture()` | **Safe** |
| 2 | Job queue exhausted | `claim is None` → break → finally | **Safe** |
| 3 | `NicheScraper()` ctor raises | outer `try` catches → finally with `scraper=None` skips cleanup | **Safe** (sentinel still sent) |
| 4 | `scraper.start()` raises | finally → `_close_for_capture()` on partial state (owner recorded in `_launch_chrome`) | **Safe** |
| 5 | Per-school exception (caught) | loop continues, no cleanup yet | Safe — cleanup at loop exit covers it |
| 6 | Worker-wide exception | caught → finally | **Safe** |
| 7 | PX block + recovery | `_close_for_capture()` + `start()` on owner thread | **Safe** |
| 8 | Cookie capture gate A | `_close_for_capture()` → wait → `start()` | **Safe** |
| 9 | Cookie capture gate B | same + `_px_blocked=False` reset | **Safe** |
| 10 | `capture_cookies()` itself | synchronous Playwright, finally `pl.stop()` on owner thread | **Safe** |
| 11 | `capture_cookies()` fails `MAX_CAPTURE_FAILURES` | worker continues with existing cookies | **Safe** |
| 12 | Stale asyncio loop recovery in `_launch_chrome` | tears down loop, re-`start()`; `_owner_thread` re-stamped | **Safe** |
| 13 | Writer crashes | `_crashed=True` + `shutdown_event.set()`; workers drain via `_close_for_capture()` | **Safe** |
| 14 | Writer keepalive fails | counted in `consec_errors`, aborts at 10 | Same as #13 |
| 15 | `db_writer.submit()` queue full 60s | drops result, returns | Data loss (workers retry next run), no orphan |
| 16 | `db_writer.worker_done()` queue full 30s | `shutdown_event.set()` + drop sentinel | Recovered by shortfall logic |
| 17 | Rate-limiter wait + `shutdown_event` | break at guarded checkpoints | **Safe** |
| 18 | `f.result(timeout=90s)` trips | logged, main proceeds; shortfall sentinel fires | **Safe** (new in Fix D) |
| 19 | `KeyboardInterrupt` | caught → finally → same as normal shutdown | **Safe** |
| 20 | Main process crashes before `db_writer.join` | writer is non-daemon now, so it gets a chance to flush | **Safe** (new in Fix C) |
| 21 | Queue full + dead writer | `worker_done()` drops after 30s, sets shutdown | Recovered |
| 22 | Playwright binary missing (ctor path) | same as #3 | **Safe** |

## Not re-tested (known acceptable risk)

- **`SIGKILL` mid-`playwright.stop()`** — if the user `kill -9`s the process while the subprocess-tree teardown is running, orphan Chromium processes can survive. This is outside Python's control; the OS process group cleanup or a user-level reaper is the right mitigation.
- **Turso plan-level block mid-write** — `is_blocked_error()` detection returns `False` early (no retries), writer logs, proceeds to next item. No orphan, but the affected school's write is lost; resume logic picks it up next run.

## Fixes

Listed fully in the plan file (`/Users/kevin/.claude/plans/investigate-potential-memory-leak-peppy-breeze.md`). Summary:

- **Fix A** (`niche_scraper.py` worker finally): call `scraper._close_for_capture()` directly on the owner thread instead of daemon-wrapping `scraper.close()`.
- **Fix B** (`NicheScraper.close`): collapse to a one-line delegation to `_close_for_capture()`. The `timeout` argument is kept for API compat and ignored.
- **Fix C** (`DBWriterThread`): `daemon=False`. Docstring explicitly declares the invariant.
- **Fix D** (`scrape_all`): `f.result(timeout=WORKER_EXIT_TIMEOUT)` where `WORKER_EXIT_TIMEOUT = 90`. `FuturesTimeout` caught and logged.
- **Fix E** (`scrape_all`): `write_queue = queue.Queue(maxsize=500)`.
- **Fix F** (`NicheScraper`): new `_owner_thread` attribute stamped in `_launch_chrome()`; `_assert_owner()` called at the top of `close`, `_close_for_capture`, `scrape_scattergram`, `scrape_grades`, `reload_cookies_from_disk`. Cross-thread Playwright use now raises `RuntimeError` instead of hanging.

## Tests

- `tests/test_niche_memory_leak.py` (new):
  - `test_cleanup_runs_on_worker_thread` — pins the owner-thread invariant.
  - `test_cleanup_spawns_no_daemon_threads` — guards against regression to daemon wrapping.
  - `test_cleanup_exception_does_not_strand_worker` — simulates a teardown hang; worker must still exit.
  - `test_no_scraper_leak_after_worker_exit` — `gc.collect()` + `weakref` asserts the instance is releasable.
  - `test_db_writer_is_not_daemon` — pins Fix C.
- `tests/test_niche_sentinel.py`: two existing tests updated to assert `_close_for_capture()` instead of the retired `close()` daemon path.

## Manual verification

1. `ps aux | grep -E 'chromium|chrome|camoufox'` after a scrape run should print **zero** rows.
2. `threading.active_count()` should return to its pre-`scrape_all` baseline within 5s of return.
3. The log line `"Browser cleanup hung"` should never appear.
