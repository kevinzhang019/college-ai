"""Match URLs in general.csv against colleges.csv.

For each URL in general.csv, find a matching URL in colleges.csv using a
normalized comparison (scheme, leading `www.`, and trailing slash stripped).
When matched rows have differing names, write `general_name, colleges_name, url`
to an output CSV. URLs from general.csv that cannot be matched are printed to
stdout at the end.
"""
from __future__ import annotations

import csv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COLLEGES_DIR = REPO_ROOT / "college_ai" / "scraping" / "colleges"
GENERAL_CSV = COLLEGES_DIR / "general.csv"
COLLEGES_CSV = COLLEGES_DIR / "colleges.csv"
OUTPUT_CSV = COLLEGES_DIR / "matched_names.csv"


def normalize_url(url: str) -> str:
    u = (url or "").strip().lower()
    for scheme in ("https://", "http://"):
        if u.startswith(scheme):
            u = u[len(scheme):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def load_colleges(path: Path) -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = normalize_url(row["url"])
            if key and key not in mapping:
                mapping[key] = (row["name"].strip(), row["url"].strip())
    return mapping


def main() -> None:
    colleges = load_colleges(COLLEGES_CSV)

    diffs: list[tuple[str, str, str]] = []
    unmatched: list[str] = []

    with GENERAL_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_url = row["url"].strip()
            key = normalize_url(raw_url)
            general_name = row["name"].strip()
            if not key:
                continue
            match = colleges.get(key)
            if match is None:
                unmatched.append(raw_url)
                continue
            colleges_name, colleges_url = match
            if general_name.casefold() != colleges_name.casefold():
                diffs.append((general_name, colleges_name, colleges_url))

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["general_name", "colleges_name", "url"])
        writer.writerows(diffs)

    print(f"Wrote {len(diffs)} differing-name rows to {OUTPUT_CSV}")
    print(f"\nUnmatched URLs ({len(unmatched)}):")
    for u in unmatched:
        print(u)


if __name__ == "__main__":
    main()
