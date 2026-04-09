#!/usr/bin/env python3
"""
Count URLs per school that were ingested with the OLD hardcoded 512-token chunker.

Detection signature (matches college_ai/scraping/crawler.py rechunk logic):
    A URL is "legacy-chunked" iff it has >= 2 chunks AND every chunk except
    the final one has exactly 512 tokens (per the text-embedding-3-small
    tokenizer). Single-chunk URLs cannot be distinguished and are excluded.

Usage:
    python scripts/count_legacy_chunked_urls.py
    python scripts/count_legacy_chunked_urls.py --college "Harvard University"
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


def count_legacy_for_college(
    collection: Collection, college_name: str, enc
) -> Tuple[int, int, int]:
    """
    Returns (legacy_url_count, multi_chunk_url_count, total_url_count) for the school.
    """
    url_chunk_tokens: Dict[str, List[int]] = defaultdict(list)

    safe = college_name.replace('"', '\\"')
    expr = f'college_name == "{safe}"'

    iterator = collection.query_iterator(
        expr=expr,
        output_fields=["url_canonical", "content"],
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
                content = rec.get("content") or ""
                url_chunk_tokens[key].append(len(enc.encode(content)))
    except Exception as exc:
        try:
            iterator.close()
        except Exception:
            pass
        print(f"  ✗ Error iterating chunks for {college_name}: {exc}")
        return 0, 0, 0

    total_urls = len(url_chunk_tokens)
    multi_chunk = 0
    legacy = 0
    for counts in url_chunk_tokens.values():
        if len(counts) >= 2:
            multi_chunk += 1
            if all(c == 512 for c in counts[:-1]):
                legacy += 1
    return legacy, multi_chunk, total_urls


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
        description="Count URLs per school that were chunked with the legacy 512-token chunker.",
    )
    parser.add_argument(
        "--college",
        type=str,
        default=None,
        help="Optional single college name to process",
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

    print("\n🚀 Counting legacy 512-token chunked URLs per school")
    print("=" * 60)
    print(f"Colleges to scan: {len(college_names)}\n")

    per_school: List[Tuple[str, int, int, int]] = []
    total_legacy = 0
    total_multi = 0
    total_urls = 0
    schools_with_legacy = 0

    for i, name in enumerate(college_names, 1):
        legacy, multi, urls = count_legacy_for_college(collection, name, enc)
        per_school.append((name, legacy, multi, urls))
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
    for name, legacy, multi, urls in sorted(per_school, key=lambda r: -r[1]):
        if legacy > 0:
            print(f"  {name}: {legacy:,} legacy  ({multi:,} multi / {urls:,} total)")

    print("\n" + "=" * 60)
    print(f"Total legacy URLs (all schools):        {total_legacy:,}")
    print(f"Total multi-chunk URLs (all schools):   {total_multi:,}")
    print(f"Total URLs scanned (all schools):       {total_urls:,}")
    print(f"Schools with legacy URLs:               {schools_with_legacy:,} / {len(college_names):,}")


if __name__ == "__main__":
    main()
