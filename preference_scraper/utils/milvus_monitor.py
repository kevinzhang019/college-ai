"""
Milvus Database Monitor Utility
Provides real-time monitoring of records in Milvus database organized by college.
Displays live statistics and updates as new records are added.
"""

import os
import sys
import time
import threading
from datetime import datetime, timedelta
import glob
import csv
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import logging
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from pymilvus import connections, Collection, utility
from preference_scraper.crawlers.config import *

# Load environment variables
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MilvusMonitor:
    """Real-time monitor for Milvus database records organized by college."""

    def __init__(self, collection_name: str = None, update_interval: float = 20.0):
        """
        Initialize the Milvus monitor.

        Args:
            collection_name: Name of the Milvus collection to monitor
            update_interval: How often to update statistics (in seconds)
        """
        self.collection_name = collection_name or ZILLIZ_COLLECTION_NAME
        self.update_interval = update_interval
        self.is_monitoring = False
        self.monitor_thread = None
        self.last_stats = {}
        self.start_time = None

        # Connect to Milvus
        self.connect_milvus()
        self.collection = self.get_collection()

        # Statistics tracking
        self.stats_history = []
        self.max_history_size = 100

    def connect_milvus(self):
        """Connect to Zilliz Cloud database."""
        try:
            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
            logger.info("✓ Connected to Zilliz Cloud")
        except Exception as e:
            logger.error(f"✗ Failed to connect to Zilliz Cloud: {e}")
            raise

    def get_collection(self) -> Optional[Collection]:
        """Get the Zilliz Cloud collection."""
        try:
            if utility.has_collection(self.collection_name):
                collection = Collection(self.collection_name)
                logger.info(f"✓ Found collection: {self.collection_name}")
                return collection
            else:
                logger.warning(f"Collection '{self.collection_name}' not found")
                return None
        except Exception as e:
            logger.error(f"Error accessing collection: {e}")
            return None

    def _iterate_all_records(self, output_fields: List[str], batch_size: int = 16384):
        """Yield all records in the collection in batches.

        This performs a full scan using a permissive boolean expression and offset pagination.
        """
        if not self.collection:
            return
        try:
            self.collection.load()
        except Exception:
            pass

        offset = 0
        while True:
            try:
                # Use a permissive expression to match all rows
                batch = self.collection.query(
                    expr='id != ""',
                    output_fields=output_fields,
                    limit=batch_size,
                    offset=offset,
                )
            except Exception as e:
                logger.error(f"Error during full scan at offset {offset}: {e}")
                break

            if not batch:
                break

            for rec in batch:
                yield rec

            if len(batch) < batch_size:
                break
            offset += batch_size

    def _get_college_names_from_csvs(self) -> List[str]:
        """Return distinct college names by reading all CSVs under crawlers/colleges.

        Avoids scanning the full collection, which is capped by Milvus per-query limits.
        """
        base_dir = os.path.join(os.path.dirname(__file__), "../crawlers/colleges")
        csv_files = glob.glob(os.path.join(base_dir, "*.csv"))
        names = set()
        for path in csv_files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        name = (row.get("name") or "").strip()
                        if name:
                            names.add(name)
            except Exception as e:
                logger.warning(f"Failed to read CSV {path}: {e}")
                continue
        return sorted(names)

    def get_college_statistics(self) -> Dict[str, Dict]:
        """
        Get current statistics for all colleges in the database.

        Returns:
            Dictionary with college statistics
        """
        if not self.collection:
            return {}

        try:
            # Load collection
            self.collection.load()

            # Process results in batches to handle large datasets
            college_stats = defaultdict(
                lambda: {
                    "count": 0,
                    "majors": defaultdict(int),
                    "latest_crawl": None,
                    "earliest_crawl": None,
                }
            )

            # Step 1: Derive college names from CSV files to avoid full collection scans
            all_results = []
            college_names = self._get_college_names_from_csvs()
            if not college_names:
                return {
                    "total": 0,
                    "colleges": {},
                    "error": "No college CSVs found or empty.",
                }
            logger.info(
                f"Step 1 complete: Found {len(college_names)} colleges from CSVs"
            )

            # Step 2: For each college, fetch all records with proper pagination
            logger.info("Step 2: Fetching all records for each college...")

            for college_name in college_names:
                if not college_name:
                    continue

                try:
                    college_records = []
                    logger.info(f"Fetching records for {college_name}...")
                    for rec in self._iterate_all_records(
                        ["college_name", "major", "crawled_at"], batch_size=16384
                    ):
                        if rec.get("college_name") == college_name:
                            college_records.append(rec)

                    all_results.extend(college_records)
                    logger.info(
                        f"  {college_name}: total records fetched: {len(college_records)}"
                    )

                except Exception as e:
                    logger.error(f"Error processing {college_name}: {e}")
                    continue

            # Process all results
            for record in all_results:
                college_name = record.get("college_name", "Unknown")
                major = record.get("major", "Unknown")
                crawled_at = record.get("crawled_at")

                # Update college count
                college_stats[college_name]["count"] += 1
                college_stats[college_name]["majors"][major] += 1

                # Update crawl timestamps
                if crawled_at:
                    try:
                        crawl_time = datetime.fromisoformat(
                            crawled_at.replace("Z", "+00:00")
                        )
                        if (
                            college_stats[college_name]["latest_crawl"] is None
                            or crawl_time > college_stats[college_name]["latest_crawl"]
                        ):
                            college_stats[college_name]["latest_crawl"] = crawl_time
                        if (
                            college_stats[college_name]["earliest_crawl"] is None
                            or crawl_time
                            < college_stats[college_name]["earliest_crawl"]
                        ):
                            college_stats[college_name]["earliest_crawl"] = crawl_time
                    except:
                        pass

            # Calculate total count from the records we actually fetched
            actual_total = len(all_results)

            # Convert defaultdict to regular dict
            result = {
                "total": actual_total,
                "colleges": dict(college_stats),
                "timestamp": datetime.now(),
                "queried_records": len(all_results),
            }

            return result

        except Exception as e:
            logger.error(f"Error getting college statistics: {e}")
            return {"total": 0, "colleges": {}, "error": str(e)}

    def format_statistics(self, stats: Dict) -> str:
        """
        Format statistics for display.

        Args:
            stats: Statistics dictionary

        Returns:
            Formatted string for display
        """
        if not stats or "colleges" not in stats:
            return "No data available"

        output = []
        output.append("=" * 80)
        output.append("MILVUS DATABASE MONITOR")
        output.append("=" * 80)

        # Overall statistics
        total_records = stats.get("total", 0)
        num_colleges = len(stats.get("colleges", {}))
        timestamp = stats.get("timestamp", datetime.now())

        output.append(f"📊 Total Records: {total_records:,}")
        output.append(f"🏫 Colleges: {num_colleges}")
        output.append(f"🕒 Last Updated: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

        if self.start_time:
            uptime = datetime.now() - self.start_time
            output.append(f"⏱️  Monitor Uptime: {str(uptime).split('.')[0]}")

        output.append("")

        # College-wise breakdown
        if stats.get("colleges"):
            output.append("COLLEGE BREAKDOWN:")
            output.append("-" * 80)

            # Sort colleges by record count (descending)
            sorted_colleges = sorted(
                stats["colleges"].items(), key=lambda x: x[1]["count"], reverse=True
            )

            for college_name, college_data in sorted_colleges:
                count = college_data["count"]
                majors = college_data["majors"]
                latest_crawl = college_data["latest_crawl"]
                earliest_crawl = college_data["earliest_crawl"]

                output.append(f"🏛️  {college_name}")
                output.append(f"   📄 Records: {count:,}")

                # Show majors if available
                if majors:
                    major_list = [
                        f"{major} ({count})" for major, count in majors.items()
                    ]
                    output.append(f"   🎓 Majors: {', '.join(major_list)}")

                # Show crawl time range
                if earliest_crawl and latest_crawl:
                    output.append(
                        f"   📅 Crawl Period: {earliest_crawl.strftime('%Y-%m-%d %H:%M')} to {latest_crawl.strftime('%Y-%m-%d %H:%M')}"
                    )

                output.append("")

        # Show changes if available
        if self.last_stats and "total" in self.last_stats:
            old_total = self.last_stats["total"]
            new_total = stats.get("total", 0)
            change = new_total - old_total

            if change != 0:
                output.append("CHANGES:")
                output.append("-" * 80)
                if change > 0:
                    output.append(f"📈 +{change:,} new records since last update")
                else:
                    output.append(f"📉 {change:,} records removed since last update")
                output.append("")

        output.append("=" * 80)
        return "\n".join(output)

    def clear_screen(self):
        """Clear the terminal screen."""
        os.system("cls" if os.name == "nt" else "clear")

    def display_live_statistics(self):
        """Display live statistics with real-time updates."""
        self.start_time = datetime.now()
        self.is_monitoring = True

        print("🚀 Starting Milvus Database Monitor...")
        print("Press Ctrl+C to stop monitoring")
        print()

        try:
            while self.is_monitoring:
                # Get current statistics
                current_stats = self.get_college_statistics()

                # Clear screen and display
                self.clear_screen()
                formatted_stats = self.format_statistics(current_stats)
                print(formatted_stats)

                # Store for change tracking
                self.last_stats = current_stats

                # Add to history
                self.stats_history.append(current_stats)
                if len(self.stats_history) > self.max_history_size:
                    self.stats_history.pop(0)

                # Wait for next update
                time.sleep(self.update_interval)

        except KeyboardInterrupt:
            print("\n🛑 Monitoring stopped by user")
            self.is_monitoring = False
        except Exception as e:
            logger.error(f"Error in live monitoring: {e}")
            self.is_monitoring = False

    def start_monitoring(self):
        """Start the monitoring in a separate thread."""
        if self.is_monitoring:
            logger.warning("Monitoring is already running")
            return

        self.monitor_thread = threading.Thread(target=self.display_live_statistics)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        logger.info("Monitoring started in background thread")

    def stop_monitoring(self):
        """Stop the monitoring."""
        self.is_monitoring = False
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)
        logger.info("Monitoring stopped")

    def get_summary_report(self) -> Dict:
        """
        Get a summary report of the database.

        Returns:
            Dictionary with summary statistics
        """
        stats = self.get_college_statistics()

        if not stats or "colleges" not in stats:
            return {"error": "No data available"}

        # Calculate summary statistics
        colleges = stats["colleges"]
        total_records = stats["total"]

        # Find top colleges by record count
        top_colleges = sorted(
            colleges.items(), key=lambda x: x[1]["count"], reverse=True
        )[:5]

        # Calculate major distribution
        all_majors = defaultdict(int)
        for college_data in colleges.values():
            for major, count in college_data["majors"].items():
                all_majors[major] += count

        # Find most common majors
        top_majors = sorted(all_majors.items(), key=lambda x: x[1], reverse=True)[:5]

        summary = {
            "total_records": total_records,
            "total_colleges": len(colleges),
            "top_colleges": top_colleges,
            "top_majors": top_majors,
            "timestamp": datetime.now(),
        }

        return summary

    def export_statistics(self, filename: str = None) -> str:
        """
        Export current statistics to a file.

        Args:
            filename: Output filename (optional)

        Returns:
            Path to the exported file
        """
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"milvus_stats_{timestamp}.txt"

        stats = self.get_college_statistics()
        formatted_stats = self.format_statistics(stats)

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(formatted_stats)
            logger.info(f"Statistics exported to {filename}")
            return filename
        except Exception as e:
            logger.error(f"Error exporting statistics: {e}")
            return None


def main():
    """Main function to run the Milvus monitor."""
    try:
        # Create monitor instance
        monitor = MilvusMonitor()

        # Start live monitoring
        monitor.display_live_statistics()

    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    except Exception as e:
        logger.error(f"Error running monitor: {e}")


if __name__ == "__main__":
    main()
