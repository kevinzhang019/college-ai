#!/usr/bin/env python3
"""
Migrate data from the v1 collection (colleges) to the v2 hybrid collection
(colleges_v2) which supports dense + BM25 search.

The new collection adds:
  - content_sparse: SPARSE_FLOAT_VECTOR auto-generated via BM25 function
  - COSINE metric for dense vectors (replaces L2)
  - INVERTED scalar indexes on college_name and url_canonical

Data is copied in batches. BM25 sparse vectors are generated automatically
by Milvus at insert time from the content field.

Usage:
    python scripts/migrate_to_hybrid.py [--drop-existing] [--batch-size 500]
"""

import argparse
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
    ZILLIZ_COLLECTION_NAME_V2,
    VECTOR_DIM,
)


def create_v2_collection(client, collection_name, drop_existing=False):
    """Create the v2 collection with hybrid search schema."""
    from pymilvus import DataType, Function, FunctionType

    if client.has_collection(collection_name):
        if drop_existing:
            print(f"  Dropping existing collection '{collection_name}'...")
            client.drop_collection(collection_name)
        else:
            print(f"  Collection '{collection_name}' already exists. Use --drop-existing to recreate.")
            return False

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)

    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=100)
    schema.add_field("college_name", DataType.VARCHAR, max_length=256)
    schema.add_field("url", DataType.VARCHAR, max_length=2048)
    schema.add_field("url_canonical", DataType.VARCHAR, max_length=512)
    schema.add_field("title", DataType.VARCHAR, max_length=500)
    schema.add_field(
        "content", DataType.VARCHAR, max_length=65535,
        enable_analyzer=True, enable_match=True,
        analyzer_params={"type": "english"},
    )
    schema.add_field("content_sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field("crawled_at", DataType.VARCHAR, max_length=32)

    # BM25 function: auto-generates content_sparse from content at insert time
    bm25_fn = Function(
        name="bm25",
        input_field_names=["content"],
        output_field_names=["content_sparse"],
        function_type=FunctionType.BM25,
    )
    schema.add_function(bm25_fn)

    # Indexes
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )
    index_params.add_index(
        field_name="content_sparse",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    index_params.add_index(
        field_name="college_name",
        index_type="INVERTED",
        index_name="college_name_idx",
    )
    index_params.add_index(
        field_name="url_canonical",
        index_type="INVERTED",
        index_name="url_canonical_idx",
    )

    client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
    )
    print(f"  Created collection '{collection_name}' with hybrid schema.")
    return True


def migrate_data(client, src_collection, dst_collection, batch_size=500):
    """Copy all rows from src to dst collection in batches."""
    from pymilvus import connections, Collection, utility

    # Use ORM API for query_iterator on the source (v1) collection
    connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)

    if not utility.has_collection(src_collection):
        print(f"  Source collection '{src_collection}' not found!")
        return 0

    src = Collection(src_collection)
    src.load(timeout=120)

    output_fields = [
        "id", "college_name", "url", "url_canonical",
        "title", "content", "embedding", "crawled_at",
    ]

    # Use query_iterator to avoid the 16k offset limit
    iterator = src.query_iterator(
        output_fields=output_fields,
        batch_size=batch_size,
    )

    total_migrated = 0
    batch_num = 0

    while True:
        batch = iterator.next()
        if not batch:
            break

        batch_num += 1
        rows = []
        for row in batch:
            # Truncate content to fit new schema max (65535 chars)
            content = (row.get("content") or "")[:65000]
            # Skip rows with empty content (BM25 needs text)
            if not content.strip():
                continue

            rows.append({
                "id": row.get("id", ""),
                "college_name": (row.get("college_name") or "")[:256],
                "url": (row.get("url") or "")[:2048],
                "url_canonical": (row.get("url_canonical") or "")[:512],
                "title": (row.get("title") or "")[:500],
                "content": content,
                # content_sparse is auto-generated by BM25 function — do NOT include
                "embedding": row.get("embedding", []),
                "crawled_at": (row.get("crawled_at") or "")[:32],
            })

        if rows:
            try:
                client.insert(collection_name=dst_collection, data=rows)
                total_migrated += len(rows)
                print(f"  Batch {batch_num}: migrated {len(rows)} rows (total: {total_migrated})")
            except Exception as e:
                print(f"  Batch {batch_num}: FAILED — {e}")
                # Try one-by-one for partial recovery
                for i, row in enumerate(rows):
                    try:
                        client.insert(collection_name=dst_collection, data=[row])
                        total_migrated += 1
                    except Exception as e2:
                        print(f"    Row {i} failed: {e2}")
        else:
            print(f"  Batch {batch_num}: skipped (all empty content)")

    iterator.close()
    return total_migrated


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Milvus data to v2 hybrid collection"
    )
    parser.add_argument(
        "--drop-existing", action="store_true",
        help="Drop v2 collection if it already exists",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Rows per migration batch (default: 500)",
    )
    args = parser.parse_args()

    from pymilvus import MilvusClient

    print(f"Connecting to Zilliz Cloud...")
    client = MilvusClient(uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
    print(f"  Connected.")

    print(f"\nStep 1: Create v2 collection '{ZILLIZ_COLLECTION_NAME_V2}'")
    created = create_v2_collection(
        client, ZILLIZ_COLLECTION_NAME_V2, drop_existing=args.drop_existing
    )

    if not created and not args.drop_existing:
        print("\n  Collection already exists. Skipping creation.")
        print("  Use --drop-existing to recreate from scratch.")

    print(f"\nStep 2: Migrate data from '{ZILLIZ_COLLECTION_NAME}' → '{ZILLIZ_COLLECTION_NAME_V2}'")
    start = time.time()
    count = migrate_data(
        client, ZILLIZ_COLLECTION_NAME, ZILLIZ_COLLECTION_NAME_V2,
        batch_size=args.batch_size,
    )
    elapsed = time.time() - start

    print(f"\nDone! Migrated {count} rows in {elapsed:.1f}s")

    # Verify
    print(f"\nStep 3: Verify")
    try:
        stats = client.get_collection_stats(ZILLIZ_COLLECTION_NAME_V2)
        print(f"  v2 collection row count: {stats.get('row_count', 'unknown')}")
    except Exception as e:
        print(f"  Could not get stats: {e}")

    print(f"\nMigration complete. Update your .env:")
    print(f"  ZILLIZ_COLLECTION_NAME_V2={ZILLIZ_COLLECTION_NAME_V2}")
    print(f"  (The old collection '{ZILLIZ_COLLECTION_NAME}' is untouched.)")


if __name__ == "__main__":
    main()
