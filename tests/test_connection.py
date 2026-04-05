"""Tests for connection.py thread-safety primitives."""

import threading

from college_ai.db.connection import get_engine, reset_engine


def test_get_engine_returns_current_engine():
    """get_engine() returns a non-None engine reference."""
    engine = get_engine()
    assert engine is not None


def test_get_engine_concurrent_with_reset():
    """get_engine() never returns None when racing with reset_engine().

    Stress test: 4 reader threads call get_engine() 100 times each while
    1 resetter thread calls reset_engine() 10 times.
    """
    errors = []

    def reader():
        for _ in range(100):
            e = get_engine()
            if e is None:
                errors.append("get_engine() returned None")

    def resetter():
        for _ in range(10):
            reset_engine()

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=resetter))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"Race detected: {errors}"
