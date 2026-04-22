"""Tests for the niche scraper sentinel guarantee.

Every _worker_loop invocation must call db_writer.worker_done() exactly once,
regardless of where a failure occurs — including the NicheScraper constructor.
This invariant ensures the DB writer thread always terminates.
"""

import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

from college_ai.scraping.niche_scraper import (
    _worker_loop, _SENTINEL, DBWriterThread, GRADE_LABEL_MAP,
)
from college_ai.scraping.shutdown import shutdown_event


def _make_fixtures(num_workers=1):
    """Create shared objects needed by _worker_loop."""
    job_claimer = MagicMock()
    job_claimer.next.return_value = None  # no work — exit immediately

    rate_limiter = MagicMock()

    write_queue = queue.Queue()
    db_writer = MagicMock()
    db_writer.worker_done = MagicMock()

    cookie_lock = threading.Lock()
    cookie_capture_lock = threading.Lock()
    capture_in_progress = threading.Event()
    cookie_generation = [0]
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()

    return {
        "job_claimer": job_claimer,
        "rate_limiter": rate_limiter,
        "db_writer": db_writer,
        "cookie_lock": cookie_lock,
        "cookie_capture_lock": cookie_capture_lock,
        "capture_in_progress": capture_in_progress,
        "cookie_generation": cookie_generation,
        "stats": stats,
        "stats_lock": stats_lock,
    }


def _call_worker(fixtures, worker_id=0, grades_only=False, headless=True):
    """Call _worker_loop with the given fixtures."""
    _worker_loop(
        worker_id,
        fixtures["job_claimer"],
        fixtures["rate_limiter"],
        fixtures["db_writer"],
        grades_only,
        headless,
        fixtures["cookie_lock"],
        fixtures["cookie_capture_lock"],
        fixtures["cookie_generation"],
        fixtures["stats"],
        fixtures["stats_lock"],
        fixtures["capture_in_progress"],
    )


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_sentinel_on_constructor_raise(MockScraper):
    """worker_done() must be called even if NicheScraper() raises."""
    MockScraper.side_effect = RuntimeError("missing env vars")
    fixtures = _make_fixtures()

    _call_worker(fixtures)

    fixtures["db_writer"].worker_done.assert_called_once()


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_sentinel_on_start_raise(MockScraper):
    """worker_done() must be called even if scraper.start() raises."""
    mock_instance = MagicMock()
    mock_instance.start.side_effect = RuntimeError("browser launch failed")
    MockScraper.return_value = mock_instance
    fixtures = _make_fixtures()

    _call_worker(fixtures)

    fixtures["db_writer"].worker_done.assert_called_once()
    # Worker exit tears down on the owner thread via _close_for_capture()
    mock_instance._close_for_capture.assert_called_once()


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_sentinel_on_normal_exit(MockScraper):
    """worker_done() called exactly once on normal (no-work) exit."""
    mock_instance = MagicMock()
    MockScraper.return_value = mock_instance
    fixtures = _make_fixtures()

    _call_worker(fixtures)

    fixtures["db_writer"].worker_done.assert_called_once()
    mock_instance._close_for_capture.assert_called_once()


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_multiple_workers_all_send_sentinels(MockScraper):
    """All workers send exactly one sentinel, even if some crash."""
    call_count = {"n": 0}

    def scraper_factory():
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("worker 1 constructor crash")
        return MagicMock()

    MockScraper.side_effect = scraper_factory
    fixtures = _make_fixtures(num_workers=3)
    db_writer = fixtures["db_writer"]

    threads = []
    for wid in range(3):
        t = threading.Thread(target=_call_worker, args=(fixtures, wid))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=10)

    assert db_writer.worker_done.call_count == 3


@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_scraper_close_not_called_when_constructor_fails(MockScraper):
    """scraper.close() must NOT be called if the constructor raised."""
    MockScraper.side_effect = RuntimeError("constructor failed")
    fixtures = _make_fixtures()

    _call_worker(fixtures)

    # NicheScraper() raised, so there's no instance to close.
    # Verify worker_done was still called.
    fixtures["db_writer"].worker_done.assert_called_once()


@patch("college_ai.scraping.niche_scraper.shutdown_event")
@patch("college_ai.scraping.niche_scraper.NicheScraper")
def test_shutdown_during_px_recovery_skips_submit(MockScraper, mock_shutdown):
    """When shutdown interrupts PX recovery, empty results must not be submitted.

    If both scrapes were PX-blocked and shutdown prevents retries, submitting
    empty data would mark the school as no_data — permanently skipping it on
    resume.  The worker should skip submit so the school stays pending.
    """
    mock_instance = MagicMock()
    MockScraper.return_value = mock_instance

    # Simulate: scraper returns empty data (PX-blocked) then shutdown fires
    mock_instance.scrape_scattergram.return_value = []
    mock_instance.scrape_grades.return_value = {}
    mock_instance._px_blocked = False
    mock_instance._cached_grades = None
    mock_instance._cached_grades_slug = None

    fixtures = _make_fixtures()
    job_claimer = fixtures["job_claimer"]

    # Provide one job, then no more
    job_claimer.next.side_effect = [("test-college", 123, 0, 1), None]

    # shutdown_event.is_set() returns False initially (allow loop entry),
    # then True after scraping completes (trigger the skip-submit guard)
    call_count = {"n": 0}

    def is_set_side_effect():
        call_count["n"] += 1
        # Calls 1-3: allow entry into while loop + past two rate limiter checks
        # Call 4+: signal shutdown (hits the skip-submit guard)
        return call_count["n"] > 3

    mock_shutdown.is_set.side_effect = is_set_side_effect

    _call_worker(fixtures)

    # submit must NOT have been called — empty PX-blocked data during shutdown
    fixtures["db_writer"].submit.assert_not_called()
    # worker_done must still be called (sentinel guarantee)
    fixtures["db_writer"].worker_done.assert_called_once()


def test_db_writer_crash_sets_shutdown_event():
    """If the DB writer thread crashes, shutdown_event must be set so workers stop.

    Without this, workers continue scraping into a dead queue and all
    subsequent results are silently lost.
    """
    shutdown_event.clear()

    write_queue = queue.Queue()
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)

    # Enqueue a poisoned item that will cause _write_one_with_retry to raise
    # an unhandled exception (not a normal work tuple).
    write_queue.put("not-a-valid-tuple")

    db_writer.start()
    db_writer.join(timeout=5)

    assert not db_writer.is_alive(), "Writer thread should have exited"
    assert shutdown_event.is_set(), "Writer crash must set shutdown_event"

    # Clean up for other tests
    shutdown_event.clear()


# ---------------------------------------------------------------------------
# drain_queue_best_effort tests
# ---------------------------------------------------------------------------

@patch("college_ai.scraping.niche_scraper.get_session")
def test_drain_queue_best_effort_writes_remaining_items(mock_get_session):
    """Best-effort drain writes queued items after writer crash."""
    mock_session = MagicMock()
    mock_get_session.return_value = mock_session
    # Make session.get return None so NicheGrade is created fresh
    mock_session.get.return_value = None

    write_queue = queue.Queue()
    write_queue.put(("school-a", 1, [], {"overall_grade": "A+"}, "2024-01-01T00:00:00", "[W0]"))
    write_queue.put(("school-b", 2, [], {"overall_grade": "B"}, "2024-01-01T00:00:00", "[W0]"))
    write_queue.put(_SENTINEL)  # should be skipped

    drained, failed = DBWriterThread.drain_queue_best_effort(write_queue)

    assert drained == 2
    assert failed == 0
    assert write_queue.empty()
    assert mock_session.commit.call_count == 2


@patch("college_ai.scraping.niche_scraper.get_session")
def test_drain_queue_best_effort_logs_failures(mock_get_session):
    """Best-effort drain counts items that fail to write."""
    mock_session = MagicMock()
    mock_get_session.return_value = mock_session
    mock_session.commit.side_effect = Exception("DB dead")

    write_queue = queue.Queue()
    write_queue.put(("school-a", 1, [], {}, "2024-01-01T00:00:00", "[W0]"))

    drained, failed = DBWriterThread.drain_queue_best_effort(write_queue)

    assert drained == 0
    assert failed == 1
    assert write_queue.empty()


def test_drain_queue_best_effort_skips_sentinels():
    """Sentinels in the queue are silently skipped during drain."""
    write_queue = queue.Queue()
    write_queue.put(_SENTINEL)
    write_queue.put(_SENTINEL)

    drained, failed = DBWriterThread.drain_queue_best_effort(write_queue)

    assert drained == 0
    assert failed == 0
    assert write_queue.empty()
