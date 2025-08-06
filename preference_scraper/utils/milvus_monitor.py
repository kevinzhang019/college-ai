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

            # Get total count from actual query results instead of num_entities
            # since num_entities might be inaccurate in Zilliz Cloud
            try:
                total_records = self.collection.query(
                    expr="college_name != ''",
                    output_fields=["id"],
                    limit=16384,
                )
                total_count = len(total_records)
            except Exception as e:
                logger.error(f"Error getting total count: {e}")
                total_count = self.collection.num_entities

            if total_count == 0:
                return {"total": 0, "colleges": {}}

            # Process results in batches to handle large datasets
            college_stats = defaultdict(
                lambda: {
                    "count": 0,
                    "majors": defaultdict(int),
                    "latest_crawl": None,
                    "earliest_crawl": None,
                }
            )

            # Since Milvus has a strict offset+limit <= 16384 constraint,
            # we need to use expressions to get all records
            all_results = []

            # Strategy: Query by college name to get all records for each college
            # First, get a sample to find all college names
            try:
                sample = self.collection.query(
                    expr="college_name != ''",
                    output_fields=["college_name"],
                    limit=16384,
                    offset=0,
                )

                # Extract unique college names
                college_names = set()
                for record in sample:
                    college_names.add(record.get("college_name", ""))

                # Query each college separately to get all their records
                for college_name in sorted(college_names):
                    if college_name:
                        try:
                            college_records = self.collection.query(
                                expr=f'college_name == "{college_name}"',
                                output_fields=["college_name", "major", "crawled_at"],
                                limit=16384,
                            )

                            all_results.extend(college_records)

                        except Exception as e:
                            logger.error(f"Error querying {college_name}: {e}")
                            continue
            except Exception as e:
                logger.error(f"Error getting college names: {e}")
                # Fallback: try to get all records directly
                try:
                    all_records = self.collection.query(
                        expr="college_name != ''",
                        output_fields=["college_name", "major", "crawled_at"],
                        limit=16384,
                    )
                    all_results.extend(all_records)
                except Exception as fallback_e:
                    logger.error(f"Fallback query also failed: {fallback_e}")
                    return {"total": total_count, "colleges": {}, "error": str(e)}

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

            # Convert defaultdict to regular dict
            result = {
                "total": total_count,
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
