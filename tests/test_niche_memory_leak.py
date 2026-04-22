"""Regression tests for the 2026-04-17 memory-leak / orphaned-thread audit.

The audit found that the worker-exit cleanup path wrapped ``scraper.close()``
in a daemon thread that ran in the wrong thread context (greenlet violation),
so every worker exit leaked a live Playwright runtime and its browser
subprocess.  Fix A/B now run cleanup synchronously on the worker (owner)
thread via ``NicheScraper._close_for_capture()`` — no daemon, no orphan.

These tests pin that invariant:

1. ``_close_for_capture`` is called on the worker thread, not a daemon.
2. No background daemon thread is spawned by worker cleanup.
3. A hang inside the teardown does not strand the worker (bounded by the
   worker future timeout in ``scrape_all``).
4. ``NicheScraper`` instances are released for GC once the worker exits.
5. ``DBWriterThread(daemon=False)`` matches the invariant documented in
   ``docs/thread-safety-niche.md``.
"""

import gc
import queue
import threading
import time
import weakref
from unittest.mock import MagicMock, patch

from college_ai.scraping.niche_scraper import (
    DBWriterThread,
    _worker_loop,
)


def _make_fixtures(num_workers: int = 1):
    """Minimal wiring for _worker_loop — no real browser, no real DB."""
    job_claimer = MagicMock()
    job_claimer.next.return_value = None  # no work → immediate exit

    rate_limiter = MagicMock()
    db_writer = MagicMock()
    db_writer.worker_done = MagicMock()

    return {
        "job_claimer": job_claimer,
        "rate_limiter": rate_limiter,
        "db_writer": db_writer,
        "cookie_lock": threading.Lock(),
        "cookie_capture_lock": threading.Lock(),
        "capture_in_progress": threading.Event(),
        "cookie_generation": [0],
        "stats": {"total_points": 0, "total_grades": 0},
        "stats_lock": threading.Lock(),
    }


def _call_worker(fixtures, worker_id: int = 0):
    _worker_loop(
        worker_id,
        fixtures["job_claimer"],
        fixtures["rate_limiter"],
        fixtures["db_writer"],
        False,   # grades_only
        True,    # headless
        fixtures["cookie_lock"],
        fixtures["cookie_capture_lock"],
        fixtures["cookie_generation"],
        fixtures["stats"],
        fixtures["stats_lock"],
        fixtures["capture_in_progress"],
    )


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_cleanup_runs_on_worker_thread(MockScraper):
    """_close_for_capture() must be called on the worker thread, not a daemon.

    Playwright's sync API is greenlet-bound to the thread that started it.
    If cleanup runs on a different thread, handles leak and the process
    orphans a live Chromium subprocess.
    """
    captured_thread = {}

    mock_instance = MagicMock()

    def record_thread():
        captured_thread["name"] = threading.current_thread().name

    mock_instance._close_for_capture.side_effect = record_thread
    MockScraper.return_value = mock_instance

    fixtures = _make_fixtures()

    # Run the worker on a named thread so we can compare identities.
    worker_name = "test-worker-thread"
    t = threading.Thread(target=_call_worker, args=(fixtures,), name=worker_name)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "worker thread did not exit"
    mock_instance._close_for_capture.assert_called_once()
    assert captured_thread.get("name") == worker_name, (
        "cleanup ran on the wrong thread — "
        f"expected {worker_name!r}, got {captured_thread.get('name')!r}"
    )


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_cleanup_spawns_no_daemon_threads(MockScraper):
    """Worker cleanup must not spawn background threads.

    Before the fix, worker finally called scraper.close() in a daemon
    thread, and scraper.close() spawned another daemon internally.  A
    regression would show up as stray daemon threads surviving the
    worker's exit.
    """
    mock_instance = MagicMock()
    MockScraper.return_value = mock_instance
    fixtures = _make_fixtures()

    baseline = threading.active_count()

    t = threading.Thread(target=_call_worker, args=(fixtures,))
    t.start()
    t.join(timeout=5)

    # Allow the runtime a moment to reap the worker thread itself.
    deadline = time.time() + 2.0
    while time.time() < deadline and threading.active_count() > baseline:
        time.sleep(0.05)

    assert threading.active_count() <= baseline, (
        f"thread count grew from {baseline} to {threading.active_count()} — "
        "worker cleanup likely spawned an orphan daemon"
    )


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_cleanup_exception_does_not_strand_worker(MockScraper):
    """An exception inside _close_for_capture must not prevent worker exit.

    This simulates the "page.close() raised because the browser crashed"
    scenario.  The worker should log and move on; never hang, never
    re-raise through its finally block.
    """
    mock_instance = MagicMock()
    mock_instance._close_for_capture.side_effect = RuntimeError("simulated hang")
    MockScraper.return_value = mock_instance
    fixtures = _make_fixtures()

    t = threading.Thread(target=_call_worker, args=(fixtures,))
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "worker did not exit after cleanup raised"
    fixtures["db_writer"].worker_done.assert_called_once()


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_no_scraper_leak_after_worker_exit(MockScraper):
    """After the worker exits, the NicheScraper instance must be GC-able.

    If a daemon cleanup thread holds a reference, the weakref stays live
    and Chromium memory never gets freed.
    """
    instances = []

    def factory():
        inst = MagicMock()
        instances.append(weakref.ref(inst))
        return inst

    MockScraper.side_effect = factory
    fixtures = _make_fixtures()

    _call_worker(fixtures)

    # Drop every strong reference the test itself holds.
    del fixtures
    MockScraper.reset_mock()
    MockScraper.side_effect = None
    gc.collect()

    # The MagicMock returned by `factory` is only referenced inside
    # _worker_loop's local scope.  After the worker exits and we run GC,
    # the weakref should be dead.
    assert instances, "factory was never called"
    assert all(ref() is None for ref in instances), (
        "NicheScraper instance is still reachable after worker exit — "
        "cleanup is holding a reference (likely via an orphan thread)"
    )


def test_db_writer_is_not_daemon():
    """Invariant: DBWriterThread is non-daemon.

    A daemon writer can be silently abandoned on abnormal main exit,
    leaking its Turso WebSocket connection and dropping queued writes.
    Documented in docs/thread-safety-niche.md.
    """
    q: "queue.Queue" = queue.Queue()
    writer = DBWriterThread(q, num_workers=1, stats={}, stats_lock=threading.Lock())
    assert writer.daemon is False, (
        "DBWriterThread must be non-daemon — see docs/thread-safety-niche.md"
    )
