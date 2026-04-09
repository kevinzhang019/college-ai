#!/usr/bin/env python3
"""
Consolidate `college_name` values in the Zilliz `colleges` collection based
on a rename mapping CSV.

CSV format:
- Column 0 is `from` (the old name to match on).
- Column 1 is `to` (the new name to write).
- Any additional columns are ignored.
- A header row of literally `from,to` (case-insensitive) is auto-detected
  and skipped. Any other first row is treated as data.
- Default path is `<repo root>/renamings.csv` when `--csv` is not supplied.

For each pair, every Milvus record whose `college_name == from` is rewritten
so that `college_name == to`. All other fields are preserved.

Atomicity:
- Uses Collection.upsert(...) keyed on the record's primary key `id`.
  Milvus upsert is atomic per record: the row never disappears, and every
  other field (including the auto-regenerated BM25 content_sparse) stays
  consistent with the unchanged `content`.
- Rows are upserted in small batches (default 50) to stay well under the
  4MB gRPC message limit given 1536-dim float vectors. On any batch error
  the script fails fast; a re-run is idempotent because completed rows no
  longer match the old `from` name.
- A delete+insert fallback is kept for pymilvus builds that lack upsert.

Usage:
    python scripts/rename_colleges_from_matched_names.py [--csv PATH] [--dry-run] [--verify]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List

from dotenv import load_dotenv
from pymilvus import Collection, connections, utility


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(PROJECT_ROOT)
COLLEGES_DIR = os.path.join(PROJECT_ROOT, "college_ai", "scraping", "colleges")

from college_ai.scraping.config import *  # noqa: F401,F403


DEFAULT_CSV = os.path.join(COLLEGES_DIR, "renamings.csv")

OUTPUT_FIELDS = [
    "id",
    "college_name",
    "url",
    "url_canonical",
    "title",
    "content",
    "embedding",
    "page_type",
    "crawled_at",
]


def load_env() -> None:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def connect() -> Collection:
    connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)  # type: ignore[name-defined]
    name = ZILLIZ_COLLECTION_NAME  # type: ignore[name-defined]
    if not utility.has_collection(name):
        raise RuntimeError(f"Collection '{name}' not found")
    col = Collection(name)
    try:
        col.load(timeout=120)
    except Exception as exc:
        print(f"⚠️  Proceeding without explicit load: {exc}")
    print(f"✓ Connected. Collection: {name}")
    return col


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def fetch_rows_for_name(
    collection: Collection, name: str, query_batch_size: int = 50
) -> List[dict]:
    """Stream rows matching college_name == name.

    batch_size is intentionally small: each record carries a 1536-dim float
    embedding (~6KB) plus content, so pages of 500+ blow past the 4MB gRPC
    receive limit. 50 rows ≈ <1MB per page is safe.
    """
    expr = f'college_name == "{_escape(name)}"'
    rows: List[dict] = []
    it = collection.query_iterator(
        expr=expr, output_fields=OUTPUT_FIELDS, batch_size=query_batch_size
    )
    while True:
        batch = it.next()
        if not batch:
            it.close()
            break
        rows.extend(batch)
    return rows


def count_for_name(collection: Collection, name: str) -> int:
    """Count rows matching college_name == name with Strong consistency.

    Zilliz defaults to Bounded consistency (~seconds of staleness on scalar
    queries), which causes false-positive verify failures on small schools
    whose single upsert batch completes inside the staleness window. Strong
    consistency makes the server wait until all in-flight writes are visible.
    """
    expr = f'college_name == "{_escape(name)}"'
    try:
        res = collection.query(
            expr=expr,
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        if res and isinstance(res, list) and "count(*)" in res[0]:
            return int(res[0]["count(*)"])
    except Exception:
        pass
    # Fallback: iterate ids-only (also Strong to avoid the same stale read)
    total = 0
    it = collection.query_iterator(
        expr=expr,
        output_fields=["id"],
        batch_size=1000,
        consistency_level="Strong",
    )
    while True:
        batch = it.next()
        if not batch:
            it.close()
            break
        total += len(batch)
    return total


def upsert_batch(collection: Collection, rows: List[dict]) -> None:
    """Upsert rows by primary key. Fallback to delete+insert if upsert missing."""
    if not rows:
        return

    ids: List[str] = []
    college_names: List[str] = []
    urls: List[str] = []
    url_canonicals: List[str] = []
    titles: List[str] = []
    contents: List[str] = []
    embeddings: List[List[float]] = []
    page_types: List[str] = []
    crawled_ats: List[str] = []

    for r in rows:
        ids.append(str(r.get("id")))
        college_names.append(str(r.get("college_name")))
        urls.append(str(r.get("url") or ""))
        url_canonicals.append(str(r.get("url_canonical") or ""))
        titles.append(str(r.get("title") or ""))
        contents.append(str(r.get("content") or ""))
        emb = r.get("embedding")
        if isinstance(emb, list) and len(emb) == VECTOR_DIM:  # type: ignore[name-defined]
            embeddings.append(emb)
        else:
            embeddings.append([0.0] * VECTOR_DIM)  # type: ignore[name-defined]
        page_types.append(str(r.get("page_type") or "other"))
        crawled_ats.append(str(r.get("crawled_at") or ""))

    # NOTE: do NOT include content_sparse — BM25 Function regenerates it.
    data = [
        ids,
        college_names,
        urls,
        url_canonicals,
        titles,
        contents,
        embeddings,
        page_types,
        crawled_ats,
    ]

    try:
        if hasattr(collection, "upsert"):
            collection.upsert(data)
        else:
            quoted = ",".join([f'"{_id}"' for _id in ids])
            collection.delete(f"id in [{quoted}]")
            collection.insert(data)
    except Exception as exc:
        print(f"    ✗ Upsert failed for {len(rows)} rows (first id={ids[0]}): {exc}")
        raise


def rename_one(
    collection: Collection,
    from_name: str,
    to_name: str,
    batch_size: int,
    query_batch_size: int,
    dry_run: bool,
    verify: bool,
) -> int:
    rows = fetch_rows_for_name(collection, from_name, query_batch_size=query_batch_size)
    if not rows:
        print(f"  • {from_name!r} → 0 records (skipped)")
        return 0

    print(
        f"  • {from_name!r} → {to_name!r}: {len(rows)} records "
        f"({'DRY' if dry_run else 'applying'})"
    )

    if dry_run:
        return len(rows)

    # Rewrite college_name on every row, then upsert in batches.
    for r in rows:
        r["college_name"] = to_name

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        upsert_batch(collection, chunk)
        print(f"    ↳ upserted {min(i + batch_size, len(rows))}/{len(rows)}")

    if verify:
        remaining = count_for_name(collection, from_name)
        if remaining != 0:
            raise RuntimeError(
                f"Verify failed: {remaining} rows still match {from_name!r}"
            )
        print(f"    ✓ verified 0 rows remain under old name")

    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--csv",
        default=DEFAULT_CSV,
        help=(
            "Path to rename mapping CSV (default: <repo root>/renamings.csv). "
            "Column 0 is the old name, column 1 is the new name, any further "
            "columns are ignored. A header row of 'from,to' is auto-skipped."
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview without writes")
    ap.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Rows per upsert call (default 50, capped at 100)",
    )
    ap.add_argument(
        "--query-batch-size",
        type=int,
        default=50,
        help=(
            "Rows per query_iterator page (default 50). Kept small because "
            "each record has a 1536-dim embedding; pages of 500+ exceed the "
            "4MB gRPC receive limit."
        ),
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="After each school, assert 0 rows still match the old name",
    )
    args = ap.parse_args()

    batch_size = max(1, min(100, int(args.batch_size)))
    query_batch_size = max(1, min(200, int(args.query_batch_size)))

    if not os.path.exists(args.csv):
        print(f"✗ CSV not found: {args.csv}")
        sys.exit(1)

    load_env()
    collection = connect()

    pairs: List[Dict[str, str]] = []
    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader):
            if not row or len(row) < 2:
                continue
            src = row[0].strip()
            dst = row[1].strip()
            if idx == 0 and src.lower() == "from" and dst.lower() == "to":
                continue  # header row
            if not src or not dst or src == dst:
                continue
            pairs.append({"from": src, "to": dst})

    print(f"📄 Loaded {len(pairs)} rename pairs from {args.csv}")
    print(
        f"🔧 batch_size={batch_size} query_batch_size={query_batch_size} "
        f"dry_run={args.dry_run} verify={args.verify}"
    )
    print("")

    total_rows = 0
    skipped = 0
    for pair in pairs:
        try:
            n = rename_one(
                collection,
                pair["from"],
                pair["to"],
                batch_size=batch_size,
                query_batch_size=query_batch_size,
                dry_run=args.dry_run,
                verify=args.verify,
            )
            total_rows += n
            if n == 0:
                skipped += 1
        except Exception as exc:
            print(f"✗ Error renaming {pair['from']!r}: {exc}")
            raise

    print("")
    print("✅ Dry-run complete" if args.dry_run else "✅ Rename complete")
    print(f"  Pairs processed: {len(pairs)}")
    print(f"  Pairs with 0 matches: {skipped}")
    print(f"  Total records {'would be' if args.dry_run else ''} upserted: {total_rows}")


if __name__ == "__main__":
    main()
