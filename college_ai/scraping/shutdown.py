"""Graceful shutdown for scrapers. Ctrl+C once = finish current work; twice = force exit."""

import signal
import sys
import threading

shutdown_event = threading.Event()

_ctrl_c_count = 0


def _handler(signum, frame):
    # Safe without a lock: Python delivers signals only to the main thread
    # and serializes handler invocations, so _ctrl_c_count += 1 is atomic.
    global _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count == 1:
        print("\n⏳ Shutting down gracefully — finishing in-progress work...")
        print("   Press Ctrl+C again to force exit.")
        shutdown_event.set()
    else:
        print("\n⛔ Force exiting.")
        sys.exit(1)


def reset():
    """Reset shutdown state for a fresh run. Safe to call before threads start."""
    global _ctrl_c_count
    _ctrl_c_count = 0
    shutdown_event.clear()


def install():
    """Install SIGINT and SIGTERM handlers. Must be called from main thread."""
    reset()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
