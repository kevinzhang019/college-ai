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
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional

from camoufox.sync_api import Camoufox

from preference_scraper.admissions.db import get_session, init_db
from preference_scraper.admissions.models import (
    School, ApplicantDatapoint, NicheGrade, ScrapeJob,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

NICHE_BASE = "https://www.niche.com"
LOGIN_URL = f"{NICHE_BASE}/account/sign-in/"
COLLEGE_URL = f"{NICHE_BASE}/colleges"

REQUEST_DELAY = 4.0  # seconds between page loads
NAV_TIMEOUT = 60_000  # ms

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


class NicheScraper:
    """Camoufox-based Niche.com scraper with Cloudflare bypass."""

    def __init__(self):
        self.email = os.getenv("NICHE_EMAIL", "")
        self.password = os.getenv("NICHE_PASSWORD", "")
        if not self.email or not self.password:
            raise RuntimeError(
                "NICHE_EMAIL and NICHE_PASSWORD must be set in .env. "
                "Register free at https://www.niche.com/account/sign-up/"
            )
        self._camoufox = None
        self.browser = None
        self.context = None
        self.page = None
        self._cookies_path = os.path.join(
            os.path.dirname(__file__), "..", "crawlers", "playwright_cookies", "niche.json"
        )
        self._intercepted_data: list[dict] = []

    def capture_cookies(self):
        """Open a visible browser so the user can solve PerimeterX challenge and log in.

        After the user logs in, cookies are saved to disk for future headless runs.
        Call this once before running headless scrapes.
        """
        logger.info("Opening browser for manual cookie capture...")
        logger.info("Steps:")
        logger.info("  1. Solve any 'Access denied' / bot challenge that appears")
        logger.info("  2. Log in to Niche with your credentials")
        logger.info("  3. Navigate to any college page (e.g. Stanford)")
        logger.info("  4. Press ENTER here to save cookies and close browser")

        self._camoufox = Camoufox(headless=False)
        self.browser = self._camoufox.__enter__()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

        self.page.goto(f"{NICHE_BASE}/colleges/stanford-university/", timeout=NAV_TIMEOUT)
        input("\n  >>> Press ENTER after you have logged in and see the Stanford page... ")

        cookies = self.context.cookies()
        os.makedirs(os.path.dirname(self._cookies_path), exist_ok=True)
        with open(self._cookies_path, "w") as f:
            json.dump(cookies, f)
        logger.info(f"Saved {len(cookies)} cookies to {self._cookies_path}")

        self.close()

    def start(self, headless: bool = True):
        """Launch camoufox browser and authenticate.

        Args:
            headless: Run headless. Requires saved cookies from capture_cookies().
                      Set False for initial login when PerimeterX blocks headless mode.
        """
        self._camoufox = Camoufox(headless=headless)
        self.browser = self._camoufox.__enter__()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

        # Load saved cookies (required to bypass PerimeterX)
        if os.path.exists(self._cookies_path):
            try:
                with open(self._cookies_path, "r") as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} saved Niche cookies.")
            except Exception as e:
                logger.warning(f"Failed to load cookies: {e}")
        else:
            logger.warning(
                f"No saved cookies found at {self._cookies_path}. "
                "Run with --capture-cookies first to bypass PerimeterX."
            )

        # Verify auth or login
        if not self._is_logged_in():
            self._login()

    def _is_logged_in(self) -> bool:
        """Check if we have a valid session."""
        try:
            self.page.goto(f"{NICHE_BASE}/account/", timeout=NAV_TIMEOUT)
            # If redirected to sign-in, we're not logged in
            return "/sign-in" not in self.page.url
        except Exception:
            return False

    def _login(self):
        """Log in to Niche via the sign-in form."""
        logger.info("Logging in to Niche...")
        self.page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)
        time.sleep(2)

        # Fill login form
        self.page.fill('input[name="email"], input[type="email"]', self.email)
        self.page.fill('input[name="password"], input[type="password"]', self.password)
        self.page.click('button[type="submit"]')

        # Wait for navigation
        self.page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        time.sleep(2)

        if "/sign-in" in self.page.url:
            raise RuntimeError("Niche login failed. Check credentials.")

        # Save cookies for reuse
        cookies = self.context.cookies()
        os.makedirs(os.path.dirname(self._cookies_path), exist_ok=True)
        with open(self._cookies_path, "w") as f:
            json.dump(cookies, f)
        logger.info("Niche login successful. Cookies saved.")

    def _setup_network_intercept(self):
        """Intercept XHR/Fetch responses to capture scatter plot API data."""
        self._intercepted_data = []

        def handle_response(response):
            try:
                url = response.url
                content_type = response.headers.get("content-type", "")
                # Look for JSON API responses that might contain scatter data
                if (
                    "application/json" in content_type
                    and response.status == 200
                    and any(kw in url.lower() for kw in [
                        "scatter", "chance", "admissions", "graph", "plot", "data"
                    ])
                ):
                    body = response.json()
                    self._intercepted_data.append({"url": url, "data": body})
            except Exception:
                pass

        self.page.on("response", handle_response)

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

            # Try to interact with the scattergram to trigger data load
            # Look for chart containers
            chart_selectors = [
                '[data-testid*="scatter"]',
                '[class*="scatter"]',
                '[class*="chart"]',
                'canvas',
                'svg',
            ]
            for sel in chart_selectors:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        el.scroll_into_view_if_needed()
                        time.sleep(1)
                        break
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"  Failed to load {slug} scattergram: {e}")
            return []

        # Parse intercepted API data
        datapoints = []
        for intercepted in self._intercepted_data:
            parsed = self._parse_scatter_response(intercepted["data"])
            datapoints.extend(parsed)

        # Fallback: try parsing the page DOM if no API data intercepted
        if not datapoints:
            datapoints = self._parse_scatter_from_dom()

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

    def scrape_grades(self, slug: str) -> dict:
        """Scrape Niche letter grades for a school (public page, no auth needed).

        Returns dict mapping grade category -> letter grade (e.g. {"academics": "A+", ...}).
        """
        url = f"{COLLEGE_URL}/{slug}/"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            time.sleep(3)  # Allow JS to hydrate
        except Exception as e:
            logger.warning(f"  Failed to load {slug} grades page: {e}")
            return {}

        grades = {}

        # --- Primary: __NEXT_DATA__ (most reliable for Next.js sites) ---
        try:
            next_data = self.page.evaluate("""() => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? JSON.parse(el.textContent) : null;
            }""")
            if next_data:
                grades = self._extract_grades_from_next_data(next_data)
                if grades:
                    logger.debug(f"  Extracted grades from __NEXT_DATA__ for {slug}")
                    return grades
        except Exception as e:
            logger.debug(f"  __NEXT_DATA__ extraction failed for {slug}: {e}")

        # --- Secondary: updated CSS selectors ---
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
                return grades
        except Exception as e:
            logger.debug(f"  CSS selector extraction failed for {slug}: {e}")

        # --- Fallback: regex on raw HTML ---
        # Only use on pages that look like a valid Niche college page (not blocked/error pages)
        try:
            html = self.page.content()
            if "Access to this page has been denied" in html or "px-captcha" in html:
                logger.warning(f"  PerimeterX blocked {slug} — run --capture-cookies first")
            else:
                for display_name, field in GRADE_LABEL_MAP.items():
                    # Require grade to be surrounded by non-word chars to avoid false positives
                    pattern = rf'{re.escape(display_name)}[^A-Za-z0-9]{{0,50}}(?<![A-Za-z])([A-F][+-]?)(?![A-Za-z0-9])'
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        grades[field] = match.group(1).upper()
                if grades:
                    logger.debug(f"  Extracted grades from HTML regex for {slug}")
        except Exception as e:
            logger.warning(f"  All grade extraction methods failed for {slug}: {e}")

        return grades

    def close(self):
        """Clean up browser resources."""
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self._camoufox:
                self._camoufox.__exit__(None, None, None)
        except Exception as e:
            logger.debug(f"Browser cleanup error (safe to ignore): {e}")


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
    headless: bool = True,
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

    scraper = NicheScraper()
    try:
        scraper.start(headless=headless)
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
                if grades and school_id:
                    existing = session.query(NicheGrade).get(school_id)
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
                    logger.info(f"  -> {len(grades)} grade categories")
                elif school_id:
                    logger.warning(f"  -> No grades extracted for {slug} — marking as no_data")

                # Only mark "done" if we actually got grades data.
                # "no_data" means selectors found nothing (likely stale); won't
                # be skipped on the next run so we can retry after fixes.
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

            time.sleep(REQUEST_DELAY)

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
        help="Run browser in headful (visible) mode"
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

    headless = not args.headful
    if args.school:
        scrape_all(slugs=[args.school], grades_only=args.grades_only, resume=not args.no_resume, headless=headless)
    else:
        scrape_all(grades_only=args.grades_only, resume=not args.no_resume, headless=headless)


if __name__ == "__main__":
    main()
