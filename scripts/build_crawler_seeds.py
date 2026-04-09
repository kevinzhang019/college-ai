"""Build a crawler seed CSV from the Turso schools table.

Pulls the top N schools by student_size and writes name + identity_url in
csv format (header: name,url). Also reports duplicate URLs.

Usage:
    python scripts/build_crawler_seeds.py                  # default: 1000 schools -> colleges.csv
    python scripts/build_crawler_seeds.py --limit 500
    python scripts/build_crawler_seeds.py --limit 2000 --output college_ai/scraping/colleges/general_top2k.csv
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

# Make project root importable
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)

from sqlalchemy import text  # noqa: E402

from college_ai.db.connection import get_session  # noqa: E402

DEFAULT_OUTPUT = os.path.join(
    _ROOT, "college_ai", "scraping", "colleges", "colleges.csv"
)

QUERY = text(
    """
    SELECT name, identity_url
    FROM schools
    WHERE identity_url IS NOT NULL
      AND TRIM(identity_url) != ''
      AND student_size IS NOT NULL
    ORDER BY student_size DESC
    LIMIT :limit
    """
)


def normalize_url(url: str) -> str:
    return url.strip().lower().rstrip("/")


def build_seeds(limit: int, output_path: str) -> None:
    session = get_session()
    try:
        rows = session.execute(QUERY, {"limit": limit}).all()
    finally:
        session.close()

    print(f"Fetched {len(rows)} rows from schools table (limit={limit})")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "url"])
        for name, url in rows:
            writer.writerow([name, url])

    print(f"Wrote {output_path}")

    # Duplicate URL detection
    groups = defaultdict(list)
    for name, url in rows:
        groups[normalize_url(url)].append((name, url))

    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    if not duplicates:
        print("\nNo duplicate URLs found.")
        return

    print(f"\nFound {len(duplicates)} duplicate URL group(s):\n")
    for key, entries in duplicates.items():
        print(f"  [{key}]")
        for name, url in entries:
            print(f"    - {name}  ->  {url}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=1000,
        help="Number of schools to fetch, ordered by student_size desc (default: 1000)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    build_seeds(args.limit, args.output)


if __name__ == "__main__":
    main()
