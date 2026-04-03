#!/usr/bin/env python3
"""
Clean non-university URLs from Milvus collection.

This script identifies and removes records from the Milvus collection
where the URL doesn't belong to the university domain of the college.
It handles chunked pages (multiple records per URL) appropriately.
"""

import os
import sys
import time
import random
import argparse
from urllib.parse import urlparse
from typing import Dict, List, Set, Tuple, Any
import pandas as pd
from tqdm import tqdm

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pymilvus import Collection, connections
from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
    VECTOR_DIM,
)


def connect_to_milvus() -> Collection:
    """Connect to Milvus/Zilliz and return collection."""
    print(f"Connecting to Zilliz Cloud at {ZILLIZ_URI}")

    # Try to create a connection with proper credentials
    try:
        connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
        print("✅ Successfully connected to Zilliz Cloud")
    except Exception as e:
        print(f"⚠️ Connection warning: {e}")
        print("Attempting to continue anyway...")

    # Try to get collection and load it
    collection = Collection(ZILLIZ_COLLECTION_NAME)

    try:
        collection.load()
        print(f"✅ Successfully loaded collection: {ZILLIZ_COLLECTION_NAME}")

        # Check if collection is writable
        try:
            has_data = collection.num_entities > 0
            print(f"Collection contains {collection.num_entities:,} entities")

            # Check collection schema
            print(f"Collection schema has {len(collection.schema.fields)} fields")
            print(f"Primary key field: {collection.schema.primary_field.name}")

        except Exception as info_err:
            print(f"⚠️ Could not retrieve collection stats: {info_err}")
    except Exception as load_err:
        print(f"⚠️ Warning: Could not fully load collection: {load_err}")
        print(
            "Will attempt operations but they may fail if collection is not accessible"
        )

    return collection


def get_all_colleges(collection: Collection) -> List[str]:
    """Get a list of all unique college names in the collection."""
    unique_colleges = set()
    iterator = collection.query_iterator(
        expr='id != ""',
        output_fields=["college_name"],
        batch_size=1000,
    )
    while True:
        batch = iterator.next()
        if not batch:
            iterator.close()
            break
        for record in batch:
            if record.get("college_name"):
                unique_colleges.add(record.get("college_name"))

    return list(unique_colleges)


def get_domain_from_url(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Strip 'www.' prefix if present
        if domain.startswith("www."):
            domain = domain[4:]

        return domain
    except Exception:
        return ""


def is_valid_university_domain(url_domain: str, university_domain: str) -> bool:
    """
    Check if a URL domain belongs to the university domain.

    Args:
        url_domain: Domain from the URL to check
        university_domain: Base university domain

    Returns:
        True if the URL domain is valid for the university, False otherwise
    """
    if not url_domain or not university_domain:
        return False

    # Extract the base university domain from both domains to compare them properly
    url_base_domain = extract_base_university_domain(url_domain)
    univ_base_domain = extract_base_university_domain(university_domain)

    # If the base domains match, this is part of the university
    if url_base_domain == univ_base_domain:
        return True

    # If one of the domains is a subdomain of the university
    # First, check if the URL domain is a subdomain of the university domain
    if url_domain.endswith("." + univ_base_domain):
        return True

    # Sometimes different colleges/schools have their own domains
    # Extract domain parts for additional checking
    url_parts = url_domain.split(".")
    univ_parts = university_domain.split(".")

    # Skip non-educational domains
    if url_parts[-1] not in ["edu", "ac", "ca", "uk", "au", "nz"]:
        # Special case: handle university systems
        if len(url_parts) >= 3:
            # Many university systems share patterns like:
            # - law.stanford.edu vs cs.stanford.edu
            # - cse.sc.edu vs business.sc.edu

            # Compare the "root" part of the domain (stanford in stanford.edu)
            if len(url_parts) >= 2 and len(univ_parts) >= 2:
                # Check if the university identifier matches
                if url_parts[-2] == univ_parts[-2]:
                    return True

    return False


def extract_base_university_domain(domain: str) -> str:
    """
    Extract the base university domain from a full domain.

    Examples:
        cs.stanford.edu -> stanford.edu
        www.harvard.edu -> harvard.edu
        mcs.illinois.edu -> illinois.edu
        catalog.unc.edu -> unc.edu

    Returns the root academic domain.
    """
    if not domain:
        return ""

    parts = domain.split(".")

    # Not enough parts for a valid domain
    if len(parts) < 2:
        return ""

    # Check for educational TLDs
    edu_tlds = [
        "edu",  # US educational institutions
        "ac",  # Academic institutions in many countries
        "edu.au",  # Australia
        "edu.uk",  # UK
        "edu.sg",  # Singapore
        "edu.cn",  # China
        "edu.tw",  # Taiwan
        "edu.my",  # Malaysia
        "edu.hk",  # Hong Kong
        "edu.jp",  # Japan
        "ac.uk",  # UK
        "ac.nz",  # New Zealand
        "ac.jp",  # Japan
        "ac.za",  # South Africa
    ]

    # For domains like *.edu
    if len(parts) >= 2 and parts[-1] == "edu":
        # Get the main domain name (e.g., stanford.edu)
        return f"{parts[-2]}.{parts[-1]}"

    # For domains like *.edu.* (e.g., example.edu.au)
    for tld in edu_tlds:
        tld_parts = tld.split(".")
        if len(parts) >= len(tld_parts) + 1:
            domain_suffix = ".".join(parts[-len(tld_parts) :])
            if domain_suffix == tld:
                # Return main domain + tld (e.g., unsw.edu.au)
                return f"{parts[-len(tld_parts)-1]}.{domain_suffix}"

    # For domains ending in ".edu.*"
    if len(parts) >= 3 and parts[-2] == "edu":
        return f"{parts[-3]}.{parts[-2]}.{parts[-1]}"

    # No educational TLD found, return as is
    return domain


def get_college_base_domain(college_name: str, collection: Collection) -> str:
    """Get the base domain for a college from records."""
    safe_college = college_name.replace('"', '\\"')
    expr = f'college_name == "{safe_college}"'
    records = collection.query(
        expr=expr,
        output_fields=["url"],
        limit=100,  # Get a few records to find a valid URL
    )

    # Store domain frequency
    domain_count = {}

    # Count domain occurrences
    for record in records:
        url = record.get("url", "")
        if not url:
            continue

        full_domain = get_domain_from_url(url)
        if not full_domain:
            continue

        # Extract the base university domain
        base_domain = extract_base_university_domain(full_domain)
        if base_domain:
            domain_count[base_domain] = domain_count.get(base_domain, 0) + 1

    # Find most common domain
    if domain_count:
        # Sort by frequency, descending
        sorted_domains = sorted(domain_count.items(), key=lambda x: x[1], reverse=True)
        return sorted_domains[0][0]

    return ""


def get_domain_patterns():
    """
    Returns a dictionary of common university domain patterns and services.

    These are domains that should be considered part of the university ecosystem
    even if they don't match the main university domain pattern.
    """
    return {
        # Common third-party education platforms used by universities
        "canvas": ["canvas.com", "instructure.com"],
        "blackboard": ["blackboard.com", "bbcollab.com"],
        "moodle": ["moodle.org", "moodlecloud.com"],
        "d2l": ["d2l.com", "brightspace.com"],
        # University IT and educational services
        "library": ["library.", "libraries."],
        "registrar": ["registrar.", "enrollment.", "admissions."],
        "student": ["students.", "student.", "studentaffairs."],
        "alumni": ["alumni.", "alum."],
        "athletics": ["athletics.", "sports."],
        "career": ["career.", "careers.", "jobs."],
        "research": ["research.", "labs.", "institute."],
        "bursar": ["bursar.", "cashier.", "finance."],
        "housing": ["housing.", "residence.", "dorm."],
        # Academic departments (common patterns)
        "departments": [
            "cs.",
            "compsci.",
            "cse.",  # Computer Science
            "eng.",
            "engineering.",  # Engineering
            "bus.",
            "business.",  # Business
            "med.",
            "medicine.",
            "health.",  # Medicine
            "law.",  # Law
            "arts.",  # Arts
            "sci.",
            "science.",  # Science
            "math.",  # Mathematics
            "econ.",  # Economics
            "physics.",  # Physics
            "chem.",
            "chemistry.",  # Chemistry
            "bio.",
            "biology.",  # Biology
            "psych.",
            "psychology.",  # Psychology
            "hist.",
            "history.",  # History
            "lang.",
            "languages.",  # Languages
            "edu.",
            "education.",  # Education
        ],
    }


def is_known_university_service(domain: str) -> bool:
    """Check if the domain is a known university service pattern."""
    patterns = get_domain_patterns()

    # Check all patterns
    for category, domain_patterns in patterns.items():
        for pattern in domain_patterns:
            if pattern in domain.lower():
                return True

    return False


def scan_and_delete_non_edu(
    collection: Collection, college_name: str, dry_run: bool = True
) -> Tuple[List[str], int, int]:
    """
    Single-pass scan and batch delete of non-.edu records for a college.

    Iterates all records for the college once, identifies non-.edu URLs,
    and deletes them in batches of 200 IDs.

    Returns:
        Tuple of (non_university_urls, total_records, deleted_count)
    """
    DELETE_BATCH_SIZE = 200
    non_university_urls = []  # unique URLs found
    seen_urls = set()
    ids_to_delete = []
    total_records = 0
    deleted_count = 0

    print(f"  Scanning records for {college_name}...")

    safe_college = college_name.replace('"', '\\"')
    expr = f'college_name == "{safe_college}"'
    iterator = collection.query_iterator(
        expr=expr,
        output_fields=["id", "url"],
        batch_size=1000,
    )

    def _flush_delete_batch(ids: List[str]) -> int:
        """Delete a batch of IDs and return count deleted."""
        if not ids:
            return 0
        quoted = ", ".join([f'"{_id}"' for _id in ids])
        delete_expr = f"id in [{quoted}]"
        try:
            collection.delete(delete_expr)
            print(f"    Deleted batch of {len(ids)} records")
            return len(ids)
        except Exception as e:
            print(f"    Error deleting batch: {e}")
            return 0

    while True:
        records = iterator.next()
        if not records:
            iterator.close()
            break

        total_records += len(records)

        for record in records:
            url = record.get("url", "")
            if not url:
                continue

            url_domain = get_domain_from_url(url)

            # Keep .edu records — only target non-.edu
            if url_domain and url_domain.endswith(".edu"):
                continue

            # Track unique URLs for reporting
            if url not in seen_urls:
                seen_urls.add(url)
                non_university_urls.append(url)

            # Queue this record's ID for deletion
            ids_to_delete.append(record["id"])

            # When batch is full, delete it
            if not dry_run and len(ids_to_delete) >= DELETE_BATCH_SIZE:
                deleted_count += _flush_delete_batch(ids_to_delete)
                ids_to_delete = []
                time.sleep(0.1)  # rate-limit guard

    # Delete remaining IDs
    if not dry_run and ids_to_delete:
        deleted_count += _flush_delete_batch(ids_to_delete)

    # Single flush at the end
    if not dry_run and deleted_count > 0:
        try:
            collection.flush()
            print(f"  Flushed all deletions for {college_name}")
        except Exception as e:
            print(f"  Warning: flush failed: {e}")

        # Sample-based verification (check up to 5 random URLs)
        sample_urls = random.sample(
            non_university_urls, min(5, len(non_university_urls))
        )
        still_exist = 0
        for url in sample_urls:
            safe_url = url.replace('"', '\\"')
            check_expr = f'college_name == "{safe_college}" && url == "{safe_url}"'
            remaining = collection.query(
                expr=check_expr, output_fields=["id"], limit=1
            )
            if remaining:
                still_exist += 1

        if still_exist:
            print(
                f"  ⚠️ Verification: {still_exist}/{len(sample_urls)} sampled URLs still exist"
            )
        else:
            print(f"  ✅ Verification passed ({len(sample_urls)} sampled URLs removed)")

    return non_university_urls, total_records, deleted_count


def analyze_domains(urls):
    """Analyze domains to categorize URLs."""
    categories = {
        "social_media": [
            "facebook.com",
            "twitter.com",
            "instagram.com",
            "linkedin.com",
            "youtube.com",
            "tiktok.com",
            "x.com",
            "pinterest.com",
        ],
        "academic_publishers": [
            "springer.com",
            "sciencedirect.com",
            "jstor.org",
            "wiley.com",
            "sage.com",
            "ieee.org",
            "acm.org",
            "ssrn.com",
        ],
        "government": [".gov", ".gov.uk", ".gc.ca"],
        "personal_pages": [
            "github.io",
            "wordpress.com",
            "blogspot.com",
            "medium.com",
            "sites.google.com",
        ],
        "commercial": ["amazon.com", "google.com", "microsoft.com", "apple.com"],
        "tech_services": [
            "github.com",
            "gitlab.com",
            "stackoverflow.com",
            "slack.com",
            "zoom.us",
            "teams.microsoft.com",
        ],
        "other_universities": [".edu", ".ac.uk", ".edu.au"],
    }

    result = {"unknown": []}

    for url in urls:
        domain = get_domain_from_url(url)
        categorized = False

        for category, patterns in categories.items():
            for pattern in patterns:
                if pattern in domain:
                    if category not in result:
                        result[category] = []
                    result[category].append(url)
                    categorized = True
                    break
            if categorized:
                break

        if not categorized:
            result["unknown"].append(url)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Clean non-university URLs from Milvus collection"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report non-university URLs but don't delete them",
    )
    parser.add_argument(
        "--college",
        type=str,
        help="Process only the specified college (leave empty for all)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Just analyze domains without deletion (implies --dry-run)",
    )
    args = parser.parse_args()

    # analyze-only implies dry-run
    if args.analyze_only:
        args.dry_run = True

    # Connect to Milvus
    collection = connect_to_milvus()

    # Get colleges to process
    if args.college:
        colleges = [args.college]
    else:
        colleges = get_all_colleges(collection)

    # Process each college
    total_removed = 0
    colleges_data = []

    for college_name in colleges:
        print(f"\n{'='*40}")
        print(f"Processing college: {college_name}")
        print(f"{'='*40}")

        # Single-pass scan + delete
        non_university_urls, total_records, deleted = scan_and_delete_non_edu(
            collection, college_name, dry_run=args.dry_run
        )

        print(
            f"  Found {len(non_university_urls)} non-.edu URLs out of {total_records} total records"
        )

        if non_university_urls:
            # Print some examples
            examples = non_university_urls[:5]
            print(f"  Examples of non-.edu URLs:")
            for url in examples:
                print(f"    - {url}")

            # If analyze-only, show additional domain analysis
            if args.analyze_only and non_university_urls:
                print("\n  Domain Analysis:")
                print(f"  {'-'*30}")

                domain_analysis = analyze_domains(non_university_urls)

                for category, urls in sorted(
                    domain_analysis.items(), key=lambda x: len(x[1]), reverse=True
                ):
                    if urls:
                        print(
                            f"  {category.replace('_', ' ').title()}: {len(urls)} URLs"
                        )
                        for example_url in urls[:3]:
                            print(f"    - {example_url}")
                        if len(urls) > 3:
                            print(f"    - ... {len(urls)-3} more")
                print(f"  {'-'*30}")

            if not args.dry_run:
                total_removed += deleted
                print(f"  Removed {deleted} records with non-.edu URLs")
            else:
                print("  [DRY RUN] No records deleted")

        # Collect data for summary
        college_entry = {
            "college_name": college_name,
            "total_records": total_records,
            "non_edu_urls": len(non_university_urls),
            "percentage": (
                round(len(non_university_urls) / total_records * 100, 2)
                if total_records > 0
                else 0
            ),
            "example_bad_urls": (
                "; ".join(non_university_urls[:3]) if non_university_urls else ""
            ),
        }

        colleges_data.append(college_entry)

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    if args.dry_run:
        print("DRY RUN - No records were deleted")
    else:
        print(f"Total records removed: {total_removed}")

    # Create a DataFrame for nice tabular output
    if colleges_data:
        df = pd.DataFrame(colleges_data)

        # Sort by percentage of non-university URLs
        df = df.sort_values(by="percentage", ascending=False)

        print("\nDetailed results by college (sorted by % non-.edu URLs):")
        print(df.to_string(index=False))

    # Create a CSV report
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    report_path = f"non_university_urls_report_{timestamp}.csv"
    if colleges_data:
        pd.DataFrame(colleges_data).to_csv(report_path, index=False)
        print(f"\nDetailed report saved to {report_path}")


if __name__ == "__main__":
    main()
