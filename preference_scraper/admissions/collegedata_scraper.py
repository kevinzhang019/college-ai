"""
CollegeData.com Admissions Tracker scraper.

Scrapes applicant scatter plot data (GPA/SAT of applicants) from
CollegeData's authenticated API using browser-based cookie auth.

How the API works:
  - Auth: POST /api/auth/login returns accessToken
  - The accessToken is stored as a 'cd_auth' cookie (URL-encoded JSON)
  - All subsequent API calls MUST send the cd_auth cookie (not a Bearer header)
  - Endpoint: GET /api/admissionstracker/id/{slug}
  - Response structure:
      {
        "id": 781, "slug": "Stanford-University", "chance": 0.2978,
        "whereYouStand": {
          "axisX": {"description": "GPA Unweighted"},
          "axisY": {"description": "SAT Score"},
          "data": [{"xData": 4.0, "yData": 1540, "color": "#4574b2"}, ...]
        }
      }
  - xData = GPA (unweighted), yData = SAT score
  - All data points are "applied" (no accept/reject color coding)

Prerequisites:
    1. Register a free account at https://www.collegedata.com/sign-up
    2. Add to .env:
         COLLEGEDATA_EMAIL=your@email.com
         COLLEGEDATA_PASSWORD=yourpassword

Usage:
    python -m preference_scraper.admissions.collegedata_scraper
    python -m preference_scraper.admissions.collegedata_scraper --school "stanford-university"
    python -m preference_scraper.admissions.collegedata_scraper --reset-empty
    python -m preference_scraper.admissions.collegedata_scraper --debug --school "stanford-university"
"""

import os
import time
import json
import logging
import argparse
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests
from camoufox.sync_api import Camoufox

from preference_scraper.admissions.db import get_session, init_db
from preference_scraper.admissions.models import School, ApplicantDatapoint, ScrapeJob

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://www.collegedata.com"
AUTH_URL = f"{BASE_URL}/api/auth/login"
TRACKER_URL = f"{BASE_URL}/api/admissionstracker/id"
COOKIES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "crawlers", "playwright_cookies", "collegedata.json"
)

REQUEST_DELAY = 2.0  # seconds between API calls

# Single-filter variants to get different population slices
FILTER_VARIANTS = [
    {},                             # base (all applicants)
    {"decisionType": "early"},
    {"decisionType": "regular"},
    {"gender": "F"},
    {"gender": "M"},
]


class CollegeDataSession:
    """Manages the cd_auth cookie used by CollegeData's API."""

    def __init__(self):
        self.email = os.getenv("COLLEGEDATA_EMAIL", "")
        self.password = os.getenv("COLLEGEDATA_PASSWORD", "")
        if not self.email or not self.password:
            raise RuntimeError(
                "COLLEGEDATA_EMAIL and COLLEGEDATA_PASSWORD must be set in .env. "
                "Register free at https://www.collegedata.com/sign-up"
            )
        self._cd_auth: Optional[str] = None
        self._token_expiry: float = 0

    def _save_cookie(self, cd_auth: str):
        os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
        with open(COOKIES_PATH, "w") as f:
            json.dump({"cd_auth": cd_auth}, f)

    def _load_cookie(self) -> Optional[str]:
        if not os.path.exists(COOKIES_PATH):
            return None
        try:
            with open(COOKIES_PATH, "r") as f:
                data = json.load(f)
            return data.get("cd_auth")
        except Exception:
            return None

    def _token_is_valid(self, cd_auth: str) -> bool:
        """Check whether the token embedded in cd_auth is still valid."""
        try:
            decoded = urllib.parse.unquote(cd_auth)
            data = json.loads(decoded)
            access_token = data.get("accessToken", "")
            expire_date = data.get("expireDate", "")
            if expire_date:
                exp = datetime.fromisoformat(expire_date.replace("Z", "+00:00"))
                # Add 60s buffer
                return exp.timestamp() > time.time() + 60
        except Exception:
            pass
        return True  # Assume valid if we can't parse expiry

    def _login_via_browser(self):
        """Use camoufox to authenticate and capture the cd_auth cookie."""
        logger.info("Authenticating with CollegeData via browser...")
        with Camoufox(headless=True) as browser:
            context = browser.new_context()
            page = context.new_page()

            page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=60_000)
            time.sleep(2)

            page.fill('input[type="email"], input[name="email"]', self.email)
            page.fill('input[type="password"], input[name="password"]', self.password)
            page.click('button[type="submit"]')
            time.sleep(4)

            cookies = context.cookies()
            cd_auth = next(
                (c["value"] for c in cookies if c["name"] == "cd_auth"), None
            )

        if not cd_auth:
            raise RuntimeError(
                "CollegeData login failed — no cd_auth cookie set. "
                "Check COLLEGEDATA_EMAIL and COLLEGEDATA_PASSWORD in .env."
            )

        self._cd_auth = cd_auth
        self._save_cookie(cd_auth)
        logger.info("CollegeData authentication successful. Cookie saved.")

    def get_cookie(self) -> str:
        """Return a valid cd_auth cookie, refreshing if expired."""
        # Try saved cookie first
        if not self._cd_auth:
            self._cd_auth = self._load_cookie()

        if self._cd_auth and self._token_is_valid(self._cd_auth):
            return self._cd_auth

        # Need fresh login
        self._login_via_browser()
        return self._cd_auth

    def get_request_cookies(self) -> dict:
        return {"cd_auth": self.get_cookie()}


def _parse_datapoints(
    school_id: int,
    data: dict,
) -> list[dict]:
    """Extract applicant data points from an admissions tracker API response.

    The API returns applicant GPA + SAT data (no accept/reject outcomes).
    All points are stored with outcome='applied'.
    """
    where_you_stand = data.get("whereYouStand", {})
    if not where_you_stand:
        return []

    points = where_you_stand.get("data", [])
    if not points:
        return []

    now = datetime.now(timezone.utc).isoformat()
    results = []

    for point in points:
        # Skip the "You" focus point (it's the user's own profile, not a real applicant)
        if point.get("isFocus"):
            continue

        gpa = point.get("xData")   # xData = GPA Unweighted
        sat = point.get("yData")   # yData = SAT Score

        if gpa is None or sat is None:
            continue

        results.append(dict(
            school_id=school_id,
            source="collegedata",
            gpa=float(gpa),
            sat_score=float(sat),
            outcome="applied",   # API doesn't distinguish accept/reject
            scraped_at=now,
        ))

    return results


def _dedup_datapoints(all_points: list[dict]) -> list[dict]:
    """Deduplicate points across filter variants."""
    seen: dict[tuple, dict] = {}
    for dp in all_points:
        key = (dp["school_id"], dp["gpa"], dp["sat_score"])
        if key not in seen:
            seen[key] = dp
    return list(seen.values())


def _get_school_slug_map(session) -> dict[str, int]:
    """Build a mapping from likely CollegeData slugs to school IDs."""
    schools = session.query(School.id, School.name).all()
    slug_map = {}
    for school_id, name in schools:
        slug = name.lower().strip()
        for char in ["&", "'", ",", ".", "(", ")", "/"]:
            slug = slug.replace(char, "")
        slug = slug.replace(" - ", "-").replace("  ", " ").replace(" ", "-")
        slug_map[slug] = school_id
    return slug_map


def scrape_school(
    slug: str,
    school_id: int,
    session_auth: CollegeDataSession,
    session,
) -> int:
    """Scrape all admissions tracker data for a single school.

    Returns:
        > 0: number of unique datapoints stored
        0  : API responded but returned 0 datapoints
        -1 : school not found (404)
    """
    all_points: list[dict] = []

    for filter_params in FILTER_VARIANTS:
        url = f"{TRACKER_URL}/{slug}"
        try:
            resp = requests.get(
                url,
                params=filter_params,
                cookies=session_auth.get_request_cookies(),
                headers={"Accept": "application/json"},
                timeout=15,
            )

            logger.debug(
                f"  {slug} filter={filter_params or 'base'}: "
                f"status={resp.status_code}"
            )

            if resp.status_code in (404, 500):
                # 404 = school not in CollegeData
                # 500 = school exists but has no tracker data (CollegeData returns 500 for this)
                logger.debug(f"  {slug}: no tracker data ({resp.status_code})")
                return -1

            if resp.status_code == 401:
                # Cookie expired — re-auth and retry once
                session_auth._cd_auth = None
                resp = requests.get(
                    url,
                    params=filter_params,
                    cookies=session_auth.get_request_cookies(),
                    headers={"Accept": "application/json"},
                    timeout=15,
                )

            resp.raise_for_status()

            data = resp.json()
            points = _parse_datapoints(school_id, data)
            all_points.extend(points)
            logger.debug(
                f"  {slug} [{filter_params or 'base'}]: {len(points)} points"
            )

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 500):
                return -1
            logger.warning(f"  {slug} filter={filter_params}: HTTP {e}")
        except Exception as e:
            logger.warning(f"  {slug} filter={filter_params}: {e}")

        time.sleep(REQUEST_DELAY)

    if not all_points:
        return 0

    unique_points = _dedup_datapoints(all_points)

    for dp in unique_points:
        session.add(ApplicantDatapoint(**dp))
    session.commit()

    return len(unique_points)


def scrape_all(
    slugs: Optional[list[str]] = None,
    resume: bool = True,
):
    """Scrape admissions tracker data for all schools."""
    init_db()
    session = get_session()
    session_auth = CollegeDataSession()

    if slugs is None:
        slug_map = _get_school_slug_map(session)
    else:
        slug_map_full = _get_school_slug_map(session)
        slug_map = {s: slug_map_full.get(s, 0) for s in slugs}

    total_schools = len(slug_map)
    total_points = 0
    done_count = 0

    logger.info(f"Starting CollegeData scrape for {total_schools} schools...")

    try:
        for i, (slug, school_id) in enumerate(slug_map.items()):
            if resume:
                job = session.query(ScrapeJob).filter_by(
                    source="collegedata", school_slug=slug
                ).first()
                if job and job.status in ("done", "not_found"):
                    done_count += 1
                    continue

            logger.info(f"[{i+1}/{total_schools}] Scraping {slug} ...")

            job = session.query(ScrapeJob).filter_by(
                source="collegedata", school_slug=slug
            ).first()
            if not job:
                job = ScrapeJob(source="collegedata", school_slug=slug, status="pending")
                session.add(job)
                session.commit()

            try:
                count = scrape_school(slug, school_id, session_auth, session)
                now = datetime.now(timezone.utc).isoformat()

                if count > 0:
                    job.status = "done"
                    total_points += count
                    logger.info(f"  -> {count} unique datapoints")
                elif count == -1:
                    job.status = "not_found"
                    logger.debug(f"  -> not found in CollegeData")
                else:
                    job.status = "no_data"
                    logger.warning(
                        f"  -> API responded but returned 0 datapoints for {slug} "
                        f"(will retry on next run)"
                    )

                job.last_attempt = now
                job.error = None
                session.commit()

            except Exception as e:
                job.status = "failed"
                job.last_attempt = datetime.now(timezone.utc).isoformat()
                job.error = str(e)[:500]
                session.commit()
                logger.error(f"  -> FAILED: {e}")

    except KeyboardInterrupt:
        logger.info("Interrupted. Progress saved — rerun to resume.")
    finally:
        session.close()

    logger.info(
        f"Done. {total_points} total datapoints scraped. "
        f"({done_count} schools previously completed, skipped.)"
    )


def reset_no_data_jobs():
    """Reset all 'done' jobs with 0 datapoints back to 'pending'."""
    from sqlalchemy import text
    init_db()
    session = get_session()
    result = session.execute(text("""
        UPDATE scrape_jobs
        SET status = 'pending'
        WHERE source = 'collegedata'
          AND status = 'done'
          AND school_slug NOT IN (
              SELECT DISTINCT s.name
              FROM applicant_datapoints a
              JOIN schools s ON a.school_id = s.id
              WHERE a.source = 'collegedata'
          )
    """))
    session.commit()
    logger.info(f"Reset {result.rowcount} 'done' CollegeData jobs that had 0 datapoints.")
    session.close()


def main():
    parser = argparse.ArgumentParser(description="Scrape CollegeData admissions tracker")
    parser.add_argument(
        "--school", type=str, default=None,
        help="Scrape a single school by slug (e.g. 'stanford-university')"
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Re-scrape all schools, ignoring previous progress"
    )
    parser.add_argument(
        "--reset-empty", action="store_true",
        help="Reset previously 'done' jobs with 0 datapoints back to pending, then exit"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging for API response inspection"
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.reset_empty:
        reset_no_data_jobs()
        return

    if args.school:
        scrape_all(slugs=[args.school], resume=not args.no_resume)
    else:
        scrape_all(resume=not args.no_resume)


if __name__ == "__main__":
    main()
