"""
Niche.com scraper for scattergram data and college letter grades.

Uses camoufox (Firefox-based stealth browser) to bypass Cloudflare protection.
Requires a free Niche account for scattergram access.

Prerequisites:
    1. Create a free account at https://www.niche.com/account/sign-up/
    2. Add to .env:
         NICHE_EMAIL=your@email.com
         NICHE_PASSWORD=yourpassword
    3. On first run, use --headful so Cloudflare can solve its challenge interactively.

Usage:
    python -m college_ai.scraping.niche_scraper
    python -m college_ai.scraping.niche_scraper --school "stanford-university"
    python -m college_ai.scraping.niche_scraper --grades-only
    python -m college_ai.scraping.niche_scraper --school "stanford-university" --headful  # first run
"""

import os
import re
import sys
import json
import time
import queue
import random
import select
import logging
import argparse
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page

from college_ai.scraping.shutdown import shutdown_event, install as install_shutdown

from college_ai.db.connection import get_session, init_db, reset_engine, is_hrana_error
from college_ai.db.models import (
    School, ApplicantDatapoint, NicheGrade,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

NICHE_BASE = "https://www.niche.com"
LOGIN_URL = f"{NICHE_BASE}/account/sign-in/"
COLLEGE_URL = f"{NICHE_BASE}/colleges"

REQUEST_DELAY_MIN = 3.0   # seconds between page loads (min)
REQUEST_DELAY_MAX = 7.0   # seconds between page loads (max)
NAV_TIMEOUT = 60_000      # ms
PX_RESTART_AFTER = 1      # restart browser after this many consecutive PX blocks
MAX_CAPTURE_FAILURES = 2  # after this many consecutive failed captures (per-worker), skip capture and just restart
CAPTURE_CLEANUP_TIMEOUT = 15  # seconds to wait for capture browser cleanup before abandoning

# Niche grade CSS selectors (public pages, no auth needed)
GRADE_CATEGORIES = [
    "academics", "value", "diversity", "campus", "athletics",
    "party_scene", "professors", "location", "dorms",
    "food", "student_life", "safety",
]

# Niche grade display names -> model field names
GRADE_LABEL_MAP = {
    "academics": "academics",
    "value": "value",
    "diversity": "diversity",
    "campus": "campus",
    "athletics": "athletics",
    "party scene": "party_scene",
    "professors": "professors",
    "location": "location",
    "dorms": "dorms",
    "campus food": "food",
    "student life": "student_life",
    "safety": "safety",
}

# Niche stat label display names -> model field names
STATS_LABEL_MAP = {
    "acceptance rate": "acceptance_rate_niche",
    "admissions rate": "acceptance_rate_niche",
    "average annual cost": "avg_annual_cost",
    "avg annual cost": "avg_annual_cost",
    "net price": "avg_annual_cost",
    "average net price": "avg_annual_cost",
    "graduation rate": "graduation_rate_niche",
    "4-year graduation rate": "graduation_rate_niche",
    "student faculty ratio": "student_faculty_ratio_niche",
    "student-faculty ratio": "student_faculty_ratio_niche",
    "setting": "setting",
    "campus setting": "setting",
    "religious affiliation": "religious_affiliation",
    "students on campus": "pct_students_on_campus",
    "percent of students on campus": "pct_students_on_campus",
    "students in fraternities": "pct_greek_life",
    "students in sororities": "pct_greek_life",
    "greek life": "pct_greek_life",
}

MAX_WORKERS = 5  # hard cap on parallel browser workers


class GlobalRateLimiter:
    """Thread-safe rate limiter shared across all workers.

    Scales delays by worker count so aggregate request rate stays constant
    regardless of how many workers are active.

    Work-time crediting: call ``record_request()`` right after a page
    navigation fires so that scraping/parsing time counts toward the
    inter-request delay.  The next ``wait()`` only sleeps the remainder.
    """

    def __init__(self, min_delay: float, max_delay: float, num_workers: int):
        self._lock = threading.Lock()
        self._min_delay = min_delay * num_workers
        self._max_delay = max_delay * num_workers
        self._last_request_time = 0.0

    def wait(self):
        """Sleep until the next request slot is available.

        Reserves the slot under the lock, then sleeps *outside* the lock so
        that ``record_request()`` and other workers are not blocked during
        the sleep.  Each worker gets a unique slot: the first waiter gets the
        earliest slot, the second gets the next one after a fresh delay, etc.

        Sleeps in 0.5 s increments so that ``shutdown_event`` is noticed
        promptly (within ~0.5 s) rather than after the full delay.
        """
        with self._lock:
            now = time.monotonic()
            delay = random.uniform(self._min_delay, self._max_delay)
            # Earliest this request can fire: last reserved slot + delay
            earliest = self._last_request_time + delay
            sleep_for = max(0.0, earliest - now)
            # Reserve this slot so the next worker computes its delay
            # relative to our projected request time, not the stale one.
            self._last_request_time = max(now, earliest)

        # Sleep outside the lock — record_request() and other workers
        # can proceed concurrently.
        while sleep_for > 0 and not shutdown_event.is_set():
            time.sleep(min(sleep_for, 0.5))
            sleep_for -= 0.5

    def record_request(self):
        """Update the request timestamp to when the page actually loaded.

        Called after page.goto returns so that the *actual* request time
        (not the end-of-sleep time) is used for the next delay calculation.
        This credits scraping/parsing work toward the inter-request gap.

        Only advances the timestamp — never regresses past a slot that
        another worker has already reserved in ``wait()``.
        """
        with self._lock:
            now = time.monotonic()
            self._last_request_time = max(self._last_request_time, now)


class JobClaimer:
    """Thread-safe dynamic work queue for distributing schools to workers.

    Uses an index-based approach for natural load balancing — faster workers
    automatically pick up more schools. The queue is pre-filtered (resume
    filtering happens in scrape_all before workers start).
    """

    def __init__(self, slugs_with_ids: list[tuple[str, int]]):
        self._lock = threading.Lock()
        self._queue = list(slugs_with_ids)
        self._index = 0
        self._total = len(slugs_with_ids)

    @property
    def total(self) -> int:
        return self._total

    def next(self) -> Optional[tuple[str, int, int, int]]:
        """Claim next school. Returns (slug, school_id, index, total) or None."""
        if shutdown_event.is_set():
            return None
        with self._lock:
            if self._index < self._total:
                idx = self._index
                self._index += 1
                slug, school_id = self._queue[idx]
                return (slug, school_id, idx, self._total)
        return None


# ---------------------------------------------------------------------------
# Centralised DB writer — single thread owns the Turso WebSocket connection
# ---------------------------------------------------------------------------

_SENTINEL = object()  # Unique sentinel — workers send this to signal they are done

# Max rows per INSERT statement — SQLite has a limit of 999 bind params,
# and each row has 10 columns, so ~99 rows per statement is safe.
_INSERT_BATCH = 90
_MAX_INTERCEPTED = 200  # cap per-page-load intercepted responses to bound memory


def _write_school_data(session, slug, school_id, points, grades, timestamp, tag):
    """Write a single school's datapoints + NicheGrade row within an existing session.

    Shared by both the DBWriterThread hot path and the best-effort drain
    fallback.  Callers are responsible for commit/rollback/close.

    Datapoints and the NicheGrade row are written in the same session so
    they commit (or roll back) atomically.  Datapoints are inserted in
    bulk (one INSERT per ~90 rows) to avoid thousands of round-trips
    over the Turso WebSocket.
    """
    from sqlalchemy import insert

    if points and school_id:
        session.query(ApplicantDatapoint).filter_by(
            school_id=school_id, source="niche"
        ).delete()

        rows = [
            dict(
                school_id=school_id,
                source="niche",
                gpa=p["gpa"],
                sat_score=p.get("sat_score"),
                act_score=p.get("act_score"),
                outcome=p["outcome"],
                major=p.get("major"),
                residency=p.get("residency"),
                scraped_at=timestamp,
            )
            for p in points
        ]
        for i in range(0, len(rows), _INSERT_BATCH):
            session.execute(
                insert(ApplicantDatapoint).values(rows[i : i + _INSERT_BATCH])
            )

    if school_id:
        existing = session.get(NicheGrade, school_id)
        if grades:
            if existing:
                for k, v in grades.items():
                    setattr(existing, k, v)
                existing.no_data = 0
                existing.updated_at = timestamp
            else:
                session.add(NicheGrade(
                    school_id=school_id,
                    no_data=0,
                    updated_at=timestamp,
                    **grades,
                ))
            grade_count = sum(1 for k in grades if k in GRADE_LABEL_MAP.values())
            stat_count = len(grades) - grade_count
            logger.info(f"{tag}   -> {grade_count} grades, {stat_count} stats")
        else:
            if not existing:
                session.add(NicheGrade(
                    school_id=school_id,
                    no_data=1,
                    updated_at=timestamp,
                ))
            else:
                existing.no_data = 1
                existing.updated_at = timestamp
            logger.info(f"{tag}   -> No grades for {slug} — marked no_data")


class DBWriterThread(threading.Thread):
    """Dedicated thread that drains a queue of scrape results and writes to Turso.

    Only this thread touches the database, eliminating all cross-thread
    WebSocket contention.  A periodic keepalive SELECT prevents Hrana stream
    expiry during long scraping gaps.

    Workers call ``submit()`` (fire-and-forget) to enqueue results.
    The writer commits each school atomically — datapoints and NicheGrade
    row succeed or fail together.

    Thread safety: ``queue.Queue`` handles all synchronization between
    producer (worker) and consumer (writer) threads.
    """

    KEEPALIVE_INTERVAL = 60.0  # seconds between idle pings
    MAX_RETRIES = 3
    MAX_CONSEC_ERRORS = 10  # abort writer after this many consecutive failures

    def __init__(self, write_queue: queue.Queue, num_workers: int,
                 stats: dict, stats_lock: threading.Lock):
        super().__init__(daemon=True, name="db-writer")
        self._q = write_queue
        self._num_workers = num_workers
        self._sentinels_seen = 0
        self._crashed = False
        self._stats = stats
        self._stats_lock = stats_lock

    # -- public interface (called by workers) --------------------------------

    def submit(self, slug: str, school_id: int, points: list,
               grades: dict, timestamp: str, tag: str):
        """Enqueue a scrape result for writing.

        Uses put-with-timeout so that if the queue is full (writer stalled)
        and shutdown_event fires, workers are not deadlocked waiting for
        queue space that will never free up.  Dropping during shutdown is
        safe: no NicheGrade row is written, so the school stays pending
        for the next run.
        """
        item = (slug, school_id, points, grades, timestamp, tag)
        for _ in range(30):  # 30 × 2s = 60s max wait
            try:
                self._q.put(item, timeout=2.0)
                return
            except queue.Full:
                if shutdown_event.is_set():
                    logger.warning(
                        "[DB] Queue full during shutdown — dropping result for %s", slug
                    )
                    return
        logger.error(
            "[DB] Queue full for 60s — dropping result for %s (will retry next run)", slug
        )

    def worker_done(self):
        """Signal that a worker has finished.

        Uses put-with-timeout so that if the writer thread has crashed
        (and stopped consuming), workers are not deadlocked in their
        finally blocks waiting for queue space that will never free up.
        If the writer is dead, the sentinel is meaningless (nobody is
        counting them), so we drop it and return to unblock the worker.
        """
        for _ in range(15):  # 15 × 2s = 30s max wait
            try:
                self._q.put(_SENTINEL, timeout=2.0)
                return
            except queue.Full:
                if shutdown_event.is_set() and not self.is_alive():
                    # Writer is dead — sentinel is meaningless, just let
                    # the worker's finally block complete so scrape_all()
                    # can proceed to drain logic.
                    logger.warning(
                        "[DB] Queue full and writer dead — dropping sentinel"
                    )
                    return
        # Exhausted retries — drop sentinel to avoid permanent hang
        logger.error("[DB] Could not enqueue sentinel after 30s — dropping to unblock worker")

    # -- thread body ---------------------------------------------------------

    def run(self):
        try:
            self._loop()
        except Exception as e:
            logger.error("[DB] Writer thread crashed: %s", e, exc_info=True)
            self._crashed = True
            # Signal workers to stop — their scraping results would go to a
            # dead queue and be silently lost.
            shutdown_event.set()

    def _loop(self):
        logger.info("[DB] Writer thread started")
        last_keepalive = time.time()
        consec_errors = 0
        while True:
            # Use a short timeout so we notice shutdown_event promptly,
            # but still send keepalives every KEEPALIVE_INTERVAL seconds.
            try:
                item = self._q.get(timeout=2.0)
            except queue.Empty:
                if time.time() - last_keepalive >= self.KEEPALIVE_INTERVAL:
                    self._keepalive()
                    last_keepalive = time.time()
                continue

            if item is _SENTINEL:
                self._sentinels_seen += 1
                if self._sentinels_seen >= self._num_workers:
                    # All workers done — drain any remaining items that
                    # were enqueued before the final sentinel.
                    #
                    # Ordering invariant: each worker calls submit() BEFORE
                    # worker_done() (see _worker_loop's finally block), so
                    # by the time the N-th sentinel arrives, every result
                    # from every worker is already in the queue.
                    while True:
                        try:
                            leftover = self._q.get_nowait()
                        except queue.Empty:
                            break
                        if leftover is not _SENTINEL:
                            if self._write_one_with_retry(*leftover):
                                consec_errors = 0
                            else:
                                consec_errors += 1
                                if consec_errors >= self.MAX_CONSEC_ERRORS:
                                    raise RuntimeError(
                                        f"Aborting after {self.MAX_CONSEC_ERRORS} consecutive write failures"
                                    )
                    break
                continue

            if self._write_one_with_retry(*item):
                consec_errors = 0
            else:
                consec_errors += 1
                if consec_errors >= self.MAX_CONSEC_ERRORS:
                    raise RuntimeError(
                        f"Aborting after {self.MAX_CONSEC_ERRORS} consecutive write failures"
                    )

        logger.info("[DB] Writer thread finished — all items flushed")

    def _write_one_with_retry(self, slug, school_id, points, grades, now, tag):
        """Write a single school atomically (datapoints + grade) with retry.

        The NicheGrade row (which marks the school as "done" for resume) is
        only committed together with the datapoints — preventing partial
        writes that would cause the school to be permanently skipped.
        """
        n_points = len(points) if points else 0
        logger.debug("[DB] Writing %s (%d points) ...", slug, n_points)
        session = None
        for attempt in range(self.MAX_RETRIES):
            session = get_session()
            try:
                self._write_one(session, slug, school_id, points, grades, now, tag)
                session.commit()
                logger.debug("[DB] Committed %s", slug)
                # Increment grade counter only after successful commit so
                # retries on Hrana errors don't double-count.
                if grades:
                    with self._stats_lock:
                        self._stats["total_grades"] += 1
                return True
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                if is_hrana_error(e) and attempt < self.MAX_RETRIES - 1:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "[DB] Hrana error writing %s (attempt %d/%d), retry in %.1fs: %s",
                        slug, attempt + 1, self.MAX_RETRIES, delay, e,
                    )
                    reset_engine()
                    time.sleep(delay)
                    continue
                logger.error("[DB] Failed to write %s: %s", slug, e)
                return False
            finally:
                if session is not None:
                    session.close()
                session = None

    @staticmethod
    def _write_one(session, slug, school_id, points, grades, now, tag):
        """Delegate to the module-level shared write function."""
        _write_school_data(session, slug, school_id, points, grades, now, tag)

    def _keepalive(self):
        """Send a lightweight SELECT to keep the Turso WebSocket alive."""
        session = None
        session = get_session()
        try:
            from sqlalchemy import text
            session.execute(text("SELECT 1"))
            session.commit()
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            if is_hrana_error(e):
                logger.info("[DB] Keepalive hit stale stream — resetting engine")
                reset_engine()
            else:
                logger.warning("[DB] Keepalive failed (non-Hrana): %s", e)
        finally:
            if session is not None:
                session.close()

    @staticmethod
    def drain_queue_best_effort(write_queue, stats=None, stats_lock=None):
        """Drain remaining items after a writer crash (single attempt per item).

        Called from the main thread after the writer has exited. Uses a fresh
        session per item — no retries, to avoid masking the root crash cause.
        Returns (drained, failed) counts.
        """
        # Reset engine in case the writer crashed due to stale Hrana connection
        try:
            reset_engine()
        except Exception as e:
            logger.warning("[DB] Engine reset before drain failed: %s", e)

        drained = 0
        failed = 0
        max_drain = write_queue.maxsize + 10
        for _ in range(max_drain):
            try:
                item = write_queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                continue
            try:
                slug, school_id, points, grades, timestamp, tag = item
                session = None
                session = get_session()
                try:
                    _write_school_data(session, slug, school_id, points, grades, timestamp, tag)
                    session.commit()
                    drained += 1
                    if grades and stats is not None and stats_lock is not None:
                        with stats_lock:
                            stats["total_grades"] += 1
                except Exception as e:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    logger.error("[DB] Best-effort drain failed for %s: %s", slug, e)
                    failed += 1
                finally:
                    if session is not None:
                        session.close()
            except Exception as e:
                logger.error("[DB] Best-effort drain — malformed item: %s", e)
                failed += 1

        if drained or failed:
            logger.info("[DB] Best-effort drain: %d written, %d failed", drained, failed)
        return drained, failed


class NicheScraper:
    """Chrome-based Niche.com scraper.

    Uses system Chrome (playwright channel=chrome) so PerimeterX fingerprints
    match between cookie-capture and scraping sessions.
    """

    def __init__(self):
        self.email = os.getenv("NICHE_EMAIL", "")
        self.password = os.getenv("NICHE_PASSWORD", "")
        if not self.email or not self.password:
            raise RuntimeError(
                "NICHE_EMAIL and NICHE_PASSWORD must be set in .env. "
                "Register free at https://www.niche.com/account/sign-up/"
            )
        self._playwright = None
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._cookies_path = os.path.join(
            os.path.dirname(__file__), "playwright_cookies", "niche.json"
        )
        self._intercepted_data: list[dict] = []
        self._px_blocked: bool = False  # set True when PerimeterX blocks a page
        self._cached_grades: Optional[dict] = None  # grades found on admissions page
        self._cached_grades_slug: Optional[str] = None  # slug the cached grades belong to
        self._response_handler = None  # Playwright response listener (set by _setup_network_intercept)

    def _launch_chrome(self, headless: bool = False):
        """Launch system Chrome browser (same engine for capture + scraping)."""
        self._playwright = sync_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            self.browser = self._playwright.chromium.launch(
                channel="chrome", headless=headless, args=launch_args
            )
        except Exception:
            self.browser = self._playwright.chromium.launch(
                headless=headless, args=launch_args
            )
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        self.page = self.context.new_page()
        # Hide webdriver flag so PerimeterX doesn't see automation markers
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def capture_cookies(self) -> bool:
        """Open Chrome so the user can log in to Niche and solve PerimeterX.

        PerimeterX fingerprints are tied to the browser engine — capture and
        scrape must use the same engine (Chrome) for cookies to work.
        Called automatically mid-scrape when PX starts blocking.

        Returns True if cookies were successfully saved, False if cancelled
        (e.g. shutdown) or if the capture failed.  Never raises — all errors
        are caught and logged so the caller's flow to restart() is not skipped.
        """
        if shutdown_event.is_set():
            return False

        logger.info("\n" + "="*60)
        logger.info("ACTION REQUIRED: Chrome is opening for cookie capture")
        logger.info("  1. Log in to Niche if prompted")
        logger.info("  2. Browse to a few college pages until they load normally")
        logger.info("  3. Press ENTER in this terminal when done")
        logger.info("="*60)

        # Close the existing playwright instance before starting a new one.
        # Playwright's sync API creates an asyncio event loop per instance;
        # starting a second sync_playwright() in the same thread while the
        # first is still running raises "using Playwright Sync API inside
        # the asyncio loop".  Closing first frees the event loop.
        # The caller (restart()) will re-create the scraping browser after
        # capture finishes.
        self.close()

        pl = None
        browser = None
        ctx = None
        pg = None
        try:
            pl = sync_playwright().start()

            launch_args = ["--disable-blink-features=AutomationControlled"]
            try:
                browser = pl.chromium.launch(channel="chrome", headless=False, args=launch_args)
            except Exception:
                browser = pl.chromium.launch(headless=False, args=launch_args)

            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            pg = ctx.new_page()
            pg.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            pg.goto(LOGIN_URL, timeout=NAV_TIMEOUT)
        except Exception as e:
            logger.error("Failed to launch capture browser: %s", e)
            # Clean up whatever was created before the failure
            for resource, label in [(pg, "page"), (ctx, "context"), (browser, "browser")]:
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if pl is not None:
                try:
                    pl.stop()
                except Exception:
                    pass
            return False

        # Collect cookies; stays None on early return (shutdown) so
        # the save logic below is skipped.
        cookies = None
        try:
            try:
                # Use a polling loop so Ctrl+C (shutdown_event) can interrupt
                # the wait.  Cap at 5 minutes so a hung terminal or absent
                # operator doesn't hold cookie_capture_lock indefinitely,
                # which would stall all workers.
                MAX_COOKIE_WAIT_SECS = 300
                max_iters = int(MAX_COOKIE_WAIT_SECS / 0.5)
                print("\n  >>> Press ENTER after you've logged in and a college page loaded... ", end="", flush=True)
                for _ in range(max_iters):
                    if shutdown_event.is_set():
                        logger.info("Shutdown requested — cancelling cookie capture.")
                        return False
                    if sys.stdin in select.select([sys.stdin], [], [], 0.5)[0]:
                        sys.stdin.readline()
                        break
                else:
                    logger.warning("Cookie capture timed out after %ds — returning without new cookies", MAX_COOKIE_WAIT_SECS)
                    return False
            except EOFError:
                logger.info("Non-interactive mode — waiting 60s for manual interaction...")
                for _ in range(120):  # 60s in 0.5s increments
                    if shutdown_event.is_set():
                        logger.info("Shutdown requested — cancelling cookie capture.")
                        return False
                    time.sleep(0.5)
            except (ValueError, OSError) as e:
                logger.warning("stdin polling error (%s) — falling back to 60s timed wait", e)
                for _ in range(120):  # 60s in 0.5s increments
                    if shutdown_event.is_set():
                        logger.info("Shutdown requested — cancelling cookie capture.")
                        return False
                    time.sleep(0.5)
            cookies = ctx.cookies()
        finally:
            # Close capture browser with a timeout — browser.close() can
            # hang indefinitely on a crashed/zombie process.  This runs
            # while cookie_capture_lock is held, so a hang here would
            # deadlock all workers waiting for the lock.
            def _cleanup_capture_browser():
                for resource, label in [(pg, "page"), (ctx, "context"), (browser, "browser")]:
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception as exc:
                            logger.debug("Cookie capture cleanup — %s.close() failed: %s", label, exc)
                if pl is not None:
                    try:
                        pl.stop()
                    except Exception as exc:
                        logger.debug("Cookie capture cleanup — pl.stop() failed: %s", exc)

            cleanup = threading.Thread(target=_cleanup_capture_browser, daemon=True)
            cleanup.start()
            cleanup.join(timeout=CAPTURE_CLEANUP_TIMEOUT)
            if cleanup.is_alive():
                logger.warning(
                    "Cookie capture browser cleanup hung — abandoning "
                    "(daemon thread will be reaped at process exit)"
                )

        if cookies is None:
            return False

        os.makedirs(os.path.dirname(self._cookies_path), exist_ok=True)
        # Atomic write: temp file + rename prevents other workers from
        # reading a truncated cookie file if the process crashes mid-write.
        cookie_dir = os.path.dirname(self._cookies_path)
        fd, tmp_path = tempfile.mkstemp(dir=cookie_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cookies, f)
            os.replace(tmp_path, self._cookies_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(f"Saved {len(cookies)} cookies.")
        # Cookies are saved to disk; restart() will load them into the new
        # browser context.  self.context is None after self.close() above.
        return True

    def start(self, headless: bool = False, grades_only: bool = False):
        """Launch Chrome and load saved cookies.

        headless=False is default — PerimeterX blocks headless Chrome on most pages.
        """
        self._launch_chrome(headless=headless)

        if os.path.exists(self._cookies_path):
            try:
                with open(self._cookies_path, "r") as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} saved Niche cookies.")
            except Exception as e:
                logger.warning(f"Failed to load cookies: {e}")
        else:
            logger.warning("No saved Niche cookies — run --capture-cookies first.")

        if not grades_only:
            if not self._is_logged_in():
                self._login()

    def _is_logged_in(self) -> bool:
        try:
            self.page.goto(f"{NICHE_BASE}/account/", timeout=NAV_TIMEOUT)
            return "/sign-in" not in self.page.url
        except Exception:
            return False

    def _login(self):
        logger.info("Logging in to Niche...")
        self.page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)
        time.sleep(2)
        self.page.fill('input[name="email"], input[type="email"]', self.email)
        self.page.fill('input[name="password"], input[type="password"]', self.password)
        self.page.click('button[type="submit"]')
        self.page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        time.sleep(2)
        if "/sign-in" in self.page.url:
            raise RuntimeError("Niche login failed. Check credentials.")
        cookies = self.context.cookies()
        os.makedirs(os.path.dirname(self._cookies_path), exist_ok=True)
        # Atomic write: temp file + rename prevents other workers from
        # reading a truncated cookie file if the process crashes mid-write.
        cookie_dir = os.path.dirname(self._cookies_path)
        fd, tmp_path = tempfile.mkstemp(dir=cookie_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cookies, f)
            os.replace(tmp_path, self._cookies_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("Niche login successful. Cookies saved.")

    def _setup_network_intercept(self):
        """Intercept XHR/Fetch responses to capture scatter plot API data."""
        self._intercepted_data = []

        # Remove previous listener to avoid duplicates across multiple calls
        if self._response_handler is not None:
            try:
                self.page.remove_listener("response", self._response_handler)
            except Exception:
                pass

        def handle_response(response):
            try:
                url = response.url
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type or response.status != 200:
                    return
                # Capture: blocks API (primary source), or any scatter/data endpoint
                if (
                    "/api/profile/" in url
                    or "/blocks/" in url
                    or any(kw in url.lower() for kw in [
                        "scatter", "chance", "admissions", "graph", "plot",
                        "graphql", "_next/data",
                    ])
                ):
                    body = response.json()
                    # Safe without a lock: Playwright sync API runs the
                    # response handler in the same OS thread's dispatcher
                    # fiber (greenlet), not a separate thread.  The worker
                    # reads _intercepted_data only after page.goto() returns,
                    # by which point the dispatcher has yielded back.
                    if len(self._intercepted_data) < _MAX_INTERCEPTED:
                        self._intercepted_data.append({"url": url, "data": body})
            except Exception:
                pass

        self._response_handler = handle_response
        self.page.on("response", handle_response)

    def _parse_blocks_scatter_from_state(self, state: dict) -> list[dict]:
        """Extract scattergram from window.__PRELOADED_STATE__ by finding BlockScatterplot blocks."""
        points = []

        def find_blocks(obj, depth=0):
            if depth > 15 or points:  # stop after first successful extraction
                return
            if isinstance(obj, list):
                # Check if this list contains BlockScatterplot blocks
                for item in obj:
                    if isinstance(item, dict) and item.get("template") == "BlockScatterplot":
                        found = self._parse_blocks_scatter(obj)
                        points.extend(found)
                        return
                for item in obj:
                    find_blocks(item, depth + 1)
            elif isinstance(obj, dict):
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        find_blocks(v, depth + 1)

        find_blocks(state)
        return points

    def _parse_blocks_scatter(self, data) -> list[dict]:
        """Parse scattergram data from Niche's /api/profile/{uuid}/blocks/ response.

        Structure:
          blocks[].template == "BlockScatterplot"
          blocks[].buckets["0"].contents[0].scatterplot.plot.units: ["GPA", "SAT/ACT"]
          blocks[].buckets["0"].contents[0].scatterplot.plot.attributeValues[0]:
              ["Considering", "Accepted", "Rejected"]
          blocks[].buckets["0"].contents[0].scatterplot.plot.points[]:
              {values: [gpa_norm, sat_norm], attributes: [decision_idx, major_idx]}

        Values are normalized 0-1. GPA maps to 0-4.0, SAT/ACT to 0-1600.
        """
        points = []
        blocks = data if isinstance(data, list) else [data]

        # Recursively find the first plot object (stop after first to avoid dupes)
        plot_found = [None]  # mutable container for nested func

        def find_plot(obj, depth=0):
            if depth > 15 or plot_found[0]:
                return
            if isinstance(obj, dict):
                # Check if this dict has a "plot" with "points"
                plot = obj.get("plot")
                if isinstance(plot, dict) and "points" in plot and "units" in plot:
                    plot_found[0] = plot
                    return
                # Also check "scatterplot" key
                scatter = obj.get("scatterplot")
                if isinstance(scatter, dict):
                    sp = scatter.get("plot")
                    if isinstance(sp, dict) and "points" in sp:
                        plot_found[0] = sp
                        return
                # Recurse into all values (handles both dict and list buckets)
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        find_plot(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        find_plot(item, depth + 1)

        find_plot(blocks)
        plot = plot_found[0]
        if not plot:
            return points

        raw_points = plot.get("points", [])
        units = plot.get("units", [])
        attributes = plot.get("attributes", [])
        attr_values = plot.get("attributeValues", [])

        # Determine axis mapping from units
        gpa_idx = 0
        score_idx = 1
        for i, u in enumerate(units):
            ul = str(u).lower()
            if "gpa" in ul:
                gpa_idx = i
            elif "sat" in ul or "act" in ul or "score" in ul:
                score_idx = i

        # Find attribute indices for Decision, Major, In-State Status, etc.
        # attributes: ["In-State Status", "Decision", "Major"] or ["Decision", "Major"]
        decision_attr_idx = 0  # default: first attribute
        major_attr_idx = None
        residency_attr_idx = None
        for i, attr_name in enumerate(attributes):
            name_lower = str(attr_name).lower()
            if "decision" in name_lower and "type" not in name_lower and "plan" not in name_lower and "round" not in name_lower:
                decision_attr_idx = i
            elif "major" in name_lower or "field" in name_lower:
                major_attr_idx = i
            elif "state" in name_lower or "residency" in name_lower:
                residency_attr_idx = i

        # --- Helper to build a labeled map from attributeValues ---
        def _build_attr_map(attr_idx):
            m = {}
            if attr_idx is not None and attr_idx < len(attr_values) and isinstance(attr_values[attr_idx], list):
                for idx, label in enumerate(attr_values[attr_idx]):
                    m[idx] = str(label).strip()
            return m

        # Build decision outcome mapping from the corresponding attributeValues
        decision_map = {}
        if decision_attr_idx < len(attr_values) and isinstance(attr_values[decision_attr_idx], list):
            for idx, label in enumerate(attr_values[decision_attr_idx]):
                ll = str(label).lower()
                if "accept" in ll or "admit" in ll:
                    decision_map[idx] = "accepted"
                elif "reject" in ll or "deny" in ll or "denied" in ll:
                    decision_map[idx] = "rejected"
                elif "wait" in ll or "defer" in ll:
                    decision_map[idx] = "waitlisted"

        major_map = _build_attr_map(major_attr_idx)

        # Residency: normalize to "inState" / "outOfState" / "international"
        residency_raw_map = _build_attr_map(residency_attr_idx)
        residency_map = {}
        for idx, label in residency_raw_map.items():
            ll = label.lower()
            if "in" in ll and "state" in ll:
                residency_map[idx] = "inState"
            elif "international" in ll or "foreign" in ll:
                residency_map[idx] = "international"
            else:
                residency_map[idx] = "outOfState"

        logger.debug(
            f"  BlockScatterplot: {len(raw_points)} raw points, "
            f"units={units}, attrs={attributes}, decision_attr_idx={decision_attr_idx}, "
            f"decisions={decision_map}, majors={len(major_map)}, "
            f"residency={len(residency_map)}"
        )

        for pt in raw_points:
            vals = pt.get("values", [])
            attrs = pt.get("attributes", [])
            if len(vals) < 2:
                continue

            gpa_norm = vals[gpa_idx]
            score_norm = vals[score_idx]
            if gpa_norm is None or score_norm is None:
                continue

            # Scale normalized values back to real ranges
            gpa = round(float(gpa_norm) * 4.0, 2)
            sat = round(float(score_norm) * 1600)

            # Determine outcome from the correct attribute index
            outcome = None
            if decision_attr_idx < len(attrs) and attrs[decision_attr_idx] is not None:
                outcome = decision_map.get(int(attrs[decision_attr_idx]))

            # Extract optional per-point attributes
            def _lookup(attr_idx, mapping):
                if attr_idx is not None and attr_idx < len(attrs) and attrs[attr_idx] is not None:
                    return mapping.get(int(attrs[attr_idx]))
                return None

            major = _lookup(major_attr_idx, major_map)
            residency = _lookup(residency_attr_idx, residency_map)

            if outcome and 0 < gpa <= 4.0 and sat > 0:
                points.append({
                    "gpa": gpa,
                    "sat_score": float(sat),
                    "act_score": None,
                    "outcome": outcome,
                    "major": major,
                    "residency": residency,
                })

        if points:
            logger.debug(f"  Parsed {len(points)} scatter points from blocks API")
        return points

    def _extract_scatter_from_next_data(self, next_data: dict) -> list[dict]:
        """Extract scattergram points from __NEXT_DATA__ JSON."""
        points = []

        def search(obj, depth=0):
            if depth > 20:
                return
            if isinstance(obj, list):
                # Check if this looks like an array of scatter points
                if len(obj) > 5 and all(isinstance(item, dict) for item in obj[:5]):
                    sample = obj[0]
                    # Detect arrays of objects with GPA + score-like fields
                    keys_lower = {k.lower() for k in sample.keys()}
                    has_gpa_like = any(
                        k in keys_lower
                        for k in ["gpa", "y", "ydata", "yvalue",
                                  "unweightedgpa", "weightedgpa", "cumulativegpa"]
                    )
                    has_score_like = any(
                        k in keys_lower
                        for k in ["sat", "act", "x", "xdata", "xvalue",
                                  "score", "testscore", "satcomposite",
                                  "actcomposite", "sattotal"]
                    )
                    if has_gpa_like and has_score_like:
                        for item in obj:
                            pt = self._parse_scatter_point(item)
                            if pt:
                                points.append(pt)
                        return
                for item in obj:
                    search(item, depth + 1)
            elif isinstance(obj, dict):
                # Check if this single object is a scatter point
                keys_lower = {k.lower(): k for k in obj.keys()}
                has_gpa = any(
                    g in keys_lower
                    for g in ["gpa", "unweightedgpa", "weightedgpa", "cumulativegpa"]
                )
                has_score = any(
                    s in keys_lower
                    for s in ["sat", "act", "satcomposite", "actcomposite",
                              "sattotal", "testscore", "score"]
                )
                has_outcome = any(
                    o in keys_lower
                    for o in ["outcome", "status", "result", "decision",
                              "accepted", "color", "category", "admissionstatus",
                              "admissionresult"]
                )
                if has_gpa and has_score and has_outcome:
                    pt = self._parse_scatter_point(obj)
                    if pt:
                        points.append(pt)
                # Always recurse into nested structures
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        search(v, depth + 1)

        search(next_data)
        return points

    def _parse_scatter_point(self, obj: dict) -> Optional[dict]:
        """Parse a single data point dict into a normalized scatter point."""
        # Case-insensitive key lookup
        ci = {k.lower(): v for k, v in obj.items()}

        gpa = (
            ci.get("gpa") or ci.get("weightedgpa") or ci.get("unweightedgpa")
            or ci.get("cumulativegpa") or ci.get("y") or ci.get("ydata")
            or ci.get("yvalue")
        )
        sat = (
            ci.get("sat") or ci.get("satcomposite") or ci.get("sattotal")
            or ci.get("x") or ci.get("xdata") or ci.get("xvalue")
            or ci.get("score") or ci.get("testscore")
        )
        act = ci.get("act") or ci.get("actcomposite") or ci.get("actscore")

        # Determine outcome
        outcome = None
        for key in ["outcome", "status", "result", "decision", "category",
                     "admissionstatus", "admissionresult"]:
            val = ci.get(key, "")
            if val:
                val_lower = str(val).lower()
                if "accept" in val_lower or "admit" in val_lower or val_lower == "green":
                    outcome = "accepted"
                elif "deny" in val_lower or "reject" in val_lower or val_lower == "red":
                    outcome = "rejected"
                elif "wait" in val_lower or "defer" in val_lower or val_lower == "yellow":
                    outcome = "waitlisted"
                if outcome:
                    break

        if not outcome:
            color = ci.get("color", "")
            if color:
                cl = color.lower()
                if "green" in cl:
                    outcome = "accepted"
                elif "red" in cl:
                    outcome = "rejected"
                elif "yellow" in cl or "orange" in cl:
                    outcome = "waitlisted"

        # Also check boolean fields
        if not outcome:
            if ci.get("accepted") is True:
                outcome = "accepted"
            elif ci.get("accepted") is False:
                outcome = "rejected"

        # Determine major
        major = None
        for key in ["major", "fieldofstudy", "intendedmajor", "field_of_study",
                     "intended_major", "program"]:
            val = ci.get(key)
            if val and str(val).strip():
                major = str(val).strip()
                break

        # Determine residency
        residency = None
        for key in ["residency", "instate", "in_state", "instatus", "in_state_status"]:
            val = ci.get(key)
            if val:
                vl = str(val).lower()
                if "in" in vl and "state" in vl:
                    residency = "inState"
                elif "international" in vl or "foreign" in vl:
                    residency = "international"
                elif val:
                    residency = "outOfState"
                break

        if gpa is not None and (sat is not None or act is not None) and outcome:
            try:
                return {
                    "gpa": float(gpa),
                    "sat_score": float(sat) if sat else None,
                    "act_score": float(act) if act else None,
                    "outcome": outcome,
                    "major": major,
                    "residency": residency,
                }
            except (ValueError, TypeError):
                return None
        return None

    def _is_blocks_api_response(self, response) -> bool:
        """Check if a Playwright response is the blocks/profile API we need."""
        url = response.url
        ct = response.headers.get("content-type", "")
        return (
            response.status == 200
            and "application/json" in ct
            and ("/api/profile/" in url or "/blocks/" in url)
        )

    def scrape_scattergram(self, slug: str) -> list[dict]:
        """Scrape scattergram data points for a single school.

        Returns list of dicts with keys: gpa, sat_score, act_score, outcome.

        Also opportunistically extracts grades from the admissions page's
        ``__NEXT_DATA__`` and caches them in ``self._cached_grades`` so that
        ``scrape_grades`` can skip an entire page load when possible.

        Intercepted response payloads are released at exit to prevent
        large JSON bodies from persisting in memory between schools.
        """
        self._setup_network_intercept()
        self._cached_grades = None
        self._cached_grades_slug = None

        url = f"{COLLEGE_URL}/{slug}/admissions/"
        logger.info(f"  Loading scattergram: {url}")

        try:
            return self._scrape_scattergram_inner(slug, url)
        finally:
            # Release intercepted response payloads so large JSON bodies
            # don't persist in memory between schools.  Also clear cached
            # grades so a failed scrape can't leak stale grades to a
            # subsequent scrape_grades() call.
            self._intercepted_data = []
            self._cached_grades = None
            self._cached_grades_slug = None

    def _scrape_scattergram_inner(self, slug: str, url: str) -> list[dict]:
        """Inner implementation of scrape_scattergram (split for try/finally cleanup)."""
        # Navigate with domcontentloaded — embedded JSON (__PRELOADED_STATE__,
        # __NEXT_DATA__) is in the SSR HTML and available immediately, so we
        # don't need to wait for images/CSS ("load").
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:
            if not self.page or not self.page.url or self.page.url == "about:blank":
                logger.warning(f"  Failed to load {slug} scattergram: {e}")
                return []

        # Check for PerimeterX block
        try:
            title = self.page.title()
            if "denied" in title.lower() or "blocked" in title.lower():
                logger.warning(f"  PerimeterX blocked {slug} admissions page")
                self._px_blocked = True
                return []
        except Exception:
            pass

        # --- Immediate: embedded data (available at domcontentloaded) ---
        # Try __PRELOADED_STATE__ first — contains BlockScatterplot data and
        # is available in SSR HTML without waiting for any XHR.
        datapoints = []
        next_data = None
        try:
            preloaded = self.page.evaluate("""() => {
                return typeof window.__PRELOADED_STATE__ !== 'undefined'
                    ? window.__PRELOADED_STATE__ : null;
            }""")
            if preloaded:
                datapoints = self._parse_blocks_scatter_from_state(preloaded)
                if datapoints:
                    logger.debug(f"  Extracted {len(datapoints)} scatter points from __PRELOADED_STATE__")
        except Exception as e:
            logger.debug(f"  __PRELOADED_STATE__ extraction failed for {slug}: {e}")

        # Try __NEXT_DATA__ — may contain both scatter data and grades
        if not datapoints:
            try:
                next_data = self.page.evaluate("""() => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? JSON.parse(el.textContent) : null;
                }""")
            except Exception as e:
                logger.debug(f"  __NEXT_DATA__ extraction failed for {slug}: {e}")

        # Opportunistically extract grades from the admissions page's
        # __NEXT_DATA__ so we can potentially skip the grades page entirely.
        if next_data:
            try:
                cached = self._extract_grades_from_next_data(next_data)
                cached_stats = self._extract_stats_from_next_data(next_data)
                if cached:
                    combined = {**cached, **cached_stats} if cached_stats else dict(cached)
                    self._cached_grades = combined
                    self._cached_grades_slug = slug
                    logger.debug(f"  Opportunistically cached {len(combined)} grade/stat fields from admissions __NEXT_DATA__")
            except Exception as e:
                logger.debug(f"  Opportunistic grade extraction failed for {slug}: {e}")

        if datapoints:
            return datapoints

        # --- Deferred: wait for blocks API response (up to 15s) ---
        # Only reached when embedded data didn't have scatter points.
        try:
            resp = self.page.wait_for_response(
                self._is_blocks_api_response, timeout=15_000,
            )
            api_data = resp.json()
            datapoints = self._parse_blocks_scatter(api_data)
            if datapoints:
                logger.debug(f"  Extracted {len(datapoints)} scatter points from blocks API (deferred)")
                return datapoints
        except Exception:
            pass

        # --- Tertiary: intercepted responses (captured by network listener) ---
        for intercepted in self._intercepted_data:
            if "/blocks/" in intercepted["url"] or "/api/profile/" in intercepted["url"]:
                parsed = self._parse_blocks_scatter(intercepted["data"])
                datapoints.extend(parsed)
        if not datapoints:
            for intercepted in self._intercepted_data:
                parsed = self._parse_scatter_response(intercepted["data"])
                datapoints.extend(parsed)
        if datapoints:
            return datapoints

        # --- Scroll to chart to trigger lazy load, then wait for API ---
        chart_selectors = [
            '[data-testid*="scatter"]',
            '.scatterplot',
            '.scatterplot-chart__canvas',
            'canvas',
        ]
        scrolled = False
        for sel in chart_selectors:
            try:
                el = self.page.query_selector(sel)
                if el:
                    # Set up response waiter before scrolling so we catch
                    # the XHR that the scroll triggers
                    try:
                        with self.page.expect_response(
                            self._is_blocks_api_response, timeout=10_000,
                        ) as resp_info:
                            el.scroll_into_view_if_needed()
                        scroll_api_data = resp_info.value.json()
                        datapoints = self._parse_blocks_scatter(scroll_api_data)
                        if datapoints:
                            return datapoints
                    except Exception:
                        pass
                    scrolled = True
                    break
            except Exception:
                continue

        if scrolled:
            # Re-check intercepted data after scroll
            for intercepted in self._intercepted_data:
                if "/blocks/" in intercepted["url"] or "/api/profile/" in intercepted["url"]:
                    parsed = self._parse_blocks_scatter(intercepted["data"])
                    datapoints.extend(parsed)
                else:
                    parsed = self._parse_scatter_response(intercepted["data"])
                    datapoints.extend(parsed)
            if datapoints:
                return datapoints

        # --- Fallback: DOM parsing ---
        datapoints = self._parse_scatter_from_dom()

        if not datapoints:
            logger.debug(f"  No scatter data found for {slug} via any method")

        return datapoints

    def _parse_scatter_response(self, data) -> list[dict]:
        """Parse scatter plot data from an intercepted API response.

        The exact structure depends on Niche's API — this handles common patterns.
        """
        points = []

        def extract_points(obj):
            if isinstance(obj, list):
                for item in obj:
                    extract_points(item)
            elif isinstance(obj, dict):
                # Look for objects with GPA + score + outcome-like fields
                has_gpa = any(k in obj for k in ["gpa", "y", "yData", "yValue"])
                has_score = any(k in obj for k in [
                    "sat", "act", "x", "xData", "xValue", "score", "testScore"
                ])
                has_outcome = any(k in obj for k in [
                    "outcome", "status", "result", "decision",
                    "accepted", "color", "category",
                ])

                if has_gpa and has_score:
                    gpa = obj.get("gpa") or obj.get("y") or obj.get("yData") or obj.get("yValue")
                    sat = obj.get("sat") or obj.get("x") or obj.get("xData") or obj.get("xValue") or obj.get("score")
                    act = obj.get("act") or obj.get("actScore")

                    # Determine outcome
                    outcome = None
                    for key in ["outcome", "status", "result", "decision", "category"]:
                        val = obj.get(key, "")
                        if val:
                            val_lower = str(val).lower()
                            if "accept" in val_lower or val_lower == "green":
                                outcome = "accepted"
                            elif "deny" in val_lower or "reject" in val_lower or val_lower == "red":
                                outcome = "rejected"
                            elif "wait" in val_lower or val_lower == "yellow":
                                outcome = "waitlisted"
                            break

                    color = obj.get("color", "")
                    if not outcome and color:
                        color_lower = color.lower()
                        if "green" in color_lower:
                            outcome = "accepted"
                        elif "red" in color_lower:
                            outcome = "rejected"
                        elif "yellow" in color_lower or "orange" in color_lower:
                            outcome = "waitlisted"

                    if gpa is not None and (sat is not None or act is not None) and outcome:
                        points.append({
                            "gpa": float(gpa),
                            "sat_score": float(sat) if sat else None,
                            "act_score": float(act) if act else None,
                            "outcome": outcome,
                        })
                else:
                    # Recurse into nested structures
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            extract_points(v)

        extract_points(data)
        return points

    def _parse_scatter_from_dom(self) -> list[dict]:
        """Fallback: try to extract scatter data from SVG/DOM elements."""
        points = []
        try:
            # Look for data attributes on SVG circles/points
            elements = self.page.query_selector_all(
                'circle[data-gpa], [data-x][data-y], .scatter-point'
            )
            for el in elements:
                gpa = el.get_attribute("data-gpa") or el.get_attribute("data-y")
                score = el.get_attribute("data-score") or el.get_attribute("data-x")
                color = el.get_attribute("fill") or el.get_attribute("data-color") or ""

                outcome = None
                if "green" in color.lower() or "#4caf50" in color.lower():
                    outcome = "accepted"
                elif "red" in color.lower() or "#f44336" in color.lower():
                    outcome = "rejected"
                elif "yellow" in color.lower() or "orange" in color.lower():
                    outcome = "waitlisted"

                if gpa and score and outcome:
                    points.append({
                        "gpa": float(gpa),
                        "sat_score": float(score),
                        "act_score": None,
                        "outcome": outcome,
                    })
        except Exception as e:
            logger.debug(f"  DOM scatter parse failed: {e}")

        return points

    def _extract_grades_from_next_data(self, next_data: dict) -> dict:
        """Extract Niche grades from __NEXT_DATA__ JSON (Next.js embedded state)."""
        grades = {}

        def search(obj):
            if isinstance(obj, dict):
                grade_val = (
                    obj.get("grade") or obj.get("letterGrade")
                    or obj.get("overallGrade") or obj.get("value")
                )
                label_val = (
                    obj.get("label") or obj.get("name")
                    or obj.get("title") or obj.get("category")
                )
                if grade_val and label_val:
                    label_lower = str(label_val).lower().strip()
                    field = GRADE_LABEL_MAP.get(label_lower)
                    if field and re.match(r'^[A-F][+-]?$', str(grade_val).strip()):
                        grades[field] = str(grade_val).strip().upper()
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        search(v)
            elif isinstance(obj, list):
                for item in obj:
                    search(item)

        search(next_data)
        return grades

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_percent(val) -> Optional[float]:
        """'4%' | 0.04 | 4 -> 0.04"""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            v = float(val)
            return v / 100 if v > 1 else v
        s = str(val).strip().replace('%', '').replace(',', '').replace('+', '')
        try:
            v = float(s)
            return v / 100 if v > 1 else v
        except ValueError:
            return None

    @staticmethod
    def _parse_cost(val) -> Optional[int]:
        """'$18,000' | 18000 -> 18000.  Returns None for values < 100 (false positives)."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            v = int(val)
            return v if v >= 100 else None
        s = str(val).strip().replace('$', '').replace(',', '').replace('+', '').split('/')[0]
        try:
            v = int(float(s))
            return v if v >= 100 else None
        except ValueError:
            return None

    @staticmethod
    def _parse_ratio(val) -> Optional[float]:
        """'5:1' | 5 | 5.0 -> 5.0"""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if ':' in s:
            try:
                return float(s.split(':')[0])
            except ValueError:
                return None
        try:
            return float(s.replace(',', ''))
        except ValueError:
            return None

    def _extract_stats_from_next_data(self, next_data: dict) -> dict:
        """Extract quantitative stats and overall grade from __NEXT_DATA__."""
        stats: dict = {}

        def apply_stat(label: str, raw_val):
            label_lower = label.lower().strip()
            field = STATS_LABEL_MAP.get(label_lower)
            if not field or field in stats:
                return
            if field == "acceptance_rate_niche":
                v = self._parse_percent(raw_val)
                if v is not None:
                    stats[field] = v
            elif field == "avg_annual_cost":
                v = self._parse_cost(raw_val)
                if v is not None:
                    stats[field] = v
            elif field in ("graduation_rate_niche", "pct_students_on_campus", "pct_greek_life"):
                v = self._parse_percent(raw_val)
                if v is not None:
                    stats[field] = v
            elif field == "student_faculty_ratio_niche":
                v = self._parse_ratio(raw_val)
                if v is not None:
                    stats[field] = v
            elif field in ("setting", "religious_affiliation"):
                if isinstance(raw_val, str) and raw_val.strip():
                    stats[field] = raw_val.strip()

        def search(obj):
            if isinstance(obj, dict):
                # Overall grade: look for a top-level grade field without a matching category label
                grade_val = obj.get("overallGrade") or obj.get("overall_grade")
                if grade_val and re.match(r'^[A-F][+-]?$', str(grade_val).strip()):
                    stats.setdefault("overall_grade", str(grade_val).strip().upper())

                # Niche rank: look for nationalRank / rank fields
                for rank_key in ("nationalRank", "national_rank", "rank", "rankValue"):
                    rv = obj.get(rank_key)
                    if rv is not None and isinstance(rv, (int, float)) and rv > 0:
                        stats.setdefault("niche_rank", int(rv))
                        break

                # Rating
                for rating_key in ("rating", "averageRating", "overallRating", "stars", "starRating"):
                    rv = obj.get(rating_key)
                    if rv is not None and isinstance(rv, (int, float)) and 1 <= float(rv) <= 5:
                        stats.setdefault("avg_rating", float(rv))
                        break

                # Review count
                for rc_key in ("reviewCount", "review_count", "totalReviews", "numReviews"):
                    rv = obj.get(rc_key)
                    if rv is not None and isinstance(rv, (int, float)) and rv > 0:
                        stats.setdefault("review_count", int(rv))
                        break

                # Stat label+value pairs (two common patterns)
                label = (obj.get("label") or obj.get("name") or obj.get("title") or "")
                value = obj.get("value") or obj.get("displayValue") or obj.get("formattedValue")
                if label and value:
                    apply_stat(str(label), value)

                # Flat field names that may directly hold stat values
                for k, v in obj.items():
                    k_lower = k.lower()
                    if "acceptancerate" in k_lower or "admissionsrate" in k_lower:
                        apply_stat("acceptance rate", v)
                    elif "netprice" in k_lower or "annualcost" in k_lower or "avgcost" in k_lower:
                        apply_stat("average annual cost", v)
                    elif "graduationrate" in k_lower:
                        apply_stat("graduation rate", v)
                    elif "studentfaculty" in k_lower or "facultyratio" in k_lower:
                        apply_stat("student faculty ratio", v)
                    elif k_lower == "setting" and isinstance(v, str):
                        apply_stat("setting", v)
                    elif "religious" in k_lower and isinstance(v, str):
                        apply_stat("religious affiliation", v)
                    elif "oncampus" in k_lower or "livingoncampus" in k_lower:
                        apply_stat("students on campus", v)
                    elif "greek" in k_lower:
                        apply_stat("greek life", v)

                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        search(v)
            elif isinstance(obj, list):
                for item in obj:
                    search(item)

        search(next_data)
        return stats

    def _extract_stats_from_dom(self) -> dict:
        """Fallback: extract stats from CSS selectors on already-loaded page."""
        stats: dict = {}
        try:
            # Niche uses .scalar elements: .scalar__value + .scalar__label
            scalars = self.page.query_selector_all(
                '.scalar, [class*="Scalar"], [class*="scalar"], '
                '[class*="StatItem"], [class*="stat-item"], [data-testid*="stat"]'
            )
            for el in scalars:
                try:
                    label_el = el.query_selector(
                        '.scalar__label, [class*="label"], [class*="Label"]'
                    )
                    value_el = el.query_selector(
                        '.scalar__value, [class*="value"], [class*="Value"]'
                    )
                    if not label_el or not value_el:
                        continue
                    label = label_el.text_content().strip().lower()
                    raw = value_el.text_content().strip()
                    field = STATS_LABEL_MAP.get(label)
                    if not field:
                        continue
                    if field == "acceptance_rate_niche":
                        v = self._parse_percent(raw)
                        if v is not None:
                            stats[field] = v
                    elif field == "avg_annual_cost":
                        v = self._parse_cost(raw)
                        if v is not None:
                            stats[field] = v
                    elif field in ("graduation_rate_niche", "pct_students_on_campus", "pct_greek_life"):
                        v = self._parse_percent(raw)
                        if v is not None:
                            stats[field] = v
                    elif field == "student_faculty_ratio_niche":
                        v = self._parse_ratio(raw)
                        if v is not None:
                            stats[field] = v
                    elif field in ("setting", "religious_affiliation"):
                        stats[field] = raw
                except Exception:
                    continue

            # Overall grade: look for the big letter grade at the top
            for sel in [
                '.niche__grade', '[class*="overall-grade"]', '[class*="OverallGrade"]',
                '[class*="overall_grade"]', '[data-testid*="overall-grade"]',
                '[class*="GradeHeader"] [class*="grade"]',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        text = el.text_content().strip()
                        if re.match(r'^[A-F][+-]?$', text):
                            stats.setdefault("overall_grade", text.upper())
                            break
                except Exception:
                    continue

            # Rating: look for star rating
            for sel in [
                '[class*="overall-rating"] [class*="value"]',
                '[class*="OverallRating"] [class*="value"]',
                '[data-testid*="rating"] [class*="value"]',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        text = el.text_content().strip()
                        v = float(text)
                        if 1 <= v <= 5:
                            stats.setdefault("avg_rating", v)
                            break
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"  DOM stats extraction failed: {e}")

        return stats

    def _extract_stats_from_html(self, html: str) -> dict:
        """Last-resort regex extraction of stats from raw HTML.

        Niche pages include national-average tooltip text like:
          "The average national acceptance rate is around 68%"
          "The 0.53 average for Graduation Rate is 53%"
        These must be skipped — we only want school-specific values.
        """
        stats: dict = {}
        # Strip HTML comments first (Niche injects <!-- --> between words)
        cleaned = re.sub(r'<!--.*?-->', '', html)
        # Strip tooltip/description text that contains national averages
        cleaned = re.sub(
            r'(national|average\s+(?:for|is)|around\s+\d|data.?source).{0,200}',
            '', cleaned, flags=re.IGNORECASE | re.DOTALL,
        )
        patterns = [
            (r'[Aa]cceptance [Rr]ate[^0-9]{0,20}(\d[\d,.]+)\s*%', "acceptance_rate_niche", "percent"),
            (r'[Gg]raduation [Rr]ate[^0-9]{0,20}(\d[\d,.]+)\s*%', "graduation_rate_niche", "percent"),
            (r'[Ss]tudent[- ][Ff]aculty [Rr]atio[^0-9]{0,20}(\d[\d.]+)\s*:?\s*1', "student_faculty_ratio_niche", "ratio"),
            (r'[Aa]verage [Aa]nnual [Cc]ost[^$0-9]{0,10}\$?([\d,]+)', "avg_annual_cost", "cost"),
            (r'[Nn]et [Pp]rice[^$0-9]{0,10}\$?([\d,]+)', "avg_annual_cost", "cost"),
        ]
        for pattern, field, kind in patterns:
            if field in stats:
                continue
            m = re.search(pattern, cleaned)
            if m:
                raw = m.group(1)
                if kind == "percent":
                    v = self._parse_percent(raw + "%")
                    if v is not None:
                        stats[field] = v
                elif kind == "ratio":
                    v = self._parse_ratio(raw)
                    if v is not None:
                        stats[field] = v
                elif kind == "cost":
                    v = self._parse_cost(raw)
                    if v is not None:
                        stats[field] = v
        return stats

    def scrape_grades(self, slug: str) -> dict:
        """Scrape Niche letter grades AND quantitative stats for a school.

        Returns combined dict with grade fields (e.g. {"academics": "A+", ...})
        and stat fields (e.g. {"overall_grade": "A+", "acceptance_rate_niche": 0.04, ...}).

        If ``scrape_scattergram`` already cached grades from the admissions
        page's ``__NEXT_DATA__``, returns them immediately without a page load.
        """
        # --- Fast path: grades already cached from admissions page ---
        if self._cached_grades and self._cached_grades_slug == slug:
            logger.info(f"  Using cached grades from admissions page for {slug} ({len(self._cached_grades)} fields)")
            cached = self._cached_grades
            self._cached_grades = None
            self._cached_grades_slug = None
            return cached

        url = f"{COLLEGE_URL}/{slug}/"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:
            logger.warning(f"  Failed to load {slug} grades page: {e}")
            return {}

        grades = {}
        stats = {}
        html = None

        # --- Primary: __NEXT_DATA__ (most reliable for Next.js sites) ---
        # Available in SSR HTML at domcontentloaded — no JS hydration needed.
        try:
            next_data = self.page.evaluate("""() => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? JSON.parse(el.textContent) : null;
            }""")
            if next_data:
                grades = self._extract_grades_from_next_data(next_data)
                stats = self._extract_stats_from_next_data(next_data)
                if grades:
                    logger.debug(f"  Extracted grades+stats from __NEXT_DATA__ for {slug}")
                    # __NEXT_DATA__ succeeded — skip hydration wait entirely
                    combined = {**grades, **stats}
                    return combined
        except Exception as e:
            logger.debug(f"  __NEXT_DATA__ extraction failed for {slug}: {e}")

        # --- Wait for JS hydration only if __NEXT_DATA__ didn't yield grades ---
        # Use a targeted selector wait instead of a fixed sleep(3).
        try:
            self.page.wait_for_selector(
                '[class*="RankingItem"], [class*="ranking-item"], '
                '[class*="ReportCard"] li, [class*="report-card"] li, '
                '.report-card__item, .ordered__list__bucket__item, '
                '[data-testid*="report-card"] li, [data-testid*="grade-item"]',
                timeout=5_000,
            )
        except Exception:
            pass  # Timeout — proceed with whatever is in the DOM

        # --- Secondary: CSS selectors (grades) ---
        if not grades:
            try:
                card_items = self.page.query_selector_all(
                    '[class*="RankingItem"], [class*="ranking-item"], '
                    '[class*="ReportCard"] li, [class*="report-card"] li, '
                    '.report-card__item, [class*="report-card-item"], '
                    '[class*="ReportCard"] > *, .ordered__list__bucket__item, '
                    '[data-testid*="report-card"] li, [data-testid*="grade-item"], '
                    '[class*="grade-item"], [class*="GradeItem"]'
                )
                for item in card_items:
                    try:
                        text = item.text_content().strip()
                        for display_name, field in GRADE_LABEL_MAP.items():
                            if display_name.lower() in text.lower():
                                match = re.search(r'\b([A-F][+-]?)\b', text)
                                if match:
                                    grades[field] = match.group(1).upper()
                    except Exception:
                        continue
                if grades:
                    logger.debug(f"  Extracted grades from CSS selectors for {slug}")
            except Exception as e:
                logger.debug(f"  CSS selector extraction failed for {slug}: {e}")

        # --- CSS selector fallback for stats ---
        if not stats:
            stats = self._extract_stats_from_dom()

        # --- Last resort: regex on raw HTML ---
        if not grades or not stats:
            try:
                html = self.page.content()
                if "Access to this page has been denied" in html or "px-captcha" in html:
                    logger.warning(f"  PerimeterX blocked {slug} — restarting browser")
                    self._px_blocked = True
                else:
                    if not grades:
                        for display_name, field in GRADE_LABEL_MAP.items():
                            pattern = rf'{re.escape(display_name)}[^A-Za-z0-9]{{0,50}}(?<![A-Za-z])([A-F][+-]?)(?![A-Za-z0-9])'
                            match = re.search(pattern, html, re.IGNORECASE)
                            if match:
                                grades[field] = match.group(1).upper()
                        if grades:
                            logger.debug(f"  Extracted grades from HTML regex for {slug}")
                    if not stats:
                        stats = self._extract_stats_from_html(html)
            except Exception as e:
                logger.warning(f"  All grade extraction methods failed for {slug}: {e}")

        combined = {**grades, **stats}
        if stats:
            logger.debug(
                f"  Stats extracted for {slug}: "
                + ", ".join(f"{k}={v}" for k, v in stats.items())
            )
        return combined

    def reload_cookies_from_disk(self):
        """Re-read saved cookies from disk into the current browser context."""
        if not self.context:
            return
        try:
            with open(self._cookies_path, "r") as f:
                cookies = json.load(f)
            self.context.add_cookies(cookies)
            logger.info(f"Reloaded {len(cookies)} cookies from disk.")
        except Exception as e:
            logger.warning(f"Failed to reload cookies from disk: {e}")

    def restart(self, headless: bool = False, grades_only: bool = False):
        """Reload saved cookies into a fresh Chrome context.

        Checks shutdown_event both before AND after the sleep so that a
        shutdown signal during the 2-4s pause doesn't cause a wasteful
        browser launch.  Sleep is interruptible in 0.5s increments,
        matching the pattern used by GlobalRateLimiter.wait().
        """
        logger.info("Restarting Chrome with saved cookies...")
        self.close()
        if shutdown_event.is_set():
            return
        sleep_for = random.uniform(2, 4)
        while sleep_for > 0 and not shutdown_event.is_set():
            time.sleep(min(sleep_for, 0.5))
            sleep_for -= 0.5
        if shutdown_event.is_set():
            return
        self.start(headless=headless, grades_only=grades_only)

    def close(self):
        """Clean up browser resources.

        Each resource is closed independently so a failure in one (e.g.
        page.close() raising on an already-crashed browser) does not
        prevent cleanup of the remaining resources and Playwright process.

        Memory-heavy data is released first since browser cleanup can hang.
        """
        self._intercepted_data = []
        self._cached_grades = None
        self._cached_grades_slug = None
        # Remove response listener before closing page to break the
        # self -> _response_handler closure -> self reference cycle.
        if self._response_handler is not None and self.page is not None:
            try:
                self.page.remove_listener("response", self._response_handler)
            except Exception:
                pass
        self._response_handler = None
        for resource, method, label in [
            (self.page, "close", "page"),
            (self.context, "close", "context"),
            (self.browser, "close", "browser"),
            (self._playwright, "stop", "playwright"),
        ]:
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception as e:
                    logger.debug("Browser cleanup — %s.%s() failed: %s", label, method, e)
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None


_CAMPUS_SUFFIX_RE = re.compile(
    r"\s*[-\u2013\u2014]\s*"
    r"(main\s+campus|central\s+campus|flagship|"
    r"all\s+campuses|global\s+campus|online)\s*$",
    re.IGNORECASE,
)


def _get_slug_from_name(name: str) -> str:
    """Convert school name to Niche URL slug."""
    name = _CAMPUS_SUFFIX_RE.sub("", name)
    slug = name.lower().strip()
    for char in ["'", ",", ".", "(", ")", "&", "/"]:
        slug = slug.replace(char, "")
    slug = slug.replace(" - ", "-").replace("  ", " ").replace(" ", "-")
    return slug


def _worker_loop(
    worker_id: int,
    job_claimer: JobClaimer,
    rate_limiter: GlobalRateLimiter,
    db_writer: DBWriterThread,
    grades_only: bool,
    headless: bool,
    cookie_lock: threading.Lock,
    cookie_capture_lock: threading.Lock,
    cookie_generation: list,
    stats: dict,
    stats_lock: threading.Lock,
):
    """Single worker thread: owns its own browser, sends results to DB writer.

    INVARIANT: db_writer.worker_done() is called exactly once per invocation,
    regardless of where a failure occurs (including the NicheScraper constructor).
    """
    tag = f"[W{worker_id}]"
    scraper = None
    try:
        scraper = NicheScraper()
        consecutive_px_blocks = 0
        my_cookie_gen = 0
        consecutive_capture_failures = 0

        scraper.start(headless=headless, grades_only=grades_only)
        logger.info(f"{tag} Browser started")

        while not shutdown_event.is_set():
            claim = job_claimer.next()
            if claim is None:
                break
            slug, school_id, idx, total = claim

            logger.info(f"{tag} [{idx+1}/{total}] Scraping {slug} ...")

            try:
                now = datetime.now(timezone.utc).isoformat()
                scraper._px_blocked = False

                # Reload cookies if another worker refreshed them
                with cookie_lock:
                    current_gen = cookie_generation[0]
                if current_gen > my_cookie_gen:
                    logger.info(f"{tag} Cookie generation {current_gen} > {my_cookie_gen} — reloading from disk")
                    scraper.reload_cookies_from_disk()
                    my_cookie_gen = current_gen

                # Scrape scattergram data (no DB connection held during network I/O)
                points = []
                if not grades_only:
                    rate_limiter.wait()
                    if shutdown_event.is_set():
                        break
                    points = [p for p in scraper.scrape_scattergram(slug)
                             if p.get("outcome") != "waitlisted"]
                    rate_limiter.record_request()  # credit scraping time toward next delay
                    with stats_lock:
                        stats["total_points"] += len(points)
                    logger.info(f"{tag}   -> {len(points)} scattergram points")

                # Scrape grades — skip page load if admissions page already
                # yielded grades (cached by scrape_scattergram).
                if scraper._cached_grades and scraper._cached_grades_slug == slug:
                    grades = scraper.scrape_grades(slug)  # returns cache, no navigation
                else:
                    rate_limiter.wait()
                    if shutdown_event.is_set():
                        break
                    grades = scraper.scrape_grades(slug)
                    rate_limiter.record_request()  # credit scraping time

                # Handle PerimeterX block — recover and retry BOTH pages
                if scraper._px_blocked:
                    consecutive_px_blocks += 1
                    if consecutive_px_blocks >= PX_RESTART_AFTER:
                        # After MAX_CAPTURE_FAILURES consecutive failed captures,
                        # skip the interactive capture and just restart the browser.
                        # This prevents an infinite cycle of 300s timeouts when no
                        # user is at the terminal to complete the capture.
                        if consecutive_capture_failures >= MAX_CAPTURE_FAILURES:
                            logger.warning(
                                f"{tag} Skipping cookie capture after "
                                f"{consecutive_capture_failures} consecutive failures "
                                "— restarting browser with existing cookies"
                            )
                        else:
                            # Check if another worker already refreshed cookies.
                            # my_cookie_gen is updated here (outer read) AND inside
                            # cookie_capture_lock (inner read).  Both updates are
                            # load-bearing:
                            #   - Outer update: if the generation was already
                            #     bumped, we skip the capture lock entirely.
                            #   - Inner update: if another worker bumped it
                            #     while we waited for cookie_capture_lock, the inner
                            #     check detects it (generation[0] > stale my_cookie_gen)
                            #     and skips the redundant capture.
                            with cookie_lock:
                                already_refreshed = cookie_generation[0] > my_cookie_gen
                                my_cookie_gen = cookie_generation[0]

                            if not already_refreshed:
                                # Serialize captures — only one worker captures at a time.
                                # cookie_capture_lock is held for the duration of interactive
                                # capture; cookie_lock is only held briefly for generation
                                # reads/writes so other workers' per-school checks don't block.
                                # Use timeout-based acquire so workers can check shutdown_event
                                # instead of blocking indefinitely (capture can take up to 300s).
                                acquired = False
                                while not acquired and not shutdown_event.is_set():
                                    acquired = cookie_capture_lock.acquire(timeout=2.0)
                                if not acquired:
                                    # shutdown_event fired while waiting
                                    break
                                try:
                                    # Double-check: another worker may have captured
                                    # while we waited for cookie_capture_lock
                                    with cookie_lock:
                                        already_refreshed = cookie_generation[0] > my_cookie_gen
                                        my_cookie_gen = cookie_generation[0]
                                    if not already_refreshed:
                                        logger.warning(f"{tag} PerimeterX blocked — capturing cookies...")
                                        captured = scraper.capture_cookies()
                                        if captured:
                                            consecutive_capture_failures = 0
                                            with cookie_lock:
                                                cookie_generation[0] += 1
                                                my_cookie_gen = cookie_generation[0]
                                        else:
                                            consecutive_capture_failures += 1
                                            logger.info(f"{tag} Cookie capture cancelled — generation not bumped")
                                    else:
                                        # Another worker succeeded while we waited
                                        consecutive_capture_failures = 0
                                finally:
                                    cookie_capture_lock.release()
                            else:
                                logger.info(f"{tag} Cookies already refreshed by another worker — reloading")
                                consecutive_capture_failures = 0

                        if shutdown_event.is_set():
                            break

                        scraper.restart(headless=headless, grades_only=grades_only)
                        consecutive_px_blocks = 0
                        scraper._px_blocked = False

                        # Retry scattergram if it was blocked
                        if not grades_only and not points and not shutdown_event.is_set():
                            rate_limiter.wait()
                            if not shutdown_event.is_set():
                                points = [p for p in scraper.scrape_scattergram(slug)
                                         if p.get("outcome") != "waitlisted"]
                                rate_limiter.record_request()
                                with stats_lock:
                                    stats["total_points"] += len(points)
                                logger.info(f"{tag}   -> {len(points)} scattergram points (retry)")

                        # Retry grades if they were blocked
                        if not grades and not shutdown_event.is_set():
                            rate_limiter.wait()
                            if not shutdown_event.is_set():
                                grades = scraper.scrape_grades(slug)
                                rate_limiter.record_request()
                else:
                    consecutive_px_blocks = 0

                # During shutdown, don't submit results with empty grades —
                # the school would be marked no_data and permanently skipped
                # on resume.  Grades may be empty because a PX retry was
                # skipped due to shutdown, not because the school truly has
                # no data.  Leaving it pending lets the next run retry cleanly.
                # Complete data (has grades) is safe to submit even during
                # shutdown so progress is preserved.
                if shutdown_event.is_set() and not grades:
                    logger.info(f"{tag}   -> Skipping submit for {slug} (shutdown + no grades — will retry next run)")
                    break

                # Enqueue results for the DB writer — worker moves on immediately
                db_writer.submit(slug, school_id, points, grades, now, tag)

            except Exception as e:
                logger.error(f"{tag}   -> FAILED {slug}: {e}")

    except Exception as e:
        logger.error(f"{tag} Worker crashed: {e}")
    finally:
        db_writer.worker_done()
        if scraper is not None:
            # Playwright's browser.close() can hang indefinitely if the
            # browser process OOMed or crashed.  Run cleanup in a daemon
            # thread with a timeout so the worker always exits promptly.
            cleanup = threading.Thread(target=scraper.close, daemon=True)
            cleanup.start()
            cleanup.join(timeout=15)
            if cleanup.is_alive():
                logger.warning(
                    "%s Browser cleanup hung — abandoning (daemon thread "
                    "will be reaped at process exit)", tag
                )
                # Drop reference so GC can reclaim the NicheScraper (and its
                # browser handles) once the daemon thread finishes or exits.
                scraper = None
        logger.info(f"{tag} Worker finished")


def scrape_all(
    slugs: Optional[list[str]] = None,
    grades_only: bool = False,
    resume: bool = True,
    headless: bool = False,
    num_workers: int = 3,
):
    """Scrape Niche data for all schools using parallel browser workers.

    Args:
        slugs: Optional specific school slugs. If None, uses all schools in DB.
        grades_only: If True, only scrape letter grades (no scattergrams).
        resume: If True, skip schools already marked 'done' in scrape_jobs.
        headless: If True, run browsers in headless mode (blocked by PerimeterX).
        num_workers: Number of parallel browser workers (default 3, max 5).
    """
    init_db()
    session = None
    session = get_session()
    try:
        # Build slug -> school_id mapping, ordered by enrollment (largest first)
        schools = (
            session.query(School.id, School.name, School.enrollment)
            .order_by(School.enrollment.desc().nullslast())
            .all()
        )
        if slugs:
            slug_map = {}
            for slug in slugs:
                for sid, name, _enr in schools:
                    if _get_slug_from_name(name) == slug:
                        slug_map[slug] = sid
                        break
                else:
                    slug_map[slug] = 0
        else:
            slug_map = {_get_slug_from_name(name): sid for sid, name, _enr in schools}

        # Skip schools that already have a NicheGrade row (either real data or no_data)
        if resume:
            existing_ids = {
                row.school_id
                for row in session.query(NicheGrade.school_id).all()
            }
            pending_slugs = [(s, sid) for s, sid in slug_map.items() if sid not in existing_ids]
            skipped_slugs = [s for s, sid in slug_map.items() if sid in existing_ids]
            if skipped_slugs:
                logger.info(f"Resuming: skipping {len(skipped_slugs)} schools already in niche_grades:")
                for sk in skipped_slugs:
                    logger.info(f"  skip: {sk}")
            slugs_with_ids = pending_slugs
        else:
            slugs_with_ids = list(slug_map.items())
    finally:
        if session is not None:
            session.close()

    # For single school, no need for parallelism
    num_workers = min(num_workers, MAX_WORKERS)
    if len(slugs_with_ids) <= 1:
        num_workers = 1

    job_claimer = JobClaimer(slugs_with_ids)
    rate_limiter = GlobalRateLimiter(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, num_workers)
    cookie_lock = threading.Lock()          # protects cookie_generation reads/writes (held briefly)
    cookie_capture_lock = threading.Lock()  # serializes interactive cookie captures (held for long periods)
    cookie_generation = [0]  # bumped after each capture; workers compare to detect stale cookies
    stats = {"total_points": 0, "total_grades": 0}
    stats_lock = threading.Lock()

    # Single DB writer thread — only this thread touches Turso
    write_queue = queue.Queue(maxsize=50)
    db_writer = DBWriterThread(write_queue, num_workers, stats, stats_lock)
    db_writer.start()

    logger.info(
        f"Starting Niche scrape: {len(slugs_with_ids)} schools pending, {num_workers} worker(s)"
    )

    install_shutdown()

    futures = []
    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            for wid in range(num_workers):
                if shutdown_event.is_set():
                    break
                f = executor.submit(
                    _worker_loop, wid, job_claimer, rate_limiter,
                    db_writer, grades_only, headless, cookie_lock,
                    cookie_capture_lock, cookie_generation, stats,
                    stats_lock,
                )
                futures.append(f)
                if wid < num_workers - 1:
                    # Stagger browser launches; check shutdown every 0.5s
                    for _ in range(4):
                        if shutdown_event.is_set():
                            break
                        time.sleep(0.5)

            for f in futures:
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"Worker failed: {e}")
    except KeyboardInterrupt:
        logger.info("Interrupted — waiting for DB writer to flush pending writes...")
    finally:
        # Workers that actually ran have each sent one sentinel via their
        # finally block.  If shutdown interrupted the launch loop, fewer
        # workers ran than num_workers, so the DB writer is still waiting
        # for the missing sentinels.  Send exactly the shortfall.
        launched = len(futures)
        for _ in range(num_workers - launched):
            db_writer.worker_done()

        # Wait for writer to flush remaining items.
        # If the writer already crashed (is_alive() False before join), its
        # run() set shutdown_event so workers stopped promptly.
        db_writer.join(timeout=60)
        if db_writer.is_alive():
            logger.warning("DB writer did not finish in time — some writes may be lost")

        # Best-effort drain if the writer died with items still queued.
        # write_queue.empty() is safe here: all workers have exited
        # (ThreadPoolExecutor joined them) and db_writer has been joined
        # above, so no concurrent producers/consumers remain.
        if not db_writer.is_alive() and db_writer._crashed and not write_queue.empty():
            logger.warning("[DB] Writer crashed — attempting best-effort queue drain")
            DBWriterThread.drain_queue_best_effort(write_queue, stats, stats_lock)

    if shutdown_event.is_set():
        logger.info("Graceful shutdown complete — all pending writes flushed.")

    logger.info(
        f"Done. {stats['total_points']} scattergram points, "
        f"{stats['total_grades']} schools with grades."
    )


def reset_no_data_schools():
    """Delete NicheGrade rows marked no_data so those schools get retried."""
    init_db()
    session = None
    session = get_session()
    try:
        count = session.query(NicheGrade).filter_by(no_data=1).delete()
        session.commit()
        logger.info(f"Deleted {count} no_data NicheGrade rows — those schools will be retried.")
    finally:
        if session is not None:
            session.close()


def main():
    parser = argparse.ArgumentParser(description="Scrape Niche.com admissions data")
    parser.add_argument(
        "--school", type=str, default=None,
        help="Scrape a single school by slug (e.g. 'stanford-university')"
    )
    parser.add_argument(
        "--grades-only", action="store_true",
        help="Only scrape letter grades, skip scattergrams"
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Re-scrape all schools, ignoring previous progress"
    )
    parser.add_argument(
        "--reset-empty", action="store_true",
        help="Delete no_data NicheGrade rows so those schools get retried, then exit"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging for selector/extraction diagnostics"
    )
    parser.add_argument(
        "--headful", action="store_true",
        help="Run in headful (visible) mode — this is the DEFAULT since PerimeterX blocks headless"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Force headless mode (will be blocked by PerimeterX on most pages)"
    )
    parser.add_argument(
        "--capture-cookies", action="store_true",
        help="Open a visible browser for manual login/challenge solving, then save cookies. "
             "Run this once before headless scrapes to bypass PerimeterX."
    )
    parser.add_argument(
        "--workers", type=int, default=3,
        help="Number of parallel browser workers (default: 3, max: 5)"
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.reset_empty:
        reset_no_data_schools()
        return

    if args.capture_cookies:
        scraper = NicheScraper()
        try:
            scraper.capture_cookies()
        finally:
            scraper.close()
        return

    # headless is blocked by PerimeterX; only use if explicitly requested
    headless = args.headless  # Default False; only True when --headless is passed
    workers = min(args.workers, MAX_WORKERS)
    if args.school:
        scrape_all(slugs=[args.school], grades_only=args.grades_only, resume=not args.no_resume, headless=headless, num_workers=workers)
    else:
        scrape_all(grades_only=args.grades_only, resume=not args.no_resume, headless=headless, num_workers=workers)


if __name__ == "__main__":
    main()
