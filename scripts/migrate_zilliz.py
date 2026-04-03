#!/usr/bin/env python3
"""
Migrate all data from one Zilliz Cloud instance to another.

Usage:
    Set environment variables for source and destination, then run:

    SOURCE_URI=https://source.zilliz.com \
    SOURCE_API_KEY=your_source_key \
    DEST_URI=https://dest.zilliz.com \
    DEST_API_KEY=your_dest_key \
    python scripts/migrate_zilliz.py

Optional env vars:
    SOURCE_COLLECTION   (default: colleges)
    DEST_COLLECTION     (default: same as SOURCE_COLLECTION)
    BATCH_SIZE          (default: 1000)
    DROP_IF_EXISTS      set to "true" to drop destination collection first
"""

import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from pymilvus import (
    connections,
    Collection,
    FieldSchema,
    CollectionSchema,
    DataType,
    utility,
)

# ── Config ────────────────────────────────────────────────────────────────────

SOURCE_URI = os.environ["SOURCE_URI"]
SOURCE_API_KEY = os.environ["SOURCE_API_KEY"]
SOURCE_COLLECTION = os.getenv("SOURCE_COLLECTION", "colleges")

DEST_URI = os.environ["DEST_URI"]
DEST_API_KEY = os.environ["DEST_API_KEY"]
DEST_COLLECTION = os.getenv("DEST_COLLECTION", SOURCE_COLLECTION)

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
DROP_IF_EXISTS = os.getenv("DROP_IF_EXISTS", "false").lower() == "true"

VECTOR_DIM = 1536
INDEX_TYPE = "IVF_FLAT"
METRIC_TYPE = "L2"

# All scalar + vector fields to export (must match the collection schema)
OUTPUT_FIELDS = [
    "id",
    "college_name",
    "url",
    "url_canonical",
    "title",
    "content",
    "embedding",
    "crawled_at",
    "majors",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def connect(alias: str, uri: str, api_key: str) -> None:
    connections.connect(alias=alias, uri=uri, token=api_key)
    print(f"✓ Connected [{alias}] → {uri}")


def get_schema() -> CollectionSchema:
    from college_ai.scraping.config import MAX_TITLE_LENGTH, MAX_CONTENT_LENGTH

    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, auto_id=False, max_length=36),
        FieldSchema(name="college_name", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="url_canonical", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=MAX_TITLE_LENGTH),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=MAX_CONTENT_LENGTH),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        FieldSchema(name="crawled_at", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="majors", dtype=DataType.JSON),
    ]
    return CollectionSchema(fields, description="College pages with embeddings")


def ensure_dest_collection(name: str) -> Collection:
    if DROP_IF_EXISTS and utility.has_collection(name, using="dest"):
        print(f"  Dropping existing destination collection '{name}'...")
        utility.drop_collection(name, using="dest")

    if not utility.has_collection(name, using="dest"):
        print(f"  Creating collection '{name}' on destination...")
        col = Collection(name, schema=get_schema(), using="dest")
        col.create_index(
            field_name="embedding",
            index_params={"index_type": INDEX_TYPE, "metric_type": METRIC_TYPE, "params": {"nlist": 1024}},
        )
        print(f"  ✓ Collection + index created")
    else:
        col = Collection(name, using="dest")
        print(f"  ✓ Using existing destination collection '{name}'")

    col.load()
    return col


def count_source(col: Collection) -> int:
    col.flush()
    return col.num_entities


# ── Main ──────────────────────────────────────────────────────────────────────

def migrate() -> None:
    print("\n=== Zilliz Cloud Migration ===")
    print(f"  Source:      {SOURCE_URI}  [{SOURCE_COLLECTION}]")
    print(f"  Destination: {DEST_URI}  [{DEST_COLLECTION}]")
    print(f"  Batch size:  {BATCH_SIZE}")
    print()

    # Connect both instances
    connect("src", SOURCE_URI, SOURCE_API_KEY)
    connect("dest", DEST_URI, DEST_API_KEY)

    # Open source collection
    if not utility.has_collection(SOURCE_COLLECTION, using="src"):
        print(f"ERROR: Source collection '{SOURCE_COLLECTION}' does not exist.")
        sys.exit(1)

    src_col = Collection(SOURCE_COLLECTION, using="src")
    src_col.load()

    total = count_source(src_col)
    print(f"  Source record count: {total:,}")

    if total == 0:
        print("  Nothing to migrate.")
        return

    # Prepare destination collection
    dst_col = ensure_dest_collection(DEST_COLLECTION)

    # Use query_iterator to avoid the offset+limit <= 16384 restriction
    migrated = 0
    start = time.time()

    print(f"\nMigrating {total:,} records in batches of {BATCH_SIZE}...\n")

    iterator = src_col.query_iterator(
        expr="id != ''",
        output_fields=OUTPUT_FIELDS,
        batch_size=BATCH_SIZE,
    )

    while True:
        results = iterator.next()
        if not results:
            iterator.close()
            break

        dst_col.insert(results)

        migrated += len(results)
        elapsed = time.time() - start
        rate = migrated / elapsed if elapsed > 0 else 0
        pct = migrated / total * 100
        print(f"  {migrated:>7,} / {total:,}  ({pct:.1f}%)  {rate:.0f} rec/s", end="\r")

    print(f"\n\n  ✓ Migrated {migrated:,} records in {time.time() - start:.1f}s")

    # Flush and rebuild index
    print("  Flushing destination collection...")
    dst_col.flush()
    print(f"  ✓ Done. Destination now has {dst_col.num_entities:,} records.")


if __name__ == "__main__":
    migrate()
