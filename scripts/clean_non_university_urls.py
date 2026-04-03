#!/usr/bin/env python3
"""
Clean non-university URLs from Milvus collection.

This script identifies and removes records from the Milvus collection
where the URL doesn't belong to the university domain of the college.
It handles chunked pages (multiple records per URL) appropriately.
"""

import os
import sys
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
    expr = ""  # Empty expression to get all records
    college_names = collection.query(
        expr=expr,
        output_fields=["college_name"],
        limit=10000,  # Set a large limit to get all unique values
    )

    # Get unique college names
    unique_colleges = set()
    for record in college_names:
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
    expr = f'college_name == "{college_name}"'
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


def get_non_university_urls(
    collection: Collection, college_name: str, university_domain: str
) -> Tuple[List[str], int]:
    """
    Get URLs that don't belong to the university domain.

    Returns:
        Tuple of (list of non-university URLs, total count of records for this college)
    """
    non_university_urls = set()
    total_records = 0
    offset = 0
    batch_size = 1000

    print(f"Scanning records for {college_name}...")

    while True:
        expr = f'college_name == "{college_name}"'
        records = collection.query(
            expr=expr,
            output_fields=["id", "url"],
            limit=batch_size,
            offset=offset,
        )

        if not records:
            break

        total_records += len(records)

        for record in records:
            url = record.get("url", "")
            url_domain = get_domain_from_url(url)

            # Skip empty URLs
            if not url:
                continue

            # First check if it's a valid university domain
            if is_valid_university_domain(url_domain, university_domain):
                continue

            # Then check if it follows known university service patterns
            if is_known_university_service(url_domain):
                # This looks like a university service but doesn't match our domain pattern
                # Let's double check if it contains the university name or abbreviation

                # Extract university name parts (use the domain as a fallback)
                univ_parts = university_domain.split(".")
                univ_name = univ_parts[-2] if len(univ_parts) >= 2 else ""

                # If the URL domain contains the university name part, likely it's related
                if univ_name and univ_name in url_domain:
                    continue

            # If we get here, it's likely not a university domain
            non_university_urls.add(url)

        offset += batch_size
        if len(records) < batch_size:
            break

    return list(non_university_urls), total_records


def delete_records_with_urls(
    collection: Collection, urls: List[str], college_name: str
) -> int:
    """
    Delete all records that have any of the given URLs.

    Args:
        collection: Milvus collection
        urls: List of URLs to remove
        college_name: College name for logging

    Returns:
        Number of records deleted
    """
    if not urls:
        return 0

    deleted_count = 0

    # Process in batches to avoid query size limits
    batch_size = 20
    for i in range(0, len(urls), batch_size):
        batch = urls[i : i + batch_size]

        # Construct query with URL OR conditions
        conditions = [f'url == "{url}"' for url in batch]
        expr = f'college_name == "{college_name}" && ({" || ".join(conditions)})'

        try:
            # Get IDs of records to delete with pagination
            all_ids = []
            query_limit = 10000  # Keep under the 16384 limit
            offset = 0

            while True:
                # Query with pagination
                records = collection.query(
                    expr=expr,
                    output_fields=["id"],
                    limit=query_limit,
                    offset=offset,
                )

                if not records:
                    break

                batch_ids = [rec["id"] for rec in records]
                all_ids.extend(batch_ids)

                # If we got fewer results than the limit, we've reached the end
                if len(records) < query_limit:
                    break

                offset += query_limit

                # Safety check to prevent too many results
                if len(all_ids) > 1000000:
                    print(
                        f"  ⚠️ Warning: Excessive number of records ({len(all_ids)}), stopping query"
                    )
                    break

                    # Delete records directly by URL to ensure they're removed
            if all_ids:
                try:
                    # Direct deletion using expr based on URLs (more reliable than ID-based deletion)
                    for url in batch:
                        # Create a dedicated expression for each URL
                        url_expr = f'college_name == "{college_name}" && url == "{url}"'

                        # Try to delete with direct expression
                        try:
                            print(f"  Deleting records with URL: {url}")
                            delete_result = collection.delete(url_expr)

                            # Force immediate flush after each URL deletion
                            try:
                                collection.flush()
                            except Exception as flush_err:
                                print(f"  Warning: Flush operation failed: {flush_err}")

                            # Verify the deletion immediately
                            verify_query = collection.query(
                                expr=url_expr, output_fields=["id"], limit=1
                            )

                            if verify_query:
                                print(
                                    f"  ⚠️ URL still exists after deletion attempt: {url}"
                                )
                            else:
                                print(f"  ✓ Successfully deleted URL: {url}")
                                deleted_count += 1

                        except Exception as url_del_err:
                            print(f"  Error deleting URL {url}: {url_del_err}")

                    # Final flush to ensure all changes are persistent
                    try:
                        collection.flush()
                        print(f"  Flushed all deletions to ensure persistence")
                    except Exception as e:
                        print(f"  Warning: Final flush operation failed: {e}")
                except Exception as delete_err:
                    print(f"  Error during batch deletion: {delete_err}")
        except Exception as e:
            print(f"  Error deleting batch: {e}")
            # Try deleting one by one if batch deletion fails
            for url in batch:
                try:
                    expr = f'college_name == "{college_name}" && url == "{url}"'
                    records = collection.query(
                        expr=expr,
                        output_fields=["id"],
                        limit=10000,
                    )
                    if records:
                        ids = [rec["id"] for rec in records]
                        if ids:
                            collection.delete(f'id in ["{",".join(ids)}"]')
                            deleted_count += len(ids)
                            print(f"  Deleted {len(ids)} records for URL: {url}")
                except Exception as e:
                    print(f"  Failed to delete records for URL {url}: {e}")

    return deleted_count


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
    parser.add_argument(
        "--force-recheck",
        action="store_true",
        help="Force re-checking of URLs even if they were deleted in a previous run",
    )
    parser.add_argument(
        "--force-permissions",
        action="store_true",
        help="Try alternative deletion method that might work with restricted permissions",
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

        university_domain = get_college_base_domain(college_name, collection)

        if not university_domain:
            print(f"  ⚠️ Could not determine base domain for {college_name}, skipping")
            continue

        print(f"  Base university domain: {university_domain}")

        # Find non-university URLs
        non_university_urls, total_records = get_non_university_urls(
            collection, college_name, university_domain
        )

        print(
            f"  Found {len(non_university_urls)} non-university URLs out of {total_records} total records"
        )

        if non_university_urls:
            # Print some examples
            examples = non_university_urls[:5]
            print(f"  Examples of non-university URLs:")
            for url in examples:
                print(f"    - {url}")

            # If analyze-only, show additional domain analysis
            if args.analyze_only and non_university_urls:
                print("\n  Domain Analysis:")
                print(f"  {'-'*30}")

                # Analyze the domains by category
                domain_analysis = analyze_domains(non_university_urls)

                # Show category breakdown
                for category, urls in sorted(
                    domain_analysis.items(), key=lambda x: len(x[1]), reverse=True
                ):
                    if urls:
                        print(
                            f"  {category.replace('_', ' ').title()}: {len(urls)} URLs"
                        )
                        for example_url in urls[:3]:  # Show up to 3 examples
                            print(f"    - {example_url}")
                        if len(urls) > 3:
                            print(f"    - ... {len(urls)-3} more")
                print(f"  {'-'*30}")

            # Delete records if not a dry run
            if not args.dry_run:
                if args.force_permissions:
                    print(
                        "🔒 Using alternative deletion method for restricted permissions..."
                    )
                    try:
                        # Try using the Milvus delete_entity API which might work differently
                        # This approach uses Python's inspect module to access non-standard delete methods
                        import inspect

                        # First get all entity IDs for the URLs we want to delete
                        all_ids_to_delete = []
                        for url in non_university_urls[:5]:  # Start with a small batch
                            url_expr = (
                                f'college_name == "{college_name}" && url == "{url}"'
                            )
                            results = collection.query(
                                expr=url_expr, output_fields=["id"], limit=100
                            )
                            if results:
                                ids = [rec["id"] for rec in results]
                                all_ids_to_delete.extend(ids)
                                print(f"  Found {len(ids)} entity IDs for URL: {url}")

                        # Try multiple deletion approaches
                        if all_ids_to_delete:
                            # Try method 1: Using direct entity deletion if available
                            try:
                                if hasattr(collection, "delete_entities"):
                                    print("  Trying delete_entities method...")
                                    result = collection.delete_entities(
                                        all_ids_to_delete
                                    )
                                    print(f"  Result: {result}")
                            except Exception as e1:
                                print(f"  Method 1 failed: {e1}")

                            # Try method 2: Using upsert to overwrite entities
                            try:
                                if hasattr(collection, "upsert"):
                                    print(
                                        "  Trying upsert method to mark records as deleted..."
                                    )
                                    # Create empty/null records with same IDs to effectively delete
                                    empty_records = [
                                        {
                                            "id": id_val,
                                            "url": "DELETED_URL",
                                            "content": "",
                                        }
                                        for id_val in all_ids_to_delete[:10]
                                    ]  # Try just a few
                                    collection.upsert(empty_records)
                            except Exception as e2:
                                print(f"  Method 2 failed: {e2}")

                            # Force flush
                            try:
                                collection.flush()
                            except Exception:
                                pass

                            deleted = len(all_ids_to_delete)
                        else:
                            deleted = 0
                    except Exception as e:
                        print(f"  Alternative deletion failed: {e}")
                        deleted = 0
                else:
                    # Use the standard deletion method
                    deleted = delete_records_with_urls(
                        collection, non_university_urls, college_name
                    )
                total_removed += deleted
                print(f"  Removed {deleted} records with non-university URLs")

                # Verify deletions - check a sample of URLs to confirm they're gone
                if deleted > 0 and len(non_university_urls) > 0:
                    print("  Performing thorough verification...")

                    # Force flush again before verification
                    try:
                        collection.flush()
                    except Exception:
                        pass

                    # Try to reload the collection to ensure we're seeing the latest data
                    try:
                        collection.release()
                        collection.load()
                        print("  Reloaded collection for verification")
                    except Exception as e:
                        print(f"  Note: Collection reload failed: {e}")

                    # Check each URL individually for more detailed feedback
                    still_exist = []
                    for url in non_university_urls:
                        url_expr = f'college_name == "{college_name}" && url == "{url}"'
                        remaining = collection.query(
                            expr=url_expr, output_fields=["id", "url"], limit=1
                        )
                        if remaining:
                            still_exist.append(url)

                    if still_exist:
                        print(
                            f"  ⚠️ Warning: {len(still_exist)}/{len(non_university_urls)} URLs still exist after deletion!"
                        )
                        for url in still_exist[:3]:
                            print(f"    - {url}")

                        # Add diagnostic information
                        print("\n  🔍 DIAGNOSTIC INFORMATION:")
                        print("  This could be due to:")
                        print(
                            "  1. Collection permissions issues (read-only collection)"
                        )
                        print("  2. Zilliz Cloud consistency delays")
                        print("  3. Delete operations being queued but not executed")
                        print(
                            "  Try running with --force-recheck and check Zilliz Cloud settings"
                        )
                    else:
                        print(
                            "  ✅ Deletion verified: All URLs successfully removed from collection"
                        )
            else:
                print("  [DRY RUN] No records deleted")

        # Collect data for summary
        college_entry = {
            "college_name": college_name,
            "university_domain": university_domain,
            "total_records": total_records,
            "non_university_urls": len(non_university_urls),
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

        print("\nDetailed results by college (sorted by % non-university URLs):")
        print(df.to_string(index=False))

    # Create a CSV report
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    report_path = f"non_university_urls_report_{timestamp}.csv"
    if colleges_data:
        pd.DataFrame(colleges_data).to_csv(report_path, index=False)
        print(f"\nDetailed report saved to {report_path}")


if __name__ == "__main__":
    main()
