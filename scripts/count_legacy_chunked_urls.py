#!/usr/bin/env python3
"""
Count (and optionally delete) URLs per school that were ingested with the OLD
hardcoded 512-token chunker.

Detection signature (matches college_ai/scraping/crawler.py rechunk logic):
    A URL is "legacy-chunked" iff it has >= 2 chunks AND every chunk except
    the final one has exactly 512 tokens (per the text-embedding-3-small
    tokenizer). Single-chunk URLs cannot be distinguished and are excluded.

Usage:
    # Read-only audit (default)
    python scripts/count_legacy_chunked_urls.py
    python scripts/count_legacy_chunked_urls.py --college "Harvard University"

    # Delete mode — removes ALL chunks for each legacy URL atomically.
    # Each URL's chunks are deleted in a single collection.delete(expr="id in [...]")
    # call, which Milvus processes as one MutationRequest (all-or-nothing).
    python scripts/count_legacy_chunked_urls.py --delete
    python scripts/count_legacy_chunked_urls.py --delete --yes   # skip prompt
"""

import os
import sys
import csv
import glob
import argparse
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from dotenv import load_dotenv
from pymilvus import connections, Collection, utility

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(PROJECT_ROOT)

from college_ai.scraping.config import *  # noqa: F401,F403
from college_ai.rag.embeddings import _ensure_tokenizer


def load_environment_variables() -> None:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def connect_to_milvus() -> None:
    try:
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)  # type: ignore[name-defined]
        print("✓ Connected to Zilliz Cloud")
    except Exception as exc:
        print(f"✗ Failed to connect to Zilliz Cloud: {exc}")
        raise


def get_collection() -> Collection:
    name = ZILLIZ_COLLECTION_NAME  # type: ignore[name-defined]
    if not utility.has_collection(name):
        raise RuntimeError(f"Collection '{name}' not found")
    print(f"✓ Found collection: {name}")
    return Collection(name)


def get_college_names_from_csvs() -> Set[str]:
    """Collect college names from the seed CSVs under college_ai/scraping/colleges."""
    colleges_dir = os.path.join(
        PROJECT_ROOT, "college_ai", "scraping", "colleges"
    )
    names: Set[str] = set()
    for path in glob.glob(os.path.join(colleges_dir, "*.csv")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("name") or row.get("college_name") or "").strip()
                    if name:
                        names.add(name)
        except Exception as exc:
            print(f"⚠️  Failed to read CSV {path}: {exc}")
    return names


def scan_legacy_for_college(
    collection: Collection, college_name: str, enc
) -> Tuple[int, int, int, Dict[str, List[str]]]:
    """
    Scan a school's chunks and identify legacy 512-token-chunked URLs.

    Returns:
        (legacy_url_count, multi_chunk_url_count, total_url_count, legacy_url_ids)

    legacy_url_ids maps each legacy canonical URL -> list of chunk PK ids.
    """
    # url_canonical -> list of (id, token_count)
    url_chunk_data: Dict[str, List[Tuple[str, int]]] = defaultdict(list)

    safe = college_name.replace('"', '\\"')
    expr = f'college_name == "{safe}"'

    iterator = collection.query_iterator(
        expr=expr,
        output_fields=["id", "url_canonical", "content"],
        batch_size=256,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                iterator.close()
                break
            for rec in batch:
                key = (rec.get("url_canonical") or "").strip()
                if not key:
                    continue
                rec_id = str(rec.get("id") or "")
                if not rec_id:
                    continue
                content = rec.get("content") or ""
                url_chunk_data[key].append((rec_id, len(enc.encode(content))))
    except Exception as exc:
        try:
            iterator.close()
        except Exception:
            pass
        print(f"  ✗ Error iterating chunks for {college_name}: {exc}")
        return 0, 0, 0, {}

    total_urls = len(url_chunk_data)
    multi_chunk = 0
    legacy_url_ids: Dict[str, List[str]] = {}
    for key, entries in url_chunk_data.items():
        if len(entries) >= 2:
            multi_chunk += 1
            counts = [c for _, c in entries]
            if all(c == 512 for c in counts[:-1]):
                legacy_url_ids[key] = [rid for rid, _ in entries]

    return len(legacy_url_ids), multi_chunk, total_urls, legacy_url_ids


def delete_url_atomically(
    collection: Collection, canonical_url: str, ids: List[str]
) -> Tuple[bool, int]:
    """
    Delete every chunk for a single URL in ONE delete() call.

    Milvus processes a single collection.delete(expr=...) as one MutationRequest
    -- either all matched entities are marked deleted, or none are. This is our
    per-URL atomicity guarantee (confirmed via pymilvus docs).

    Returns (success, deleted_count).
    """
    if not ids:
        return True, 0
    # Escape any embedded double quotes in the PK strings (IDs are project-generated
    # UUID-ish tokens, but be defensive).
    quoted = ",".join('"' + _id.replace('"', '\\"') + '"' for _id in ids)
    expr = f"id in [{quoted}]"
    try:
        result = collection.delete(expr)
        # MutationResult.delete_count reflects what Milvus acknowledged.
        reported = getattr(result, "delete_count", None)
        if reported is None:
            reported = len(ids)
        return True, int(reported)
    except Exception as exc:
        print(f"    ✗ Delete failed for {canonical_url}: {exc}")
        return False, 0


def main() -> None:
    load_environment_variables()
    connect_to_milvus()
    collection = get_collection()

    try:
        collection.load()
        utility.wait_for_loading_complete(collection.name)
        print("✓ Collection loaded")
    except Exception as exc:
        print(f"✗ Failed to load collection: {exc}")
        raise

    parser = argparse.ArgumentParser(
        description="Count (and optionally delete) URLs per school that were chunked with the legacy 512-token chunker.",
    )
    parser.add_argument(
        "--college",
        type=str,
        default=None,
        help="Optional single college name to process",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete all chunks belonging to legacy-chunked URLs. "
             "Each URL's chunks are removed in a single atomic Milvus delete() call.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt before deleting.",
    )
    args = parser.parse_args()

    enc = _ensure_tokenizer("text-embedding-3-small")

    college_names = sorted(get_college_names_from_csvs())
    if not college_names:
        print("No colleges found in CSVs under college_ai/scraping/colleges; nothing to analyze.")
        return

    if args.college:
        college_names = [c for c in college_names if c == args.college]
        if not college_names:
            print(f"No matching college found for filter: {args.college}")
            return

    mode = "SCAN + DELETE" if args.delete else "SCAN (read-only)"
    print(f"\n🚀 Counting legacy 512-token chunked URLs per school — {mode}")
    print("=" * 60)
    print(f"Colleges to scan: {len(college_names)}\n")

    # Phase 1: scan every school, collect per-URL chunk IDs for any legacy hits.
    per_school_plan: List[Tuple[str, int, int, int, Dict[str, List[str]]]] = []
    total_legacy = 0
    total_multi = 0
    total_urls = 0
    schools_with_legacy = 0

    for i, name in enumerate(college_names, 1):
        legacy, multi, urls, legacy_ids = scan_legacy_for_college(collection, name, enc)
        per_school_plan.append((name, legacy, multi, urls, legacy_ids))
        total_legacy += legacy
        total_multi += multi
        total_urls += urls
        if legacy > 0:
            schools_with_legacy += 1

        print(
            f"[{i:>4}/{len(college_names)}] 📊 {name}: "
            f"{legacy:,} legacy  ({multi:,} multi-chunk / {urls:,} total URLs)"
        )

    print("\n" + "=" * 60)
    print("📈 OVERALL — schools with legacy URLs (sorted by count)")
    print("=" * 60)
    for name, legacy, multi, urls, _ in sorted(per_school_plan, key=lambda r: -r[1]):
        if legacy > 0:
            print(f"  {name}: {legacy:,} legacy  ({multi:,} multi / {urls:,} total)")

    print("\n" + "=" * 60)
    print(f"Total legacy URLs (all schools):        {total_legacy:,}")
    print(f"Total multi-chunk URLs (all schools):   {total_multi:,}")
    print(f"Total URLs scanned (all schools):       {total_urls:,}")
    print(f"Schools with legacy URLs:               {schools_with_legacy:,} / {len(college_names):,}")

    if not args.delete:
        return

    # Phase 2: delete.
    if total_legacy == 0:
        print("\nNothing to delete.")
        return

    total_chunks_to_delete = sum(
        sum(len(ids) for ids in ids_map.values())
        for _, _, _, _, ids_map in per_school_plan
    )
    print("\n" + "=" * 60)
    print(f"⚠️  About to delete {total_chunks_to_delete:,} chunks across "
          f"{total_legacy:,} legacy URLs in {schools_with_legacy:,} schools.")
    print("   Each URL is deleted in one atomic Milvus delete() call.")
    print("=" * 60)

    if not args.yes:
        try:
            confirm = input("Type 'DELETE' to proceed: ").strip()
        except EOFError:
            confirm = ""
        if confirm != "DELETE":
            print("Aborted.")
            return

    deleted_urls = 0
    deleted_chunks = 0
    failed_urls = 0

    for name, legacy, _, _, ids_map in per_school_plan:
        if not ids_map:
            continue
        print(f"\n🗑  {name} — deleting {legacy:,} legacy URLs")
        for canonical_url, ids in ids_map.items():
            ok, count = delete_url_atomically(collection, canonical_url, ids)
            if ok:
                deleted_urls += 1
                deleted_chunks += count
                print(f"    ✓ {canonical_url} → {count} chunks")
            else:
                failed_urls += 1

    print("\n" + "=" * 60)
    print("🧹 DELETE SUMMARY")
    print("=" * 60)
    print(f"URLs deleted:        {deleted_urls:,} / {total_legacy:,}")
    print(f"Chunks deleted:      {deleted_chunks:,} / {total_chunks_to_delete:,}")
    if failed_urls:
        print(f"URLs failed:         {failed_urls:,} (safe to re-run; each URL's delete is atomic)")


if __name__ == "__main__":
    main()
