#!/usr/bin/env python3
"""
Test script for URL validation in the MultithreadedCrawler.
This script tests that our domain validation logic correctly prevents
crawling websites that don't belong to the university being crawled.
"""

from crawlers.multithreaded_crawler import MultithreadedCrawler


def main():
    # Initialize crawler with default settings
    crawler = MultithreadedCrawler()

    # Run the domain validation test
    crawler.test_domain_validation()

    print("Testing specific examples:")

    # Same domain should pass
    assert (
        crawler.is_internal_link("https://harvard.edu/about", "https://harvard.edu")
        == True
    )

    # Subdomain should pass
    assert (
        crawler.is_internal_link(
            "https://cs.harvard.edu/courses", "https://harvard.edu"
        )
        == True
    )

    # Different domains should fail
    assert (
        crawler.is_internal_link(
            "https://harvarduniversity.com/about", "https://harvard.edu"
        )
        == False
    )
    assert (
        crawler.is_internal_link("https://mit.edu/about", "https://harvard.edu")
        == False
    )
    assert (
        crawler.is_internal_link(
            "https://fake-harvard.edu/about", "https://harvard.edu"
        )
        == False
    )
    assert (
        crawler.is_internal_link(
            "https://harvardimposter.edu/about", "https://harvard.edu"
        )
        == False
    )

    print("All tests passed!")


if __name__ == "__main__":
    main()
