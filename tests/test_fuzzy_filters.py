#!/usr/bin/env python3
"""
Test script to verify the improved fuzzy filtering for college and major filters.

This script tests the enhanced RAG service with flexible matching.
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.rag.service import CollegeRAG


def test_fuzzy_filters():
    """Test the improved fuzzy filtering functionality."""
    print("🧪 Testing Fuzzy Filter Improvements")
    print("=" * 50)

    try:
        # Initialize RAG engine
        print("📡 Connecting to RAG service...")
        rag = CollegeRAG()
        print(f"✅ Connected to collection: {rag.collection_name}")

        # Test cases for fuzzy matching
        test_cases = [
            {
                "description": "Partial major match: 'Computer' should match 'Computer Science'",
                "question": "What are the requirements?",
                "major": "Computer",
                "college": None,
                "top_k": 3,
            },
            {
                "description": "Partial college match: 'University of' should match various universities",
                "question": "What scholarships are available?",
                "major": None,
                "college": "University of",
                "top_k": 3,
            },
            {
                "description": "Combined filters (College required, Major optional) - University of + Business",
                "question": "Application requirements",
                "major": "Business",
                "college": "University of",  # Must match this, Business is optional boost
                "top_k": 5,
            },
        ]

        for i, test_case in enumerate(test_cases, 1):
            print(f"\n🔍 Test {i}: {test_case['description']}")
            print(f"   Question: '{test_case['question']}'")
            print(f"   Major filter: '{test_case['major']}'")
            print(f"   College filter: '{test_case['college']}'")

            # Perform search
            results = rag.search(
                question=test_case["question"],
                top_k=test_case["top_k"],
                major=test_case["major"],
                college_name=test_case["college"],
            )

            print(f"   Results found: {len(results)}")

            # Display results
            for idx, result in enumerate(results, 1):
                college = result.get("college_name", "Unknown")
                majors = result.get("majors", [])
                title = result.get("title", "No title")[:50] + "..."
                distance = result.get("distance", 0)

                print(f"     [{idx}] {college}")
                print(f"         Title: {title}")
                print(f"         Majors: {majors}")
                print(f"         Distance: {distance:.3f}")

            if not results:
                print(
                    "     ⚠️  No results found - this may indicate the filters are too restrictive"
                )

        print(f"\n✅ Fuzzy filter testing completed!")
        print(f"💡 Current filtering behavior:")
        print(f"   - Single Major filter: Must match major")
        print(f"   - Single College filter: Must match college")
        print(f"   - Both filters: College MUST match, Major optional (boosts ranking)")
        print(f"   - Available: Computer Science, Business, General majors")
        print(f"   - Available: Various 'University of...' schools")

    except Exception as e:
        print(f"❌ Error during testing: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit_code = test_fuzzy_filters()
    sys.exit(exit_code)
