#!/usr/bin/env python3
"""
Remove duplicates within each college in Milvus database.
This script identifies and removes duplicate records for each college while keeping the most recent one.
"""

import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Set
import glob
import csv
from collections import defaultdict
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from pymilvus import connections, Collection, utility
from preference_scraper.crawlers.config import *

# Load environment variables
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)


class CollegeDuplicateRemover:
    """Remove duplicates within each college in Milvus database."""

    def __init__(self):
        """Initialize the college duplicate remover."""
        self.connect_milvus()
        self.collection = self.get_collection()

    def connect_milvus(self):
        """Connect to Zilliz Cloud database."""
        try:
            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
            print("✓ Connected to Zilliz Cloud")
        except Exception as e:
            print(f"✗ Failed to connect to Zilliz Cloud: {e}")
            raise

    def get_collection(self) -> Collection:
        """Get the Zilliz Cloud collection."""
        collection_name = ZILLIZ_COLLECTION_NAME

        if not utility.has_collection(collection_name):
            print(f"✗ Collection '{collection_name}' not found")
            raise Exception(f"Collection '{collection_name}' not found")

        collection = Collection(collection_name)
        print(f"✓ Found collection: {collection_name}")
        return collection

    # Index/Load responsibility moved to crawler. Duplicate removal assumes collection ready.

    def get_colleges_with_duplicates(self) -> Dict[str, Dict]:
        """
        Find all colleges and identify which ones have duplicates.

        Returns:
            Dictionary mapping college names to their duplicate statistics
        """
        print("🔍 Finding colleges with duplicates...")

        # Load collection
        try:
            self.collection.load()
        except Exception as e:
            # Proceed without loading if vector index is not present; scalar queries still work
            print(f"⚠️  Proceeding without explicit load (reason: {e})")

        # Get total count
        total_count = self.collection.num_entities
        print(f"📊 Total records: {total_count:,}")

        # Get all colleges and their record counts
        colleges_data = {}

        # Get distinct college names from CSVs to avoid full-collection scans
        college_names: Set[str] = set()
        base_dir = os.path.join(os.path.dirname(__file__), "../crawlers/colleges")
        for path in glob.glob(os.path.join(base_dir, "*.csv")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        name = (row.get("name") or "").strip()
                        if name:
                            college_names.add(name)
            except Exception as e:
                print(f"⚠️  Failed to read CSV {path}: {e}")
                continue

        print(f"📋 Found {len(college_names)} unique colleges")

        # Check each college for duplicates
        for college_name in sorted(college_names):
            if college_name:
                try:
                    print(f"📊 Checking {college_name}...")

                    # Get all records for this college in batches
                    college_records = []
                    offset = 0
                    while True:
                        batch = self.collection.query(
                            expr=f'college_name == "{college_name}"',
                            output_fields=[
                                "id",
                                "url",
                                "college_name",
                                "major",
                                "crawled_at",
                            ],
                            limit=16384,
                            offset=offset,
                        )
                        if not batch:
                            break
                        college_records.extend(batch)
                        if len(batch) < 16384:
                            break
                        offset += 16384

                    # Group by URL to find duplicates
                    url_to_records = defaultdict(list)
                    for record in college_records:
                        url = record.get("url", "")
                        if url:
                            url_to_records[url].append(record)

                    # Find URLs with duplicates
                    duplicates = {
                        url: records
                        for url, records in url_to_records.items()
                        if len(records) > 1
                    }

                    if duplicates:
                        total_duplicates = sum(
                            len(records) - 1 for records in duplicates.values()
                        )
                        colleges_data[college_name] = {
                            "total_records": len(college_records),
                            "unique_urls": len(url_to_records),
                            "duplicate_urls": len(duplicates),
                            "duplicate_records": total_duplicates,
                            "urls_with_duplicates": duplicates,
                        }
                        print(
                            f"  ⚠️  Found {len(duplicates)} URLs with {total_duplicates} duplicates"
                        )
                    else:
                        print(f"  ✅ No duplicates found")

                except Exception as e:
                    print(f"❌ Error checking {college_name}: {e}")
                    continue

        return colleges_data

    def remove_duplicates_for_college(
        self, college_name: str, duplicates_data: Dict
    ) -> Dict[str, int]:
        """
        Remove duplicates for a specific college.

        Args:
            college_name: Name of the college
            duplicates_data: Duplicate data for this college

        Returns:
            Dictionary with removal statistics
        """
        print(f"\n🗑️  Removing duplicates for {college_name}...")

        stats = {
            "urls_processed": 0,
            "records_kept": 0,
            "records_removed": 0,
            "errors": 0,
        }

        urls_with_duplicates = duplicates_data["urls_with_duplicates"]

        for url, records in urls_with_duplicates.items():
            try:
                print(f"📊 Processing: {url[:50]}... ({len(records)} records)")

                # Sort records by crawled_at timestamp (most recent first)
                sorted_records = sorted(
                    records, key=lambda x: x.get("crawled_at", ""), reverse=True
                )

                # Keep the most recent record
                record_to_keep = sorted_records[0]
                records_to_remove = sorted_records[1:]

                # Delete duplicate records
                if records_to_remove:
                    ids_to_remove = [r["id"] for r in records_to_remove]

                    # Delete in batches to avoid query limits
                    batch_size = 1000
                    for i in range(0, len(ids_to_remove), batch_size):
                        batch_ids = ids_to_remove[i : i + batch_size]
                        quoted = ",".join([f'"{_id}"' for _id in batch_ids])
                        delete_expr = f"id in [{quoted}]"

                        try:
                            self.collection.delete(delete_expr)
                            print(f"    ✅ Removed {len(batch_ids)} duplicates")
                        except Exception as e:
                            print(f"    ❌ Error removing batch: {e}")
                            stats["errors"] += 1
                            continue

                    stats["records_removed"] += len(records_to_remove)
                    stats["records_kept"] += 1

                stats["urls_processed"] += 1

            except Exception as e:
                print(f"❌ Error processing URL {url}: {e}")
                stats["errors"] += 1
                continue

        return stats

    def verify_college_cleanup(self, college_name: str) -> Dict[str, int]:
        """
        Verify that duplicates have been removed for a specific college.

        Args:
            college_name: Name of the college to verify

        Returns:
            Dictionary with verification statistics
        """
        try:
            # Get all records for this college
            college_records = self.collection.query(
                expr=f'college_name == "{college_name}"',
                output_fields=["url"],
                limit=16384,
            )

            # Check for any remaining duplicates
            url_counts = defaultdict(int)
            for record in college_records:
                url_counts[record.get("url", "")] += 1

            remaining_duplicates = 0
            for url, count in url_counts.items():
                if count > 1:
                    remaining_duplicates += count - 1

            return {
                "final_count": len(college_records),
                "remaining_duplicates": remaining_duplicates,
                "unique_urls": len(url_counts),
            }

        except Exception as e:
            print(f"❌ Error verifying cleanup for {college_name}: {e}")
            return {"final_count": 0, "remaining_duplicates": 0, "unique_urls": 0}

    def run_college_cleanup(self):
        """Run the complete college-specific duplicate removal process."""
        print("🚀 Starting College-Specific Duplicate Removal")
        print("=" * 60)

        try:
            # Step 1: Find colleges with duplicates
            colleges_with_duplicates = self.get_colleges_with_duplicates()

            if not colleges_with_duplicates:
                print("✅ No colleges with duplicates found!")
                return

            print(
                f"\n📊 Found {len(colleges_with_duplicates)} colleges with duplicates"
            )

            # Calculate total statistics
            total_duplicate_records = sum(
                data["duplicate_records"] for data in colleges_with_duplicates.values()
            )
            print(
                f"📊 Total duplicate records across all colleges: {total_duplicate_records}"
            )

            # Step 2: Remove duplicates for each college
            overall_stats = {
                "colleges_processed": 0,
                "total_records_removed": 0,
                "total_records_kept": 0,
                "total_errors": 0,
            }

            for college_name, duplicates_data in colleges_with_duplicates.items():
                print(f"\n{'='*20} {college_name} {'='*20}")

                # Remove duplicates for this college
                removal_stats = self.remove_duplicates_for_college(
                    college_name, duplicates_data
                )

                # Verify cleanup
                verification_stats = self.verify_college_cleanup(college_name)

                # Print college results
                print(f"\n📊 RESULTS FOR {college_name}:")
                print(f"  URLs processed: {removal_stats['urls_processed']}")
                print(f"  Records kept: {removal_stats['records_kept']}")
                print(f"  Records removed: {removal_stats['records_removed']}")
                print(f"  Errors: {removal_stats['errors']}")
                print(f"  Final record count: {verification_stats['final_count']:,}")
                print(
                    f"  Remaining duplicates: {verification_stats['remaining_duplicates']}"
                )
                print(f"  Unique URLs: {verification_stats['unique_urls']:,}")

                # Update overall statistics
                overall_stats["colleges_processed"] += 1
                overall_stats["total_records_removed"] += removal_stats[
                    "records_removed"
                ]
                overall_stats["total_records_kept"] += removal_stats["records_kept"]
                overall_stats["total_errors"] += removal_stats["errors"]

            # Print final results
            print("\n" + "=" * 60)
            print("📊 OVERALL CLEANUP RESULTS")
            print("=" * 60)
            print(f"Colleges processed: {overall_stats['colleges_processed']}")
            print(f"Total records kept: {overall_stats['total_records_kept']}")
            print(f"Total records removed: {overall_stats['total_records_removed']}")
            print(f"Total errors: {overall_stats['total_errors']}")

            if overall_stats["total_records_removed"] > 0:
                print(
                    f"\n✅ Successfully removed {overall_stats['total_records_removed']} duplicate records!"
                )
            else:
                print("\nℹ️  No duplicates were removed.")

        except Exception as e:
            print(f"❌ Error during cleanup: {e}")
            raise


def main():
    """Main function to run the college-specific duplicate removal."""
    try:
        remover = CollegeDuplicateRemover()
        remover.run_college_cleanup()
    except Exception as e:
        print(f"❌ Failed to run college duplicate removal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
