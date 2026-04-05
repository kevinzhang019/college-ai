"""Tests for niche scraper thread-safety hardening fixes.

Covers: daemon=False, bounded queue back-pressure, deadlock-safe submit().
"""

import queue
import time
import threading

import pytest

from college_ai.scraping.niche_scraper import DBWriterThread, _SENTINEL
from college_ai.scraping.shutdown import shutdown_event


def test_db_writer_is_not_daemon():
    """DBWriterThread must be daemon=False to survive unexpected main-thread exit."""
    write_queue = queue.Queue(maxsize=50)
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)
    assert not db_writer.daemon, "DBWriterThread must not be a daemon thread"


def test_submit_does_not_deadlock_on_full_queue_with_shutdown():
    """submit() must not block forever when queue is full and shutdown fires."""
    shutdown_event.clear()
    write_queue = queue.Queue(maxsize=1)
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)

    # Fill the queue so the next put() would block
    write_queue.put(("dummy", 0, [], {}, "ts", "[test]"))

    # Fire shutdown in a background thread after a short delay
    def fire_shutdown():
        time.sleep(0.3)
        shutdown_event.set()

    t = threading.Thread(target=fire_shutdown, daemon=True)
    t.start()

    # submit() should return promptly rather than blocking forever
    start = time.time()
    db_writer.submit("school-x", 1, [], {}, "ts", "[test]")
    elapsed = time.time() - start

    assert elapsed < 5.0, f"submit() blocked for {elapsed:.1f}s — deadlock risk"

    # Clean up
    shutdown_event.clear()
    t.join(timeout=2)


def test_submit_succeeds_when_queue_has_space():
    """submit() enqueues normally when the queue is not full."""
    shutdown_event.clear()
    write_queue = queue.Queue(maxsize=50)
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)

    db_writer.submit("school-a", 1, [{"gpa": 3.5}], {"overall_grade": "A"}, "ts", "[W0]")

    assert not write_queue.empty()
    item = write_queue.get_nowait()
    assert item[0] == "school-a"
    assert item[2] == [{"gpa": 3.5}]


def test_worker_done_sentinel_on_bounded_queue():
    """worker_done() must succeed even on a bounded queue."""
    write_queue = queue.Queue(maxsize=1)
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)

    db_writer.worker_done()

    item = write_queue.get_nowait()
    assert item is _SENTINEL


def test_worker_done_does_not_deadlock_when_queue_full_and_writer_dead():
    """worker_done() must not block forever when queue is full and writer crashed.

    Simulates: writer thread has crashed (shutdown_event set, thread not alive),
    queue is full.  worker_done() should force the sentinel through and return.
    """
    shutdown_event.clear()
    write_queue = queue.Queue(maxsize=1)
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()
    db_writer = DBWriterThread(write_queue, 1, stats, stats_lock)
    # Do NOT start the writer — simulates a dead writer (is_alive() = False)

    # Fill the queue
    write_queue.put(("dummy", 0, [], {}, "ts", "[test]"))

    # Set shutdown to simulate writer crash signalling
    shutdown_event.set()

    # worker_done() should return promptly, not block forever
    start = time.time()
    db_writer.worker_done()
    elapsed = time.time() - start

    assert elapsed < 10.0, f"worker_done() blocked for {elapsed:.1f}s — deadlock risk"

    # Clean up
    shutdown_event.clear()
