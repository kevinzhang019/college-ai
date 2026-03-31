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
    python -m preference_scraper.admissions.niche_scraper
    python -m preference_scraper.admissions.niche_scraper --school "stanford-university"
    python -m preference_scraper.admissions.niche_scraper --grades-only
    python -m preference_scraper.admissions.niche_scraper --school "stanford-university" --headful  # first run
"""

import os
import re
import json
import time
import random
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page

from preference_scraper.admissions.db import get_session, init_db
from preference_scraper.admissions.models import (
    School, ApplicantDatapoint, NicheGrade, ScrapeJob,
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
            os.path.dirname(__file__), "..", "crawlers", "playwright_cookies", "niche.json"
        )
        self._intercepted_data: list[dict] = []
        self._px_blocked: bool = False  # set True when PerimeterX blocks a page

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

    def capture_cookies(self):
        """Open Chrome so the user can log in to Niche and solve PerimeterX.

        PerimeterX fingerprints are tied to the browser engine — capture and
        scrape must use the same engine (Chrome) for cookies to work.
        Called automatically mid-scrape when PX starts blocking.
        """
        logger.info("\n" + "="*60)
        logger.info("ACTION REQUIRED: Chrome is opening for cookie capture")
        logger.info("  1. Log in to Niche if prompted")
        logger.info("  2. Browse to a few college pages until they load normally")
        logger.info("  3. Press ENTER in this terminal when done")
        logger.info("="*60)

        # Use the existing playwright instance if one is running (avoid double-init)
        pl = self._playwright
        owns_pl = False
        if pl is None:
            pl = sync_playwright().start()
            owns_pl = True

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
        try:
            input("\n  >>> Press ENTER after you've logged in and a college page loaded... ")
        except EOFError:
            logger.info("Non-interactive mode — waiting 60s for manual interaction...")
            time.sleep(60)
        cookies = ctx.cookies()
        browser.close()

        if owns_pl:
            pl.stop()

        os.makedirs(os.path.dirname(self._cookies_path), exist_ok=True)
        with open(self._cookies_path, "w") as f:
            json.dump(cookies, f)
        logger.info(f"Saved {len(cookies)} cookies.")

        if self.context:
            try:
                self.context.add_cookies(cookies)
            except Exception:
                pass

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
        with open(self._cookies_path, "w") as f:
            json.dump(cookies, f)
        logger.info("Niche login successful. Cookies saved.")

    def _setup_network_intercept(self):
        """Intercept XHR/Fetch responses to capture scatter plot API data."""
        self._intercepted_data = []

        # Remove previous listener to avoid duplicates across multiple calls
        if hasattr(self, "_response_handler"):
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

        # Recursively find all plot objects in the response
        plots_found = []

        def find_plots(obj, depth=0):
            if depth > 15:
                return
            if isinstance(obj, dict):
                # Check if this dict has a "plot" with "points"
                plot = obj.get("plot")
                if isinstance(plot, dict) and "points" in plot and "units" in plot:
                    plots_found.append(plot)
                # Also check "scatterplot" key
                scatter = obj.get("scatterplot")
                if isinstance(scatter, dict):
                    sp = scatter.get("plot")
                    if isinstance(sp, dict) and "points" in sp:
                        plots_found.append(sp)
                # Recurse into all values (handles both dict and list buckets)
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        find_plots(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        find_plots(item, depth + 1)

        find_plots(blocks)

        for plot in plots_found:
            raw_points = plot.get("points", [])
            units = plot.get("units", [])
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

            # Decision outcome mapping from attributeValues[0]
            decision_map = {}
            if attr_values and isinstance(attr_values[0], list):
                for idx, label in enumerate(attr_values[0]):
                    ll = str(label).lower()
                    if "accept" in ll or "admit" in ll:
                        decision_map[idx] = "accepted"
                    elif "reject" in ll or "deny" in ll or "denied" in ll:
                        decision_map[idx] = "rejected"
                    elif "wait" in ll or "defer" in ll or "consider" in ll:
                        decision_map[idx] = "waitlisted"

            logger.debug(
                f"  BlockScatterplot: {len(raw_points)} raw points, "
                f"units={units}, decisions={decision_map}"
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

                # Determine outcome from attributes[0] (decision index)
                outcome = None
                if attrs and attrs[0] is not None:
                    outcome = decision_map.get(int(attrs[0]))

                if outcome and 0 < gpa <= 4.0 and sat > 0:
                    points.append({
                        "gpa": gpa,
                        "sat_score": float(sat),
                        "act_score": None,
                        "outcome": outcome,
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

        if gpa is not None and (sat is not None or act is not None) and outcome:
            try:
                return {
                    "gpa": float(gpa),
                    "sat_score": float(sat) if sat else None,
                    "act_score": float(act) if act else None,
                    "outcome": outcome,
                }
            except (ValueError, TypeError):
                return None
        return None

    def scrape_scattergram(self, slug: str) -> list[dict]:
        """Scrape scattergram data points for a single school.

        Returns list of dicts with keys: gpa, sat_score, act_score, outcome.
        """
        self._setup_network_intercept()

        url = f"{COLLEGE_URL}/{slug}/admissions/"
        logger.info(f"  Loading scattergram: {url}")

        try:
            self.page.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
            time.sleep(3)  # Extra wait for async chart data
        except Exception as e:
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

        # --- Primary: __PRELOADED_STATE__ (embedded in page HTML) ---
        datapoints = []
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

        # --- Secondary: blocks API (intercepted /api/profile/{uuid}/blocks/) ---
        if not datapoints:
            for intercepted in self._intercepted_data:
                if "/blocks/" in intercepted["url"] or "/api/profile/" in intercepted["url"]:
                    parsed = self._parse_blocks_scatter(intercepted["data"])
                    datapoints.extend(parsed)

        # --- Tertiary: generic intercepted JSON responses ---
        if not datapoints:
            for intercepted in self._intercepted_data:
                parsed = self._parse_scatter_response(intercepted["data"])
                datapoints.extend(parsed)

        # --- Try scrolling to chart to trigger lazy load ---
        if not datapoints:
            chart_selectors = [
                '[data-testid*="scatter"]',
                '.scatterplot',
                '.scatterplot-chart__canvas',
                'canvas',
            ]
            for sel in chart_selectors:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        el.scroll_into_view_if_needed()
                        time.sleep(3)
                        break
                except Exception:
                    continue

            # Re-check intercepted data after scroll
            for intercepted in self._intercepted_data:
                if "/blocks/" in intercepted["url"] or "/api/profile/" in intercepted["url"]:
                    parsed = self._parse_blocks_scatter(intercepted["data"])
                    datapoints.extend(parsed)
                else:
                    parsed = self._parse_scatter_response(intercepted["data"])
                    datapoints.extend(parsed)

        # --- Fallback: DOM parsing ---
        if not datapoints:
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
        """'$18,000' | 18000 -> 18000"""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val).strip().replace('$', '').replace(',', '').replace('+', '').split('/')[0]
        try:
            return int(float(s))
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
        """Last-resort regex extraction of stats from raw HTML."""
        stats: dict = {}
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
            m = re.search(pattern, html)
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
        """
        url = f"{COLLEGE_URL}/{slug}/"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            time.sleep(3)  # Allow JS to hydrate
        except Exception as e:
            logger.warning(f"  Failed to load {slug} grades page: {e}")
            return {}

        grades = {}
        stats = {}
        html = None

        # --- Primary: __NEXT_DATA__ (most reliable for Next.js sites) ---
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
        except Exception as e:
            logger.debug(f"  __NEXT_DATA__ extraction failed for {slug}: {e}")

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

    def restart(self, headless: bool = False, grades_only: bool = False):
        """Reload saved cookies into a fresh Chrome context."""
        logger.info("Restarting Chrome with saved cookies...")
        self.close()
        time.sleep(random.uniform(2, 4))
        self.start(headless=headless, grades_only=grades_only)

    def close(self):
        """Clean up browser resources."""
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.debug(f"Browser cleanup error (safe to ignore): {e}")
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None


def _get_slug_from_name(name: str) -> str:
    """Convert school name to Niche URL slug."""
    slug = name.lower().strip()
    for char in ["'", ",", ".", "(", ")", "&", "/"]:
        slug = slug.replace(char, "")
    slug = slug.replace(" - ", "-").replace("  ", " ").replace(" ", "-")
    return slug


def scrape_all(
    slugs: Optional[list[str]] = None,
    grades_only: bool = False,
    resume: bool = True,
    headless: bool = False,  # headless is blocked by PerimeterX; default to headful
):
    """Scrape Niche data for all schools.

    Args:
        slugs: Optional specific school slugs. If None, uses all schools in DB.
        grades_only: If True, only scrape letter grades (no scattergrams).
        resume: If True, skip schools already marked 'done' in scrape_jobs.
    """
    init_db()
    session = get_session()

    # Build slug -> school_id mapping
    schools = session.query(School.id, School.name).all()
    if slugs:
        slug_map = {}
        for slug in slugs:
            # Try to find matching school
            for sid, name in schools:
                if _get_slug_from_name(name) == slug:
                    slug_map[slug] = sid
                    break
            else:
                slug_map[slug] = 0
    else:
        slug_map = {_get_slug_from_name(name): sid for sid, name in schools}

    total = len(slug_map)
    total_points = 0
    total_grades = 0
    consecutive_px_blocks = 0

    scraper = NicheScraper()
    try:
        scraper.start(headless=headless, grades_only=grades_only)
        logger.info(f"Starting Niche scrape for {total} schools...")

        for i, (slug, school_id) in enumerate(slug_map.items()):
            source_tag = "niche_grades" if grades_only else "niche"

            # Check resume — skip only confirmed-good scrapes
            if resume:
                job = session.query(ScrapeJob).filter_by(
                    source=source_tag, school_slug=slug
                ).first()
                if job and job.status == "done":
                    continue

            logger.info(f"[{i+1}/{total}] Scraping {slug} ...")

            job = session.query(ScrapeJob).filter_by(
                source=source_tag, school_slug=slug
            ).first()
            if not job:
                job = ScrapeJob(source=source_tag, school_slug=slug, status="pending")
                session.add(job)
                session.commit()

            try:
                now = datetime.now(timezone.utc).isoformat()
                scraper._px_blocked = False  # reset flag before each page

                # Scrape scattergram data
                if not grades_only:
                    points = scraper.scrape_scattergram(slug)
                    for p in points:
                        session.add(ApplicantDatapoint(
                            school_id=school_id,
                            source="niche",
                            gpa=p["gpa"],
                            sat_score=p.get("sat_score"),
                            act_score=p.get("act_score"),
                            outcome=p["outcome"],
                            scraped_at=now,
                        ))
                    total_points += len(points)
                    logger.info(f"  -> {len(points)} scattergram points")

                # Scrape grades
                grades = scraper.scrape_grades(slug)

                # Handle PerimeterX block: refresh cookies + restart browser
                if scraper._px_blocked:
                    consecutive_px_blocks += 1
                    if consecutive_px_blocks >= PX_RESTART_AFTER:
                        logger.warning(
                            f"\nPerimeterX blocked. Refreshing cookies and restarting browser..."
                        )
                        # capture_cookies saves new cookies AND reloads into context
                        scraper.capture_cookies()
                        # Fully restart browser so PX gets a clean session
                        scraper.restart(headless=headless, grades_only=grades_only)
                        consecutive_px_blocks = 0
                        scraper._px_blocked = False
                        grades = scraper.scrape_grades(slug)
                else:
                    consecutive_px_blocks = 0

                if grades and school_id:
                    existing = session.get(NicheGrade, school_id)
                    if existing:
                        for k, v in grades.items():
                            setattr(existing, k, v)
                        existing.updated_at = now
                    else:
                        session.add(NicheGrade(
                            school_id=school_id,
                            updated_at=now,
                            **grades,
                        ))
                    total_grades += 1
                    grade_count = sum(1 for k in grades if k in GRADE_LABEL_MAP.values())
                    stat_count = len(grades) - grade_count
                    logger.info(f"  -> {grade_count} grades, {stat_count} stats")
                elif school_id:
                    logger.warning(f"  -> No grades extracted for {slug} — marking as no_data")

                if grades:
                    job.status = "done"
                else:
                    job.status = "no_data"
                job.last_attempt = now
                job.error = None
                session.commit()

            except Exception as e:
                job.status = "failed"
                job.last_attempt = datetime.now(timezone.utc).isoformat()
                job.error = str(e)[:500]
                session.commit()
                logger.error(f"  -> FAILED: {e}")

            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    except KeyboardInterrupt:
        logger.info("Interrupted. Progress saved — rerun to resume.")
    finally:
        scraper.close()
        session.close()

    logger.info(
        f"Done. {total_points} scattergram points, {total_grades} schools with grades."
    )


def reset_no_data_jobs():
    """Reset all 'done' jobs that have 0 niche_grades back to 'pending'.

    Run this once after a broken scrape so those schools get retried.
    """
    from sqlalchemy import text
    init_db()
    session = get_session()
    for source_tag in ("niche", "niche_grades"):
        result = session.execute(text(f"""
            UPDATE scrape_jobs
            SET status = 'pending'
            WHERE source = '{source_tag}'
              AND status IN ('done', 'no_data')
              AND school_slug NOT IN (
                  SELECT ng.school_id
                  FROM niche_grades ng
                  WHERE ng.academics IS NOT NULL
              )
        """))
        logger.info(
            f"Reset {result.rowcount} '{source_tag}' jobs with 0 grade data."
        )
    session.commit()
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
        help="Reset previously 'done'/'no_data' jobs with 0 grades back to pending, then exit"
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
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.reset_empty:
        reset_no_data_jobs()
        return

    if args.capture_cookies:
        scraper = NicheScraper()
        scraper.capture_cookies()
        return

    # headless is blocked by PerimeterX; only use if explicitly requested
    headless = args.headless  # Default False; only True when --headless is passed
    if args.school:
        scrape_all(slugs=[args.school], grades_only=args.grades_only, resume=not args.no_resume, headless=headless)
    else:
        scrape_all(grades_only=args.grades_only, resume=not args.no_resume, headless=headless)


if __name__ == "__main__":
    main()
