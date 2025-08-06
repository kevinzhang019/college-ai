#!/usr/bin/env python3
"""
Command-line interface for the Milvus Database Monitor.
Provides easy access to monitor Milvus database statistics.
"""

import argparse
import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from preference_scraper.utils.milvus_monitor import MilvusMonitor


def main():
    """Main function for the command-line interface."""
    parser = argparse.ArgumentParser(
        description="Milvus Database Monitor - Real-time statistics for college data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_monitor.py                    # Start live monitoring
  python run_monitor.py --summary          # Show one-time summary
  python run_monitor.py --export           # Export statistics to file
  python run_monitor.py --interval 5       # Update every 5 seconds
        """,
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show one-time summary instead of live monitoring",
    )

    parser.add_argument(
        "--export", action="store_true", help="Export statistics to a file"
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=20.0,
        help="Update interval in seconds (default: 20.0)",
    )

    parser.add_argument(
        "--collection",
        type=str,
        help="Milvus collection name to monitor",
    )

    parser.add_argument(
        "--output", type=str, help="Output filename for export (optional)"
    )

    args = parser.parse_args()

    try:
        # Create monitor instance
        monitor = MilvusMonitor(
            collection_name=args.collection, update_interval=args.interval
        )

        if args.summary:
            # Show one-time summary
            print("📊 MILVUS DATABASE SUMMARY")
            print("=" * 50)

            summary = monitor.get_summary_report()

            if "error" in summary:
                print(f"❌ Error: {summary['error']}")
                return

            print(f"📄 Total Records: {summary['total_records']:,}")
            print(f"🏫 Total Colleges: {summary['total_colleges']}")
            print(f"🕒 Timestamp: {summary['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}")
            print()

            if summary["top_colleges"]:
                print("🏆 TOP COLLEGES BY RECORD COUNT:")
                for i, (college, data) in enumerate(summary["top_colleges"], 1):
                    print(f"  {i}. {college}: {data['count']:,} records")
                print()

            if summary["top_majors"]:
                print("🎓 TOP MAJORS BY RECORD COUNT:")
                for i, (major, count) in enumerate(summary["top_majors"], 1):
                    print(f"  {i}. {major}: {count:,} records")
                print()

        elif args.export:
            # Export statistics
            filename = monitor.export_statistics(args.output)
            if filename:
                print(f"✅ Statistics exported to: {filename}")
            else:
                print("❌ Failed to export statistics")

        else:
            # Start live monitoring
            print("🚀 Starting Milvus Database Monitor...")
            print(f"📊 Collection: {monitor.collection_name}")
            print(f"⏱️  Update Interval: {args.interval} seconds")
            print("Press Ctrl+C to stop monitoring")
            print()

            monitor.display_live_statistics()

    except KeyboardInterrupt:
        print("\n👋 Monitoring stopped by user")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
