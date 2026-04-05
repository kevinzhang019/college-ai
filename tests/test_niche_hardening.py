"""Tests for thread-safety hardening fixes (restart TOCTOU, intercepted data cap,
keepalive logging, capture_cookies error handling, ENGINE alias removal).
"""

import logging
import queue
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from college_ai.scraping.niche_scraper import (
    DBWriterThread, NicheScraper, _MAX_INTERCEPTED,
)
from college_ai.scraping.shutdown import shutdown_event


# ---------------------------------------------------------------------------
# Fix 1+6: restart() — shutdown during/after sleep skips start()
# ---------------------------------------------------------------------------

class TestRestartShutdownGuards:
    """restart() must not launch a browser if shutdown fires during or before sleep."""

    def setup_method(self):
        shutdown_event.clear()

    def teardown_method(self):
        shutdown_event.clear()

    def _make_scraper_stub(self):
        """Create a NicheScraper without running __init__ (no env vars needed)."""
        scraper = NicheScraper.__new__(NicheScraper)
        scraper.page = None
        scraper.context = None
        scraper.browser = None
        scraper._playwright = None
        scraper._intercepted_data = []
        scraper._cached_grades = None
        scraper._cached_grades_slug = None
        scraper._response_handler = None
        scraper.close = MagicMock()
        scraper.start = MagicMock()
        return scraper

    def test_shutdown_before_sleep_skips_start(self):
        scraper = self._make_scraper_stub()
        shutdown_event.set()
        scraper.restart(headless=True, grades_only=True)
        scraper.close.assert_called_once()
        scraper.start.assert_not_called()

    @patch("college_ai.scraping.niche_scraper.time.sleep")
    def test_shutdown_during_sleep_skips_start(self, mock_sleep):
        """Simulate shutdown firing on the second sleep iteration."""
        scraper = self._make_scraper_stub()
        call_count = [0]

        def sleep_side_effect(_duration):
            call_count[0] += 1
            if call_count[0] >= 2:
                shutdown_event.set()

        mock_sleep.side_effect = sleep_side_effect

        scraper.restart(headless=True, grades_only=True)
        scraper.close.assert_called_once()
        scraper.start.assert_not_called()

    @patch("college_ai.scraping.niche_scraper.time.sleep")
    def test_normal_restart_calls_start(self, mock_sleep):
        """Without shutdown, restart() should call start()."""
        scraper = self._make_scraper_stub()
        scraper.restart(headless=True, grades_only=True)
        scraper.close.assert_called_once()
        scraper.start.assert_called_once_with(headless=True, grades_only=True)


# ---------------------------------------------------------------------------
# Fix 2: _intercepted_data cap
# ---------------------------------------------------------------------------

class TestInterceptedDataCap:
    """handle_response callback must not grow _intercepted_data beyond _MAX_INTERCEPTED."""

    def test_cap_value_exists(self):
        assert _MAX_INTERCEPTED == 200

    def test_intercepted_data_capped(self):
        """Fire the response handler more times than the cap and verify the list is bounded."""
        scraper = NicheScraper.__new__(NicheScraper)
        scraper._intercepted_data = []
        scraper._response_handler = None
        scraper.page = MagicMock()

        # Capture the callback registered via page.on("response", cb)
        captured_cb = [None]

        def capture_on(event, cb):
            if event == "response":
                captured_cb[0] = cb

        scraper.page.on = capture_on
        scraper.page.remove_listener = MagicMock()
        scraper._setup_network_intercept()

        assert captured_cb[0] is not None, "_setup_network_intercept did not register a handler"

        # Create a mock response that matches the capture filter
        mock_response = MagicMock()
        mock_response.url = "https://www.niche.com/api/profile/test/blocks/scatter"
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status = 200
        mock_response.json.return_value = {"data": "test"}

        # Fire 250 times — should cap at _MAX_INTERCEPTED
        for _ in range(250):
            captured_cb[0](mock_response)

        assert len(scraper._intercepted_data) == _MAX_INTERCEPTED


# ---------------------------------------------------------------------------
# Fix 3: _keepalive() non-Hrana error logging
# ---------------------------------------------------------------------------

class TestKeepaliveLogging:
    """_keepalive() must log non-Hrana errors instead of silently swallowing them."""

    @patch("college_ai.scraping.niche_scraper.is_hrana_error", return_value=False)
    @patch("college_ai.scraping.niche_scraper.get_session")
    def test_non_hrana_error_logged(self, mock_get_session, mock_is_hrana, caplog):
        mock_session = MagicMock()
        mock_session.execute.side_effect = ConnectionError("connection refused")
        mock_get_session.return_value = mock_session

        write_queue = queue.Queue(maxsize=50)
        stats = {"total_points": 0, "total_grades": 0}
        stats_lock = threading.Lock()
        writer = DBWriterThread(write_queue, 1, stats, stats_lock)

        with caplog.at_level(logging.WARNING):
            writer._keepalive()

        assert "non-Hrana" in caplog.text
        assert "connection refused" in caplog.text
        mock_session.close.assert_called_once()

    @patch("college_ai.scraping.niche_scraper.reset_engine")
    @patch("college_ai.scraping.niche_scraper.is_hrana_error", return_value=True)
    @patch("college_ai.scraping.niche_scraper.get_session")
    def test_hrana_error_resets_engine(self, mock_get_session, mock_is_hrana, mock_reset):
        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("stream not found")
        mock_get_session.return_value = mock_session

        write_queue = queue.Queue(maxsize=50)
        stats = {"total_points": 0, "total_grades": 0}
        stats_lock = threading.Lock()
        writer = DBWriterThread(write_queue, 1, stats, stats_lock)
        writer._keepalive()

        mock_reset.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 4: capture_cookies() — ValueError/OSError fallback
# ---------------------------------------------------------------------------

class TestCaptureCookiesStdinErrors:
    """capture_cookies() must not raise on ValueError/OSError from select.select."""

    def setup_method(self):
        shutdown_event.clear()

    def teardown_method(self):
        shutdown_event.clear()

    def _make_scraper_with_mock_browser(self):
        scraper = NicheScraper.__new__(NicheScraper)
        scraper._intercepted_data = []
        scraper._cookies_path = "/tmp/test_niche_cookies.json"
        scraper.context = MagicMock()
        scraper.page = None
        scraper.browser = None
        scraper._playwright = None
        scraper._response_handler = None
        return scraper

    @patch("college_ai.scraping.niche_scraper.sync_playwright")
    @patch("college_ai.scraping.niche_scraper.select.select",
           side_effect=ValueError("bad file descriptor"))
    def test_value_error_falls_back(self, mock_select, mock_pw):
        """ValueError from select.select should not propagate — returns False."""
        # Set shutdown immediately so the 60s fallback exits fast
        shutdown_event.set()

        mock_pl = MagicMock()
        mock_pw.return_value.start.return_value = mock_pl
        mock_browser = MagicMock()
        mock_pl.chromium.launch.return_value = mock_browser
        mock_ctx = MagicMock()
        mock_browser.new_context.return_value = mock_ctx
        mock_page = MagicMock()
        mock_ctx.new_page.return_value = mock_page

        scraper = self._make_scraper_with_mock_browser()
        result = scraper.capture_cookies()
        assert result is False

    @patch("college_ai.scraping.niche_scraper.sync_playwright")
    @patch("college_ai.scraping.niche_scraper.select.select",
           side_effect=OSError("bad fd"))
    def test_os_error_falls_back(self, mock_select, mock_pw):
        """OSError from select.select should not propagate — returns False."""
        shutdown_event.set()

        mock_pl = MagicMock()
        mock_pw.return_value.start.return_value = mock_pl
        mock_browser = MagicMock()
        mock_pl.chromium.launch.return_value = mock_browser
        mock_ctx = MagicMock()
        mock_browser.new_context.return_value = mock_ctx
        mock_page = MagicMock()
        mock_ctx.new_page.return_value = mock_page

        scraper = self._make_scraper_with_mock_browser()
        result = scraper.capture_cookies()
        assert result is False


# ---------------------------------------------------------------------------
# capture_cookies() closes existing playwright before starting capture
# ---------------------------------------------------------------------------

class TestCaptureCookiesClosesExistingPlaywright:
    """capture_cookies() must call self.close() before sync_playwright().start()
    to avoid 'using Playwright Sync API inside the asyncio loop' error."""

    def setup_method(self):
        shutdown_event.clear()

    def teardown_method(self):
        shutdown_event.clear()

    @patch("college_ai.scraping.niche_scraper.sync_playwright")
    def test_close_called_before_new_playwright(self, mock_pw):
        """self.close() must be called before sync_playwright().start()."""
        mock_pl = MagicMock()
        mock_pw.return_value.start.return_value = mock_pl
        mock_browser = MagicMock()
        mock_pl.chromium.launch.return_value = mock_browser
        mock_ctx = MagicMock()
        mock_browser.new_context.return_value = mock_ctx
        mock_page = MagicMock()
        mock_ctx.new_page.return_value = mock_page
        # Set shutdown when pg.goto is called (after self.close(), during
        # capture browser setup) so the stdin polling loop exits immediately.
        mock_page.goto.side_effect = lambda *a, **kw: shutdown_event.set()

        scraper = NicheScraper.__new__(NicheScraper)
        scraper._intercepted_data = []
        scraper._cookies_path = "/tmp/test_niche_cookies.json"
        scraper._cached_grades = None
        scraper._cached_grades_slug = None
        scraper._response_handler = None
        # Simulate a live playwright instance (as during normal scraping)
        orig_page = MagicMock()
        orig_context = MagicMock()
        orig_browser = MagicMock()
        orig_pw = MagicMock()
        scraper.page = orig_page
        scraper.context = orig_context
        scraper.browser = orig_browser
        scraper._playwright = orig_pw

        result = scraper.capture_cookies()

        # self.close() nulled out the existing browser resources
        assert scraper.page is None
        assert scraper._playwright is None
        # Original playwright stop was called (via close())
        orig_pw.stop.assert_called_once()
        assert result is False  # shutdown cancelled the capture

    @patch("college_ai.scraping.niche_scraper.sync_playwright")
    def test_capture_browser_launch_failure_returns_false(self, mock_pw):
        """If the capture browser fails to launch, return False (don't raise)."""
        mock_pw.return_value.start.side_effect = RuntimeError("event loop conflict")

        scraper = NicheScraper.__new__(NicheScraper)
        scraper._intercepted_data = []
        scraper._cookies_path = "/tmp/test_niche_cookies.json"
        scraper._cached_grades = None
        scraper._cached_grades_slug = None
        scraper._response_handler = None
        scraper.page = None
        scraper.context = None
        scraper.browser = None
        scraper._playwright = None

        result = scraper.capture_cookies()
        assert result is False


# ---------------------------------------------------------------------------
# Fix 5: ENGINE alias removed
# ---------------------------------------------------------------------------

class TestEngineAliasRemoved:
    """The deprecated ENGINE module alias must not exist."""

    def test_engine_alias_not_exported(self):
        import college_ai.db.connection as conn
        assert not hasattr(conn, "ENGINE"), (
            "ENGINE module alias should be removed — "
            "use get_engine() instead to avoid stale references"
        )
