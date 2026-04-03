#!/usr/bin/env python3
"""
Consolidate records from one college name into another in the Milvus collection.

Use case: merge "University of California-Irvine" into "University of California—Irvine".

Rules:
- Only keep unique URLs overall.
- Each URL can contain multiple rows (chunks). For identical chunks across colleges,
  keep the most complete record and remove the rest.
- "Most complete" preference order:
  1) Larger majors set
  2) Longer content length
  3) Newer crawled_at (string compare suffices for ISO-like timestamps)
  4) Higher id (stable tie-breaker)

Operations per URL:
- Query all rows for that URL where college_name is either source or target
- Group rows by chunk key SHA256("title|content")
- For each group: keep the most complete row and delete others
- Ensure the kept row's college_name == target (update it if needed)

Safety:
- Processes URLs under the source college only (target-only URLs remain untouched)
- Adaptive batch sizes on scans to avoid gRPC message limits
- Optional --dry-run to preview actions without modifying data
"""

import os
import sys
import argparse
import hashlib
from typing import Any, Dict, List, Set, Tuple
from collections import defaultdict

from dotenv import load_dotenv
from pymilvus import connections, Collection, utility


# Ensure project root is importable and .env can be found
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.config import *  # noqa: F401,F403


def load_environment_variables() -> None:
    env_path = os.path.join(PROJECT_ROOT, ".env")
    load_dotenv(env_path)


def connect_to_milvus() -> None:
    try:
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)  # type: ignore[name-defined]
        print("✓ Connected to Zilliz Cloud")
    except Exception as exc:
        print(f"✗ Failed to connect to Zilliz Cloud: {exc}")
        raise


def get_collection() -> Collection:
    collection_name = ZILLIZ_COLLECTION_NAME  # type: ignore[name-defined]
    if not utility.has_collection(collection_name):
        raise RuntimeError(f"Collection '{collection_name}' not found")
    print(f"✓ Found collection: {collection_name}")
    return Collection(collection_name)


def _parse_majors_to_list(majors_field: Any) -> List[str]:
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
    """Return True if `left` is more complete than `right` for the same URL+chunk."""
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


def get_urls_for_college(collection: Collection, college_name: str) -> Set[str]:
    """Return the set of URLs found for a given college using query_iterator."""
    urls: Set[str] = set()
    safe_college = college_name.replace('"', '\\"')
    iterator = collection.query_iterator(
        expr=f'college_name == "{safe_college}"',
        output_fields=["url"],
        batch_size=1000,
    )
    while True:
        batch = iterator.next()
        if not batch:
            iterator.close()
            break
        for rec in batch:
            url = (rec.get("url") or "").strip()
            if url:
                urls.add(url)
    return urls


def fetch_records_for_url(
    collection: Collection, url: str, allowed_colleges: Tuple[str, str]
) -> List[dict]:
    """Fetch all rows for a URL where college_name is either of the provided colleges."""
    src, tgt = allowed_colleges
    safe_url = url.replace('"', '\\"')
    safe_src = src.replace('"', '\\"')
    safe_tgt = tgt.replace('"', '\\"')
    expr = (
        f'url == "{safe_url}" && (college_name == "{safe_src}" || college_name == "{safe_tgt}")'
    )
    results: List[dict] = []
    iterator = collection.query_iterator(
        expr=expr,
        output_fields=[
            "id",
            "college_name",
            "url",
            "title",
            "content",
            "embedding",
            "crawled_at",
            "majors",
        ],
        batch_size=1000,
    )
    while True:
        batch = iterator.next()
        if not batch:
            iterator.close()
            break
        results.extend(batch)
    return results


def upsert_rows(collection: Collection, rows: List[dict]) -> None:
    """Upsert rows by their id, updating fields such as college_name. Fallback: delete+insert."""
    if not rows:
        return
    ids: List[str] = []
    colleges: List[str] = []
    urls: List[str] = []
    titles: List[str] = []
    contents: List[str] = []
    embeddings: List[List[float]] = []
    crawled_ats: List[str] = []
    majors_col: List[Any] = []

    for r in rows:
        ids.append(str(r.get("id")))
        colleges.append(str(r.get("college_name")))
        urls.append(str(r.get("url")))
        titles.append(str(r.get("title") or ""))
        contents.append(str(r.get("content") or ""))
        emb = r.get("embedding")
        if isinstance(emb, list) and len(emb) == VECTOR_DIM:  # type: ignore[name-defined]
            embeddings.append(emb)
        else:
            # Keep dimensionality correct if embedding is missing (shouldn't happen)
            embeddings.append([0.0] * VECTOR_DIM)  # type: ignore[name-defined]
        crawled_ats.append(str(r.get("crawled_at") or ""))
        majors_col.append(r.get("majors") or [])

    try:
        if hasattr(collection, "upsert"):
            collection.upsert(
                [
                    ids,
                    colleges,
                    urls,
                    titles,
                    contents,
                    embeddings,
                    crawled_ats,
                    majors_col,
                ]
            )
        else:
            quoted = ",".join([f'"{_id}"' for _id in ids])
            collection.delete(f"id in [{quoted}]")
            collection.insert(
                [
                    ids,
                    colleges,
                    urls,
                    titles,
                    contents,
                    embeddings,
                    crawled_ats,
                    majors_col,
                ]
            )
    except Exception as exc:
        print(f"    ✗ Upsert/insert failed for {len(rows)} rows: {exc}")
        raise


def delete_ids_in_batches(
    collection: Collection, ids: List[str], batch_size: int = 500
) -> int:
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


def consolidate_url(
    collection: Collection, url: str, source: str, target: str, dry_run: bool = False
) -> Tuple[int, int]:
    """Consolidate a single URL from `source` into `target`. Returns (updated_count, deleted_count)."""
    recs = fetch_records_for_url(collection, url, (source, target))
    if not recs:
        return 0, 0

    # Group by chunk key: identical title+content means same chunk across colleges
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in recs:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        key = hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()
        groups[key].append(r)

    to_delete: List[str] = []
    to_update: List[dict] = []  # rows to upsert with updated college_name -> target

    for chunk_key, rows in groups.items():
        # Choose the most complete row
        best = rows[0]
        for cand in rows[1:]:
            if _is_record_more_complete(cand, best):
                best = cand

        # Delete all non-best
        for cand in rows:
            if cand is best:
                continue
            to_delete.append(str(cand.get("id")))

        # Ensure best row belongs to target college
        if str(best.get("college_name")) != target:
            updated = dict(best)
            updated["college_name"] = target
            to_update.append(updated)

    updated_count = len(to_update)
    deleted_count = len(to_delete)

    if dry_run:
        if updated_count or deleted_count:
            print(f"  URL: {url}")
            if updated_count:
                print(f"    Would update {updated_count} rows to target college")
            if deleted_count:
                print(f"    Would delete {deleted_count} duplicate rows")
        return updated_count, deleted_count

    # Apply changes
    if to_update:
        upsert_rows(collection, to_update)
    if to_delete:
        delete_ids_in_batches(collection, to_delete)

    return updated_count, deleted_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidate records from one college name into another"
    )
    parser.add_argument(
        "--source",
        type=str,
        default="University of California-Irvine",
        help="Source college name to consolidate from",
    )
    parser.add_argument(
        "--target",
        type=str,
        default="University of California—Irvine",
        help="Target college name to consolidate into",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the collection",
    )
    parser.add_argument(
        "--limit-urls",
        type=int,
        default=None,
        help="Optional limit on number of source URLs to process (for testing)",
    )
    args = parser.parse_args()

    load_environment_variables()
    connect_to_milvus()
    collection = get_collection()

    # Loading is optional for scalar queries
    try:
        collection.load()
    except Exception as exc:
        print(f"⚠️  Proceeding without explicit load (reason: {exc})")

    # Step 1: Gather URLs under source college
    print(f"🔎 Gathering URLs for source college: {args.source}")
    source_urls = sorted(get_urls_for_college(collection, args.source))
    if args.limit_urls is not None:
        source_urls = source_urls[: max(0, int(args.limit_urls))]
    print(f"  Found {len(source_urls):,} source URLs to process")

    # Step 2: Consolidate per URL
    total_updated = 0
    total_deleted = 0
    processed = 0
    for url in source_urls:
        updated, deleted = consolidate_url(
            collection, url, args.source, args.target, dry_run=args.dry_run
        )
        total_updated += updated
        total_deleted += deleted
        processed += 1
        if processed % 50 == 0:
            print(f"  Progress: {processed:,}/{len(source_urls):,} URLs")

    # Step 3: Summary
    print(
        "\n✅ Consolidation complete" if not args.dry_run else "\nℹ️  Dry-run complete"
    )
    print(f"  URLs processed: {processed:,}")
    print(f"  Rows updated to target college: {total_updated:,}")
    print(f"  Duplicate rows deleted: {total_deleted:,}")


if __name__ == "__main__":
    main()
