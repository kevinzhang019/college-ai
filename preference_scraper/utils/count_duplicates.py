#!/usr/bin/env python3
"""
Count and print duplicate records within each college by URL.

Definitions:
- A single URL can legitimately have multiple rows because content is chunked.
- We consider rows to be duplicates when they belong to the same URL AND have
  identical content (same title + content pair). These typically come from
  repeated crawls inserting the same chunk again.

For each college, we print:
- Total records
- Unique URLs
- Number of URLs that contain any duplicates
- Total duplicate records (sum over all URLs of (count_per_chunk - 1))
- Per-URL duplicate counts (only URLs that actually have duplicates)
"""

import os
import sys
import csv
import glob
import hashlib
import argparse
from collections import defaultdict
from typing import Any, Dict, List, Tuple, Set
from urllib.parse import urlparse

from dotenv import load_dotenv
from pymilvus import connections, Collection, utility

# Ensure project root is importable and .env can be found
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from preference_scraper.crawlers.config import *  # noqa: F401,F403


def load_environment_variables() -> None:
    """Load environment variables from the project root .env file if present."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    load_dotenv(env_path)


def connect_to_milvus() -> None:
    """Connect to Zilliz/Milvus using credentials from config."""
    try:
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)  # type: ignore[name-defined]
        print("✓ Connected to Zilliz Cloud")
    except (
        Exception
    ) as exc:  # pragma: no cover - connection issues are runtime-specific
        print(f"✗ Failed to connect to Zilliz Cloud: {exc}")
        raise


def get_collection() -> Collection:
    """Get the configured collection from Zilliz/Milvus."""
    collection_name = ZILLIZ_COLLECTION_NAME  # type: ignore[name-defined]
    if not utility.has_collection(collection_name):
        raise RuntimeError(f"Collection '{collection_name}' not found")
    print(f"✓ Found collection: {collection_name}")
    return Collection(collection_name)


def get_college_names_from_csvs() -> Set[str]:
    """Collect a set of college names from the seed CSVs under crawlers/colleges."""
    colleges_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../crawlers/colleges")
    )
    names: Set[str] = set()
    for path in glob.glob(os.path.join(colleges_dir, "*.csv")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("name") or "").strip()
                    if name:
                        names.add(name)
        except Exception as exc:
            print(f"⚠️  Failed to read CSV {path}: {exc}")
            continue
    return names


def _parse_majors_to_list(majors_field: Any) -> List[str]:
    """Normalize the majors field (JSON) into a flat list of strings."""
    if majors_field is None:
        return []
    if isinstance(majors_field, list):
        return [str(m).strip() for m in majors_field if str(m).strip()]
    if isinstance(majors_field, dict):
        values = majors_field.get("list") or majors_field.get("values")
        if isinstance(values, list):
            return [str(m).strip() for m in values if str(m).strip()]
    try:
        text = str(majors_field)
        if "," in text:
            return [part.strip() for part in text.split(",") if part.strip()]
    except Exception:
        pass
    return []


def _is_record_more_complete(left: dict, right: dict) -> bool:
    """
    Decide if `left` is more complete than `right` for the same URL+chunk.

    Priority:
    1) Larger majors set size
    2) Longer content length
    3) Newer crawled_at (string compare as tie-breaker)
    4) Higher id (stable final tie-breaker)
    """
    left_majors = set(_parse_majors_to_list(left.get("majors")))
    right_majors = set(_parse_majors_to_list(right.get("majors")))
    if len(left_majors) != len(right_majors):
        return len(left_majors) > len(right_majors)

    left_content_len = len((left.get("content") or ""))
    right_content_len = len((right.get("content") or ""))
    if left_content_len != right_content_len:
        return left_content_len > right_content_len

    left_crawled = str(left.get("crawled_at") or "")
    right_crawled = str(right.get("crawled_at") or "")
    if left_crawled != right_crawled:
        return left_crawled > right_crawled

    return str(left.get("id") or "") > str(right.get("id") or "")


def compute_duplicates_for_college_streaming(
    collection: Collection, college_name: str
) -> Tuple[Dict[str, int], int, int, int, List[str]]:
    """
    Stream records for a college in small batches and compute duplicate counts by URL.

    Returns:
        - url_to_duplicate_count: map of url -> total duplicates for that URL
        - unique_url_count: number of distinct URLs observed
        - total_duplicate_records: sum of duplicates across all URLs
        - total_records: total rows scanned for the college
        - ids_to_delete: ids of duplicate rows to delete, keeping the most complete per URL+chunk
    """
    # Aggregate counts of identical chunks per URL using SHA256 of title|content
    url_to_hash_counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    # Track the best (most complete) record for each (url, chunk-key) and duplicates to delete
    best_record_for_key: Dict[Tuple[str, str], dict] = {}
    ids_to_delete: List[str] = []
    ids_to_delete_by_url: Dict[str, List[str]] = defaultdict(list)

    total_records = 0
    offset = 0
    batch_size = 512  # start conservatively to avoid large gRPC frames

    while True:
        try:
            batch = collection.query(
                expr=f'college_name == "{college_name}"',
                output_fields=["id", "url", "title", "content", "majors", "crawled_at"],
                limit=batch_size,
                offset=offset,
            )
        except Exception as exc:
            msg = str(exc)
            if "received message larger than max" in msg or "RESOURCE_EXHAUSTED" in msg:
                # Reduce the batch size and retry the same offset
                new_batch_size = max(16, batch_size // 2)
                if new_batch_size == batch_size:
                    raise
                print(
                    f"⚠️  gRPC message too large at batch_size={batch_size}. Retrying with {new_batch_size}"
                )
                batch_size = new_batch_size
                continue
            else:
                raise

        if not batch:
            break

        for record in batch:
            url = (record.get("url") or "").strip()
            if not url:
                continue
            # Canonical key for grouping: ignore scheme, strip leading 'www.', drop trailing slash (non-root)
            try:
                parsed = urlparse(url)
                netloc = parsed.netloc.lower()
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                path = parsed.path or ""
                if path.endswith("/") and len(path) > 1:
                    path = path.rstrip("/")
                canonical_url = f"{netloc}{path}"
                if parsed.query:
                    canonical_url += f"?{parsed.query}"
            except Exception:
                s = url.strip()
                if s.startswith("http://"):
                    s = s[len("http://") :]
                elif s.startswith("https://"):
                    s = s[len("https://") :]
                canonical_url = s[4:] if s.lower().startswith("www.") else s
            title = (record.get("title") or "").strip()
            content = (record.get("content") or "").strip()
            dedupe_key = hashlib.sha256(
                f"{title}|{content}".encode("utf-8")
            ).hexdigest()
            url_to_hash_counts[canonical_url][dedupe_key] += 1
            composite_key = (canonical_url, dedupe_key)
            existing_best = best_record_for_key.get(composite_key)
            if existing_best is None:
                best_record_for_key[composite_key] = record
            else:
                if _is_record_more_complete(record, existing_best):
                    prev_id = str(existing_best.get("id") or "")
                    if prev_id:
                        ids_to_delete.append(prev_id)
                        ids_to_delete_by_url[canonical_url].append(prev_id)
                    best_record_for_key[composite_key] = record
                else:
                    cur_id = str(record.get("id") or "")
                    if cur_id:
                        ids_to_delete.append(cur_id)
                        ids_to_delete_by_url[canonical_url].append(cur_id)
            total_records += 1

        if len(batch) < batch_size:
            break
        offset += batch_size

    # Convert to counts using the actual ids we plan to delete per URL
    url_to_duplicate_count: Dict[str, int] = {}
    total_duplicate_records = 0
    for canonical_url, delete_ids in ids_to_delete_by_url.items():
        if delete_ids:
            url_to_duplicate_count[canonical_url] = len(delete_ids)
            total_duplicate_records += len(delete_ids)

    unique_url_count = len(url_to_hash_counts)
    return (
        url_to_duplicate_count,
        unique_url_count,
        total_duplicate_records,
        total_records,
        ids_to_delete,
    )


def _delete_ids_in_batches(
    collection: Collection, ids: List[str], batch_size: int = 500
) -> int:
    """Delete records by id in batches. Returns the number of records removed."""
    removed = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        if not batch:
            continue
        quoted = ",".join([f'"{_id}"' for _id in batch])
        expr = f"id in [{quoted}]"
        try:
            collection.delete(expr)
            removed += len(batch)
        except Exception as exc:
            print(f"    ✗ Error deleting batch of {len(batch)}: {exc}")
    return removed


def compute_duplicates_by_url(records: List[dict]) -> Tuple[Dict[str, int], int, int]:
    """
    Compute duplicate counts per URL.

    A duplicate is an extra occurrence of the same content chunk for the same URL.
    We identify identical chunks using SHA256 of "title|content".

    Returns:
        - url_to_duplicate_count: map of url -> total duplicates for that URL
        - unique_url_count: number of distinct URLs observed
        - total_duplicate_records: sum of duplicates across all URLs
    """
    url_to_hash_counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for record in records:
        url = (record.get("url") or "").strip()
        if not url:
            continue
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            path = parsed.path or ""
            if path.endswith("/") and len(path) > 1:
                path = path.rstrip("/")
            canonical_url = f"{netloc}{path}"
            if parsed.query:
                canonical_url += f"?{parsed.query}"
        except Exception:
            s = url.strip()
            if s.startswith("http://"):
                s = s[len("http://") :]
            elif s.startswith("https://"):
                s = s[len("https://") :]
            canonical_url = s[4:] if s.lower().startswith("www.") else s
        title = (record.get("title") or "").strip()
        content = (record.get("content") or "").strip()
        dedupe_key = hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()
        url_to_hash_counts[canonical_url][dedupe_key] += 1

    url_to_duplicate_count: Dict[str, int] = {}
    total_duplicate_records = 0
    for url, hash_counts in url_to_hash_counts.items():
        duplicates_for_url = 0
        for count in hash_counts.values():
            if count > 1:
                duplicates_for_url += count - 1
        if duplicates_for_url > 0:
            url_to_duplicate_count[url] = duplicates_for_url
            total_duplicate_records += duplicates_for_url

    return url_to_duplicate_count, len(url_to_hash_counts), total_duplicate_records


def main() -> None:
    load_environment_variables()
    connect_to_milvus()
    collection = get_collection()

    # Loading the collection is optional for scalar queries; try and continue if it fails
    try:
        collection.load()
    except Exception as exc:
        print(f"⚠️  Proceeding without explicit load (reason: {exc})")

    parser = argparse.ArgumentParser(
        description="Count and optionally remove duplicate records by URL per college",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove duplicates (keep most complete per URL+chunk)",
    )
    parser.add_argument(
        "--college",
        type=str,
        default=None,
        help="Optional single college name to process",
    )
    args = parser.parse_args()

    college_names = sorted(get_college_names_from_csvs())
    if not college_names:
        print("No colleges found in CSVs under crawlers/colleges; nothing to analyze.")
        return

    print("\n🚀 Counting duplicates by URL per college")
    print("=" * 60)
    print(f"Colleges discovered: {len(college_names)}\n")

    overall_total_records = 0
    overall_total_duplicate_records = 0
    overall_colleges_with_duplicates = 0

    target_colleges = college_names
    if args.college:
        target_colleges = [c for c in college_names if c == args.college]
        if not target_colleges:
            print(f"No matching college found for filter: {args.college}")
            return

    for college_name in target_colleges:
        print(f"📊 {college_name}")
        (
            url_to_duplicate_count,
            unique_url_count,
            total_duplicate_records,
            total_records,
            ids_to_delete,
        ) = compute_duplicates_for_college_streaming(collection, college_name)
        overall_total_records += total_records

        duplicate_url_count = len(url_to_duplicate_count)
        if duplicate_url_count > 0:
            overall_colleges_with_duplicates += 1

        overall_total_duplicate_records += total_duplicate_records

        print(f"  Total records: {total_records:,}")
        print(f"  Unique URLs: {unique_url_count:,}")
        print(f"  URLs with duplicates: {duplicate_url_count:,}")
        print(f"  Duplicate records: {total_duplicate_records:,}")

        if duplicate_url_count > 0:
            print("  Duplicates by URL:")
            for url, dup_count in sorted(
                url_to_duplicate_count.items(), key=lambda kv: kv[1], reverse=True
            ):
                print(f"    - {url} -> {dup_count} duplicate rows")
        else:
            print("  No duplicates found for this college.")

        if args.remove and ids_to_delete:
            print(
                f"  Removing {len(ids_to_delete):,} duplicate rows (keeping most complete per URL+chunk)..."
            )
            removed = _delete_ids_in_batches(collection, ids_to_delete)
            print(f"  ✓ Removed {removed:,} rows")

        print("")

    print("=" * 60)
    print("📈 OVERALL")
    print("=" * 60)
    print(f"Total records (all colleges): {overall_total_records:,}")
    print(
        f"Total duplicate records (all colleges): {overall_total_duplicate_records:,}"
    )
    print(
        f"Colleges with any duplicates: {overall_colleges_with_duplicates:,} / {len(target_colleges):,}"
    )


if __name__ == "__main__":
    main()
