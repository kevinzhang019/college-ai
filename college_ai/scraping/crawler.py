"""
Multithreaded College Site Crawler
Reads college URLs from CSV files and performs multithreaded crawling of each site.
Uses BeautifulSoup to find internal links and uploads each page directly to Milvus.
"""

import os
os.environ["GRPC_VERBOSITY"] = "ERROR"
import sys
import csv
import glob
import time
import uuid
import math
import threading
import queue
import random
import concurrent.futures
import json
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import OrderedDict
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
import hashlib
import requests
import re

from college_ai.scraping.shutdown import shutdown_event as global_shutdown_event, install as install_shutdown
import yaml

try:
    from curl_cffi import requests as curl_requests  # type: ignore
except Exception:
    curl_requests = None
# Optional Playwright (sync API) for JS-rendered fallback
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore
# Stealth patches for Playwright (15+ detection vectors)
try:
    from playwright_stealth import Stealth as _PlaywrightStealth  # type: ignore
    _pw_stealth = _PlaywrightStealth()
except Exception:
    _pw_stealth = None
# Camoufox: Firefox-based stealth browser for deep fingerprint spoofing
try:
    from camoufox.sync_api import Camoufox  # type: ignore
except Exception:
    Camoufox = None
# Browserforge: realistic browser fingerprint generation
try:
    from browserforge.headers import HeaderGenerator  # type: ignore
    from browserforge.fingerprints import FingerprintGenerator  # type: ignore
except Exception:
    HeaderGenerator = None
    FingerprintGenerator = None
from bs4 import BeautifulSoup
from pymilvus import (
    connections,
    Collection,
    CollectionSchema,
    FieldSchema,
    DataType,
    Function,
    FunctionType,
    utility,
)


# ==================== Chromium Launch Flags ====================
# Safe memory-saving flags with no fingerprint/detection impact.
# Shared between PlaywrightPool and non-pool fallback browser launches.
_CHROMIUM_FLAGS_SAFE = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-extensions", "--disable-sync", "--mute-audio",
    "--disable-features=ExternalProtocolDialog",
    # Memory-saving flags (no fingerprint impact — purely internal)
    "--no-first-run", "--no-zygote",
    "--disable-ipc-flooding-protection",
    "--disable-default-apps",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--disable-logging",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

# Additional flags for non-pool fallback path only.
# These affect fingerprinting but are acceptable in the rarely-used fallback.
_CHROMIUM_FLAGS_FALLBACK_EXTRA = [
    "--disable-accelerated-2d-canvas",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees,ExternalProtocolDialog",
    "--disable-permissions-api",
    "--allow-running-insecure-content",
    "--force-device-scale-factor=1",
]


# ==================== Proxy Pool ====================
class ProxyPool:
    """Thread-safe, health-aware proxy pool with bounded concurrency per proxy and sticky assignments."""

    def __init__(
        self,
        proxies: List[str],
        max_concurrency_per_proxy: int = 2,
        cooldown_sec: int = 120,
        max_consec_fails: int = 3,
        sticky_requests: int = 3,
    ) -> None:
        self.proxies = list(dict.fromkeys([p.strip() for p in proxies if p.strip()]))
        self.max_concurrency_per_proxy = max(1, int(max_concurrency_per_proxy))
        self.cooldown_sec = max(10, int(cooldown_sec))
        self.max_consec_fails = max(1, int(max_consec_fails))
        self.sticky_requests = max(1, int(sticky_requests))
        self._lock = threading.Lock()
        # Per-proxy metrics/state
        self._state: Dict[str, Dict[str, Any]] = {}
        for proxy in self.proxies:
            self._state[proxy] = {
                "sema": threading.Semaphore(self.max_concurrency_per_proxy),
                "successes": 0,
                "failures": 0,
                "consec_fails": 0,
                "avg_latency_ms": None,
                "cooldown_until": 0.0,
                "last_status": None,
            }
        # Sticky assignments: key -> {proxy, remaining}
        self._sticky: Dict[tuple, Dict[str, Any]] = {}

    def _is_available(self, proxy: str, now_ts: float) -> bool:
        st = self._state[proxy]
        if now_ts < st["cooldown_until"]:
            return False
        # Try non-blocking acquire to test capacity
        acquired = st["sema"].acquire(blocking=False)
        if not acquired:
            return False
        # Put it back; actual acquire happens in acquire()
        st["sema"].release()
        return True

    def _score(self, proxy: str) -> float:
        st = self._state[proxy]
        succ = st["successes"]
        fail = st["failures"]
        total = succ + fail
        success_rate = (succ / total) if total > 0 else 0.5
        latency = st["avg_latency_ms"] if st["avg_latency_ms"] is not None else 500.0
        # Higher is better: prioritize higher success rate and lower latency
        return success_rate * 1.0 - (latency / 5000.0)

    def acquire(self, netloc: str, sticky_key: Optional[tuple] = None) -> tuple:
        """Return (proxy_url or None, token) where token must be passed to release()."""
        if not self.proxies:
            return None, None
        now_ts = time.time()
        with self._lock:
            # Evict expired sticky entries to prevent unbounded dict growth
            _sticky_ttl = self.cooldown_sec * 2
            expired_keys = [
                k for k, v in self._sticky.items()
                if now_ts - v.get("created_at", 0.0) > _sticky_ttl
            ]
            for k in expired_keys:
                del self._sticky[k]

            # Try sticky
            if sticky_key and sticky_key in self._sticky:
                entry = self._sticky[sticky_key]
                proxy = entry.get("proxy")
                remaining = int(entry.get("remaining", 0))
                if (
                    proxy in self._state
                    and remaining > 0
                    and self._is_available(proxy, now_ts)
                ):
                    # consume one sticky use and actually acquire (non-blocking)
                    if self._state[proxy]["sema"].acquire(blocking=False):
                        entry["remaining"] = remaining - 1
                        return proxy, {
                            "proxy": proxy,
                            "sticky_key": sticky_key,
                            "start": time.monotonic(),
                        }
                else:
                    # drop sticky
                    self._sticky.pop(sticky_key, None)

            # Choose best available proxy by score
            candidates = [p for p in self.proxies if self._is_available(p, now_ts)]
            if not candidates:
                return None, None
            # Sort by score descending
            candidates.sort(key=self._score, reverse=True)
            # Try top 3 candidates (or fewer)
            for proxy in candidates[:3]:
                # Take capacity now (non-blocking)
                if self._state[proxy]["sema"].acquire(blocking=False):
                    if sticky_key:
                        self._sticky[sticky_key] = {
                            "proxy": proxy,
                            "remaining": self.sticky_requests - 1,
                            "created_at": now_ts,
                        }
                    return proxy, {
                        "proxy": proxy,
                        "sticky_key": sticky_key,
                        "start": time.monotonic(),
                    }
            return None, None

    def release(
        self,
        token: Optional[Dict[str, Any]],
        success: bool,
        status_code: Optional[int] = None,
        error: Optional[Exception] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        if not token or "proxy" not in token:
            return
        proxy = token["proxy"]
        if proxy not in self._state:
            return
        with self._lock:
            # prevent double release
            if token.get("released"):
                return
            token["released"] = True
            st = self._state[proxy]
            # Update latency
            if latency_ms is None and token.get("start"):
                latency_ms = max(0.0, (time.monotonic() - token["start"]) * 1000.0)
            if latency_ms is not None:
                if st["avg_latency_ms"] is None:
                    st["avg_latency_ms"] = float(latency_ms)
                else:
                    st["avg_latency_ms"] = 0.8 * st["avg_latency_ms"] + 0.2 * float(
                        latency_ms
                    )
            # Update outcomes
            if success:
                st["successes"] += 1
                st["consec_fails"] = 0
                st["last_status"] = status_code
            else:
                st["failures"] += 1
                st["consec_fails"] += 1
                st["last_status"] = status_code
                # Cooldown on repeated failures or specific statuses
                if st["consec_fails"] >= self.max_consec_fails or (
                    status_code in {403, 429}
                ):
                    st["cooldown_until"] = time.time() + self.cooldown_sec
            # Release capacity
            try:
                st["sema"].release()
            except Exception:
                pass


# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from college_ai.rag.embeddings import (
    get_embedding,
    get_embeddings_batch,
    chunk_text_by_tokens,
    chunk_text_by_sentences,
    EmbeddingBatcher,
    _ensure_tokenizer,
)
from college_ai.rag.text_cleaner import clean_text
from college_ai.scraping.config import *

import sqlite3


# ==================== Page Type Classification ====================


def classify_page_type(url: str) -> str:
    """Classify a URL into a page type using regex patterns from config.

    Returns one of: transfer, international, admissions, academics,
    financial_aid, outcomes, safety_health, diversity, about,
    campus_life, research, or 'other'.
    """
    for page_type, patterns in PAGE_TYPE_PATTERNS.items():
        if any(re.search(p, url, re.IGNORECASE) for p in patterns):
            return page_type
    return "other"


# ==================== Playwright Pool ====================
class PlaywrightPool:
    """Thread-local pool of reusable Playwright browsers with rotation.

    Playwright's sync API is greenlet-based and tied to the thread that created
    the browser, so we can't share browsers across threads.  Instead this pool
    manages one browser *per thread*, keeps it alive for reuse across requests,
    and rotates it after `rotate_after` uses to prevent fingerprint accumulation.

    Benefits vs the old thread-local pattern:
    - Centrally caps total concurrent browsers (bounded semaphore)
    - Automatic rotation after N uses (fresh fingerprint)
    - Clean lifecycle management (shutdown closes all thread-local browsers)
    """

    def __init__(
        self,
        pool_size: int = 5,
        rotate_after: int = 50,
        headless: bool = True,
        use_camoufox: bool = False,
    ):
        self.pool_size = pool_size
        self.rotate_after = rotate_after
        self.headless = headless
        self.use_camoufox = use_camoufox and Camoufox is not None
        self._semaphore = threading.Semaphore(pool_size)
        self._local = threading.local()  # thread-local {pw, browser, camoufox_cm, uses}
        self._all_locals_lock = threading.Lock()
        self._all_locals: list = []  # track all thread-local slots for shutdown
        self._started = False

    def start(self):
        """Mark the pool as ready. Browsers are created lazily per-thread."""
        self._started = True
        print(f"    🎭 Playwright pool ready (max {self.pool_size} concurrent browsers, rotate every {self.rotate_after} uses)")

    def _create_browser(self) -> dict:
        """Create a browser on the current thread."""
        try:
            if self.use_camoufox:
                camoufox_cm = Camoufox(headless=self.headless)
                browser = camoufox_cm.__enter__()
                slot = {"pw": None, "browser": browser, "camoufox_cm": camoufox_cm, "uses": 0, "_healthy": True}
            else:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(
                    headless=self.headless,
                    args=list(_CHROMIUM_FLAGS_SAFE),
                )
                slot = {"pw": pw, "browser": browser, "camoufox_cm": None, "uses": 0, "_healthy": True}
            with self._all_locals_lock:
                self._all_locals.append(slot)
            return slot
        except Exception as e:
            print(f"    ⚠️  Failed to create Playwright browser: {e}")
            return None

    def _close_slot(self, slot: dict):
        slot["_healthy"] = False
        try:
            if slot.get("browser"):
                slot["browser"].close()
        except Exception:
            pass
        try:
            if slot.get("camoufox_cm"):
                slot["camoufox_cm"].__exit__(None, None, None)
        except Exception:
            pass
        try:
            if slot.get("pw"):
                slot["pw"].stop()
        except Exception:
            pass

    def acquire(self, timeout: float = 30.0):
        """Acquire a browser for the current thread. Returns (browser, token) or (None, -1).
        The browser is lazily created on first call from each thread.
        Blocks on the semaphore to cap total concurrent browsers.
        """
        if not self._started or sync_playwright is None:
            return None, -1

        if not self._semaphore.acquire(timeout=timeout):
            return None, -1

        slot = getattr(self._local, "slot", None)

        # Rotate if exceeded usage threshold
        if slot and slot["uses"] >= self.rotate_after:
            with self._all_locals_lock:
                if not self._started:
                    self._semaphore.release()
                    return None, -1
                try:
                    self._all_locals.remove(slot)
                except ValueError:
                    pass
            # Close outside lock — _close_slot does blocking browser I/O
            # that could hang on zombie Chromium, and holding the lock
            # would block all acquire()/shutdown()/prune calls.
            self._close_slot(slot)
            slot = None

        # Create lazily on this thread
        if slot is None:
            with self._all_locals_lock:
                if not self._started:
                    self._semaphore.release()
                    return None, -1
            slot = self._create_browser()
            if slot is None:
                self._semaphore.release()
                return None, -1
            self._local.slot = slot

        # Reuse branch: verify pool wasn't shut down between semaphore
        # acquire and here.  shutdown() closes all slots under
        # _all_locals_lock; check _started before using a stale slot.
        with self._all_locals_lock:
            if not self._started:
                self._semaphore.release()
                return None, -1

        # Guard against a narrow race where shutdown() closed this thread's
        # slot between semaphore acquire and the _started re-check above.
        if not slot.get("_healthy", True):
            self._local.slot = None  # clear stale ref so next acquire() creates a fresh browser
            self._semaphore.release()
            return None, -1
        slot["uses"] += 1
        return slot["browser"], 1  # token=1 means "pool-managed"

    def release(self, token: int):
        """Release the semaphore slot. Browser stays alive for reuse on this thread."""
        if token >= 0:
            # Health-check on the owning thread (safe — no cross-thread call).
            # If disconnected, mark unhealthy so prune_dead_slots can clean up.
            slot = getattr(self._local, "slot", None)
            if slot:
                try:
                    if slot.get("browser") and not slot["browser"].is_connected():
                        slot["_healthy"] = False
                except Exception:
                    slot["_healthy"] = False
            self._semaphore.release()

    def shutdown(self):
        """Close all tracked browsers across all threads."""
        with self._all_locals_lock:
            self._started = False  # Prevent new browser creation while we clean up
            slots_to_close = list(self._all_locals)
            self._all_locals.clear()
        # Close each slot in a daemon thread with timeout — browser.close()
        # can hang indefinitely on a zombie Chromium process.
        for slot in slots_to_close:
            t = threading.Thread(target=self._close_slot, args=(slot,), daemon=True)
            t.start()
            t.join(timeout=10)
            if t.is_alive():
                print("    ⚠️  Playwright browser close hung — abandoning "
                      "(daemon thread will be reaped at process exit)")
        print("    🎭 Playwright pool shut down")

    def prune_dead_slots(self) -> int:
        """Close and remove browser slots marked unhealthy by their owning thread.

        Returns the number of slots pruned. Does NOT release the semaphore for
        pruned slots (intentional — avoids over-release).

        Uses the ``_healthy`` flag (set by the owning thread in ``release()``)
        instead of calling ``browser.is_connected()`` cross-thread, which would
        violate Playwright's thread-safety contract and risk corrupting the
        owning thread's internal greenlet/asyncio state.
        """
        to_close = []
        with self._all_locals_lock:
            alive = []
            for slot in self._all_locals:
                if slot.get("_healthy", True):
                    alive.append(slot)
                else:
                    to_close.append(slot)
            self._all_locals[:] = alive
        # Close dead slots outside the lock in daemon threads with timeout —
        # browser.close() / pw.stop() can hang indefinitely on zombie Chromium
        # processes, and the caller (BFS orchestration thread) must not stall.
        for slot in to_close:
            t = threading.Thread(target=self._close_slot, args=(slot,), daemon=True)
            t.start()
            t.join(timeout=10)
            if t.is_alive():
                print("    ⚠️  Dead slot close hung — abandoning "
                      "(daemon thread will be reaped at process exit)")
        pruned = len(to_close)
        if pruned:
            print(f"    🧹 PlaywrightPool: pruned {pruned} dead browser slot(s)")
        return pruned


# ==================== Delta Crawl Cache ====================
class DeltaCrawlCache:
    """SQLite-backed cache for incremental/delta crawling.

    Stores per-URL metadata (ETag, Last-Modified, content hash) so that
    subsequent runs can skip unchanged pages via HTTP conditional headers
    or content comparison.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._all_conns_lock = threading.Lock()
        self._all_conns: list = []  # track all thread-local connections for shutdown
        self._closed = threading.Event()  # thread-safe (no bare-bool GIL dependency)
        # Create table on first use
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_cache (
                canonical_url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT,
                crawled_at TEXT,
                links TEXT
            )
        """)
        # Migrate existing tables that lack the links column
        try:
            conn.execute("ALTER TABLE crawl_cache ADD COLUMN links TEXT")
        except Exception:
            pass  # column already exists
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection."""
        if self._closed.is_set():  # fast path — Event.is_set() is thread-safe
            raise RuntimeError("DeltaCrawlCache is closed")
        if not hasattr(self._local, "conn") or self._local.conn is None:
            # Hold lock across creation + registration so close() cannot
            # clear _all_conns between our connect() and append() (TOCTOU).
            with self._all_conns_lock:
                if self._closed.is_set():  # re-check inside lock
                    raise RuntimeError("DeltaCrawlCache is closed")
                self._local.conn = sqlite3.connect(self._db_path, timeout=10)
                self._local.conn.execute("PRAGMA journal_mode=WAL")
                self._all_conns.append(self._local.conn)
        return self._local.conn

    def get(self, canonical_url: str) -> dict:
        """Get cached metadata for a URL. Returns empty dict if not cached."""
        if self._closed.is_set():
            return {}
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT etag, last_modified, content_hash, crawled_at, links "
                "FROM crawl_cache WHERE canonical_url = ?",
                (canonical_url,),
            ).fetchone()
        except (RuntimeError, sqlite3.ProgrammingError):
            # Connection was closed by another thread during shutdown
            return {}
        if row:
            links = []
            if row[4]:
                try:
                    links = json.loads(row[4])
                except Exception:
                    pass
            return {
                "etag": row[0],
                "last_modified": row[1],
                "content_hash": row[2],
                "crawled_at": row[3],
                "links": links,
            }
        return {}

    def put(self, canonical_url: str, etag: str = None, last_modified: str = None,
            content_hash: str = None, links: list = None):
        """Store or update cache entry for a URL."""
        if self._closed.is_set():
            return
        try:
            conn = self._get_conn()
        except (RuntimeError, sqlite3.ProgrammingError):
            return  # connection closed during shutdown
        links_json = json.dumps(links) if links else None
        try:
            conn.execute(
                "INSERT OR REPLACE INTO crawl_cache "
                "(canonical_url, etag, last_modified, content_hash, crawled_at, links) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (canonical_url, etag, last_modified, content_hash,
                 datetime.now().isoformat(), links_json),
            )
            conn.commit()
        except sqlite3.ProgrammingError:
            return  # connection closed during shutdown
        except Exception:
            try:
                conn.rollback()
            except sqlite3.ProgrammingError:
                pass
            raise

    def close(self):
        """Close all tracked SQLite connections across all threads."""
        with self._all_conns_lock:
            self._closed.set()  # set inside lock so _get_conn() re-check is reliable
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()


class MultithreadedCollegeCrawler:
    """Multithreaded crawler that crawls college websites and uploads directly to Milvus."""

    def __init__(self, delay: float = None, max_workers: int = None,
                 no_resume: bool = False, rechunk: bool = False):
        """
        Initialize the crawler.

        Args:
            delay: Delay between requests to be respectful (uses config if None)
            max_workers: Number of worker threads per college (uses config if None)
            no_resume: Force full re-crawl, ignoring delta cache and replacing existing vectors
            rechunk: Re-crawl pages with old 512-token chunks, replacing with sentence-aware chunks
        """
        self.delay = delay or CRAWLER_DELAY
        self.max_workers = max_workers or CRAWLER_MAX_WORKERS
        self.no_resume = no_resume
        self.rechunk = rechunk
        self.colleges_dir = os.path.join(os.path.dirname(__file__), "colleges")

        # Ensure colleges directory exists
        os.makedirs(self.colleges_dir, exist_ok=True)

        # Browserforge header generator for realistic, rotating fingerprints
        self._header_gen = None
        self._fingerprint_gen = None
        if HeaderGenerator is not None:
            try:
                self._header_gen = HeaderGenerator(
                    browser=("chrome", "firefox", "edge"),
                    os=("macos", "windows"),
                )
            except Exception:
                self._header_gen = None
        if FingerprintGenerator is not None:
            try:
                self._fingerprint_gen = FingerprintGenerator(
                    browser=("chrome", "firefox"),
                    os=("macos", "windows"),
                )
            except Exception:
                self._fingerprint_gen = None

        # curl_cffi impersonation targets for TLS fingerprint rotation
        self._curl_impersonate_targets = [
            "chrome124", "chrome131", "chrome136", "safari18_0", "edge101", "firefox135",
        ]

        # Initialize HTTP session — prefer curl_cffi for realistic TLS fingerprints
        if USE_CURL_CFFI and curl_requests is not None:
            self.session = curl_requests.Session(
                impersonate=random.choice(self._curl_impersonate_targets),
            )
        else:
            self.session = requests.Session()

        # Generate initial headers via browserforge or fall back to static
        initial_headers = self._generate_headers()
        self.session.headers.update(initial_headers)

        # Frozen snapshot of initial headers for seeding per-thread worker sessions.
        # Reading self.session.headers at worker creation time is a data race when
        # INTER_COLLEGE_PARALLELISM > 1 because scrape_page() may mutate
        # request_session.headers on 403 retries.  Snapshot here is single-threaded.
        self._base_headers_snapshot = dict(self.session.headers)

        # Anti-bot detection settings
        self.min_delay = max(0.5, self.delay * 0.5)  # Minimum delay
        self.max_delay = self.delay * 2.0  # Maximum delay for randomization
        self.max_retries = MAX_RETRIES

        self.lock = threading.Lock()
        self._close_lock = threading.Lock()
        # Concurrency controls for Milvus operations
        # - Queries: allow bounded parallelism
        # - Writes: exclusive
        query_parallelism = int(os.getenv("MILVUS_QUERY_PARALLELISM", "3") or "2")
        self.collection_query_sema = threading.Semaphore(max(1, query_parallelism))
        self.collection_write_lock = threading.Lock()
        # Limit concurrent embedding generation to reduce rate-limit errors
        try:
            embed_concurrency = int(os.getenv("EMBED_MAX_CONCURRENCY", "3"))
        except Exception:
            embed_concurrency = 2
        self.embed_semaphore = threading.Semaphore(max(1, embed_concurrency))

        # Per-host rate limiting and adaptive concurrency state (thread-safe)
        self._host_lock = threading.Lock()
        self._host_tokens: Dict[str, Dict[str, Any]] = {}
        self._host_concurrency: Dict[str, int] = {}
        self._host_failures: Dict[str, int] = {}
        self._host_circuit_until: Dict[str, float] = {}
        self.max_concurrency_per_host = int(
            os.getenv("MAX_CONCURRENCY_PER_HOST", "6") or "6"
        )
        self.min_concurrency_per_host = 1
        self.token_refill_per_sec = float(
            os.getenv("HOST_TOKEN_REFILL_PER_SEC", "2.0") or "2.0"
        )
        self.max_tokens_per_host = int(os.getenv("HOST_MAX_TOKENS", "6") or "6")

        # Playwright fallback controls
        self.playwright_enabled = USE_PLAYWRIGHT_FALLBACK
        self.playwright_max_workers = max(1, PLAYWRIGHT_MAX_CONCURRENCY)
        self.playwright_semaphore = threading.Semaphore(self.playwright_max_workers)
        self.playwright_nav_timeout_ms = PLAYWRIGHT_NAV_TIMEOUT_MS
        self.playwright_aggressive_fallback = PLAYWRIGHT_AGGRESSIVE_FALLBACK
        self.playwright_cookie_persistence = PLAYWRIGHT_COOKIE_PERSISTENCE

        # Cookie and storage management
        self.cookie_storage_dir = os.path.join(
            os.path.dirname(__file__), "playwright_cookies"
        )
        os.makedirs(self.cookie_storage_dir, exist_ok=True)
        self._cookie_storage_lock = threading.Lock()
        # Profiles directory and cache
        self.playwright_profiles_dir = os.path.join(
            os.path.dirname(__file__), "playwright_profiles"
        )
        self._pw_profile_cache_lock = threading.Lock()
        self._pw_profile_cache: OrderedDict = OrderedDict()
        self._PW_PROFILE_CACHE_MAX = 50
        # Playwright runtime and browser cache (thread-local)
        self._pw_local = threading.local()  # Thread-local Playwright instances
        # Registry of all thread-local objects that hold Playwright resources,
        # so close() can clean up browsers from ALL threads, not just the caller's.
        self._pw_local_registry = []  # type: list
        self._pw_local_registry_lock = threading.Lock()

        # Proxy pool initialization (optional)
        try:
            max_per_proxy = int(
                os.getenv("PROXY_MAX_CONCURRENCY_PER_PROXY", "2") or "2"
            )
            cooldown_sec = int(os.getenv("PROXY_COOLDOWN_SEC", "120") or "120")
            max_consec_fails = int(os.getenv("PROXY_MAX_CONSEC_FAILS", "3") or "3")
            sticky_reqs = int(os.getenv("PROXY_STICKY_REQUESTS", "3") or "3")
        except Exception:
            max_per_proxy, cooldown_sec, max_consec_fails, sticky_reqs = 2, 120, 3, 3
        self.proxy_pool = (
            ProxyPool(
                HTTP_PROXIES,
                max_concurrency_per_proxy=max_per_proxy,
                cooldown_sec=cooldown_sec,
                max_consec_fails=max_consec_fails,
                sticky_requests=sticky_reqs,
            )
            if HTTP_PROXIES
            else None
        )

        # Milvus connection
        self.connect_milvus()
        self.collection = self.get_or_create_collection()
        self.ensure_collection_ready()

        # Schema feature detection (url_canonical is required going forward)
        try:
            self.has_url_canonical = any(
                f.name == "url_canonical" for f in self.collection.schema.fields
            )
        except Exception:
            self.has_url_canonical = False
        if not self.has_url_canonical:
            raise RuntimeError("Collection missing required field: url_canonical")

        # Crawling statistics
        self.stats = {
            "total_pages_crawled": 0,
            "total_vectors_uploaded": 0,
            "total_errors": 0,
            "colleges_processed": 0,
            "duplicate_urls_skipped": 0,
            "existing_urls_skipped": 0,
            "rows_dropped_alignment": 0,
            "rows_dropped_insert_fail": 0,
        }

        # === Performance: Batched insert buffer ===
        # Instead of acquiring collection_write_lock per page, accumulate rows
        # and flush in batches (reduces lock contention by ~50x).
        self._insert_buffer: queue.Queue = queue.Queue(maxsize=200)
        self._insert_buffer_size = MILVUS_INSERT_BUFFER_SIZE
        self._insert_flush_interval = MILVUS_INSERT_FLUSH_INTERVAL
        self._insert_flush_stop = threading.Event()
        # Prevents TOCTOU duplicates: URLs claimed here between Milvus query
        # and buffered insert, released after the flush thread commits them.
        self._pending_canonical_urls: set = set()
        self._pending_canonical_lock = threading.Lock()
        self._insert_flush_thread = threading.Thread(
            target=self._insert_flush_loop, daemon=True, name="MilvusFlushThread"
        )
        self._insert_flush_thread.start()
        self._flush_thread_crashed = threading.Event()

        # === Performance: Cross-thread embedding batcher ===
        # Consolidates embedding requests from multiple worker threads into
        # fewer, larger API calls (up to 100 texts per call).
        self.embedding_batcher = EmbeddingBatcher(
            model="text-embedding-3-small", max_batch=100, max_wait_ms=200,
        )

        # Content dedup: per-college hash caches are created in
        # crawl_college_site() and passed to upload_to_milvus() — no
        # instance-level cache (unsafe with INTER_COLLEGE_PARALLELISM > 1).

        # === Performance: Persistent Playwright browser pool ===
        # Pre-launches browser instances to avoid ~2-3s startup per request.
        # Pool size matches PLAYWRIGHT_MAX_CONCURRENCY (not PLAYWRIGHT_POOL_SIZE)
        # to avoid idle browser processes wasting ~300-500 MB each.
        self.pw_pool = PlaywrightPool(
            pool_size=min(PLAYWRIGHT_POOL_SIZE, self.playwright_max_workers),
            rotate_after=PLAYWRIGHT_POOL_ROTATE_AFTER,
            headless=True,
            use_camoufox=USE_CAMOUFOX,
        )
        if self.playwright_enabled and sync_playwright is not None:
            self.pw_pool.start()

        # === Performance: Delta crawl cache ===
        # SQLite cache for ETag/Last-Modified/content hash to skip unchanged pages.
        self._delta_cache: Optional[DeltaCrawlCache] = None
        if self.no_resume:
            print("    No-resume mode: delta cache disabled, existing vectors will be replaced")
        elif self.rechunk:
            print("    Rechunk mode: delta cache disabled, old 512-token chunks will be replaced")
        elif ENABLE_DELTA_CRAWLING:
            cache_path = os.path.join(DATA_DIR, "crawl_cache.db")
            self._delta_cache = DeltaCrawlCache(cache_path)
            print(f"    Delta crawling enabled (cache: {cache_path})")

    # === Insert buffer flush infrastructure ===

    def _insert_flush_loop(self):
        """Background thread: drain the insert buffer periodically or when full."""
        _consec_errors = 0
        while not self._insert_flush_stop.is_set():
            try:
                self._flush_insert_buffer(block_timeout=self._insert_flush_interval)
                _consec_errors = 0
            except Exception as e:
                _consec_errors += 1
                print(f"    ✗ Insert flush error ({_consec_errors}): {e}")
                if _consec_errors >= 10:
                    print("    ⚠️  Flush loop aborting after 10 consecutive errors")
                    self._flush_thread_crashed.set()
                    global_shutdown_event.set()  # stop all workers — buffer is dead
                    break
                time.sleep(min(2 ** (_consec_errors - 1), 30))
        # Final drain after stop signal — bounded loop caps iterations
        # in case late callbacks keep adding rows.
        _drain_errors = 0
        for _ in range(200):
            if self._insert_buffer.qsize() == 0:
                break
            try:
                self._flush_insert_buffer(block_timeout=0.1)
                _drain_errors = 0
            except Exception as e:
                _drain_errors += 1
                print(f"    ✗ Final drain error ({_drain_errors}): {e}")
                if _drain_errors >= 3:
                    print(f"    ⚠️  Abandoning final drain after {_drain_errors} consecutive errors")
                    break
        # Release canonical URL claims for any rows still stuck in the buffer
        # so those URLs are not permanently blocked from future crawl attempts.
        # Bounded to buffer maxsize to avoid spinning if late callbacks keep adding.
        _abandoned = 0
        for _ in range(500):
            try:
                row = self._insert_buffer.get_nowait()
                with self._pending_canonical_lock:
                    for uc in row.get("url_canonical", []):
                        self._pending_canonical_urls.discard(uc)
                _abandoned += 1
            except queue.Empty:
                break
        if _abandoned:
            with self.lock:
                self.stats["rows_dropped_insert_fail"] += _abandoned
            print(f"    ⚠️  Released claims for {_abandoned} abandoned buffer row(s)")

    def _flush_insert_buffer(self, block_timeout: float = 2.0):
        """Drain all pending rows from the buffer and do one batched insert."""
        rows = []
        # Block on first item to avoid busy-waiting
        try:
            rows.append(self._insert_buffer.get(timeout=block_timeout))
        except queue.Empty:
            return
        # Drain remaining without blocking
        while len(rows) < self._insert_buffer_size * 4:  # cap single flush at 4x buffer
            try:
                rows.append(self._insert_buffer.get_nowait())
            except queue.Empty:
                break

        if not rows:
            return

        # Extract canonical URL claims from raw rows BEFORE merging so they
        # can always be released in a finally block, even if merging fails.
        inserted_canonicals = []
        for row in rows:
            inserted_canonicals.extend(row.get("url_canonical", []))

        try:
            self._flush_insert_buffer_inner(rows, inserted_canonicals)
        except Exception:
            # Release claims so URLs are not permanently blocked
            if inserted_canonicals:
                with self._pending_canonical_lock:
                    for uc in inserted_canonicals:
                        self._pending_canonical_urls.discard(uc)
            raise

    def _flush_insert_buffer_inner(self, rows, inserted_canonicals):
        """Inner flush logic — caller guarantees claim release on failure."""
        # Merge all rows into column-based format for a single insert.
        # In-place merge: accumulate into the first row's lists and null out
        # consumed rows immediately to avoid doubling peak memory.
        # Exclude BM25 function output fields (auto-generated by Milvus at insert time)
        bm25_output_fields = set()
        try:
            for func in self.collection.schema.functions:
                if func.type == FunctionType.BM25:
                    bm25_output_fields.update(func.output_field_names)
        except (AttributeError, TypeError):
            # Fallback for pymilvus versions without schema.functions
            bm25_output_fields = {"content_sparse"}
        field_names = [
            f.name for f in self.collection.schema.fields
            if f.name not in bm25_output_fields
        ]
        # Use first row as accumulator; extend from remaining rows in-place
        merged = rows[0]
        for i in range(1, len(rows)):
            row = rows[i]
            for name in field_names:
                target = merged.get(name)
                source = row.get(name, [])
                if target is not None:
                    target.extend(source)
                elif source:
                    merged[name] = source
            rows[i] = None  # Release reference immediately

        if not merged.get("id"):
            # No data to insert — release claims
            if inserted_canonicals:
                with self._pending_canonical_lock:
                    for uc in inserted_canonicals:
                        self._pending_canonical_urls.discard(uc)
            return

        # Validate column alignment to prevent corrupted inserts
        col_lengths = {name: len(merged.get(name, [])) for name in field_names}
        unique_lengths = set(col_lengths.values())
        if len(unique_lengths) > 1:
            expected = len(merged.get("id", []))
            bad_cols = {k: v for k, v in col_lengths.items() if v != expected}
            print(f"    ✗ Column alignment mismatch (expected {expected} rows): {bad_cols}")
            # In-place merge has already consumed the original rows, so
            # per-row recovery is not possible.  Drop the batch and release
            # canonical URL claims so these URLs can be re-crawled.
            with self.lock:
                self.stats["rows_dropped_alignment"] += expected
            if inserted_canonicals:
                with self._pending_canonical_lock:
                    for uc in inserted_canonicals:
                        self._pending_canonical_urls.discard(uc)
            print(f"    ⚠️  Dropped {expected} rows from misaligned batch")
            return

        count = len(merged["id"])

        # Split into sub-batches to stay under Zilliz's 4MB gRPC message
        # limit.  Each row carries a 1536-dim float32 embedding (~6KB) plus
        # content text, so 50 rows is a safe ceiling (~2MB per batch).
        MAX_INSERT_BATCH = 50
        total_inserted = 0
        total_dropped = 0

        for batch_start in range(0, count, MAX_INSERT_BATCH):
            batch_end = min(batch_start + MAX_INSERT_BATCH, count)
            batch_data = [
                merged.get(name, [])[batch_start:batch_end]
                for name in field_names
            ]
            batch_size = batch_end - batch_start

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    with self.collection_write_lock:
                        self.collection.insert(batch_data)
                    total_inserted += batch_size
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        backoff = 2 ** attempt  # 1s, 2s
                        print(f"    ✗ Batched insert failed (attempt {attempt + 1}/{max_retries}, "
                              f"retrying in {backoff}s): {e}")
                        time.sleep(backoff)
                    else:
                        print(f"    ✗ Batched insert permanently failed ({batch_size} rows dropped): {e}")
                        total_dropped += batch_size

        if total_dropped > 0 and total_inserted > 0:
            print(f"    ⚠️  Partial batch: {total_inserted} rows inserted, "
                  f"{total_dropped} rows dropped from same flush")
        if total_inserted > 0:
            with self.lock:
                self.stats["total_vectors_uploaded"] += total_inserted
        if total_dropped > 0:
            with self.lock:
                self.stats["rows_dropped_insert_fail"] += total_dropped

        # Release pending URL claims — committed or permanently failed
        if inserted_canonicals:
            with self._pending_canonical_lock:
                for uc in inserted_canonicals:
                    self._pending_canonical_urls.discard(uc)
        # Diagnostic: warn if pending set is growing unexpectedly
        # len() is GIL-atomic; no lock needed for approximate check
        _pending_size = len(self._pending_canonical_urls)
        if _pending_size > 500:
            print(f"    diag: {_pending_size} pending canonical URLs")

    def _flush_all_inserts(self):
        """Flush any remaining buffered rows (call at end of crawl)."""
        _drain_errors = 0
        for _ in range(200):  # bounded loop avoids empty() TOCTOU
            if self._insert_buffer.qsize() == 0:
                break
            try:
                self._flush_insert_buffer(block_timeout=0.1)
                _drain_errors = 0
            except Exception as e:
                _drain_errors += 1
                print(f"    ✗ Final flush error ({_drain_errors}): {e}")
                if _drain_errors >= 3:
                    print(f"    ⚠️  Abandoning flush after {_drain_errors} consecutive errors")
                    break

    def _content_hash(self, text: str) -> str:
        """Return a hash for content deduplication. Uses normalized text."""
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _write_pw_delta_cache(self, pw_result, original_url):
        # type: (dict, str) -> None
        """Write delta cache entry for a Playwright-rendered page.

        Playwright does not provide HTTP headers (ETag/Last-Modified), so the
        cache entry uses content hash only as the change detector.
        """
        if not self._delta_cache or not pw_result:
            return
        try:
            canon = self._url_canonical_key(
                pw_result.get("url") or original_url
            )
            c_hash = (
                self._content_hash(pw_result["content"])
                if pw_result.get("content")
                else None
            )
            self._delta_cache.put(
                canon,
                content_hash=c_hash,
                links=pw_result.get("internal_links", []),
            )
        except Exception:
            pass

    def _generate_headers(self) -> dict:
        """Generate a complete, realistic header set via browserforge or static fallback."""
        if self._header_gen is not None:
            try:
                headers = self._header_gen.generate()
                # Ensure essential navigation headers are present
                headers.setdefault("DNT", "1")
                headers.setdefault("Upgrade-Insecure-Requests", "1")
                headers.setdefault("Sec-Fetch-Dest", "document")
                headers.setdefault("Sec-Fetch-Mode", "navigate")
                headers.setdefault("Sec-Fetch-Site", "none")
                headers.setdefault("Cache-Control", "max-age=0")
                headers.setdefault("Referer", "https://www.google.com/")
                return headers
            except Exception:
                pass
        # Static fallback
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.google.com/",
        }

    def rotate_user_agent(self):
        """Rotate headers (and User-Agent) using browserforge or static fallback."""
        headers = self._generate_headers()
        return headers.get("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    # Use a dedicated alias so the crawler's disconnect() does not kill
    # the retriever's connection when both share the same process.
    _MILVUS_ALIAS = "crawler"

    def connect_milvus(self):
        """Connect to Zilliz Cloud database."""
        try:
            connections.connect(
                alias=self._MILVUS_ALIAS, uri=ZILLIZ_URI, token=ZILLIZ_API_KEY
            )
            print("✓ Connected to Zilliz Cloud")
        except Exception as e:
            print(f"✗ Failed to connect to Zilliz Cloud: {e}")
            raise

    def get_or_create_collection(self):
        """Get or create the Zilliz Cloud collection with hybrid search schema.

        Uses ORM API throughout (supports BM25 functions in pymilvus ≥2.5).
        """
        collection_name = ZILLIZ_COLLECTION_NAME

        _alias = self._MILVUS_ALIAS
        if utility.has_collection(collection_name, using=_alias):
            existing = Collection(collection_name, using=_alias)
            actual_fields = {f.name for f in existing.schema.fields}
            if "content_sparse" in actual_fields and "page_type" in actual_fields:
                print(f"✅ Collection '{collection_name}' exists with hybrid schema.")
                return existing
            print(
                f"♻️ Collection '{collection_name}' has old schema (missing hybrid fields). "
                "Recreating (this drops existing data)."
            )
            utility.drop_collection(collection_name, using=_alias)

        print(f"🔧 Creating collection '{collection_name}' with hybrid search schema...")

        schema = CollectionSchema(fields=[
            FieldSchema("id", DataType.VARCHAR, is_primary=True, max_length=100),
            FieldSchema("college_name", DataType.VARCHAR, max_length=256),
            FieldSchema("url", DataType.VARCHAR, max_length=2048),
            FieldSchema("url_canonical", DataType.VARCHAR, max_length=512),
            FieldSchema("title", DataType.VARCHAR, max_length=MAX_TITLE_LENGTH),
            FieldSchema(
                "content", DataType.VARCHAR, max_length=MAX_CONTENT_LENGTH,
                enable_analyzer=True, enable_match=True,
                analyzer_params={"type": "english"},
            ),
            FieldSchema("content_sparse", DataType.SPARSE_FLOAT_VECTOR),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            FieldSchema("page_type", DataType.VARCHAR, max_length=64),
            FieldSchema("crawled_at", DataType.VARCHAR, max_length=32),
        ])

        # BM25 function: auto-generates content_sparse from content at insert time
        schema.add_function(Function(
            name="bm25",
            input_field_names=["content"],
            output_field_names=["content_sparse"],
            function_type=FunctionType.BM25,
        ))

        col = Collection(collection_name, schema=schema, using=_alias)

        # Indexes
        col.create_index("embedding", {"index_type": "AUTOINDEX", "metric_type": "COSINE"})
        col.create_index("content_sparse", {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
        col.create_index("college_name", {"index_type": "INVERTED"}, index_name="college_name_idx")
        col.create_index("url_canonical", {"index_type": "INVERTED"}, index_name="url_canonical_idx")
        col.create_index("page_type", {"index_type": "INVERTED"}, index_name="page_type_idx")

        print(f"✅ Created collection '{collection_name}' with hybrid schema.")
        return col

    def ensure_collection_ready(self):
        """Load collection to make it queryable. Indexes are created at schema time."""
        try:
            self.collection.load(timeout=120)
            print("✅ Collection loaded")
        except Exception as e:
            print(f"⚠️  Could not load collection yet: {e}")

    def read_csv_files(self) -> List[Dict[str, str]]:
        """
        Read all CSV files in the colleges directory.

        Returns:
            List of college dicts with 'name' and 'url' keys, deduplicated by URL.
        """
        all_colleges = []
        seen_urls = set()

        # Find all CSV files in colleges directory
        csv_pattern = os.path.join(self.colleges_dir, "*.csv")
        csv_files = glob.glob(csv_pattern)

        if not csv_files:
            print(f"No CSV files found in {self.colleges_dir}")
            self.create_sample_csv_files()
            csv_files = glob.glob(csv_pattern)

        for csv_file in csv_files:
            print(f"Reading colleges from {csv_file}")

            try:
                with open(csv_file, "r", encoding="utf-8", newline="") as f:
                    sample = f.read(1024)
                    f.seek(0)

                    if not sample.strip():
                        print(f"Warning: {csv_file} is empty")
                        continue

                    reader = csv.DictReader(f)

                    fieldnames = reader.fieldnames
                    if not fieldnames:
                        print(f"Warning: {csv_file} has no headers")
                        continue

                    name_col = None
                    url_col = None

                    for field in fieldnames:
                        field_lower = field.lower().strip()
                        if field_lower in [
                            "name",
                            "college_name",
                            "university_name",
                            "school_name",
                        ]:
                            name_col = field
                        elif field_lower in [
                            "url",
                            "website",
                            "link",
                            "college_url",
                            "university_url",
                        ]:
                            url_col = field

                    if not name_col or not url_col:
                        print(
                            f"Warning: {csv_file} missing required columns (name/url)"
                        )
                        print(f"Available columns: {fieldnames}")
                        continue

                    for row in reader:
                        name = row.get(name_col, "").strip()
                        url = row.get(url_col, "").strip()

                        if name and url:
                            if not url.startswith(("http://", "https://")):
                                url = "https://" + url

                            if url not in seen_urls:
                                seen_urls.add(url)
                                all_colleges.append({"name": name, "url": url})

                print(f"✓ Loaded colleges from {csv_file}")

            except Exception as e:
                print(f"Error reading {csv_file}: {e}")

        print(f"Total unique colleges loaded: {len(all_colleges)}")
        return all_colleges

    def create_sample_csv_files(self):
        """Create a sample CSV file for demonstration purposes."""
        print("Creating sample CSV file for demonstration...")

        sample_colleges = [
            {"name": "MIT", "url": "https://www.mit.edu/"},
            {"name": "Stanford University", "url": "https://www.stanford.edu/"},
            {"name": "Harvard University", "url": "https://www.harvard.edu/"},
        ]

        csv_path = os.path.join(self.colleges_dir, "general.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "url"])
            writer.writeheader()
            writer.writerows(sample_colleges)
        print(f"Created sample file: {csv_path}")

    def is_internal_link(self, url: str, base_url: str) -> bool:
        """Check if a URL is an internal link to the same domain."""
        try:
            parsed_url = urlparse(url)
            parsed_base = urlparse(base_url)

            # Handle relative URLs (no netloc means it's relative)
            if not parsed_url.netloc:
                return True

            # Normalize domains by removing www. prefix for comparison
            url_domain = parsed_url.netloc.lower()
            if url_domain.startswith("www."):
                url_domain = url_domain[4:]
            base_domain = parsed_base.netloc.lower()
            if base_domain.startswith("www."):
                base_domain = base_domain[4:]

            # Domain validation - either exact match or proper subdomain
            is_same_domain = False

            # Extract actual TLD and domain parts
            url_parts = url_domain.split(".")
            base_parts = base_domain.split(".")

            # Need at least 2 parts for a valid domain (domain.tld)
            if len(url_parts) < 2 or len(base_parts) < 2:
                return False

            # Exact match - simplest case
            if url_domain == base_domain:
                is_same_domain = True
            # Subdomain check
            elif len(url_parts) > len(base_parts):
                # Check for proper subdomain pattern
                # This ensures domain endings match exactly (e.g. stanford.edu)
                # and prevents matching similar-ending domains (e.g. notstanford.edu)
                suffix_match = url_domain.endswith("." + base_domain)

                # Additional validation to prevent false matches
                # For example, if base is "stanford.edu", prevent "fakestanford.edu"
                if suffix_match:
                    # The URL must be a proper subdomain of the base
                    # (e.g. "cs.stanford.edu" -> subdomain of "stanford.edu")
                    # Calculate matching parts based on domain components
                    base_domain_components = len(base_parts)
                    url_trailing_parts = url_parts[-base_domain_components:]
                    base_domain_parts = base_parts

                    # Must match all trailing parts exactly
                    is_same_domain = url_trailing_parts == base_domain_parts

            # If not same domain, reject immediately
            if not is_same_domain:
                return False

            # Skip certain file types
            if any(url.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                return False

            # Skip certain paths
            if any(skip_path in url.lower() for skip_path in SKIP_PATHS):
                return False

            # Skip fragments and javascript links
            if parsed_url.fragment and not parsed_url.path:
                return False

            if url.lower().startswith(("javascript:", "mailto:", "tel:", "ftp:")):
                return False

            return True

        except Exception as e:
            print(f"    Warning: Error parsing URL {url}: {e}")
            return False

    # Educational TLDs recognized as valid university domains
    _EDU_TLDS = (
        ".edu", ".ac.uk", ".ac.nz", ".ac.jp", ".ac.za", ".ac.kr", ".ac.in",
        ".edu.au", ".edu.uk", ".edu.sg", ".edu.cn", ".edu.tw", ".edu.my",
        ".edu.hk", ".edu.jp", ".edu.br", ".edu.mx", ".edu.co", ".edu.ar",
    )

    def is_valid_university_url(self, url: str) -> bool:
        """Check if a URL belongs to a recognized university domain.

        Returns True if the URL's domain ends with an educational TLD
        (e.g. .edu, .ac.uk). Non-educational domains are rejected to
        prevent crawling social media, commercial sites, etc.
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if not domain:
                return False
            # Strip www. for cleaner matching
            if domain.startswith("www."):
                domain = domain[4:]
            # Check against known educational TLDs
            for tld in self._EDU_TLDS:
                if domain.endswith(tld):
                    return True
            return False
        except Exception:
            return False

    def test_domain_validation(self):
        """Test function to validate domain restriction logic.

        This is for development/testing only - can be safely removed in production.
        """
        test_cases = [
            # Format: (url, base_url, expected_result, description)
            # Same domain - should allow
            ("https://stanford.edu/about", "https://stanford.edu", True, "Same domain"),
            (
                "https://www.stanford.edu/contact",
                "https://stanford.edu",
                True,
                "www prefix, same domain",
            ),
            (
                "https://stanford.edu/about",
                "https://www.stanford.edu",
                True,
                "Base with www, same domain",
            ),
            # Subdomains - should allow
            (
                "https://cs.stanford.edu/courses",
                "https://stanford.edu",
                True,
                "Valid subdomain",
            ),
            (
                "https://info.cs.stanford.edu/staff",
                "https://stanford.edu",
                True,
                "Nested subdomain",
            ),
            # Different domains - should block
            (
                "https://stanforduniversity.com/about",
                "https://stanford.edu",
                False,
                "Different TLD",
            ),
            (
                "https://fake-stanford.edu/about",
                "https://stanford.edu",
                False,
                "Domain with hyphen",
            ),
            (
                "https://stanfordedu.com/contact",
                "https://stanford.edu",
                False,
                "Similar name, different TLD",
            ),
            (
                "https://notstandford.edu/about",
                "https://stanford.edu",
                False,
                "Different domain name",
            ),
            (
                "https://stanford-clone.edu/about",
                "https://stanford.edu",
                False,
                "Domain with suffix",
            ),
            (
                "https://stanfordedu.co.uk/about",
                "https://stanford.edu",
                False,
                "Different TLD",
            ),
            (
                "https://evil.com/stanford.edu/phishing",
                "https://stanford.edu",
                False,
                "Path contains domain",
            ),
        ]

        print("\n=== DOMAIN VALIDATION TEST CASES ===")

        for url, base_url, expected, desc in test_cases:
            result = self.is_internal_link(url, base_url)
            status = "✅" if result == expected else "❌"
            print(
                f"{status} {desc}: {url} vs {base_url} => Got {result}, Expected {expected}"
            )

            if result != expected:
                parsed_url = urlparse(url)
                parsed_base = urlparse(base_url)
                url_domain = parsed_url.netloc.lower()
                if url_domain.startswith("www."):
                    url_domain = url_domain[4:]
                base_domain = parsed_base.netloc.lower()
                if base_domain.startswith("www."):
                    base_domain = base_domain[4:]
                print(f"    Debug: {url_domain} vs {base_domain}")

        print("=== END DOMAIN VALIDATION TEST ===\n")

    def normalize_url(self, url: str, base_url: Optional[str] = None) -> str:
        """Normalize URL by resolving relative, stripping trackers, sorting whitelisted queries, and collapsing slashes."""
        try:
            if base_url:
                url = urljoin(base_url, url)
            parsed = urlparse(url)
            scheme = parsed.scheme.lower() or "https"
            # Reject non-HTTP schemes (mailto:, tel:, javascript:, etc.)
            if scheme not in ("http", "https"):
                return url
            netloc = parsed.netloc.lower()
            # Do NOT strip 'www.' here — many sites have SSL certs only
            # for the www subdomain and stripping causes cert-mismatch
            # errors (curl 60).  Deduplication across www / non-www is
            # handled by _url_canonical_key().
            # Remove default ports
            if netloc.endswith(":80") and scheme == "http":
                netloc = netloc[:-3]
            elif netloc.endswith(":443") and scheme == "https":
                netloc = netloc[:-4]
            # Collapse duplicate slashes in path
            path = re.sub(r"/{2,}", "/", parsed.path or "/")
            # Remove trailing slash unless root
            if path.endswith("/") and len(path) > 1:
                path = path.rstrip("/")
            # Strip trackers / session ids
            query_pairs = []
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                kl = k.lower()
                if kl.startswith("utm_") or kl in {
                    "fbclid",
                    "gclid",
                    "msclkid",
                    "sessionid",
                    "phpsessid",
                }:
                    continue
                query_pairs.append((k, v))
            # Sort whitelisted query params for stability
            query_pairs.sort(key=lambda kv: kv[0])
            query = urlencode(query_pairs, doseq=True)
            # Drop fragments
            normalized = f"{scheme}://{netloc}{path}"
            if query:
                normalized += f"?{query}"
            return normalized
        except Exception:
            return url

    def _url_canonical_key(self, url: str) -> str:
        """Return a canonical URL key for deduplication that ignores scheme and a leading 'www.' on host.

        - Lowercases host
        - Strips a leading 'www.' only (keeps www2., cs., etc.)
        - Keeps path (with duplicate slashes collapsed by normalize_url if used before)
        - Keeps query as-is (already sorted/cleaned by normalize_url for crawled URLs)
        - Drops fragment
        """
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            path = parsed.path or ""
            if path.endswith("/") and len(path) > 1:
                path = path.rstrip("/")
            key = f"{netloc}{path}"
            if parsed.query:
                key += f"?{parsed.query}"
            return key
        except Exception:
            # Fallback: strip scheme prefix and leading www manually
            s = url.strip()
            if s.startswith("http://"):
                s = s[len("http://") :]
            elif s.startswith("https://"):
                s = s[len("https://") :]
            if s.lower().startswith("www."):
                s = s[4:]
            return s

    def _load_college_canonicals(
        self, college_name: str, rechunk: bool = False
    ):
        # type: (...) -> Tuple[Set[str], Set[str], Dict[str, str]]
        """Load canonical URL keys for a specific college to prevent re-crawling duplicates.

        Args:
            college_name: Name of the college to load canonicals for
            rechunk: If True, also identify URLs with old 512-token chunks

        Returns:
            Tuple of (canonical_urls, rechunk_urls, rechunk_full_urls).
            rechunk_urls is empty when rechunk=False. URLs in rechunk_urls are
            excluded from canonical_urls. rechunk_full_urls maps canonical key
            to a full URL (with scheme) for seeding into the BFS queue.

        Raises:
            Exception: If all retry attempts fail (caller should skip this college
                       rather than crawling with an empty dedup set).

        Uses 'url_canonical' when available; otherwise derives from 'url'.
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            canonical_urls = set()  # type: Set[str]
            rechunk_urls = set()  # type: Set[str]
            rechunk_full_urls = {}  # type: Dict[str, str]
            try:
                # Get records only for this specific college using query_iterator
                # (avoids the offset+limit <= 16,384 Milvus restriction)
                safe_college = college_name.replace('"', '\\"')
                expr = f'college_name == "{safe_college}"'
                # In rechunk mode, also fetch 'url' to seed the BFS queue
                output_fields = ["url_canonical", "content", "url"] if rechunk else ["url_canonical"]

                # Track per-URL chunk token counts to detect old chunker pattern:
                # old chunker produces exactly 512 tokens for every chunk except the
                # last (remainder). Single-chunk pages can't be distinguished and are
                # skipped since rechunking them has no benefit.
                url_chunk_tokens = {}  # type: Dict[str, List[int]]
                # Map canonical key → full URL (first occurrence wins)
                canon_to_full = {}  # type: Dict[str, str]
                if rechunk:
                    enc = _ensure_tokenizer("text-embedding-3-small")

                # In rechunk mode we fetch content + url per record (~3KB each),
                # so use a smaller batch to stay under the 4MB gRPC response limit.
                _batch_size = 256 if rechunk else 2048
                with self.collection_query_sema:
                    iterator = self.collection.query_iterator(
                        expr=expr,
                        output_fields=output_fields,
                        batch_size=_batch_size,
                    )
                    while True:
                        batch = iterator.next()
                        if not batch:
                            iterator.close()
                            break
                        for rec in batch:
                            key = (rec.get("url_canonical") or "").strip()
                            if not key:
                                continue
                            canonical_urls.add(key)
                            if rechunk:
                                content = rec.get("content") or ""
                                token_count = len(enc.encode(content))
                                url_chunk_tokens.setdefault(key, []).append(token_count)
                                full_url = (rec.get("url") or "").strip()
                                if full_url and key not in canon_to_full:
                                    canon_to_full[key] = full_url

                if rechunk:
                    # Old chunker pattern: multi-chunk pages where every chunk
                    # except the last is exactly 512 tokens.
                    for url_key, counts in url_chunk_tokens.items():
                        if len(counts) >= 2 and all(c == 512 for c in counts[:-1]):
                            rechunk_urls.add(url_key)
                    canonical_urls -= rechunk_urls
                    # Build full-URL map for rechunk URLs (for BFS seeding)
                    for rk in rechunk_urls:
                        if rk in canon_to_full:
                            rechunk_full_urls[rk] = canon_to_full[rk]
                    print(
                        f"    ✓ Loaded {len(canonical_urls):,} canonical URLs for {college_name}"
                        f" ({len(rechunk_urls):,} flagged for rechunking)"
                    )
                else:
                    print(
                        f"    ✓ Loaded {len(canonical_urls):,} canonical URLs for {college_name}"
                    )
                return canonical_urls, rechunk_urls, rechunk_full_urls
            except Exception as e:
                if attempt < max_attempts - 1:
                    backoff = 2 ** attempt
                    print(f"    ⚠️  Failed to load canonical URLs for {college_name} "
                          f"(attempt {attempt + 1}/{max_attempts}, retrying in {backoff}s): {e}")
                    time.sleep(backoff)
                else:
                    print(f"    ✗ Failed to load canonical URLs for {college_name} "
                          f"after {max_attempts} attempts: {e}")
                    raise
        return set(), set(), {}  # unreachable, satisfies type checker

    def extract_internal_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all internal links from a BeautifulSoup object."""
        links = set()  # Use set for automatic deduplication

        # Find all anchor tags with href attributes
        for link in soup.find_all("a", href=True):
            href = link.get("href")
            if not href or not href.strip():
                continue

            href = href.strip()

            # Skip empty fragments and anchor-only links
            if href == "#" or href.startswith("#"):
                continue

            # Skip non-HTTP schemes early (before normalize_url mangles them)
            if href.lower().startswith(("mailto:", "tel:", "javascript:", "ftp:", "data:", "blob:")):
                continue

            try:
                # Convert relative URLs to absolute
                absolute_url = self.normalize_url(href, base_url)

                # Normalize URL by removing fragments and trailing slashes
                normalized_url = absolute_url

                # Check if it's an internal link on a valid university domain
                if self.is_internal_link(normalized_url, base_url) and self.is_valid_university_url(normalized_url):
                    links.add(normalized_url)

            except Exception as e:
                print(f"    Warning: Error processing link '{href}': {e}")
                continue

        # Also consider <link rel="next"> pagination hints
        try:
            next_link = soup.find("link", rel=lambda v: v and "next" in str(v).lower())
            if next_link and next_link.get("href"):
                try:
                    absolute_next = urljoin(base_url, next_link.get("href"))
                    parsed = urlparse(absolute_next)
                    normalized_next = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if parsed.query:
                        normalized_next += f"?{parsed.query}"
                    if self.is_internal_link(normalized_next, base_url) and self.is_valid_university_url(normalized_next):
                        links.add(normalized_next)
                except Exception:
                    pass
        except Exception:
            pass

        return list(links)

    def is_js_heavy(self, html_text: str, soup: BeautifulSoup, url: str) -> bool:
        """Heuristically detect if a page is likely JS-rendered/SPAs.

        Signals:
        - Framework markers (__NEXT_DATA__, __NUXT__, data-reactroot, ng-app, ember)
        - Boot-time globals (window.__INITIAL_STATE__, window.__APOLLO_STATE__)
        - High script density or many external scripts
        - URL patterns typical of SPAs
        """
        try:
            text_lower = html_text.lower() if html_text else ""
        except Exception:
            text_lower = ""
        try:
            script_tags = soup.find_all("script") if soup else []
            total_tags = len(soup.find_all(True)) if soup else 0
        except Exception:
            script_tags, total_tags = [], 0
        external_js = 0
        for s in script_tags:
            try:
                if s.get("src"):
                    external_js += 1
            except Exception:
                continue

        markers = [
            "__next_data__",
            'id="__nuxt"',
            "data-reactroot",
            "ng-app",
            "ember",
            "window.__initial_state__",
            "window.__apollo_state__",
        ]
        if any(m in text_lower for m in markers):
            return True

        script_ratio = (len(script_tags) / max(1, total_tags)) if total_tags else 0.0
        if len(script_tags) >= 30 or script_ratio >= 0.25 or external_js >= 10:
            return True

        try:
            path = urlparse(url).path.lower()
        except Exception:
            path = ""
        if "#/" in url or "/app/" in path or "/wp-json/" in path:
            return True
        return False

    _HOST_STATE_MAX_ENTRIES = 200

    def _prune_host_state(self, max_age_seconds: float = 1800.0) -> None:
        """Evict stale entries from per-host rate-limit dicts.

        Entries are stale when their token bucket was last refilled more than
        max_age_seconds ago AND their circuit-breaker has already expired.
        After TTL eviction, a hard cap (``_HOST_STATE_MAX_ENTRIES``) evicts the
        oldest entries by last-seen time to prevent unbounded growth when many
        hosts are concurrently active.
        """
        now = time.time()
        evicted: List[str] = []
        with self._host_lock:
            # TTL-based eviction
            for netloc, bucket in list(self._host_tokens.items()):
                last_seen = bucket.get("last", 0.0)
                circuit_until = self._host_circuit_until.get(netloc, 0.0)
                if (now - last_seen) > max_age_seconds and now >= circuit_until:
                    evicted.append(netloc)
            for netloc in evicted:
                self._host_tokens.pop(netloc, None)
                self._host_failures.pop(netloc, None)
                self._host_concurrency.pop(netloc, None)
                self._host_circuit_until.pop(netloc, None)
            # Hard cap: evict oldest entries if still over limit
            remaining = len(self._host_tokens)
            if remaining > self._HOST_STATE_MAX_ENTRIES:
                sorted_hosts = sorted(
                    self._host_tokens.items(),
                    key=lambda kv: kv[1].get("last", 0.0),
                )
                excess = remaining - self._HOST_STATE_MAX_ENTRIES
                for netloc, _ in sorted_hosts[:excess]:
                    self._host_tokens.pop(netloc, None)
                    self._host_failures.pop(netloc, None)
                    self._host_concurrency.pop(netloc, None)
                    self._host_circuit_until.pop(netloc, None)
                    evicted.append(netloc)
        if evicted:
            print(f"    🧹 Pruned {len(evicted)} host-state entries")

    def scrape_page(
        self, url: str, session: requests.Session = None
    ) -> Optional[Dict[str, Any]]:
        """Scrape a single page and return structured data."""
        # Guard against non-HTTP schemes that could trigger OS handlers (e.g. mailto:)
        if not url.lower().startswith(("http://", "https://")):
            print(f"    ⚠️  Skipping non-HTTP URL: {url}")
            return None
        try:
            print(f"    Crawling: {url}")

            # Periodic host state pruning (every ~100 requests per thread).
            # Uses thread-local counter — no lock needed (threading.local).
            _prune_ctr = getattr(self._pw_local, "_host_prune_counter", 0) + 1
            if _prune_ctr >= 100:
                _prune_ctr = 0
                self._prune_host_state(max_age_seconds=600.0)
            self._pw_local._host_prune_counter = _prune_ctr

            # Human-like delay: log-normal distribution (mostly short, occasional longer pauses)
            delay = min(self.max_delay, random.lognormvariate(math.log(self.delay), 0.4))
            delay = max(self.min_delay, delay)
            time.sleep(delay)

            # Per-host token bucket and circuit breaker
            try:
                netloc = urlparse(url).netloc
            except Exception:
                netloc = ""
            # Circuit breaker check + token bucket rate limiting
            # Compute delay inside lock, sleep outside to avoid blocking all hosts
            _rate_delay = 0.0
            with self._host_lock:
                cb_until = self._host_circuit_until.get(netloc, 0.0)
                now = time.time()
                if now < cb_until:
                    wait_left = cb_until - now
                    print(
                        f"    ⏳ Host {netloc} in cooldown ({wait_left:.1f}s). Skipping {url}"
                    )
                    return None
                # Refill tokens
                bucket = self._host_tokens.get(netloc)
                if not bucket:
                    bucket = {
                        "tokens": float(self.max_tokens_per_host),
                        "last": now,
                    }
                    self._host_tokens[netloc] = bucket
                else:
                    elapsed = now - bucket["last"]
                    bucket["tokens"] = min(
                        float(self.max_tokens_per_host),
                        bucket["tokens"] + elapsed * self.token_refill_per_sec,
                    )
                    bucket["last"] = now
                if bucket["tokens"] < 1.0:
                    need = 1.0 - bucket["tokens"]
                    _rate_delay = min(1.0, need / max(0.1, self.token_refill_per_sec))
                # Consume token optimistically so concurrent threads see updated state
                bucket["tokens"] = max(0.0, bucket["tokens"] - 1.0)
            if _rate_delay > 0:
                time.sleep(_rate_delay)
            # Use provided session or fall back to shared session.
            # Callers in multithreaded contexts MUST provide an explicit session;
            # self.session is only safe as a single-threaded fallback.
            if session is None and threading.current_thread() is not threading.main_thread():
                raise RuntimeError(
                    "scrape_page() called with session=None from a non-main thread; "
                    "callers must provide an explicit thread-local session"
                )
            request_session = session or self.session

            # Delta crawling: check cache for conditional headers
            _delta_headers = {}
            _delta_canon = None
            if self._delta_cache:
                try:
                    _delta_canon = self._url_canonical_key(url)
                    cached = self._delta_cache.get(_delta_canon)
                    if cached.get("etag"):
                        _delta_headers["If-None-Match"] = cached["etag"]
                    if cached.get("last_modified"):
                        _delta_headers["If-Modified-Since"] = cached["last_modified"]
                except Exception:
                    pass

            # Fetch the page with retry logic for 403 errors
            response = None
            for attempt in range(self.max_retries):
                try:
                    # Light probabilistic proxy usage on first attempt, rotate on retries
                    proxy_dict = None
                    proxy_token = None
                    selected_proxy_url = None
                    sticky_key = (urlparse(url).netloc, threading.get_ident())
                    if self.proxy_pool:
                        # acquire best available proxy
                        selected_proxy_url, proxy_token = self.proxy_pool.acquire(
                            netloc=sticky_key[0], sticky_key=sticky_key
                        )
                        if selected_proxy_url:
                            proxy_dict = {
                                "http": selected_proxy_url,
                                "https": selected_proxy_url,
                            }

                    # Consume one host token for this attempt
                    with self._host_lock:
                        bucket2 = self._host_tokens.get(netloc)
                        if bucket2:
                            bucket2["tokens"] = max(0.0, bucket2["tokens"] - 1.0)

                    _is_curl_session = (
                        curl_requests is not None
                        and hasattr(request_session, "curl")
                    )
                    if _is_curl_session:
                        # curl_cffi Session: rotate TLS fingerprint on retries,
                        # reuse the underlying libcurl handle across requests.
                        impersonate_target = self._curl_impersonate_targets[
                            attempt % len(self._curl_impersonate_targets)
                        ]
                        req_headers = dict(request_session.headers)
                        if attempt > 0:
                            req_headers.update(self._generate_headers())
                        if _delta_headers:
                            req_headers.update(_delta_headers)
                        response = request_session.get(
                            url,
                            impersonate=impersonate_target,
                            headers=req_headers,
                            proxies=proxy_dict,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                        )
                    else:
                        # plain requests.Session
                        if _delta_headers:
                            request_session.headers.update(_delta_headers)
                        response = request_session.get(
                            url,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                            proxies=proxy_dict,
                        )
                        if _delta_headers:
                            for k in _delta_headers:
                                request_session.headers.pop(k, None)

                    # Delta crawling: 304 Not Modified — page unchanged, skip embedding
                    # but return cached links so BFS can still discover new pages
                    if response.status_code == 304 and self._delta_cache:
                        cached = self._delta_cache.get(_delta_canon) if _delta_canon else {}
                        cached_links = cached.get("links", [])
                        if cached_links:
                            print(f"    304 Not Modified (delta skip): {url} — restored {len(cached_links)} cached links")
                        else:
                            print(f"    304 Not Modified (delta skip): {url}")
                        return {
                            "url": url,
                            "title": "",
                            "content": "",
                            "internal_links": cached_links,
                            "word_count": 0,
                            "crawled_at": datetime.now().isoformat(),
                            "needs_pw": False,
                            "skip_embed": True,
                        }

                    # Handle 403 errors specifically
                    if response.status_code == 403:
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate full header set and wait longer for 403 errors
                            new_headers = self._generate_headers()
                            new_ua = new_headers.get("User-Agent", "")
                            try:
                                request_session.headers.update(new_headers)
                            except Exception:
                                pass
                            print(f"    🔄 Rotated headers (UA: {new_ua[:50]}...)")
                            backoff = self.delay * (attempt + 1) * 2
                            if proxy_dict:
                                backoff *= 1.5
                            time.sleep(backoff)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            return None

                    response.raise_for_status()
                    # inform proxy pool of success
                    if self.proxy_pool and proxy_token is not None:
                        self.proxy_pool.release(
                            proxy_token,
                            success=True,
                            status_code=response.status_code,
                            latency_ms=max(
                                0.0,
                                (
                                    time.monotonic()
                                    - proxy_token.get("start", time.monotonic())
                                )
                                * 1000.0,
                            ),
                        )
                    # Update host success and adjust concurrency upwards slowly
                    with self._host_lock:
                        self._host_failures[netloc] = 0
                        cur = self._host_concurrency.get(
                            netloc, self.min_concurrency_per_host
                        )
                        if (
                            cur < self.max_concurrency_per_host
                            and random.random() < 0.1
                        ):
                            self._host_concurrency[netloc] = cur + 1
                    break

                except requests.exceptions.HTTPError as e:
                    if "403" in str(e):
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate full header set and wait longer for 403 errors
                            new_headers = self._generate_headers()
                            new_ua = new_headers.get("User-Agent", "")
                            try:
                                request_session.headers.update(new_headers)
                            except Exception:
                                pass
                            print(f"    🔄 Rotated headers (UA: {new_ua[:50]}...)")
                            backoff = self.delay * (attempt + 1) * 2
                            if proxy_dict:
                                backoff *= 1.5
                            time.sleep(backoff)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            # mark proxy as failed if used
                            if self.proxy_pool and proxy_token is not None:
                                self.proxy_pool.release(
                                    proxy_token,
                                    success=False,
                                    status_code=403,
                                )
                            # Circuit breaker on 403
                            with self._host_lock:
                                fails = self._host_failures.get(netloc, 0) + 1
                                self._host_failures[netloc] = fails
                                if fails >= 3:
                                    self._host_circuit_until[netloc] = (
                                        time.time() + min(300, 30 * fails)
                                    )
                            return None
                    else:
                        # for other HTTP errors, mark failure then re-raise to outer except
                        if self.proxy_pool and proxy_token is not None:
                            try:
                                code = getattr(e.response, "status_code", None)
                            except Exception:
                                code = None
                            self.proxy_pool.release(
                                proxy_token,
                                success=False,
                                status_code=code,
                            )
                        # adaptive backoff + circuit breaker increments
                        with self._host_lock:
                            fails = self._host_failures.get(netloc, 0) + 1
                            self._host_failures[netloc] = fails
                            if code in {429, 408, 500, 502, 503, 504} and fails >= 3:
                                self._host_circuit_until[netloc] = time.time() + min(
                                    300, 20 * fails
                                )
                        raise e
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        # final failure
                        if self.proxy_pool and proxy_token is not None:
                            self.proxy_pool.release(
                                proxy_token,
                                success=False,
                                status_code=None,
                                error=e,
                            )
                        with self._host_lock:
                            fails = self._host_failures.get(netloc, 0) + 1
                            self._host_failures[netloc] = fails
                            if fails >= 3:
                                self._host_circuit_until[netloc] = time.time() + min(
                                    300, 20 * fails
                                )
                        raise e
                    # bounded backoff with jitter
                    time.sleep(self.delay * (attempt + 1) * random.uniform(0.8, 1.3))
                    continue
                finally:
                    # Ensure proxy semaphore is released on early continues (e.g. 403 retries).
                    # Use success=False so 403 retries don't inflate proxy success metrics.
                    # On the success path, release() was already called — the double-release
                    # guard (token["released"]) makes this a no-op.
                    if self.proxy_pool and proxy_token is not None:
                        try:
                            self.proxy_pool.release(proxy_token, success=False)
                        except Exception:
                            pass

            if not response:
                return None

            # Validate content-type before parsing
            try:
                content_type_header = response.headers.get("Content-Type", "")
            except Exception:
                content_type_header = ""
            mime_type = (
                content_type_header.split(";")[0].strip().lower()
                if content_type_header
                else ""
            )
            needs_pw = False
            try:
                if mime_type and (mime_type not in VALID_CONTENT_TYPES):
                    print(
                        f"    ⚠️  Skipping non-HTML content-type for {url}: {mime_type}"
                    )
                    # Consider JS-rendered fallback for non-HTML or mismatched types
                    if self.playwright_enabled and sync_playwright is not None:
                        needs_pw = True
                    # Try to proceed in case servers mislabel content-type
                    # Fallback to parsing anyway to avoid missing pages due to mislabeled headers
            except Exception:
                pass

            # Parse with BeautifulSoup, then free the response body immediately.
            # Holding both response.content (~1 MB) and soup (~4 MB) simultaneously
            # across 24 workers wastes ~30 MB.
            soup = BeautifulSoup(response.content, "html.parser")
            try:
                final_url = response.url or url
            except Exception:
                final_url = url
            try:
                _response_text = response.text if hasattr(response, "text") else ""
            except Exception:
                _response_text = ""
            try:
                _resp_etag = response.headers.get("ETag")
                _resp_last_modified = response.headers.get("Last-Modified")
            except Exception:
                _resp_etag = None
                _resp_last_modified = None
            del response  # free response body bytes

            # Extract title
            title = soup.find("title")
            title_text = title.get_text(strip=True) if title else ""

            # Extract main content
            # Remove script, style, nav, footer, header elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Try to find main content areas
            main_content = ""
            main_selectors = [
                "main",
                "article",
                '[role="main"]',
                ".main-content",
                ".content",
                "#content",
                ".post-content",
                ".entry-content",
            ]

            for selector in main_selectors:
                main_element = soup.select_one(selector)
                if main_element:
                    main_content = main_element.get_text(separator=" ", strip=True)
                    break

            # Fallback to body if no main content found
            if not main_content:
                body = soup.find("body")
                if body:
                    main_content = body.get_text(separator=" ", strip=True)

            # If still no content found, this is a strong indicator for Playwright fallback
            if (
                not main_content
                and self.playwright_enabled
                and sync_playwright is not None
            ):
                print(
                    f"    🔄 Triggering Playwright fallback (no content found in HTML)"
                )
                needs_pw = True

            # Clean the content
            cleaned_content = clean_text(main_content)

            # Check if we have meaningful content using config thresholds
            word_count = len(cleaned_content.split())
            try:
                min_chars = MIN_CONTENT_LENGTH
            except Exception:
                min_chars = 0
            try:
                min_words = MIN_WORDS_PER_PAGE
            except Exception:
                min_words = 0
            # JS-heavy heuristic
            js_heavy = self.is_js_heavy(_response_text, soup, url)
            del _response_text  # no longer needed

            # Enhanced content insufficiency detection
            content_insufficient = len(cleaned_content.strip()) < max(
                1, min_chars
            ) or word_count < max(1, min_words)

            if content_insufficient:
                print(
                    f"    ⚠️  Insufficient content for {url} (chars={len(cleaned_content)}, words={word_count})"
                )
                # Enhanced Playwright fallback triggers:
                # 1. JS-heavy pages (likely dynamic content)
                # 2. Pages with very low content (likely blocked or incomplete)
                # 3. Pages with no title (likely loading issues)
                # 4. Pages with suspiciously low content regardless of JS detection
                should_use_pw = False

                if self.playwright_enabled and sync_playwright is not None:
                    if js_heavy:
                        should_use_pw = True
                        print(f"    🔄 Triggering Playwright fallback (JS-heavy page)")
                    elif word_count < 10 or len(cleaned_content.strip()) < 50:
                        should_use_pw = True
                        print(
                            f"    🔄 Triggering Playwright fallback (very low content)"
                        )
                    elif not title_text.strip():
                        should_use_pw = True
                        print(f"    🔄 Triggering Playwright fallback (no title)")
                    elif word_count < max(
                        1, min_words // 2
                    ):  # Less than half the minimum words
                        should_use_pw = True
                        print(
                            f"    🔄 Triggering Playwright fallback (significantly insufficient content)"
                        )
                    elif self.playwright_aggressive_fallback and content_insufficient:
                        # Aggressive mode: use Playwright for any insufficient content
                        should_use_pw = True
                        print(
                            f"    🔄 Triggering Playwright fallback (aggressive mode - insufficient content)"
                        )

                if should_use_pw:
                    needs_pw = True

                # Do not return early; still extract internal links so BFS can progress

            # Extract internal links
            internal_links = self.extract_internal_links(soup, final_url)
            # Normalize discovered links
            internal_links = [self.normalize_url(u) for u in internal_links]

            # Enhanced heuristic: if no links found, consider Playwright fallback
            if (
                not internal_links
                and self.playwright_enabled
                and sync_playwright is not None
            ):
                # Use PW on zero-links if:
                # 1. JS detected (likely SPA)
                # 2. Content is insufficient (likely incomplete page)
                # 3. URL suggests dynamic content
                should_use_pw_for_links = False

                if js_heavy:
                    should_use_pw_for_links = True
                    print(
                        f"    🔄 Triggering Playwright fallback (no links, JS-heavy page)"
                    )
                elif content_insufficient:
                    should_use_pw_for_links = True
                    print(
                        f"    🔄 Triggering Playwright fallback (no links, insufficient content)"
                    )
                elif any(
                    pattern in url.lower()
                    for pattern in ["/app/", "/dashboard/", "/portal/", "/admin/"]
                ):
                    should_use_pw_for_links = True
                    print(
                        f"    🔄 Triggering Playwright fallback (no links, dynamic URL pattern)"
                    )

                if should_use_pw_for_links:
                    needs_pw = True

            # Debug info for stuck crawler
            if len(internal_links) > 0:
                print(f"    🔗 Found {len(internal_links)} internal links for {url}")

            # Delta crawling: compare content hash to detect unchanged pages.
            # The cache WRITE is deferred to the caller (worker_task) so it
            # happens AFTER the insert buffer accepts the row.  This prevents
            # a crash-consistency gap where a stale hash blocks re-insertion.
            skip_embed = False
            _delta_meta = None  # will be set if delta cache should be updated
            if self._delta_cache and not needs_pw:
                try:
                    canon = _delta_canon or self._url_canonical_key(url)
                    etag = _resp_etag
                    last_mod = _resp_last_modified
                    c_hash = self._content_hash(cleaned_content) if cleaned_content else None

                    # Check if content actually changed vs cached hash
                    if _delta_canon:
                        cached = self._delta_cache.get(_delta_canon)
                        if cached.get("content_hash") and cached["content_hash"] == c_hash:
                            print(f"    Delta: content unchanged (hash match), skipping embed: {url}")
                            skip_embed = True

                    # Package metadata for deferred write by caller
                    _delta_meta = {
                        "canon": canon,
                        "etag": etag,
                        "last_modified": last_mod,
                        "content_hash": c_hash,
                        "links": internal_links,
                    }
                except Exception:
                    pass

            return {
                # Store the final URL (post-redirect) for better link resolution and traceability
                "url": final_url,
                "title": title_text,
                "content": cleaned_content,
                "internal_links": internal_links,
                "word_count": word_count,
                "crawled_at": datetime.now().isoformat(),
                "needs_pw": needs_pw,
                "skip_embed": skip_embed,  # delta crawl: content unchanged, skip embedding
                "_delta_meta": _delta_meta,  # deferred delta cache write metadata
            }

        except Exception as e:
            print(f"    ✗ Error scraping {url}: {e}")
            return None

    def _scrape_with_playwright(self, url: str) -> Optional[Dict[str, Any]]:
        """Render page with Playwright and extract title/content/links. Best-effort and bounded.

        This is a sync helper guarded by a semaphore to cap concurrency.
        """
        if not self.playwright_enabled or sync_playwright is None:
            return None

        # Retry logic for Playwright failures
        max_retries = 2
        for attempt in range(max_retries + 1):
            # Abort retries if shutdown is in progress
            if global_shutdown_event.is_set():
                return None
            try:
                return self._scrape_with_playwright_single_attempt(url, attempt)
            except Exception as e:
                if attempt < max_retries:
                    print(
                        f"    🔄 Playwright attempt {attempt + 1} failed for {url}, retrying: {str(e)[:100]}"
                    )
                    time.sleep(1 + attempt)  # Progressive backoff
                    continue
                else:
                    print(
                        f"    ⚠️  Playwright fallback failed for {url} after {max_retries + 1} attempts: {e}"
                    )
                    return None

        return None

    def _scrape_with_playwright_single_attempt(
        self, url: str, attempt: int = 0
    ) -> Optional[Dict[str, Any]]:
        """Single attempt at Playwright scraping with enhanced error handling."""
        # Guard against non-HTTP schemes that could trigger OS handlers (e.g. mailto:)
        if not url.lower().startswith(("http://", "https://")):
            print(f"    ⚠️  Skipping non-HTTP URL in Playwright: {url}")
            return None
        try:
            # Ensure local variables exist even on early exceptions
            html_dom: str = ""
            html_idle: str = ""
            final_url: str = url  # Initialize to prevent reference errors
            redirect_detected: bool = False
            # Try to use the pool first (lazy per-thread browser creation, fast
            # context reuse after first call)
            _pool_token = -1
            _pool_browser = None
            _using_pool = False

            if self.pw_pool._started:
                _pool_browser, _pool_token = self.pw_pool.acquire(timeout=10.0)
                if _pool_browser is not None:
                    _using_pool = True
                else:
                    # Pool is full — this thread may already have a running
                    # Playwright asyncio loop (from a previous pool browser
                    # created via Camoufox).  Creating another sync_playwright()
                    # or Camoufox instance on this thread would hit
                    # "Playwright Sync API inside asyncio loop".  Bail out so
                    # the caller can retry later.
                    return None

            # Timed acquire prevents threads from blocking forever on the
            # semaphore, which would cause pw_executor.shutdown() to hang.
            _PW_SEM_TIMEOUT = 30.0
            if not self.playwright_semaphore.acquire(timeout=_PW_SEM_TIMEOUT):
                # Release pool slot before bailing out
                if _using_pool and _pool_token >= 0:
                    try:
                        self.pw_pool.release(_pool_token)
                    except Exception:
                        pass
                return None  # let caller retry later

            try:  # matches the semaphore acquire — released in finally below
                # Start or reuse a thread-local Playwright runtime (only when pool is NOT active)
                if not _using_pool:
                    if not hasattr(self._pw_local, "pw") or self._pw_local.pw is None:
                        self._pw_local.pw = sync_playwright().start()
                        # Init browsers dict alongside PW so the registry
                        # entry captures a direct reference to the real dict.
                        if not hasattr(self._pw_local, "browsers"):
                            self._pw_local.browsers = {}
                            self._pw_local.browser_uses = {}
                        # Store direct object references (not the threading.local
                        # wrapper) so close() can reach them from any thread.
                        with self._pw_local_registry_lock:
                            self._pw_local_registry.append({
                                "pw": self._pw_local.pw,
                                "browsers": self._pw_local.browsers,
                            })
                p = self._pw_local.pw if not _using_pool else None
                # Acquire proxy for Playwright (optional)
                pw_proxy_token = None
                pw_proxy_settings = None
                selected_proxy_url = None  # Initialize to avoid reference errors
                try:
                    if self.proxy_pool:
                        netloc = urlparse(url).netloc
                        sticky_key = (netloc, "pw")
                        selected_proxy_url, pw_proxy_token = self.proxy_pool.acquire(
                            netloc=netloc, sticky_key=sticky_key
                        )
                        if selected_proxy_url:
                            parsed = urlparse(selected_proxy_url)
                            server = (
                                f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                            )
                            pw_proxy_settings = {"server": server}
                            if parsed.username or parsed.password:
                                if parsed.username:
                                    pw_proxy_settings["username"] = parsed.username
                                if parsed.password:
                                    pw_proxy_settings["password"] = parsed.password
                except Exception:
                    pw_proxy_settings = None
                    selected_proxy_url = None  # Reset on error

                pw_start = time.monotonic()

                # Diversified geolocation profiles (US university cities)
                _geo_profiles = [
                    {"lat": 42.3601, "lon": -71.0589, "tz": "America/New_York"},    # Boston
                    {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},    # NYC
                    {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles"},  # SF
                    {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},  # LA
                    {"lat": 41.8781, "lon": -87.6298, "tz": "America/Chicago"},      # Chicago
                    {"lat": 29.7604, "lon": -95.3698, "tz": "America/Chicago"},      # Houston
                    {"lat": 33.7490, "lon": -84.3880, "tz": "America/New_York"},     # Atlanta
                    {"lat": 47.6062, "lon": -122.3321, "tz": "America/Los_Angeles"},  # Seattle
                    {"lat": 39.9526, "lon": -75.1652, "tz": "America/New_York"},     # Philadelphia
                    {"lat": 38.9072, "lon": -77.0369, "tz": "America/New_York"},     # DC
                    {"lat": 35.2271, "lon": -80.8431, "tz": "America/New_York"},     # Charlotte
                    {"lat": 30.2672, "lon": -97.7431, "tz": "America/Chicago"},      # Austin
                    {"lat": 36.1627, "lon": -86.7816, "tz": "America/Chicago"},      # Nashville
                    {"lat": 39.7392, "lon": -104.9903, "tz": "America/Denver"},      # Denver
                    {"lat": 25.7617, "lon": -80.1918, "tz": "America/New_York"},     # Miami
                    {"lat": 44.9778, "lon": -93.2650, "tz": "America/Chicago"},      # Minneapolis
                    {"lat": 42.3314, "lon": -83.0458, "tz": "America/New_York"},     # Detroit
                    {"lat": 37.5407, "lon": -77.4360, "tz": "America/New_York"},     # Richmond
                ]
                geo = random.choice(_geo_profiles)
                locales = ["en-US", "en-GB", "en-CA"]
                viewport_opts = [(1280, 800), (1366, 768), (1440, 900), (1536, 864), (1920, 1080)]
                vw, vh = random.choice(viewport_opts)

                # Generate user-agent via browserforge or fallback
                ua = self.rotate_user_agent()

                # Load cookies for this domain if available
                storage_state = None
                if self.playwright_cookie_persistence:
                    try:
                        netloc = urlparse(url).netloc
                        storage_state = self._load_cookies(netloc)
                    except Exception:
                        storage_state = None

                # Context kwargs shared between camoufox and chromium paths
                context_kwargs = {
                    "user_agent": ua,
                    "java_script_enabled": True,
                    "locale": random.choice(locales),
                    "timezone_id": geo["tz"],
                    "viewport": {"width": vw, "height": vh},
                    "permissions": ["geolocation"],
                    "geolocation": {"latitude": geo["lat"], "longitude": geo["lon"]},
                    "service_workers": "block",  # Prevent SW from bypassing route()
                    "bypass_csp": True,  # Allow JS extraction on strict-CSP sites
                    "ignore_https_errors": True,  # Handle expired/self-signed certs
                    "extra_http_headers": {
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Cache-Control": "max-age=0",
                    },
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                    print(f"    🍪 Loaded cookies for {urlparse(url).netloc}")

                # --- Browser launch: use pool (fast) or fallback to thread-local ---
                camoufox_cm = None  # context manager reference for cleanup

                if _using_pool:
                    # Pool browser already acquired above — no startup cost
                    browser = _pool_browser
                else:
                    use_camoufox = USE_CAMOUFOX and Camoufox is not None

                    if use_camoufox:
                        try:
                            camoufox_cm = Camoufox(headless=True, proxy=pw_proxy_settings)
                            browser = camoufox_cm.__enter__()
                        except Exception as e:
                            print(f"    ⚠️  Camoufox launch failed ({e}), falling back to Chromium")
                            camoufox_cm = None
                            use_camoufox = False

                    if not use_camoufox:
                        # Use thread-local browser cache for Chromium
                        if not hasattr(self._pw_local, "browsers"):
                            self._pw_local.browsers = {}
                            self._pw_local.browser_uses = {}
                        browser_key = selected_proxy_url or "direct"
                        browser = self._pw_local.browsers.get(browser_key)
                        # Rotate if this browser exceeded usage threshold
                        if browser is not None:
                            uses = self._pw_local.browser_uses.get(browser_key, 0)
                            if uses >= PLAYWRIGHT_POOL_ROTATE_AFTER:
                                try:
                                    browser.close()
                                except Exception:
                                    pass
                                del self._pw_local.browsers[browser_key]
                                self._pw_local.browser_uses.pop(browser_key, None)
                                browser = None
                        if browser is None:
                            launch_options = {
                                "headless": True,
                                "args": list(_CHROMIUM_FLAGS_SAFE) + _CHROMIUM_FLAGS_FALLBACK_EXTRA,
                            }
                            if pw_proxy_settings:
                                launch_options["proxy"] = pw_proxy_settings
                            browser = p.chromium.launch(**launch_options)
                            self._pw_local.browsers[browser_key] = browser
                            self._pw_local.browser_uses[browser_key] = 0
                        # Increment use counter
                        self._pw_local.browser_uses[browser_key] = (
                            self._pw_local.browser_uses.get(browser_key, 0) + 1
                        )

                try:
                    context = browser.new_context(**context_kwargs)
                    # Kill popup windows to prevent resource leaks
                    context.on("page", lambda p: p.close())
                    page = context.new_page()
                    # Auto-dismiss dialog boxes (alert/confirm/prompt) to prevent hangs
                    page.on("dialog", lambda d: d.dismiss())

                    # Apply playwright-stealth patches (15+ detection vectors)
                    # Camoufox handles fingerprinting at C++ level but stealth still helps with CDP leaks
                    if _pw_stealth is not None:
                        try:
                            _pw_stealth.apply_stealth_sync(page)
                        except Exception:
                            pass

                    # Block heavy resources, analytics/tracking, and non-HTTP navigations
                    def route_filter(route):
                        req = route.request
                        req_url = req.url.lower()
                        if req_url.startswith(("mailto:", "tel:", "javascript:", "ftp:", "data:", "blob:")):
                            return route.abort()
                        if req.resource_type in PLAYWRIGHT_BLOCKED_RESOURCE_TYPES:
                            return route.abort()
                        if any(p in req_url for p in PLAYWRIGHT_BLOCKED_URL_PATTERNS):
                            return route.abort()
                        return route.continue_()

                    context.route("**/*", route_filter)
                    page.set_default_timeout(self.playwright_nav_timeout_ms)
                    # Snapshot at DOMContentLoaded and after short idle
                    html_dom = ""
                    html_idle = ""
                    final_url = url  # Track final URL after redirects
                    redirect_detected = False

                    try:
                        # Navigate with domcontentloaded (fast, reliable)
                        try:
                            response = page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=self.playwright_nav_timeout_ms,
                            )
                            final_url = page.url
                        except Exception as nav_e:
                            if "timeout" not in str(nav_e).lower():
                                raise
                            # Fallback: just wait for commit (first bytes)
                            response = page.goto(
                                url,
                                wait_until="commit",
                                timeout=self.playwright_nav_timeout_ms,
                            )
                            final_url = page.url

                        # Detect redirect
                        if final_url != url:
                            redirect_detected = True
                            print(
                                f"    🔄 Redirect detected: {url} -> {final_url}"
                            )

                        # Brief wait for JS to finish rendering (cheaper than networkidle)
                        try:
                            page.wait_for_load_state("load", timeout=5000)
                        except Exception:
                            pass

                        # If redirected, give a short extra wait for content
                        if redirect_detected and PLAYWRIGHT_REDIRECT_DETECTION:
                            time.sleep(2)

                        # Snapshot DOM after initial load
                        try:
                            html_dom = page.content()
                        except Exception:
                            html_dom = ""

                        # Human-like scroll to trigger lazy loading and look natural
                        try:
                            page.evaluate(
                                "window.scrollTo(0, document.body.scrollHeight * (0.2 + Math.random() * 0.4))"
                            )
                            time.sleep(random.uniform(0.3, 0.8))
                        except Exception:
                            pass

                        # Cookie handling - try accept then force-accept
                        cookies_accepted = False
                        for attempt in range(3):
                            if self._try_accept_cookies(page):
                                cookies_accepted = True
                                break
                            time.sleep(0.3)

                        if not cookies_accepted:
                            try:
                                page.evaluate(
                                    """
                                    // Force accept OneTrust
                                    if (window.OneTrust) {
                                        window.OneTrust.AllowAll && window.OneTrust.AllowAll();
                                    }
                                    // Force accept Cookiebot
                                    if (window.Cookiebot) {
                                        window.Cookiebot.show && window.Cookiebot.show();
                                        window.Cookiebot.consent && window.Cookiebot.consent.setAllConsent && window.Cookiebot.consent.setAllConsent(true);
                                    }
                                    // Remove any remaining overlays
                                    document.querySelectorAll('[style*="position: fixed"], [style*="position:fixed"]').forEach(el => {
                                        if (el.style.zIndex > 100) el.remove();
                                    });
                                """
                                )
                                cookies_accepted = True
                            except Exception:
                                pass

                        if cookies_accepted and self.playwright_cookie_persistence:
                            time.sleep(0.5)
                            try:
                                storage_state = context.storage_state()
                                self._save_cookies(netloc, storage_state)
                                print(f"    💾 Saved cookies for {netloc}")
                            except Exception as e:
                                print(
                                    f"    ⚠️  Failed to save cookies for {netloc}: {e}"
                                )

                        # Apply per-domain profile actions
                        try:
                            netloc2 = urlparse(url).netloc
                        except Exception:
                            netloc2 = ""
                        if netloc2:
                            profile = self._load_playwright_profile(netloc2)
                            if profile:
                                self._apply_playwright_profile(page, profile)

                        # Wait for content to be ready — single combined selector
                        try:
                            page.wait_for_selector(
                                "main, article, [role='main'], .content, #content, .page-content",
                                timeout=3000,
                            )
                        except Exception:
                            # Fallback: wait for any meaningful text
                            try:
                                page.wait_for_function(
                                    """
                                    () => {
                                        const body = document.body;
                                        if (!body) return false;
                                        const text = body.innerText || body.textContent || '';
                                        return text.trim().length > 100;
                                    }
                                """,
                                    timeout=3000,
                                )
                            except Exception:
                                time.sleep(1)

                        try:
                            html_idle = page.content()
                        except Exception:
                            html_idle = ""
                    except PlaywrightTimeoutError:
                        pass
                    finally:
                        # Ensure per-page resources are released
                        try:
                            page.close()
                        except Exception:
                            pass
                        try:
                            context.close()
                        except Exception:
                            pass

                    # Do not close shared Chromium browser (cached per thread)
                    # But camoufox uses a context manager — clean it up
                    if camoufox_cm is not None:
                        try:
                            camoufox_cm.__exit__(None, None, None)
                        except Exception:
                            pass
                        camoufox_cm = None  # prevent double __exit__ in finally

                finally:
                    # Release pool semaphore (browser stays alive on this thread)
                    if _using_pool and _pool_token >= 0:
                        try:
                            self.pw_pool.release(_pool_token)
                        except Exception:
                            pass

                    # Ensure camoufox is cleaned up even on exceptions
                    if camoufox_cm is not None:
                        try:
                            camoufox_cm.__exit__(None, None, None)
                        except Exception:
                            pass

                    # Update proxy pool with success
                    if self.proxy_pool and pw_proxy_token is not None:
                        try:
                            self.proxy_pool.release(
                                pw_proxy_token,
                                success=True,
                                status_code=None,
                                latency_ms=max(
                                    0.0, (time.monotonic() - pw_start) * 1000.0
                                ),
                            )
                        except Exception:
                            pass

            finally:
                self.playwright_semaphore.release()

            # --- Memory-efficient single-soup strategy ---
            # Pick the longer snapshot (more content), build only ONE soup.
            # Extract title from the other via lightweight regex.
            html_best = html_idle if len(html_idle or "") >= len(html_dom or "") else html_dom
            html_other = html_dom if html_best is html_idle else html_idle
            # Free the raw HTML we won't parse
            if html_best is html_idle:
                del html_dom
            else:
                del html_idle

            # Extract title from the other snapshot via regex (avoids full parse)
            _title_match = re.search(r"<title[^>]*>([^<]+)</title>", html_other or "", re.IGNORECASE)
            title_other = _title_match.group(1).strip() if _title_match else ""
            del html_other  # free immediately

            soup = BeautifulSoup(html_best or "", "html.parser")
            del html_best  # raw HTML no longer needed

            # Remove common consent overlays
            cookie_selectors = [
                '[id*="cookie"]', '[class*="cookie"]',
                '[id*="consent"]', '[class*="consent"]',
                '[id*="gdpr"]', '[class*="gdpr"]',
                "#onetrust-banner-sdk", "#onetrust-consent-sdk",
                "#truste-consent-track", ".truste_overlay",
                "#qc-cmp2-ui", "#sp-cc", ".sp_choice_type",
                "#CybotCookiebotDialog",
            ]
            for sel in cookie_selectors:
                for el in soup.select(sel):
                    el.decompose()

            # Extract links BEFORE decomposing nav/footer (links live in nav)
            internal_links = self.extract_internal_links(soup, final_url)

            # Extract title from soup (prefer idle-snapshot title, fall back to soup)
            title_tag = soup.find("title")
            title_text = title_other or (title_tag.get_text(strip=True) if title_tag else "")

            # Now decompose non-content elements for text extraction
            for element in soup(["script", "style", "noscript"]):
                element.decompose()

            main_selectors_local = [
                "main", "article", '[role="main"]',
                ".main-content", ".content", "#content",
                ".post-content", ".entry-content", ".page-content",
                ".article-content", "[data-content]",
                ".container .row",
            ]
            chosen_content = ""
            for selector in main_selectors_local:
                main_element = soup.select_one(selector)
                if main_element:
                    content_text = main_element.get_text(separator=" ", strip=True)
                    if len(content_text.split()) > 10:
                        chosen_content = content_text
                        break
            if not chosen_content:
                body = soup.find("body")
                if body:
                    for unwanted in body.select(
                        "nav, footer, header, aside, .sidebar, .navigation, .menu"
                    ):
                        unwanted.decompose()
                    chosen_content = body.get_text(separator=" ", strip=True)
            if not chosen_content:
                chosen_content = soup.get_text(separator=" ", strip=True)

            chosen_content = clean_text(chosen_content)
            del soup  # free soup tree
            word_count = len(chosen_content.split())

            print(
                f"    🔍 Playwright content: {word_count} words, {len(chosen_content)} chars, {len(internal_links)} links"
            )

            # Enhanced content validation for Playwright results
            try:
                min_chars = MIN_CONTENT_LENGTH
            except Exception:
                min_chars = 0
            try:
                min_words = MIN_WORDS_PER_PAGE
            except Exception:
                min_words = 0

            # Check if Playwright fallback actually improved the content
            pw_content_sufficient = len(chosen_content.strip()) >= max(
                1, min_chars
            ) and word_count >= max(1, min_words)

            if pw_content_sufficient:
                print(
                    f"    ✅ Playwright fallback successful for {url} (words={word_count}, chars={len(chosen_content)})"
                )
            else:
                print(
                    f"    ⚠️  Playwright fallback still insufficient for {url} (words={word_count}, chars={len(chosen_content)})"
                )

            return {
                "url": final_url,  # Use final URL after redirects
                "original_url": (
                    url if final_url != url else None
                ),  # Track original if redirected
                "title": title_text,
                "content": chosen_content,
                "internal_links": internal_links,
                "word_count": word_count,
                "crawled_at": datetime.now().isoformat(),
                "redirect_detected": redirect_detected,
            }
        except Exception as e:
            # Update proxy pool with failure
            try:
                if (
                    "pw_proxy_token" in locals()
                    and pw_proxy_token is not None
                    and self.proxy_pool
                ):
                    self.proxy_pool.release(
                        pw_proxy_token,
                        success=False,
                        status_code=None,
                        error=e,
                    )
            except Exception:
                pass
            print(f"    ⚠️  Playwright fallback failed for {url}: {e}")
            return None

    def _get_cookie_storage_path(self, netloc: str) -> str:
        """Get the path for storing cookies for a specific domain."""
        # Sanitize netloc for filename
        safe_netloc = re.sub(r"[^\w\-_.]", "_", netloc)
        return os.path.join(self.cookie_storage_dir, f"{safe_netloc}_cookies.json")

    def _load_cookies(self, netloc: str) -> Optional[Dict[str, Any]]:
        """Load cookies for a specific domain with enhanced fallback."""
        try:
            cookie_path = self._get_cookie_storage_path(netloc)
            if os.path.exists(cookie_path):
                with self._cookie_storage_lock:
                    with open(cookie_path, "r") as f:
                        storage_state = json.load(f)
                # Validate/filter outside lock (read-only on local copy)
                if isinstance(storage_state, dict) and "cookies" in storage_state:
                    current_time = time.time()
                    valid_cookies = []
                    for cookie in storage_state.get("cookies", []):
                        expires = cookie.get("expires", -1)
                        if expires == -1 or expires > current_time:
                            valid_cookies.append(cookie)
                    storage_state["cookies"] = valid_cookies
                    return storage_state
        except Exception as e:
            print(f"    ⚠️  Failed to load cookies for {netloc}: {e}")

        # Try to load cookies from parent domain
        try:
            domain_parts = netloc.split(".")
            if len(domain_parts) > 2:
                parent_domain = ".".join(domain_parts[-2:])
                parent_path = self._get_cookie_storage_path(parent_domain)
                if os.path.exists(parent_path):
                    with self._cookie_storage_lock:
                        with open(parent_path, "r") as f:
                            return json.load(f)
        except Exception:
            pass

        return None

    def _save_cookies(self, netloc: str, storage_state: Dict[str, Any]) -> None:
        """Save cookies for a specific domain."""
        try:
            cookie_path = self._get_cookie_storage_path(netloc)
            with self._cookie_storage_lock:
                with open(cookie_path, "w") as f:
                    json.dump(storage_state, f, indent=2)
        except Exception as e:
            print(f"    ⚠️  Failed to save cookies for {netloc}: {e}")

    def _try_accept_cookies(self, page) -> bool:
        """Enhanced cookie banner acceptor with comprehensive coverage.

        Returns:
            True if a cookie banner was accepted, False otherwise
        """
        try:
            # Comprehensive list of cookie banner selectors
            cookie_selectors = [
                # OneTrust
                "#onetrust-accept-btn-handler",
                "#onetrust-banner-sdk .accept-btn",
                '[data-testid="accept-cookies"]',
                # Cookiebot
                "#CybotCookiebotDialogBodyLevelButtonAccept",
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                ".CybotCookiebotDialogBodyButton",
                # GDPR/Consent frameworks
                "#sp-cc-accept",
                "#sp-cc-accept-all",
                ".sp_choice_type_11",
                ".sp_choice_type_12",
                # Generic accept buttons
                'button:has-text("Accept all")',
                'button:has-text("Accept All")',
                'button:has-text("I agree")',
                'button:has-text("I Agree")',
                'button:has-text("Accept cookies")',
                'button:has-text("Accept Cookies")',
                'button:has-text("OK")',
                'button:has-text("Got it")',
                'button:has-text("Continue")',
                # Common patterns
                '[data-testid*="accept"]',
                '[data-testid*="cookie"]',
                '[class*="accept"]',
                '[class*="cookie"]',
                '[id*="accept"]',
                '[id*="cookie"]',
                # Specific frameworks
                ".fc-consent-root .fc-primary-button",
                ".gdpr-banner .accept",
                ".cookie-banner .accept",
                ".consent-banner .accept",
                ".privacy-banner .accept",
                # Language variations
                'button:has-text("Akzeptieren")',  # German
                'button:has-text("Accepter")',  # French
                'button:has-text("Aceptar")',  # Spanish
                'button:has-text("Accetta")',  # Italian
            ]

            # Try to find and click cookie accept buttons
            for sel in cookie_selectors:
                try:
                    # Try multiple strategies for each selector
                    strategies = [
                        lambda: page.locator(sel).first,
                        lambda: page.locator(sel).nth(0),
                        lambda: page.locator(f"{sel}:visible").first,
                    ]

                    for strategy in strategies:
                        try:
                            btn = strategy()
                            if btn and btn.is_visible(timeout=1000):
                                btn.click(timeout=1000)
                                print(f"    🍪 Accepted cookies using selector: {sel}")
                                return True
                        except Exception:
                            continue

                except Exception:
                    continue

            # Try JavaScript-based cookie acceptance for stubborn banners
            js_scripts = [
                # Remove cookie banners
                """
                document.querySelectorAll('[id*="cookie"], [class*="cookie"], [id*="consent"], [class*="consent"]').forEach(el => {
                    if (el.style.display !== 'none') {
                        el.style.display = 'none';
                        el.remove();
                    }
                });
                """,
                # Accept all cookies via JavaScript
                """
                window.acceptAllCookies && window.acceptAllCookies();
                window.acceptCookies && window.acceptCookies();
                window.acceptAll && window.acceptAll();
                """,
                # Click any visible accept buttons
                """
                document.querySelectorAll('button').forEach(btn => {
                    const text = btn.textContent.toLowerCase();
                    if (text.includes('accept') || text.includes('agree') || text.includes('ok') || text.includes('continue')) {
                        if (btn.offsetParent !== null) { // Check if visible
                            btn.click();
                        }
                    }
                });
                """,
            ]

            for script in js_scripts:
                try:
                    page.evaluate(script)
                    time.sleep(0.5)  # Brief pause to let changes take effect
                except Exception:
                    continue

            return False

        except Exception as e:
            print(f"    ⚠️  Cookie acceptance error: {e}")
            return False

    def _load_playwright_profile(self, netloc: str) -> Dict[str, Any]:
        """Load a YAML profile for a given domain (netloc) with caching. Thread-safe.

        Merges with default.yml if present; domain overrides take precedence.
        """
        # Use lock to protect cache updates (LRU: move hit to end)
        with self._pw_profile_cache_lock:
            if netloc in self._pw_profile_cache:
                self._pw_profile_cache.move_to_end(netloc)
                return self._pw_profile_cache[netloc]
        # Outside lock for IO
        default_path = os.path.join(self.playwright_profiles_dir, "default.yml")
        profile_path = os.path.join(self.playwright_profiles_dir, f"{netloc}.yml")
        data: Dict[str, Any] = {}
        try:
            # Load default first
            if os.path.exists(default_path):
                with open(default_path, "r", encoding="utf-8") as f:
                    base = yaml.safe_load(f) or {}
                    if isinstance(base, dict):
                        data.update(base)
            if os.path.exists(profile_path):
                with open(profile_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                    if isinstance(loaded, dict):
                        data.update(loaded)
        except Exception as e:
            print(f"    ⚠️  Failed to load profile for {netloc}: {e}")
        # Cache the result (even empty) for this run; LRU eviction at cap
        with self._pw_profile_cache_lock:
            self._pw_profile_cache[netloc] = data
            while len(self._pw_profile_cache) > self._PW_PROFILE_CACHE_MAX:
                self._pw_profile_cache.popitem(last=False)
        return data

    def _apply_playwright_profile(self, page, profile: Dict[str, Any]) -> None:
        """Apply generic scripted actions from a profile (clicks, scrolls, waits)."""
        try:
            # Click actions (e.g., load more, expand accordions)
            for sel in profile.get("click_selectors", []) or []:
                try:
                    loc = page.locator(sel).first
                    if loc and loc.is_visible(timeout=1500):
                        loc.click(timeout=1500)
                except Exception:
                    continue
            # Pagination: click next up to N times (bounded)
            next_sel = profile.get("pagination_selector")
            max_pages = int(profile.get("pagination_max", 0) or 0)
            for _ in range(max(0, max_pages)):
                try:
                    loc = page.locator(next_sel).first
                    if loc and loc.is_visible(timeout=1500):
                        loc.click(timeout=1500)
                        page.wait_for_load_state(
                            "networkidle", timeout=self.playwright_nav_timeout_ms
                        )
                    else:
                        break
                except Exception:
                    break
            # Bounded infinite scroll
            scroll_loops = int(profile.get("scroll_loops", 0) or 0)
            for _ in range(max(0, scroll_loops)):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.75)
                except Exception:
                    break
        except Exception:
            pass

    def upload_to_milvus(
        self, page_data: Dict[str, Any], college_name: str,
        content_hash_cache: Optional[set] = None,
        content_hash_lock: Optional[threading.Lock] = None,
        force_replace: bool = False,
    ) -> bool:
        """Upload a single page to Milvus with per-chunk embeddings for RAG.

        Args:
            content_hash_cache: Per-college dedup cache (required).
            content_hash_lock: Lock for the per-college dedup cache (required).
            force_replace: Delete existing vectors and re-insert (used by rechunk mode).
        """
        _pending_claimed = False  # track whether we claimed a pending URL slot
        try:
            # Check if URL already exists — skip if so
            try:
                normalized_page_url = (
                    self.normalize_url(page_data["url"]) or page_data["url"]
                )
                page_canon = self._url_canonical_key(normalized_page_url)

                # Atomically claim this canonical URL to prevent concurrent
                # threads from inserting the same URL between our Milvus query
                # and the eventual batched insert.
                with self._pending_canonical_lock:
                    if page_canon in self._pending_canonical_urls:
                        with self.lock:
                            self.stats["duplicate_urls_skipped"] += 1
                        return False
                    self._pending_canonical_urls.add(page_canon)
                _pending_claimed = True

                _canon_val = page_canon.replace('"', '\\"')
                with self.collection_query_sema:
                    existing_records = self.collection.query(
                        expr=f'url_canonical == "{_canon_val}"',
                        output_fields=["id"],
                        limit=1,
                    )
                if existing_records:
                    if self.no_resume or force_replace:
                        # Delete old vectors, then proceed to re-insert
                        with self.collection_write_lock:
                            self.collection.delete(
                                expr=f'url_canonical == "{_canon_val}"'
                            )
                    else:
                        with self._pending_canonical_lock:
                            self._pending_canonical_urls.discard(page_canon)
                        _pending_claimed = False
                        with self.lock:
                            self.stats["existing_urls_skipped"] += 1
                        return False
            except Exception as e:
                if _pending_claimed:
                    with self._pending_canonical_lock:
                        self._pending_canonical_urls.discard(page_canon)
                    _pending_claimed = False
                print(f"    ⚠️  Could not query existing URL '{page_data['url']}': {e}")

            # Chunk content and embed per chunk for better RAG retrieval
            title_text = page_data["title"]
            content_text = page_data["content"]
            if CHUNK_SENTENCE_AWARE:
                chunks = chunk_text_by_sentences(
                    content_text,
                    max_tokens=CHUNK_MAX_TOKENS,
                    overlap_sentences=1,
                    model="text-embedding-3-small",
                )
            else:
                chunks = chunk_text_by_tokens(
                    content_text,
                    max_tokens=CHUNK_MAX_TOKENS,
                    overlap_tokens=CHUNK_OVERLAP_TOKENS,
                    model="text-embedding-3-small",
                )

            # Content dedup: skip chunks we've already embedded for this college.
            # Falling back to the instance-level hash cache is unsafe with
            # INTER_COLLEGE_PARALLELISM > 1 (cross-college contamination).
            if content_hash_cache is None or content_hash_lock is None:
                raise RuntimeError(
                    "upload_to_milvus called without per-college hash cache; "
                    "caller must provide content_hash_cache and content_hash_lock"
                )
            _hash_cache = content_hash_cache
            _hash_lock = content_hash_lock
            chunk_inputs = []
            chunk_indices = []  # maps back to original chunk index
            for i, c in enumerate(chunks):
                h = self._content_hash(c)
                with _hash_lock:
                    if h in _hash_cache:
                        continue
                    _hash_cache.add(h)
                # Build embedding input: contextual prefix or title prefix
                if CONTEXTUAL_PREFIXES:
                    from college_ai.rag.embeddings import generate_contextual_prefix
                    prefix = generate_contextual_prefix(c, content_text, college_name)
                    chunk_input = f"{prefix}\n\n{c}" if prefix else (
                        f"{title_text}\n\n{c}" if title_text else c
                    )
                else:
                    chunk_input = f"{title_text}\n\n{c}" if title_text else c
                chunk_inputs.append(chunk_input)
                chunk_indices.append(i)

            if not chunk_inputs:
                # All chunks were duplicates of previously embedded content.
                # Release the pending-canonical claim — nothing was queued for
                # the flush thread, so it will never release this claim for us.
                if _pending_claimed:
                    with self._pending_canonical_lock:
                        self._pending_canonical_urls.discard(page_canon)
                    _pending_claimed = False
                return True

            # Use the cross-thread embedding batcher for consolidated API calls
            try:
                future = self.embedding_batcher.submit(chunk_inputs)
                chunks_embeddings = future.result(timeout=60)
            except Exception as e:
                print(f"    ⚠️  Batcher failed for {page_data['url']}: {e}, falling back")
                with self.embed_semaphore:
                    chunks_embeddings = get_embeddings_batch(
                        chunk_inputs, model="text-embedding-3-small"
                    )

            # If no embeddings were produced, fall back to whole-page embedding
            if not chunks_embeddings or all(e is None for e in chunks_embeddings):
                with self.embed_semaphore:
                    fallback_emb = get_embedding(f"{title_text} {content_text}")
                if not fallback_emb:
                    print(f"    ✗ Failed to generate embedding for {page_data['url']}")
                    if _pending_claimed:
                        with self._pending_canonical_lock:
                            self._pending_canonical_urls.discard(page_canon)
                        _pending_claimed = False
                    return False
                chunks_embeddings = [fallback_emb]
                chunk_indices = [0]
                chunks = [content_text]

            # Classify page type from URL
            page_type = classify_page_type(page_data["url"])

            # Prepare column-based insert payload
            ids: List[str] = []
            colleges: List[str] = []
            urls: List[str] = []
            url_canonicals: List[str] = []
            titles: List[str] = []
            contents: List[str] = []
            embeddings: List[List[float]] = []
            page_types: List[str] = []
            crawled_ats: List[str] = []

            total_chunks = len(chunk_indices)
            for emb_idx, (emb, orig_idx) in enumerate(
                zip(chunks_embeddings, chunk_indices)
            ):
                if emb is None:
                    continue
                if not isinstance(emb, list) or len(emb) != VECTOR_DIM:
                    print(
                        f"    ✗ Skipping invalid embedding for {page_data['url']} (dim={len(emb) if isinstance(emb, list) else 'N/A'})"
                    )
                    continue
                chunk_text = chunks[orig_idx]
                chunked_title = page_data["title"]
                if total_chunks > 1:
                    chunked_title = f"{page_data['title']} (chunk {emb_idx + 1}/{total_chunks})"
                try:
                    _chunk_canonical = self._url_canonical_key(page_data["url"])
                except Exception:
                    # Skip chunk rather than insert url_canonical="" which would
                    # collide with all other failed-canonicalization entries in
                    # Milvus dedup queries, silently shadowing future pages.
                    print(f"    ✗ Cannot canonicalize URL, skipping chunk: {page_data.get('url')}")
                    continue
                ids.append(str(uuid.uuid4()))
                colleges.append(college_name)
                urls.append(page_data["url"])
                url_canonicals.append(_chunk_canonical)
                titles.append(chunked_title[: MAX_TITLE_LENGTH - 1])
                contents.append(chunk_text[: MAX_CONTENT_LENGTH - 1])
                embeddings.append(emb)
                page_types.append(page_type)
                crawled_ats.append(page_data["crawled_at"])

            # Buffer inserts instead of immediate write (flushed by background thread)
            # Note: content_sparse is auto-generated by Milvus BM25 function — do NOT include
            if embeddings:
                row_data = {
                    "id": ids,
                    "college_name": colleges,
                    "url": urls,
                    "url_canonical": url_canonicals,
                    "title": titles,
                    "content": contents,
                    "embedding": embeddings,
                    "page_type": page_types,
                    "crawled_at": crawled_ats,
                }
                # Retry with timeout to avoid deadlock if flush thread is dead.
                while True:
                    try:
                        self._insert_buffer.put(row_data, timeout=2.0)
                        break
                    except queue.Full:
                        if self._flush_thread_crashed.is_set() or global_shutdown_event.is_set():
                            if _pending_claimed:
                                with self._pending_canonical_lock:
                                    self._pending_canonical_urls.discard(page_canon)
                                _pending_claimed = False
                            raise RuntimeError(
                                "Insert buffer full and flush thread is not draining"
                            )

            print(
                f"    ✓ Queued {len(embeddings)} vector(s) for Milvus: {page_data['url']}"
            )
            return True

        except Exception as e:
            print(f"    ✗ Error uploading to Milvus: {e}")
            if _pending_claimed:
                with self._pending_canonical_lock:
                    self._pending_canonical_urls.discard(page_canon)
                _pending_claimed = False
            with self.lock:
                self.stats["total_errors"] += 1
            return False

    def crawl_college_site(
        self, college: Dict[str, str], max_pages: int = None
    ) -> Dict[str, Any]:
        """Crawl a single college website using efficient BFS with work-stealing."""
        college_name = college["name"]
        base_url = college["url"]
        max_pages = max_pages or MAX_PAGES_PER_COLLEGE

        # Early exit if shutdown was already requested
        if global_shutdown_event.is_set():
            return {
                "college_name": college_name,
                "base_url": base_url,
                "pages_crawled": 0,
                "pages_uploaded": 0,
                "urls_discovered": 0,
                "status": "shutdown",
            }

        print(f"\n=== Crawling {college_name} ===")
        print(f"Base URL: {base_url}")

        # Prune stale per-host rate-limit entries (prevents unbounded growth)
        self._prune_host_state(max_age_seconds=1800.0)

        # Prune dead Playwright pool slots (browser processes that died without cleanup)
        if self.playwright_enabled and sync_playwright is not None:
            self.pw_pool.prune_dead_slots()

        # Per-college content dedup cache (local, not shared across parallel colleges)
        college_hash_cache = set()  # type: set
        college_hash_lock = threading.Lock()

        # Load canonical URLs for this college to prevent crawling duplicates
        # Local set — safe with inter-college parallelism (no shared mutation).
        # If loading fails after retries, skip this college entirely rather than
        # re-crawling everything (which would produce mass duplicate vectors).
        try:
            college_canonical_urls, rechunk_urls, rechunk_full_urls = self._load_college_canonicals(
                college_name, rechunk=self.rechunk
            )
        except Exception as e:
            print(f"    ✗ Skipping {college_name}: failed to load canonical URLs after retries: {e}")
            return {
                "college": college_name,
                "base_url": base_url,
                "pages_crawled": 0,
                "pages_uploaded": 0,
                "status": "error_loading_canonicals",
            }
        print(
            f"    Found {len(college_canonical_urls):,} existing canonical URLs for {college_name}"
        )
        if self.rechunk and rechunk_urls:
            print(f"    Rechunk mode: {len(rechunk_urls):,} URLs will be re-chunked")

        # Reset state for this college (shared across workers)
        crawled_urls = set()
        discovered_urls = set()
        # Canonical (scheme-agnostic, no leading www.) keys for robust dedupe
        crawled_canon = set()
        discovered_canon = set()
        state_lock = threading.Lock()
        stop_event = threading.Event()
        pages_crawled_shared = 0  # successful pages scraped
        pw_uploaded = {"count": 0}  # pages uploaded via Playwright callbacks (dict pattern — no nonlocal needed)

        # Use the base URL as-is (preserve www. prefix).
        # Stripping www. can cause redirects to a different subdomain
        # (e.g., mit.edu → web.mit.edu instead of www.mit.edu) which
        # breaks internal link detection.
        try:
            parsed = urlparse(base_url)
            normalized_base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            normalized_base = base_url

        # Test the base URL first to check if the site is accessible.
        # Use a short-lived session — self.session headers are mutated on 403
        # retries, which is a data race with INTER_COLLEGE_PARALLELISM > 1.
        print(f"    Testing base URL: {normalized_base}")
        if USE_CURL_CFFI and curl_requests is not None:
            _test_session = curl_requests.Session(
                impersonate=random.choice(self._curl_impersonate_targets),
            )
        else:
            _test_session = requests.Session()
        _test_session.headers.update(self._base_headers_snapshot)
        try:
            test_page = self.scrape_page(normalized_base, _test_session)
        finally:
            try:
                _test_session.close()
            except Exception:
                pass
        if not test_page:
            print(
                f"    ✗ Cannot access {college_name} - site may be blocked or unavailable"
            )
            print(f"    ⏭️  Moving to next college...")
            return {
                "college_name": college_name,
                "base_url": base_url,
                "pages_crawled": 0,
                "pages_uploaded": 0,
                "urls_discovered": 0,
                "status": "blocked",
            }

        # Initialize BFS queue
        discovered_urls.add(normalized_base)
        try:
            discovered_canon.add(self._url_canonical_key(normalized_base))
        except Exception:
            pass

        pages_crawled = 0
        pages_uploaded = 0

        # Use ThreadPoolExecutor with true work-stealing
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Dedicated executor for Playwright fallback jobs (bounded workers)
            pw_executor = ThreadPoolExecutor(max_workers=self.playwright_max_workers)
            # Use a real queue for thread-safe operations
            from queue import Queue

            work_queue = Queue()
            work_queue.put((0, normalized_base))

            # Seed rechunk URLs directly into the BFS queue so they are
            # guaranteed to be re-crawled (not dependent on link discovery).
            # state_lock is held for consistency with the documented invariant
            # that discovered_canon/discovered_urls are always mutated under lock.
            if self.rechunk and rechunk_full_urls:
                seeded = 0
                with state_lock:
                    for rk, full_url in rechunk_full_urls.items():
                        norm = self.normalize_url(full_url)
                        if norm:
                            discovered_urls.add(norm)
                            discovered_canon.add(rk)
                            work_queue.put((0, norm))
                            seeded += 1
                print(f"    ✓ Seeded {seeded:,} rechunk URLs into BFS queue")

            # Track active futures
            active_futures = set()
            # Track active Playwright futures separately for queue management
            active_pw_futures = set()
            pw_futures_lock = threading.Lock()

            def _pw_task_with_cleanup(task_url: str) -> Optional[Dict[str, Any]]:
                """Playwright fallback task with guaranteed thread-local cleanup."""
                try:
                    return self._scrape_with_playwright(task_url)
                finally:
                    self._cleanup_thread_local_playwright()

            def worker_task():
                """Efficient worker for work-stealing BFS"""
                nonlocal pages_crawled_shared
                local_crawled = 0
                local_uploaded = 0
                consecutive_empty_checks = 0
                max_empty_checks = MAX_EMPTY_CHECKS  # Configurable to account for Playwright processing time

                # Create thread-local session for thread safety.
                # When curl_cffi is available, use a curl_cffi Session to reuse
                # the libcurl handle across requests (avoids C-level allocations
                # per request from curl_requests.get() module-level calls).
                if USE_CURL_CFFI and curl_requests is not None:
                    worker_session = curl_requests.Session(
                        impersonate=random.choice(self._curl_impersonate_targets),
                    )
                else:
                    worker_session = requests.Session()
                worker_session.headers.update(self._base_headers_snapshot)

                while not stop_event.is_set() and not global_shutdown_event.is_set():
                    # Propagate global shutdown into per-college stop so
                    # all existing stop_event checks also trigger.
                    if global_shutdown_event.is_set():
                        stop_event.set()
                        break
                    # Check global stop condition early
                    with state_lock:
                        if pages_crawled_shared >= max_pages:
                            stop_event.set()
                            break
                    try:
                        # Get next URL with timeout
                        depth, url = work_queue.get(
                            timeout=QUEUE_TIMEOUT_SECONDS
                        )  # Configurable timeout for Playwright compatibility
                        consecutive_empty_checks = 0  # Reset counter

                        # Claim URL atomically (using canonical key) and skip if exists globally
                        with state_lock:
                            if stop_event.is_set():
                                break
                            try:
                                canon_key = self._url_canonical_key(url)
                            except Exception:
                                canon_key = url

                            # Skip URLs already in the collection (resume mode only)
                            if not self.no_resume and canon_key in college_canonical_urls:
                                with self.lock:
                                    self.stats["existing_urls_skipped"] += 1
                                continue

                            if canon_key in crawled_canon:
                                with self.lock:
                                    self.stats["duplicate_urls_skipped"] += 1
                                continue
                            # Add to crawled sets within the state lock to ensure thread safety
                            crawled_urls.add(url)
                            crawled_canon.add(canon_key)

                        # Scrape page with thread-local session
                        page_data = self.scrape_page(url, worker_session)
                        if not page_data:
                            # Initial scraping failed - try Playwright fallback if enabled
                            # Skip if shutdown is in progress (Playwright retries are slow)
                            if (self.playwright_enabled and sync_playwright is not None
                                    and not stop_event.is_set()
                                    and not global_shutdown_event.is_set()):
                                print(
                                    f"    🔄 Initial scraping failed for {url}, trying Playwright fallback"
                                )
                                try:
                                    pw_result = self._scrape_with_playwright(url)
                                    if pw_result:
                                        # Upload PW result directly.
                                        # Delta cache write is AFTER upload_to_milvus
                                        # so a crash before buffer acceptance doesn't
                                        # leave a stale hash that prevents re-insertion.
                                        _pw_force = self.rechunk and canon_key in rechunk_urls
                                        if self.upload_to_milvus(
                                            pw_result, college_name,
                                            content_hash_cache=college_hash_cache,
                                            content_hash_lock=college_hash_lock,
                                            force_replace=_pw_force,
                                        ):
                                            local_uploaded += 1
                                        self._write_pw_delta_cache(pw_result, url)
                                        # Add discovered links to queue
                                        links = pw_result.get("internal_links", [])
                                        for link in links:
                                            norm = self.normalize_url(link)
                                            try:
                                                canon_link = self._url_canonical_key(
                                                    norm
                                                )
                                            except Exception:
                                                canon_link = norm
                                            with state_lock:
                                                if stop_event.is_set():
                                                    break
                                                already_seen = (
                                                    (canon_link in college_canonical_urls and not self.no_resume)
                                                    or canon_link in crawled_canon
                                                    or canon_link in discovered_canon
                                                )
                                                if (
                                                    not already_seen
                                                    and pages_crawled_shared < max_pages
                                                ):
                                                    discovered_urls.add(norm)
                                                    discovered_canon.add(canon_link)
                                                    work_queue.put((depth + 1, norm))
                                    else:
                                        print(
                                            f"    ✗ Playwright fallback also failed for {url}"
                                        )
                                except Exception as e:
                                    print(
                                        f"    ✗ Playwright fallback error for {url}: {e}"
                                    )
                            continue

                        local_crawled += 1
                        # Update global count and check for stop condition
                        with state_lock:
                            pages_crawled_shared += 1
                            if pages_crawled_shared >= max_pages:
                                stop_event.set()

                        # Upload to Milvus unless we plan a PW fallback upload
                        # or delta crawling detected unchanged content
                        if not page_data.get("needs_pw") and not page_data.get("skip_embed"):
                            _force = self.rechunk and canon_key in rechunk_urls
                            if self.upload_to_milvus(page_data, college_name,
                                                       content_hash_cache=college_hash_cache,
                                                       content_hash_lock=college_hash_lock,
                                                       force_replace=_force):
                                local_uploaded += 1

                        # Write delta cache AFTER insert buffer acceptance (or
                        # skip_embed confirmation) to prevent crash-consistency
                        # gap where a stale hash blocks re-insertion on next run.
                        _dm = page_data.get("_delta_meta")
                        if _dm and self._delta_cache:
                            try:
                                self._delta_cache.put(
                                    _dm["canon"],
                                    etag=_dm.get("etag"),
                                    last_modified=_dm.get("last_modified"),
                                    content_hash=_dm.get("content_hash"),
                                    links=_dm.get("links"),
                                )
                            except Exception:
                                pass

                        # If this URL needs Playwright, offload the job and merge results asynchronously
                        if page_data.get("needs_pw"):

                            def _merge_pw_result(fut, src_url=url, _depth=depth):
                                new_depth = _depth + 1
                                try:
                                    result = fut.result()
                                except Exception:
                                    return
                                if not result:
                                    return
                                if stop_event.is_set():
                                    return
                                # Upload PW-rendered page.
                                # Delta cache write is AFTER upload_to_milvus so a
                                # crash before buffer acceptance doesn't leave a stale
                                # hash that prevents re-insertion on next run.
                                try:
                                    _pw_cb_canon = self._url_canonical_key(src_url)
                                    _pw_cb_force = self.rechunk and _pw_cb_canon in rechunk_urls
                                    if self.upload_to_milvus(result, college_name,
                                                            content_hash_cache=college_hash_cache,
                                                            content_hash_lock=college_hash_lock,
                                                            force_replace=_pw_cb_force):
                                        with state_lock:
                                            pw_uploaded["count"] += 1
                                except Exception as _pw_upload_err:
                                    print(f"    ✗ PW callback upload failed for {src_url}: {_pw_upload_err}")
                                    with self.lock:
                                        self.stats["total_errors"] += 1
                                self._write_pw_delta_cache(result, src_url)
                                # Enqueue discovered links (canonical dedupe)
                                links = result.get("internal_links", [])
                                for link in links:
                                    norm = self.normalize_url(link)
                                    try:
                                        canon_link = self._url_canonical_key(norm)
                                    except Exception:
                                        canon_link = norm
                                    with state_lock:
                                        if stop_event.is_set():
                                            break
                                        already_seen = (
                                            (canon_link in college_canonical_urls and not self.no_resume)
                                            or canon_link in crawled_canon
                                            or canon_link in discovered_canon
                                        )
                                        if (
                                            not already_seen
                                            and pages_crawled_shared < max_pages
                                        ):
                                            discovered_urls.add(norm)
                                            discovered_canon.add(canon_link)
                                            work_queue.put((new_depth, norm))
                                return

                            try:
                                fut = pw_executor.submit(
                                    _pw_task_with_cleanup, url
                                )
                                # Track Playwright future for queue management
                                with pw_futures_lock:
                                    active_pw_futures.add(fut)

                                def pw_done_callback(future, _cb=_merge_pw_result):
                                    try:
                                        _cb(future)
                                    finally:
                                        with pw_futures_lock:
                                            active_pw_futures.discard(future)

                                fut.add_done_callback(pw_done_callback)
                            except Exception as e:
                                print(f"    ⚠️  Failed to submit Playwright task: {e}")
                                with self.lock:
                                    self.stats["total_errors"] += 1
                        # Add new links to queue (BFS)
                        new_links = page_data.get("internal_links", [])
                        new_depth = depth + 1
                        links_added = 0

                        # Filter and enqueue new links atomically (canonical dedupe)
                        for link in new_links:
                            with state_lock:
                                if stop_event.is_set():
                                    break
                                try:
                                    canon_link = self._url_canonical_key(link)
                                except Exception:
                                    canon_link = link

                                already_seen = (
                                    (canon_link in college_canonical_urls and not self.no_resume)
                                    or canon_link in crawled_canon
                                    or canon_link in discovered_canon
                                )
                                if (
                                    not already_seen
                                    and pages_crawled_shared < max_pages
                                ):
                                    discovered_urls.add(link)
                                    discovered_canon.add(canon_link)
                                    work_queue.put((new_depth, link))
                                    links_added += 1
                                elif already_seen:
                                    with self.lock:
                                        self.stats["duplicate_urls_skipped"] += 1

                        # Free link list — no longer needed after enqueuing
                        page_data.pop("internal_links", None)

                        # Progress update
                        if local_crawled % 5 == 0:  # More frequent updates
                            print(
                                f"    {college_name}: {local_crawled} crawled, {local_uploaded} uploaded, {links_added} new links found"
                            )

                    except queue.Empty:
                        consecutive_empty_checks += 1

                        # Fast exit if another worker already signalled stop
                        if stop_event.is_set() or global_shutdown_event.is_set():
                            stop_event.set()
                            break

                        # Check if we hit the page limit
                        with state_lock:
                            if pages_crawled_shared >= max_pages:
                                stop_event.set()
                                break

                        # Check if there are active Playwright jobs
                        with pw_futures_lock:
                            active_pw_count = len(active_pw_futures)

                        if consecutive_empty_checks >= max_empty_checks:
                            if active_pw_count > 0:
                                # Playwright jobs in flight may enqueue new URLs.
                                # Grant a bounded extension instead of decrementing
                                # by 1 (which caused near-infinite oscillation).
                                consecutive_empty_checks = max_empty_checks // 2
                                print(
                                    f"    🎭 Waiting for {active_pw_count} active Playwright job(s) for {college_name}"
                                )
                            else:
                                print(
                                    f"    ⏭️  Exiting worker for {college_name} - queue empty for too long"
                                )
                                stop_event.set()
                                break
                        continue
                    except Exception as e:
                        print(f"    ✗ Worker error for {college_name}: {e}")
                        continue

                # Propagate global shutdown to per-college stop so the
                # monitor loop and Playwright callbacks also exit promptly.
                if global_shutdown_event.is_set():
                    stop_event.set()

                # Clean up thread-local resources.  Wrapped in try/finally so
                # cleanup runs even if a BaseException (MemoryError) escaped
                # the while-loop.  We can't wrap the whole while-loop in try
                # without re-indenting hundreds of lines, so we catch it here.
                try:
                    return local_crawled, local_uploaded
                finally:
                    try:
                        worker_session.close()
                    except Exception:
                        pass
                    self._cleanup_thread_local_playwright()

            # Submit initial workers
            num_workers = min(
                self.max_workers, max_pages, 20
            )  # Cap at 20 for efficiency

            for i in range(num_workers):
                future = executor.submit(worker_task)
                active_futures.add(future)

            # Monitor progress
            try:
                start_time = time.time()
                max_crawl_time = MAX_CRAWL_TIME_PER_COLLEGE

                while active_futures and not stop_event.is_set():
                    # Check for timeout or global shutdown
                    if global_shutdown_event.is_set():
                        stop_event.set()
                        break
                    if time.time() - start_time > max_crawl_time:
                        print(
                            f"    ⏰ Timeout reached for {college_name} ({max_crawl_time}s)"
                        )
                        stop_event.set()
                        break

                    done, active_futures = wait(
                        active_futures,
                        return_when=FIRST_COMPLETED,
                        timeout=2.0,  # Increased timeout
                    )

                    for future in done:
                        try:
                            worker_crawled, worker_uploaded = future.result()

                            # Thread-safe statistics updates
                            pages_crawled += worker_crawled
                            pages_uploaded += worker_uploaded

                            # Do not respawn workers here; rely on initial pool.

                        except Exception as e:
                            print(f"    ✗ Worker failed for {college_name}: {e}")

                # If we exited due to stop_event, wait for remaining workers to finish
                # and aggregate their results so counts are accurate.
                # Use a bounded timeout to prevent hanging on stuck workers.
                if active_futures:
                    done, not_done = wait(active_futures, timeout=30)
                    if not_done:
                        print(
                            f"    ⚠️  {len(not_done)} worker(s) for {college_name} did not finish within 30s, "
                            f"cancelling"
                        )
                        for future in not_done:
                            future.cancel()
                    for future in done:
                        try:
                            worker_crawled, worker_uploaded = future.result()
                            pages_crawled += worker_crawled
                            pages_uploaded += worker_uploaded
                        except Exception as e:
                            print(
                                f"    ✗ Worker failed during shutdown for {college_name}: {e}"
                            )
                if global_shutdown_event.is_set():
                    # Shutdown path: cancel queued, short bounded wait for
                    # running tasks so we exit promptly after Ctrl+C.
                    pw_executor.shutdown(wait=False, cancel_futures=True)
                    _pw_wait_limit = 5
                    _pw_wait_start = time.time()
                    while time.time() - _pw_wait_start < _pw_wait_limit:
                        with pw_futures_lock:
                            if not active_pw_futures:
                                break
                        time.sleep(0.5)
                    else:
                        with pw_futures_lock:
                            _leftover = len(active_pw_futures)
                        if _leftover:
                            print(f"    ⚠️  {_leftover} Playwright future(s) still active after "
                                  f"{_pw_wait_limit}s wait")
                else:
                    # Normal completion: cancel queued tasks, wait for running
                    # ones.  shutdown(wait=True) joins worker threads — all
                    # task functions AND done_callbacks complete before it
                    # returns.  Bounded by Playwright's page navigation
                    # timeout (30s) + retry overhead.
                    pw_executor.shutdown(wait=True, cancel_futures=True)

                # Final progress
                if pages_crawled > 0:
                    print(
                        f"  {college_name}: {pages_crawled} pages crawled, {pages_uploaded} uploaded"
                    )

            except KeyboardInterrupt:
                print(f"\n  Interrupted crawling {college_name}")
                for future in active_futures:
                    future.cancel()
                wait(active_futures, timeout=2.0)
                pw_executor.shutdown(wait=False, cancel_futures=True)

        # Drain work_queue to free queued (depth, url) tuples before flush
        while not work_queue.empty():
            try:
                work_queue.get_nowait()
            except queue.Empty:
                break

        # Flush any remaining buffered inserts for this college
        self._flush_all_inserts()

        # Include PW callback uploads in the total
        pages_uploaded += pw_uploaded["count"]

        # Capture stats before clearing (discovered_urls used in return value)
        urls_discovered_count = len(discovered_urls)

        # Break closure references held by PW callbacks.
        # In the global-shutdown path (wait=False), _merge_pw_result callbacks
        # may still be running on pw_executor threads.  Clearing under locks
        # ensures no concurrent mutation.  In the normal path (wait=True),
        # all callbacks have completed, but clearing still frees memory.
        with state_lock:
            crawled_urls.clear()
            discovered_urls.clear()
            crawled_canon.clear()
            discovered_canon.clear()
            college_canonical_urls.clear()
            rechunk_urls.clear()
            rechunk_full_urls.clear()
        with college_hash_lock:
            college_hash_cache.clear()

        print(f"\n✓ Completed crawling {college_name}")
        print(f"  Pages crawled: {pages_crawled}")
        print(f"  Pages uploaded to Milvus: {pages_uploaded}")
        print(f"  Unique URLs discovered: {urls_discovered_count}")

        return {
            "college_name": college_name,
            "base_url": base_url,
            "pages_crawled": pages_crawled,
            "pages_uploaded": pages_uploaded,
            "urls_discovered": urls_discovered_count,
            "status": "completed",
        }

    def crawl_all_colleges(
        self,
        colleges: List[Dict[str, str]],
        max_pages_per_college: int = 50,
        inter_college_parallelism: int = None,
    ):
        """
        Crawl all colleges and upload directly to Milvus.
        Uses inter-college parallelism: colleges on different domains are
        crawled simultaneously (each has its own per-host rate limit).

        Args:
            colleges: List of college dicts with 'name' and 'url' keys
            max_pages_per_college: Maximum pages to crawl per college
            inter_college_parallelism: Number of colleges in parallel (uses config if None)
        """
        inter_college_workers = inter_college_parallelism or INTER_COLLEGE_PARALLELISM
        print("=== MULTITHREADED COLLEGE CRAWLING PIPELINE ===")
        print(f"Configuration:")
        print(f"  - Max workers per college: {self.max_workers}")
        print(f"  - Inter-college parallelism: {inter_college_workers}")
        print(f"  - Max pages per college: {max_pages_per_college}")
        print(f"  - Delay between requests: {self.delay}s")
        print(f"  - Batched embedding & insert buffer: ✓")
        print(f"  - Direct upload to Milvus: ✓")

        # Randomize order so repeated runs don't always hit the same schools
        # first (spreads load / avoids deterministic rate-limit patterns).
        all_jobs = list(colleges)
        random.shuffle(all_jobs)

        total_colleges = len(all_jobs)
        college_counter = {"count": 0}
        counter_lock = threading.Lock()

        def _process_college(college: Dict[str, str]):
            with counter_lock:
                college_counter["count"] += 1
                idx = college_counter["count"]
            print(
                f"\n--- [{idx}/{total_colleges}] Processing {college['name']} ---"
            )

            try:
                college_result = self.crawl_college_site(
                    college, max_pages_per_college
                )

                if college_result.get("status") == "completed":
                    with self.lock:
                        self.stats["total_pages_crawled"] += college_result[
                            "pages_crawled"
                        ]
                elif college_result.get("status") == "blocked":
                    print(
                        f"  ⚠️  {college['name']} was blocked - skipping statistics"
                    )
                    with self.lock:
                        self.stats["total_errors"] += 1

                with self.lock:
                    self.stats["colleges_processed"] += 1

            except Exception as e:
                print(f"  ✗ Error processing {college['name']}: {e}")
                with self.lock:
                    self.stats["total_errors"] += 1

        # Process colleges in parallel across different domains
        with ThreadPoolExecutor(max_workers=inter_college_workers) as college_executor:
            futures = []
            for college in all_jobs:
                if global_shutdown_event.is_set():
                    break
                futures.append(college_executor.submit(_process_college, college))
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    print(f"  ✗ College-level error: {e}")

        # Flush any remaining inserts
        self._flush_all_inserts()

        # Print overall summary
        print(f"\n=== FINAL CRAWLING SUMMARY ===")
        print(f"Total colleges processed: {self.stats['colleges_processed']}")
        print(f"Total pages crawled: {self.stats['total_pages_crawled']}")
        print(
            f"Total vectors uploaded to Milvus: {self.stats['total_vectors_uploaded']}"
        )
        print(
            f"Existing URLs skipped (from previous runs): {self.stats['existing_urls_skipped']}"
        )
        print(
            f"Duplicate URLs skipped (this run): {self.stats['duplicate_urls_skipped']}"
        )
        print(f"Total errors: {self.stats['total_errors']}")
        dropped_align = self.stats["rows_dropped_alignment"]
        dropped_insert = self.stats["rows_dropped_insert_fail"]
        if dropped_align or dropped_insert:
            print(f"Rows dropped (alignment): {dropped_align}")
            print(f"Rows dropped (insert failure): {dropped_insert}")
        print(f"All data is now available in Milvus for vector search!")

    def run_full_crawling_pipeline(self, max_pages_per_college: int = None,
                                    inter_college_parallelism: int = None):
        """
        Run the complete multithreaded crawling pipeline.

        Args:
            max_pages_per_college: Maximum pages to crawl per college (uses config if None)
            inter_college_parallelism: Number of colleges to crawl in parallel (uses config if None)
        """
        max_pages_per_college = max_pages_per_college or MAX_PAGES_PER_COLLEGE
        install_shutdown()

        try:
            # Step 1: Read CSV files
            print("\n1. Reading CSV files...")
            colleges = self.read_csv_files()

            if not colleges:
                print("No college data found. Please check your CSV files.")
                return

            # Step 2: Crawl all colleges and upload to Milvus
            print("\n2. Starting multithreaded crawling and uploading to Milvus...")
            self.crawl_all_colleges(colleges, max_pages_per_college,
                                    inter_college_parallelism=inter_college_parallelism)
        finally:
            # close() handles full shutdown: embedding batcher -> flush thread ->
            # Milvus disconnect -> Playwright pool -> non-pool Playwright ->
            # delta cache -> HTTP session.  Idempotent — safe to call again.
            self.close()

        if global_shutdown_event.is_set():
            print("\n✅ Graceful shutdown complete — all in-progress data saved.")
        else:
            print(f"\n🎉 Multithreaded crawling completed successfully!")
        print(f"📊 All pages have been uploaded to Zilliz Cloud for vector search!")

    def close(self):
        """Clean up resources including Milvus connections and Playwright instances."""
        with self._close_lock:
            if getattr(self, '_closed', False):
                return
            self._closed = True
        # Stop background threads first (same order as run_full_crawling_pipeline)
        try:
            self.embedding_batcher.shutdown()
        except Exception:
            pass
        try:
            self._insert_flush_stop.set()
            self._insert_flush_thread.join(timeout=30)
        except Exception:
            pass

        try:
            connections.disconnect(self._MILVUS_ALIAS)
            print("Disconnected from Milvus")
        except Exception as e:
            print(f"Error disconnecting from Milvus: {e}")

        # Clean up Playwright pool
        try:
            self.pw_pool.shutdown()
        except Exception:
            pass

        # Clean up ALL non-pool Playwright instances (fallback path).
        # Registry entries share the same dict object as the owning thread's
        # self._pw_local.browsers.  To avoid a data race where a worker is
        # still modifying the dict, we atomically swap it to an empty dict
        # under the lock, then close browsers from the private snapshot.
        # Each entry is cleaned in a daemon thread with timeout — browser.close()
        # or pw.stop() can hang on zombie Chromium processes.
        try:
            with self._pw_local_registry_lock:
                pw_entries = list(self._pw_local_registry)
                self._pw_local_registry.clear()  # Release stale references
            for entry in pw_entries:
                def _cleanup_entry(e=entry):
                    try:
                        with self._pw_local_registry_lock:
                            old_browsers = e.get("browsers", {})
                            e["browsers"] = {}
                        for browser in list(old_browsers.values()):
                            try:
                                browser.close()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        pw_handle = e.get("pw")
                        if pw_handle:
                            pw_handle.stop()
                    except Exception:
                        pass

                t = threading.Thread(target=_cleanup_entry, daemon=True)
                t.start()
                t.join(timeout=10)
                if t.is_alive():
                    print("    ⚠️  Non-pool Playwright cleanup hung — abandoning "
                          "(daemon thread will be reaped at process exit)")
            print("Cleaned up Playwright resources")
        except Exception as e:
            print(f"Error cleaning up Playwright resources: {e}")

        # Clean up delta crawl cache
        if self._delta_cache:
            try:
                self._delta_cache.close()
            except Exception:
                pass

        # Clean up HTTP session (holds open socket file descriptors)
        try:
            if hasattr(self, 'session') and self.session is not None:
                self.session.close()
        except Exception:
            pass

    def _cleanup_thread_local_playwright(self):
        """Clean up thread-local Playwright resources for the current thread only."""
        try:
            # Close thread-local browsers for this thread
            if hasattr(self._pw_local, "browsers"):
                for browser in self._pw_local.browsers.values():
                    try:
                        browser.close()
                    except Exception:
                        pass
                self._pw_local.browsers.clear()
                if hasattr(self._pw_local, "browser_uses"):
                    self._pw_local.browser_uses.clear()

            # Close thread-local Playwright instance for this thread
            if hasattr(self._pw_local, "pw") and self._pw_local.pw:
                try:
                    self._pw_local.pw.stop()
                except Exception:
                    pass
                self._pw_local.pw = None

            # Debug logging for verification
            thread_id = threading.current_thread().ident
            print(f"    🧹 Thread {thread_id}: Cleaned up Playwright resources")
        except Exception as e:
            thread_id = threading.current_thread().ident
            print(
                f"    ⚠️ Thread {thread_id}: Error cleaning up Playwright resources: {e}"
            )


def main():
    """Main function to run the multithreaded crawler."""
    import argparse
    parser = argparse.ArgumentParser(description="Multithreaded college website crawler")
    parser.add_argument(
        "--workers", type=int, default=None,
        help=f"Worker threads per college (default: {CRAWLER_MAX_WORKERS})"
    )
    parser.add_argument(
        "--colleges", type=int, default=None,
        help=f"Number of colleges to crawl in parallel (default: {INTER_COLLEGE_PARALLELISM})"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help=f"Max pages to crawl per college (default: {MAX_PAGES_PER_COLLEGE})"
    )
    parser.add_argument(
        "--no-resume", action="store_true", default=False,
        help="Force full re-crawl: ignore delta cache and replace existing Milvus vectors"
    )
    parser.add_argument(
        "--rechunk", action="store_true", default=False,
        help="Re-crawl pages with old 512-token chunks, replacing with sentence-aware chunks"
    )
    args = parser.parse_args()

    if args.rechunk and args.no_resume:
        parser.error("--rechunk and --no-resume are mutually exclusive; use --no-resume for a full re-crawl")

    crawler = MultithreadedCollegeCrawler(max_workers=args.workers,
                                         no_resume=args.no_resume,
                                         rechunk=args.rechunk)
    try:
        crawler.run_full_crawling_pipeline(
            max_pages_per_college=args.max_pages,
            inter_college_parallelism=args.colleges,
        )
    finally:
        crawler.close()


if __name__ == "__main__":
    main()
