"""Graceful shutdown for scrapers. Ctrl+C once = finish current work; twice = force exit."""

import signal
import sys
import threading

shutdown_event = threading.Event()

_ctrl_c_count = 0


def _handler(signum, frame):
    global _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count == 1:
        print("\n⏳ Shutting down gracefully — finishing in-progress work...")
        print("   Press Ctrl+C again to force exit.")
        shutdown_event.set()
    else:
        print("\n⛔ Force exiting.")
        sys.exit(1)


def install():
    """Install SIGINT handler. Must be called from main thread."""
    signal.signal(signal.SIGINT, _handler)
