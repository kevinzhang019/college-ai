"""
Multithreaded College Site Crawler
Reads college URLs from CSV files and performs multithreaded crawling of each site.
Uses BeautifulSoup to find internal links and uploads each page directly to Milvus.
"""

import os
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
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
import hashlib
import requests
import re
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
    FieldSchema,
    CollectionSchema,
    DataType,
    utility,
)


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
    EmbeddingBatcher,
)
from college_ai.rag.text_cleaner import clean_text
from college_ai.scraping.config import *

import sqlite3


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
                slot = {"pw": None, "browser": browser, "camoufox_cm": camoufox_cm, "uses": 0}
            else:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage", "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-background-networking",
                        "--disable-extensions", "--disable-sync", "--mute-audio",
                        "--disable-features=ExternalProtocolDialog",
                    ],
                )
                slot = {"pw": pw, "browser": browser, "camoufox_cm": None, "uses": 0}
            with self._all_locals_lock:
                self._all_locals.append(slot)
            return slot
        except Exception as e:
            print(f"    ⚠️  Failed to create Playwright browser: {e}")
            return None

    def _close_slot(self, slot: dict):
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
            self._close_slot(slot)
            with self._all_locals_lock:
                try:
                    self._all_locals.remove(slot)
                except ValueError:
                    pass
            slot = None

        # Create lazily on this thread
        if slot is None:
            slot = self._create_browser()
            if slot is None:
                self._semaphore.release()
                return None, -1
            self._local.slot = slot

        slot["uses"] += 1
        return slot["browser"], 1  # token=1 means "pool-managed"

    def release(self, token: int):
        """Release the semaphore slot. Browser stays alive for reuse on this thread."""
        if token >= 0:
            self._semaphore.release()

    def shutdown(self):
        """Close all tracked browsers across all threads."""
        with self._all_locals_lock:
            for slot in self._all_locals:
                self._close_slot(slot)
            self._all_locals.clear()
        self._started = False
        print("    🎭 Playwright pool shut down")

    def prune_dead_slots(self) -> int:
        """Close and remove browser slots whose browser process is no longer connected.

        Returns the number of slots pruned. Does NOT release the semaphore for
        pruned slots (intentional — avoids over-release).
        """
        pruned = 0
        with self._all_locals_lock:
            alive = []
            for slot in self._all_locals:
                browser = slot.get("browser")
                try:
                    still_connected = browser is not None and browser.is_connected()
                except Exception:
                    still_connected = False
                if still_connected:
                    alive.append(slot)
                else:
                    self._close_slot(slot)
                    pruned += 1
            self._all_locals[:] = alive
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
        # Create table on first use
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_cache (
                canonical_url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT,
                crawled_at TEXT
            )
        """)
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def get(self, canonical_url: str) -> dict:
        """Get cached metadata for a URL. Returns empty dict if not cached."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT etag, last_modified, content_hash, crawled_at "
            "FROM crawl_cache WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if row:
            return {
                "etag": row[0],
                "last_modified": row[1],
                "content_hash": row[2],
                "crawled_at": row[3],
            }
        return {}

    def put(self, canonical_url: str, etag: str = None, last_modified: str = None,
            content_hash: str = None):
        """Store or update cache entry for a URL."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO crawl_cache "
            "(canonical_url, etag, last_modified, content_hash, crawled_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (canonical_url, etag, last_modified, content_hash,
             datetime.now().isoformat()),
        )
        conn.commit()

    def close(self):
        """Close all thread-local connections."""
        if hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None


class MultithreadedCollegeCrawler:
    """Multithreaded crawler that crawls college websites and uploads directly to Milvus."""

    def __init__(self, delay: float = None, max_workers: int = None):
        """
        Initialize the crawler.

        Args:
            delay: Delay between requests to be respectful (uses config if None)
            max_workers: Number of worker threads per college (uses config if None)
        """
        self.delay = delay or CRAWLER_DELAY
        self.max_workers = max_workers or CRAWLER_MAX_WORKERS
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

        # Anti-bot detection settings
        self.min_delay = max(0.5, self.delay * 0.5)  # Minimum delay
        self.max_delay = self.delay * 2.0  # Maximum delay for randomization
        self.max_retries = MAX_RETRIES

        # Thread-safe sets for preventing duplicates
        self.crawled_urls = set()
        self.discovered_urls = set()
        self.uploaded_urls = set()  # Track URLs already uploaded to Milvus
        self.existing_urls = (
            set()
        )  # URLs that already exist in Milvus from previous runs
        self.lock = threading.Lock()
        # Serialize Milvus inserts to avoid driver-side races; keep stats lock separate
        self.insert_lock = threading.Lock()
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
        self._pw_profile_cache: Dict[str, Dict[str, Any]] = {}
        # Playwright runtime and browser cache (thread-local)
        self._pw_local = threading.local()  # Thread-local Playwright instances

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

        # Initialize per-college canonical URLs as empty - will be populated when needed
        self.college_canonical_urls: Set[str] = set()

        # Initialize existing URLs as empty - will be populated per college
        self.existing_urls = set()

        # Crawling statistics
        self.stats = {
            "total_pages_crawled": 0,
            "total_vectors_uploaded": 0,
            "total_errors": 0,
            "colleges_processed": 0,
            "duplicate_urls_skipped": 0,
            "existing_urls_skipped": 0,
        }

        # === Performance: Batched insert buffer ===
        # Instead of acquiring collection_write_lock per page, accumulate rows
        # and flush in batches (reduces lock contention by ~50x).
        self._insert_buffer: queue.Queue = queue.Queue()
        self._insert_buffer_size = MILVUS_INSERT_BUFFER_SIZE
        self._insert_flush_interval = MILVUS_INSERT_FLUSH_INTERVAL
        self._insert_flush_stop = threading.Event()
        self._insert_flush_thread = threading.Thread(
            target=self._insert_flush_loop, daemon=True, name="MilvusFlushThread"
        )
        self._insert_flush_thread.start()

        # === Performance: Cross-thread embedding batcher ===
        # Consolidates embedding requests from multiple worker threads into
        # fewer, larger API calls (up to 100 texts per call).
        self.embedding_batcher = EmbeddingBatcher(
            model="text-embedding-3-small", max_batch=100, max_wait_ms=200,
        )

        # === Performance: Content dedup hash cache ===
        # Prevents embedding near-identical chunks (boilerplate nav/footer text).
        # Reset per college in crawl_college_site().
        self._content_hash_cache: set = set()
        self._content_hash_lock = threading.Lock()

        # === Performance: Persistent Playwright browser pool ===
        # Pre-launches browser instances to avoid ~2-3s startup per request.
        self.pw_pool = PlaywrightPool(
            pool_size=PLAYWRIGHT_POOL_SIZE,
            rotate_after=PLAYWRIGHT_POOL_ROTATE_AFTER,
            headless=True,
            use_camoufox=USE_CAMOUFOX,
        )
        if self.playwright_enabled and sync_playwright is not None:
            self.pw_pool.start()

        # === Performance: Delta crawl cache ===
        # SQLite cache for ETag/Last-Modified/content hash to skip unchanged pages.
        self._delta_cache: Optional[DeltaCrawlCache] = None
        if ENABLE_DELTA_CRAWLING:
            cache_path = os.path.join(DATA_DIR, "crawl_cache.db")
            self._delta_cache = DeltaCrawlCache(cache_path)
            print(f"    Delta crawling enabled (cache: {cache_path})")

    # === Insert buffer flush infrastructure ===

    def _insert_flush_loop(self):
        """Background thread: drain the insert buffer periodically or when full."""
        while not self._insert_flush_stop.is_set():
            try:
                self._flush_insert_buffer(block_timeout=self._insert_flush_interval)
            except Exception as e:
                print(f"    ✗ Insert flush error: {e}")

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

        # Merge all rows into column-based format for a single insert
        field_names = [f.name for f in self.collection.schema.fields]
        merged: dict = {name: [] for name in field_names}
        for row in rows:
            for name in field_names:
                merged[name].extend(row.get(name, []))

        if not merged.get("id"):
            return

        try:
            with self.collection_write_lock:
                ordered_columns = [merged.get(name, []) for name in field_names]
                self.collection.insert(ordered_columns)
            count = len(merged["id"])
            with self.lock:
                self.stats["total_vectors_uploaded"] += count
        except Exception as e:
            print(f"    ✗ Batched insert failed ({len(merged.get('id', []))} rows): {e}")

    def _flush_all_inserts(self):
        """Flush any remaining buffered rows (call at end of crawl)."""
        for _ in range(10):  # drain up to 10 batches
            try:
                self._flush_insert_buffer(block_timeout=0.1)
            except Exception:
                break
            if self._insert_buffer.empty():
                break

    def _content_hash(self, text: str) -> str:
        """Return a hash for content deduplication. Uses normalized text."""
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]

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

    def connect_milvus(self):
        """Connect to Zilliz Cloud database."""
        try:
            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
            print("✓ Connected to Zilliz Cloud")
        except Exception as e:
            print(f"✗ Failed to connect to Zilliz Cloud: {e}")
            raise

    def get_or_create_collection(self):
        """Get or create the Zilliz Cloud collection."""
        collection_name = ZILLIZ_COLLECTION_NAME

        fields = [
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                is_primary=True,
                auto_id=False,
                max_length=36,
            ),
            FieldSchema(name="college_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="url_canonical", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(
                name="title", dtype=DataType.VARCHAR, max_length=MAX_TITLE_LENGTH
            ),
            FieldSchema(
                name="content", dtype=DataType.VARCHAR, max_length=MAX_CONTENT_LENGTH
            ),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            FieldSchema(name="crawled_at", dtype=DataType.VARCHAR, max_length=32),
        ]

        schema = CollectionSchema(fields, description="College pages with embeddings")

        if utility.has_collection(collection_name):
            existing = Collection(collection_name)
            try:
                # Build expected field map
                expected = {f.name: f for f in schema.fields}
                actual = {f.name: f for f in existing.schema.fields}

                def varchar_len(f):
                    try:
                        return int(f.params.get("max_length"))
                    except Exception:
                        return None

                # Validate presence and compatibility for each expected field
                recreate = False
                for name, exp in expected.items():
                    if name not in actual:
                        print(f"⚠️ Missing field in existing collection: {name}")
                        recreate = True
                        continue
                    act = actual[name]
                    if act.dtype != exp.dtype:
                        print(
                            f"⚠️ Field dtype mismatch for '{name}': {act.dtype} != {exp.dtype}"
                        )
                        recreate = True
                        continue
                    if name == "embedding":
                        exp_dim = VECTOR_DIM
                        act_dim = act.params.get("dim")
                        if act_dim != exp_dim:
                            print(f"⚠️ Embedding dim mismatch: {act_dim} != {exp_dim}")
                            recreate = True
                    elif act.dtype == DataType.VARCHAR:
                        exp_len = varchar_len(exp)
                        act_len = varchar_len(act)
                        if (
                            act_len is not None
                            and exp_len is not None
                            and act_len < exp_len
                        ):
                            print(
                                f"⚠️ VARCHAR max_length for '{name}' too small: {act_len} < {exp_len}"
                            )
                            recreate = True

                # Also ensure primary key settings
                try:
                    id_field = actual.get("id")
                    if not id_field or not id_field.is_primary or id_field.auto_id:
                        print("⚠️ Primary key field 'id' is misconfigured")
                        recreate = True
                except Exception:
                    pass

                if recreate:
                    print(
                        "♻️ Recreating collection to match expected schema (this drops existing data)."
                    )
                    utility.drop_collection(collection_name)
                    return Collection(collection_name, schema)

                return existing
            except Exception as e:
                print(
                    f"⚠️ Could not verify existing collection schema: {e}. Proceeding with existing collection."
                )
                return existing
        return Collection(collection_name, schema)

    def ensure_collection_ready(self):
        """Create vector index if missing and load collection for querying/search."""
        try:
            # Create vector index if not present
            has_index = False
            try:
                idx = getattr(self.collection, "indexes", None)
                has_index = bool(idx)
            except Exception:
                has_index = False

            if not has_index:
                print("🔧 Creating vector index on 'embedding' field...")
                self.collection.create_index(
                    field_name="embedding",
                    index_params={
                        "index_type": INDEX_TYPE,
                        "metric_type": METRIC_TYPE,
                        "params": {"nlist": 1024},
                    },
                    timeout=600,
                )
                print("✅ Index creation requested")

            # Load collection to make it queryable
            try:
                self.collection.load(timeout=120)
                print("✅ Collection loaded")
            except Exception as e:
                print(f"⚠️  Could not load collection yet: {e}")
        except Exception as e:
            print(f"❌ Error ensuring collection readiness: {e}")

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

    def _load_college_canonicals(self, college_name: str) -> Set[str]:
        """Load canonical URL keys for a specific college to prevent re-crawling duplicates.

        Args:
            college_name: Name of the college to load canonicals for

        Returns:
            Set of canonical URL keys for the college

        Uses 'url_canonical' when available; otherwise derives from 'url'.
        """
        canonical_urls = set()
        try:
            # Attempt to load for scalar queries
            try:
                self.collection.load()
            except Exception:
                pass

            # Get records only for this specific college
            expr = f'college_name == "{college_name}"'
            offset = 0
            batch_size = 2048  # Smaller batch size to avoid memory issues
            total_loaded = 0

            while True:
                try:
                    fields = ["url_canonical"]
                    batch = self.collection.query(
                        expr=expr,  # Filter by college name
                        output_fields=fields,
                        limit=batch_size,
                        offset=offset,
                    )
                except Exception as exc:
                    msg = str(exc)
                    if (
                        "received message larger than max" in msg
                        or "RESOURCE_EXHAUSTED" in msg
                    ):
                        new_batch_size = max(128, batch_size // 2)
                        if new_batch_size == batch_size:
                            raise
                        batch_size = new_batch_size
                        continue
                    else:
                        raise

                if not batch:
                    break
                for rec in batch:
                    key = (rec.get("url_canonical") or "").strip()
                    if key:
                        canonical_urls.add(key)
                total_loaded += len(batch)
                if len(batch) < batch_size:
                    break
                offset += batch_size
            print(
                f"    ✓ Loaded {len(canonical_urls):,} canonical URLs for {college_name}"
            )
        except Exception as e:
            print(f"    ⚠️  Failed to load canonical URLs for {college_name}: {e}")

        return canonical_urls

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

    def _prune_host_state(self, max_age_seconds: float = 1800.0) -> None:
        """Evict stale entries from per-host rate-limit dicts.

        Entries are stale when their token bucket was last refilled more than
        max_age_seconds ago AND their circuit-breaker has already expired.
        """
        now = time.time()
        stale_netlocs: List[str] = []
        with self._host_lock:
            for netloc, bucket in list(self._host_tokens.items()):
                last_seen = bucket.get("last", 0.0)
                circuit_until = self._host_circuit_until.get(netloc, 0.0)
                if (now - last_seen) > max_age_seconds and now >= circuit_until:
                    stale_netlocs.append(netloc)
            for netloc in stale_netlocs:
                self._host_tokens.pop(netloc, None)
                self._host_failures.pop(netloc, None)
                self._host_concurrency.pop(netloc, None)
                self._host_circuit_until.pop(netloc, None)
        if stale_netlocs:
            print(f"    🧹 Pruned {len(stale_netlocs)} stale host-state entries")

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

            # Human-like delay: log-normal distribution (mostly short, occasional longer pauses)
            delay = min(self.max_delay, random.lognormvariate(math.log(self.delay), 0.4))
            delay = max(self.min_delay, delay)
            time.sleep(delay)

            # Per-host token bucket and circuit breaker
            try:
                netloc = urlparse(url).netloc
            except Exception:
                netloc = ""
            # Circuit breaker check
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
                    # Not enough tokens; delay slightly
                    need = 1.0 - bucket["tokens"]
                    delay_s = need / max(0.1, self.token_refill_per_sec)
                    time.sleep(min(1.0, delay_s))
            # Use provided session or fall back to shared session
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

                    if USE_CURL_CFFI and curl_requests is not None:
                        # curl_cffi as primary — rotate TLS fingerprint on retries
                        impersonate_target = self._curl_impersonate_targets[
                            attempt % len(self._curl_impersonate_targets)
                        ]
                        # Generate fresh headers on retry attempts
                        req_headers = dict(request_session.headers)
                        if attempt > 0:
                            req_headers.update(self._generate_headers())
                        # Delta crawling: add conditional headers
                        if _delta_headers:
                            req_headers.update(_delta_headers)
                        response = curl_requests.get(
                            url,
                            impersonate=impersonate_target,
                            headers=req_headers,
                            proxies=proxy_dict,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                        )
                    else:
                        # Delta crawling: add conditional headers to session
                        if _delta_headers:
                            request_session.headers.update(_delta_headers)
                        response = request_session.get(
                            url,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                            proxies=proxy_dict,
                        )
                        # Remove conditional headers after use
                        if _delta_headers:
                            for k in _delta_headers:
                                request_session.headers.pop(k, None)

                    # Delta crawling: 304 Not Modified — page unchanged, skip embedding
                    # but still return a result so BFS doesn't stall
                    if response.status_code == 304 and self._delta_cache:
                        print(f"    304 Not Modified (delta skip): {url}")
                        return {
                            "url": url,
                            "title": "",
                            "content": "",
                            "internal_links": [],
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
                    # ensure capacity released on early continues
                    if self.proxy_pool and proxy_token is not None:
                        # ensure semaphore released if not yet
                        try:
                            self.proxy_pool.release(proxy_token, success=True)
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

            # Parse with BeautifulSoup
            soup = BeautifulSoup(response.content, "html.parser")

            # Use the final resolved URL after redirects as the base for link resolution and storage
            try:
                final_url = response.url or url
            except Exception:
                final_url = url

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
            js_heavy = self.is_js_heavy(
                response.text if hasattr(response, "text") else "", soup, url
            )

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

            # Delta crawling: update cache with response headers and content hash
            skip_embed = False
            if self._delta_cache and not needs_pw:
                try:
                    canon = _delta_canon or self._url_canonical_key(url)
                    etag = None
                    last_mod = None
                    try:
                        etag = response.headers.get("ETag")
                        last_mod = response.headers.get("Last-Modified")
                    except Exception:
                        pass
                    c_hash = self._content_hash(cleaned_content) if cleaned_content else None

                    # Check if content actually changed vs cached hash
                    if _delta_canon:
                        cached = self._delta_cache.get(_delta_canon)
                        if cached.get("content_hash") and cached["content_hash"] == c_hash:
                            print(f"    Delta: content unchanged (hash match), skipping embed: {url}")
                            skip_embed = True

                    self._delta_cache.put(canon, etag=etag, last_modified=last_mod,
                                          content_hash=c_hash)
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
                                "args": [
                                    "--no-sandbox",
                                    "--disable-setuid-sandbox",
                                    "--disable-dev-shm-usage",
                                    "--disable-accelerated-2d-canvas",
                                    "--no-first-run",
                                    "--no-zygote",
                                    "--disable-gpu",
                                    "--disable-features=TranslateUI,BlinkGenPropertyTrees,ExternalProtocolDialog",
                                    "--disable-ipc-flooding-protection",
                                    "--disable-background-networking",
                                    "--disable-default-apps",
                                    "--disable-extensions",
                                    "--disable-sync",
                                    "--metrics-recording-only",
                                    "--no-default-browser-check",
                                    "--mute-audio",
                                    "--disable-logging",
                                    "--disable-permissions-api",
                                    "--disable-blink-features=AutomationControlled",
                                    "--allow-running-insecure-content",
                                    "--disable-client-side-phishing-detection",
                                    "--disable-component-update",
                                    "--disable-hang-monitor",
                                    "--disable-prompt-on-repost",
                                    "--disable-background-timer-throttling",
                                    "--disable-renderer-backgrounding",
                                    "--disable-backgrounding-occluded-windows",
                                    "--force-device-scale-factor=1",
                                ],
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

            # Choose best snapshot and build soups
            soup_dom = BeautifulSoup(html_dom or "", "html.parser")
            soup_idle = BeautifulSoup(html_idle or "", "html.parser")
            # Remove common consent overlays
            cookie_selectors = [
                '[id*="cookie"]',
                '[class*="cookie"]',
                '[id*="consent"]',
                '[class*="consent"]',
                '[id*="gdpr"]',
                '[class*="gdpr"]',
                "#onetrust-banner-sdk",
                "#onetrust-consent-sdk",
                "#truste-consent-track",
                ".truste_overlay",
                "#qc-cmp2-ui",
                "#sp-cc",
                ".sp_choice_type",
                "#CybotCookiebotDialog",
            ]
            for sel in cookie_selectors:
                for el in soup_dom.select(sel):
                    el.decompose()
                for el in soup_idle.select(sel):
                    el.decompose()

            # Pick better title/content
            def extract_text_and_links(soup_obj: BeautifulSoup, base_url: str) -> tuple:
                if not soup_obj:
                    return "", []

                # Make a copy to avoid modifying the original
                soup_copy = BeautifulSoup(str(soup_obj), "html.parser")

                # Remove unwanted elements but preserve content structure
                for element in soup_copy(["script", "style", "noscript"]):
                    element.decompose()

                # Try multiple strategies for content extraction
                main_selectors_local = [
                    "main",
                    "article",
                    '[role="main"]',
                    ".main-content",
                    ".content",
                    "#content",
                    ".post-content",
                    ".entry-content",
                    ".page-content",
                    ".article-content",
                    "[data-content]",
                    ".container .row",  # Common bootstrap patterns
                ]

                main_content_local = ""

                # Try each selector
                for selector in main_selectors_local:
                    main_element = soup_copy.select_one(selector)
                    if main_element:
                        content_text = main_element.get_text(separator=" ", strip=True)
                        if len(content_text.split()) > 10:  # Require at least 10 words
                            main_content_local = content_text
                            break

                # Fallback strategies
                if not main_content_local:
                    # Try looking for the largest text container
                    body = soup_copy.find("body")
                    if body:
                        # Remove common non-content areas but keep their siblings
                        for unwanted in body.select(
                            "nav, footer, header, aside, .sidebar, .navigation, .menu"
                        ):
                            unwanted.decompose()
                        main_content_local = body.get_text(separator=" ", strip=True)

                # Final fallback - just get all visible text
                if not main_content_local and soup_copy:
                    main_content_local = soup_copy.get_text(separator=" ", strip=True)

                cleaned_local = clean_text(main_content_local)
                links_local = self.extract_internal_links(
                    soup_obj, base_url
                )  # Use final URL for proper link resolution
                return cleaned_local, links_local

            cleaned_dom, links_dom = extract_text_and_links(soup_dom, final_url)
            cleaned_idle, links_idle = extract_text_and_links(soup_idle, final_url)

            # Debug output for content extraction
            print(f"    🔍 Playwright extraction debug for {url}:")
            print(
                f"        DOM snapshot: {len(html_dom)} chars, {len(cleaned_dom)} cleaned chars"
            )
            print(
                f"        Idle snapshot: {len(html_idle)} chars, {len(cleaned_idle)} cleaned chars"
            )
            print(f"        DOM links: {len(links_dom)}, Idle links: {len(links_idle)}")

            title_dom = soup_dom.find("title") if soup_dom else None
            title_idle = soup_idle.find("title") if soup_idle else None
            title_text = (
                title_idle.get_text(strip=True)
                if title_idle
                else (title_dom.get_text(strip=True) if title_dom else "")
            )

            chosen_content = (
                cleaned_idle if len(cleaned_idle) >= len(cleaned_dom) else cleaned_dom
            )
            internal_links = list({*links_dom, *links_idle})
            word_count = len(chosen_content.split())

            print(
                f"        Final content: {word_count} words, {len(chosen_content)} chars"
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
                with open(cookie_path, "r") as f:
                    storage_state = json.load(f)
                    # Validate storage state structure
                    if isinstance(storage_state, dict) and "cookies" in storage_state:
                        # Filter out expired cookies
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
        # Use lock to protect cache updates
        with self._pw_profile_cache_lock:
            if netloc in self._pw_profile_cache:
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
        # Cache the result (even empty) for this run
        with self._pw_profile_cache_lock:
            self._pw_profile_cache[netloc] = data
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
        self, page_data: Dict[str, Any], college_name: str
    ) -> bool:
        """Upload a single page to Milvus with per-chunk embeddings for RAG."""
        try:
            # Check if URL already exists — skip if so
            try:
                normalized_page_url = (
                    self.normalize_url(page_data["url"]) or page_data["url"]
                )
                page_canon = self._url_canonical_key(normalized_page_url)
                _canon_val = page_canon.replace('"', '\\"')
                with self.collection_query_sema:
                    existing_records = self.collection.query(
                        expr=f'url_canonical == "{_canon_val}"',
                        output_fields=["id"],
                        limit=1,
                    )
                if existing_records:
                    with self.lock:
                        self.stats["existing_urls_skipped"] += 1
                    return False
            except Exception as e:
                print(f"    ⚠️  Could not query existing URL '{page_data['url']}': {e}")

            # Chunk content and embed per chunk for better RAG retrieval
            title_text = page_data["title"]
            content_text = page_data["content"]
            chunks = chunk_text_by_tokens(
                content_text,
                max_tokens=800,
                overlap_tokens=80,
                model="text-embedding-3-small",
            )

            # Content dedup: skip chunks we've already embedded for this college
            chunk_inputs = []
            chunk_indices = []  # maps back to original chunk index
            for i, c in enumerate(chunks):
                h = self._content_hash(c)
                with self._content_hash_lock:
                    if h in self._content_hash_cache:
                        continue
                    self._content_hash_cache.add(h)
                chunk_inputs.append(f"{title_text}\n\n{c}" if title_text else c)
                chunk_indices.append(i)

            if not chunk_inputs:
                # All chunks were duplicates of previously embedded content
                return True

            # Use the cross-thread embedding batcher for consolidated API calls
            try:
                future = self.embedding_batcher.submit(chunk_inputs)
                chunks_embeddings = future.result(timeout=60)
            except Exception as e:
                print(f"    ⚠️  Batcher failed for {page_data['url']}: {e}, falling back")
                chunks_embeddings = get_embeddings_batch(
                    chunk_inputs, model="text-embedding-3-small"
                )

            # If no embeddings were produced, fall back to whole-page embedding
            if not chunks_embeddings or all(e is None for e in chunks_embeddings):
                fallback_emb = get_embedding(f"{title_text} {content_text}")
                if not fallback_emb:
                    print(f"    ✗ Failed to generate embedding for {page_data['url']}")
                    return False
                chunks_embeddings = [fallback_emb]
                chunk_indices = [0]
                chunks = [content_text]

            # Prepare column-based insert payload
            ids: List[str] = []
            colleges: List[str] = []
            urls: List[str] = []
            url_canonicals: List[str] = []
            titles: List[str] = []
            contents: List[str] = []
            embeddings: List[List[float]] = []
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
                ids.append(str(uuid.uuid4()))
                colleges.append(college_name)
                urls.append(page_data["url"])
                try:
                    url_canonicals.append(self._url_canonical_key(page_data["url"]))
                except Exception:
                    url_canonicals.append("")
                titles.append(chunked_title[: MAX_TITLE_LENGTH - 1])
                contents.append(chunk_text[: MAX_CONTENT_LENGTH - 1])
                embeddings.append(emb)
                crawled_ats.append(page_data["crawled_at"])

            # Buffer inserts instead of immediate write (flushed by background thread)
            if embeddings:
                row_data = {
                    "id": ids,
                    "college_name": colleges,
                    "url": urls,
                    "url_canonical": url_canonicals,
                    "title": titles,
                    "content": contents,
                    "embedding": embeddings,
                    "crawled_at": crawled_ats,
                }
                self._insert_buffer.put(row_data)

            print(
                f"    ✓ Queued {len(embeddings)} vector(s) for Milvus: {page_data['url']}"
            )
            return True

        except Exception as e:
            print(f"    ✗ Error uploading to Milvus: {e}")
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

        print(f"\n=== Crawling {college_name} ===")
        print(f"Base URL: {base_url}")

        # Clear instance-level URL sets that are never read cross-college
        # (prevents unbounded growth over 10+ hour runs).
        with self.lock:
            self.crawled_urls.clear()
            self.discovered_urls.clear()
            self.uploaded_urls.clear()
            self.existing_urls.clear()

        # Prune stale per-host rate-limit entries (prevents unbounded growth)
        self._prune_host_state(max_age_seconds=1800.0)

        # Prune dead Playwright pool slots (browser processes that died without cleanup)
        if self.playwright_enabled and sync_playwright is not None:
            self.pw_pool.prune_dead_slots()

        # Reset content dedup cache for this college
        with self._content_hash_lock:
            self._content_hash_cache.clear()

        # Load canonical URLs for this college to prevent crawling duplicates
        self.college_canonical_urls = self._load_college_canonicals(college_name)
        print(
            f"    Found {len(self.college_canonical_urls):,} existing canonical URLs for {college_name}"
        )

        # Reset state for this college (shared across workers)
        crawled_urls = set()
        discovered_urls = set()
        # Canonical (scheme-agnostic, no leading www.) keys for robust dedupe
        crawled_canon = set()
        discovered_canon = set()
        state_lock = threading.Lock()
        stop_event = threading.Event()
        pages_crawled_shared = 0  # successful pages scraped

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
        _test_session = requests.Session()
        _test_session.headers.update(self.session.headers)
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

                # Create thread-local session for thread safety
                worker_session = requests.Session()
                worker_session.headers.update(self.session.headers)

                while not stop_event.is_set():
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

                            # Skip URLs already in the collection
                            if canon_key in self.college_canonical_urls:
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
                            if self.playwright_enabled and sync_playwright is not None:
                                print(
                                    f"    🔄 Initial scraping failed for {url}, trying Playwright fallback"
                                )
                                try:
                                    pw_result = self._scrape_with_playwright(url)
                                    if pw_result:
                                        # Upload PW result directly
                                        if self.upload_to_milvus(
                                            pw_result, college_name
                                        ):
                                            local_uploaded += 1
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
                                                    canon_link in self.college_canonical_urls
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
                            if self.upload_to_milvus(page_data, college_name):
                                local_uploaded += 1

                        # If this URL needs Playwright, offload the job and merge results asynchronously
                        if page_data.get("needs_pw"):

                            def _merge_pw_result(fut, src_url=url):
                                # Calculate new_depth inside the function
                                new_depth = depth + 1
                                try:
                                    result = fut.result()
                                except Exception:
                                    return
                                if not result:
                                    return
                                if stop_event.is_set():
                                    return
                                # Upload PW-rendered page
                                try:
                                    self.upload_to_milvus(result, college_name)
                                except Exception:
                                    pass
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
                                            canon_link in self.college_canonical_urls
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

                                def pw_done_callback(future):
                                    # Remove from tracking when done
                                    with pw_futures_lock:
                                        active_pw_futures.discard(future)
                                    # Call original callback
                                    _merge_pw_result(future)

                                fut.add_done_callback(pw_done_callback)
                            except Exception:
                                pass
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
                                    canon_link in self.college_canonical_urls
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

                        # Progress update
                        if local_crawled % 5 == 0:  # More frequent updates
                            print(
                                f"    {college_name}: {local_crawled} crawled, {local_uploaded} uploaded, {links_added} new links found"
                            )

                    except queue.Empty:
                        consecutive_empty_checks += 1

                        # Fast exit if another worker already signalled stop
                        if stop_event.is_set():
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

                # Close thread-local session before exiting
                try:
                    worker_session.close()
                except Exception:
                    pass

                # Clean up thread-local Playwright resources
                self._cleanup_thread_local_playwright()

                return local_crawled, local_uploaded

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
                    # Check for timeout
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
                # and aggregate their results so counts are accurate
                if active_futures:
                    done, _ = wait(active_futures)
                    for future in done:
                        try:
                            worker_crawled, worker_uploaded = future.result()
                            pages_crawled += worker_crawled
                            pages_uploaded += worker_uploaded
                        except Exception as e:
                            print(
                                f"    ✗ Worker failed during shutdown for {college_name}: {e}"
                            )
                # Playwright threads now have bounded semaphore waits (30s) and
                # page navigation timeouts, so shutdown should complete.
                # Use wait=True to ensure clean browser process cleanup.
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

        # Flush any remaining buffered inserts for this college
        self._flush_all_inserts()

        print(f"\n✓ Completed crawling {college_name}")
        print(f"  Pages crawled: {pages_crawled}")
        print(f"  Pages uploaded to Milvus: {pages_uploaded}")
        print(f"  Unique URLs discovered: {len(discovered_urls)}")

        return {
            "college_name": college_name,
            "base_url": base_url,
            "pages_crawled": pages_crawled,
            "pages_uploaded": pages_uploaded,
            "urls_discovered": len(discovered_urls),
            "status": "completed",
        }

    def crawl_all_colleges(
        self,
        colleges: List[Dict[str, str]],
        max_pages_per_college: int = 50,
    ):
        """
        Crawl all colleges and upload directly to Milvus.
        Uses inter-college parallelism: colleges on different domains are
        crawled simultaneously (each has its own per-host rate limit).

        Args:
            colleges: List of college dicts with 'name' and 'url' keys
            max_pages_per_college: Maximum pages to crawl per college
        """
        inter_college_workers = INTER_COLLEGE_PARALLELISM
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
            futures = [
                college_executor.submit(_process_college, college)
                for college in all_jobs
            ]
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
        print(f"All data is now available in Milvus for vector search!")

    def run_full_crawling_pipeline(self, max_pages_per_college: int = None):
        """
        Run the complete multithreaded crawling pipeline.

        Args:
            max_pages_per_college: Maximum pages to crawl per college (uses config if None)
        """
        max_pages_per_college = max_pages_per_college or MAX_PAGES_PER_COLLEGE

        # Step 1: Read CSV files
        print("\n1. Reading CSV files...")
        colleges = self.read_csv_files()

        if not colleges:
            print("No college data found. Please check your CSV files.")
            return

        # Step 2: Crawl all colleges and upload to Milvus
        print("\n2. Starting multithreaded crawling and uploading to Milvus...")
        self.crawl_all_colleges(colleges, max_pages_per_college)

        # Shutdown background threads and resources
        self._insert_flush_stop.set()
        self._insert_flush_thread.join(timeout=10)
        self.embedding_batcher.shutdown()
        self.pw_pool.shutdown()
        if self._delta_cache:
            self._delta_cache.close()

        print(f"\n🎉 Multithreaded crawling completed successfully!")
        print(f"📊 All pages have been uploaded to Zilliz Cloud for vector search!")

    def get_existing_urls_for_college(self, college_name: str) -> Set[str]:
        """
        Get existing URLs for a specific college from Zilliz Cloud.

        Args:
            college_name: Name of the college to check

        Returns:
            Set of URLs that already exist for this college
        """
        existing_urls = set()

        try:
            # Load collection
            self.collection.load()

            # Query URLs for this specific college
            college_records = self.collection.query(
                expr=f'college_name == "{college_name}"',
                output_fields=["url"],
                limit=16384,
            )

            # Add URLs to set
            for record in college_records:
                url = record.get("url", "")
                if url:
                    existing_urls.add(url)

            print(f"📊 Found {len(existing_urls):,} existing URLs for {college_name}")
            return existing_urls

        except Exception as e:
            print(f"❌ Error getting existing URLs for {college_name}: {e}")
            return existing_urls

    def check_urls_batch_for_college(
        self, urls: List[str], college_name: str, batch_size: int = 100
    ) -> Set[str]:
        """
        Check a batch of URLs against existing URLs for a specific college in Zilliz Cloud.

        Args:
            urls: List of URLs to check
            college_name: Name of the college
            batch_size: Size of batches to query at once

        Returns:
            Set of URLs that already exist for this college
        """
        existing_urls = set()

        # Split URLs into batches
        for i in range(0, len(urls), batch_size):
            batch_urls = urls[i : i + batch_size]

            # Build query expression for this batch (college-specific)
            url_conditions = []
            for url in batch_urls:
                # Escape quotes in URL for Milvus query
                escaped_url = url.replace('"', '\\"')
                url_conditions.append(f'url == "{escaped_url}"')

            if not url_conditions:
                continue

            # Combine conditions with OR and add college filter
            url_expr = " || ".join(url_conditions)
            query_expr = f'college_name == "{college_name}" && ({url_expr})'

            try:
                # Query Milvus for existing URLs in this batch for this college
                existing_records = self.collection.query(
                    expr=query_expr,
                    output_fields=["url"],
                    limit=len(batch_urls),
                )

                # Add found URLs to set
                for record in existing_records:
                    url = record.get("url", "")
                    if url:
                        existing_urls.add(url)

            except Exception as e:
                print(f"❌ Error checking URL batch for {college_name}: {e}")
                continue

        return existing_urls

    def close(self):
        """Clean up resources including Milvus connections and Playwright instances."""
        try:
            connections.disconnect("default")
            print("Disconnected from Milvus")
        except Exception as e:
            print(f"Error disconnecting from Milvus: {e}")

        # Clean up Playwright pool
        try:
            self.pw_pool.shutdown()
        except Exception:
            pass

        # Clean up thread-local Playwright instances (fallback path)
        try:
            if hasattr(self._pw_local, "browsers"):
                for browser in self._pw_local.browsers.values():
                    try:
                        browser.close()
                    except Exception:
                        pass
                self._pw_local.browsers.clear()

            if hasattr(self._pw_local, "pw") and self._pw_local.pw:
                try:
                    self._pw_local.pw.stop()
                except Exception:
                    pass
                self._pw_local.pw = None

            print("Cleaned up Playwright resources")
        except Exception as e:
            print(f"Error cleaning up Playwright resources: {e}")

        # Clean up delta crawl cache
        if self._delta_cache:
            try:
                self._delta_cache.close()
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
    # Initialize crawler with config settings
    crawler = MultithreadedCollegeCrawler()

    # Run the full pipeline
    crawler.run_full_crawling_pipeline()


if __name__ == "__main__":
    main()
