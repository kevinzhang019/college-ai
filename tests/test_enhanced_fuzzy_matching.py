#!/usr/bin/env python3
"""
Test script to verify enhanced fuzzy matching works with misspellings and typos.
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.rag.service import CollegeRAG, _fuzzy_match_text


def test_fuzzy_matching_function():
    """Test the fuzzy matching function directly with various inputs."""
    print("🧪 Testing Fuzzy Matching Function")
    print("=" * 50)

    # Test cases: (search_term, target_text, expected_result, description)
    test_cases = [
        # Exact matches
        ("Stanford", "Stanford University", True, "Exact substring match"),
        ("Computer", "Computer Science", True, "Exact substring match"),
        # Typos and misspellings
        ("Stanfrd", "Stanford University", True, "Missing letter"),
        ("Standford", "Stanford University", True, "Extra letter"),
        ("Stanforf", "Stanford University", True, "Letter swap"),
        ("Computr", "Computer Science", True, "Missing letter in major"),
        ("Computeer", "Computer Science", True, "Extra letter in major"),
        # Abbreviations and partial matches
        ("MIT", "Massachusetts Institute of Technology", True, "Abbreviation"),
        ("UC", "University of California", True, "Abbreviation"),
        ("University of", "University of Michigan", True, "Partial match"),
        # Word order and partial word matches
        ("York University", "University of New York", True, "Word order difference"),
        (
            "Cal Berkeley",
            "University of California—Berkeley",
            True,
            "Nickname/abbreviation",
        ),
        # Should NOT match
        ("Harvard", "Stanford University", False, "Completely different"),
        ("Engineering", "Business Administration", False, "Different major"),
        ("xyz", "Computer Science", False, "Random letters"),
        # Edge cases
        ("", "Stanford University", False, "Empty search"),
        ("Stanford", "", False, "Empty target"),
    ]

    passed = 0
    failed = 0

    for search_term, target_text, expected, description in test_cases:
        result = _fuzzy_match_text(search_term, target_text)
        status = "✅ PASS" if result == expected else "❌ FAIL"

        print(f"{status} | '{search_term}' vs '{target_text}' | {description}")
        if result == expected:
            passed += 1
        else:
            failed += 1
            print(f"      Expected: {expected}, Got: {result}")

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_enhanced_rag_fuzzy_matching():
    """Test the enhanced fuzzy matching in the RAG system with real data."""
    print("\n🎓 Testing Enhanced RAG Fuzzy Matching")
    print("=" * 50)

    try:
        # Initialize RAG engine
        print("📡 Connecting to RAG service...")
        rag = CollegeRAG()
        print(f"✅ Connected to collection: {rag.collection_name}")

        # Test cases with intentional misspellings
        test_cases = [
            {
                "description": "College with typo: 'Rutgrs' → should match 'Rutgers'",
                "question": "What are the requirements?",
                "major": None,
                "college": "Rutgrs",  # Missing 'e'
                "expected_matches": True,
            },
            {
                "description": "College with extra letter: 'Stanfford' → should match Stanford-like schools",
                "question": "What scholarships are available?",
                "major": None,
                "college": "Stanfford",  # Extra 'f'
                "expected_matches": False,  # Stanford not in our test DB
            },
            {
                "description": "Major with typo: 'Computr' → should match 'Computer Science'",
                "question": "Application requirements",
                "major": "Computr",  # Missing 'e'
                "college": None,
                "expected_matches": True,
            },
            {
                "description": "Major with extra letter: 'Businness' → should match 'Business'",
                "question": "What scholarships exist?",
                "major": "Businness",  # Extra 'n'
                "college": None,
                "expected_matches": True,
            },
            {
                "description": "College with minor typo: 'Yalee' → should match 'Yale University'",
                "question": "Admission requirements",
                "major": None,
                "college": "Yalee",  # Extra 'e'
                "expected_matches": True,  # Should match Yale University
            },
            {
                "description": "Combined typos: 'Businness' + 'Univeristy of'",
                "question": "Requirements and deadlines",
                "major": "Businness",  # Extra 'n'
                "college": "Univeristy of",  # Missing 's' in University
                "expected_matches": True,
            },
        ]

        results = []
        for i, test_case in enumerate(test_cases, 1):
            print(f"\n🔍 Test {i}: {test_case['description']}")
            print(f"   Question: '{test_case['question']}'")
            print(f"   Major: '{test_case['major']}'")
            print(f"   College: '{test_case['college']}'")

            # Perform search
            search_results = rag.search(
                question=test_case["question"],
                top_k=5,
                major=test_case["major"],
                college_name=test_case["college"],
            )

            found_results = len(search_results) > 0
            expected = test_case["expected_matches"]

            status = "✅ PASS" if found_results == expected else "❌ FAIL"
            print(
                f"   {status} | Expected: {expected}, Found: {found_results} ({len(search_results)} results)"
            )

            if search_results:
                print(
                    f"   📄 Sample result: {search_results[0].get('college_name', 'No name')}"
                )
                if test_case["major"]:
                    print(f"   📚 Majors: {search_results[0].get('majors', [])}")

            results.append(found_results == expected)

        passed = sum(results)
        total = len(results)
        print(f"\n📊 Overall Results: {passed}/{total} tests passed")

        if passed == total:
            print("🎉 All enhanced fuzzy matching tests passed!")
        else:
            print("⚠️  Some tests failed - fuzzy matching may need further tuning")

        return passed == total

    except Exception as e:
        print(f"❌ Error during testing: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Run all fuzzy matching tests."""
    print("🚀 Enhanced Fuzzy Matching Test Suite")
    print("=" * 60)

    # Test the core fuzzy matching function
    function_tests_passed = test_fuzzy_matching_function()

    # Test the RAG system integration
    rag_tests_passed = test_enhanced_rag_fuzzy_matching()

    print("\n" + "=" * 60)
    if function_tests_passed and rag_tests_passed:
        print("🎉 All tests passed! Enhanced fuzzy matching is working correctly.")
        return 0
    else:
        print("❌ Some tests failed. Check the output above for details.")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
