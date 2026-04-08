"""Tests for the query router: greeting detection and school extraction."""

import unittest

from college_ai.rag.router import QueryRouter, GREETING, ESSAY_IDEAS, ESSAY_REVIEW


class TestGreetingDetection(unittest.TestCase):
    """Test that greeting patterns are correctly identified."""

    def setUp(self):
        self.router = QueryRouter()

    def test_basic_greetings(self):
        for greeting in ["hi", "hello", "hey", "howdy", "yo", "sup"]:
            pre = self.router.classify(greeting)
            self.assertEqual(pre.query_type, GREETING, f"Failed for: {greeting}")

    def test_good_morning(self):
        pre = self.router.classify("good morning")
        self.assertEqual(pre.query_type, GREETING)

    def test_thanks(self):
        pre = self.router.classify("thanks")
        self.assertEqual(pre.query_type, GREETING)

    def test_how_are_you(self):
        pre = self.router.classify("how are you")
        self.assertEqual(pre.query_type, GREETING)

    def test_whats_up(self):
        pre = self.router.classify("what's up")
        self.assertEqual(pre.query_type, GREETING)

    def test_long_message_not_greeting(self):
        """Messages over 8 words should not be classified as greetings."""
        pre = self.router.classify(
            "hi there I have a question about MIT admissions deadlines"
        )
        self.assertNotEqual(pre.query_type, GREETING)

    def test_greeting_in_longer_question_not_greeting(self):
        pre = self.router.classify("hello can you tell me about Stanford's CS program?")
        self.assertNotEqual(pre.query_type, GREETING)

    def test_non_greeting(self):
        pre = self.router.classify("What is MIT's acceptance rate?")
        self.assertNotEqual(pre.query_type, GREETING)


class TestEssayShortCircuits(unittest.TestCase):
    """Test essay_text and essay_prompt short-circuits."""

    def setUp(self):
        self.router = QueryRouter()

    def test_essay_text_triggers_review(self):
        pre = self.router.classify("Review my essay", essay_text="My essay draft here")
        self.assertEqual(pre.query_type, ESSAY_REVIEW)

    def test_essay_prompt_triggers_ideas(self):
        pre = self.router.classify(
            "Help with my essay",
            essay_prompt="Tell us why you want to attend our school",
        )
        self.assertEqual(pre.query_type, ESSAY_IDEAS)

    def test_essay_text_takes_precedence_over_prompt(self):
        """When both are provided, essay_text wins (review mode)."""
        pre = self.router.classify(
            "Help",
            essay_text="My draft",
            essay_prompt="The prompt",
        )
        self.assertEqual(pre.query_type, ESSAY_REVIEW)


class TestSchoolExtraction(unittest.TestCase):
    """Test multi-school extraction from query text."""

    def setUp(self):
        self.router = QueryRouter()

    def test_alias_mit(self):
        pre = self.router.classify("Tell me about MIT")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("massachusetts" in s for s in schools),
            f"Expected MIT alias match, got: {pre.detected_schools}",
        )

    def test_alias_ucla(self):
        pre = self.router.classify("How is UCLA for engineering?")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("california" in s and "los angeles" in s for s in schools),
            f"Expected UCLA alias match, got: {pre.detected_schools}",
        )

    def test_alias_stanford(self):
        pre = self.router.classify("Stanford acceptance rate")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("stanford" in s for s in schools),
            f"Expected Stanford match, got: {pre.detected_schools}",
        )

    def test_multiple_schools(self):
        pre = self.router.classify("Compare MIT and Stanford for CS")
        self.assertGreaterEqual(len(pre.detected_schools), 2)

    def test_max_schools_cap(self):
        """Should cap at 5 schools even if more are mentioned."""
        query = "Compare MIT, Stanford, Harvard, Yale, Princeton, Columbia, and Brown"
        pre = self.router.classify(query)
        self.assertLessEqual(len(pre.detected_schools), 5)

    def test_no_schools_detected(self):
        pre = self.router.classify("What are good study habits?")
        self.assertEqual(pre.detected_schools, [])

    def test_case_insensitive(self):
        pre = self.router.classify("tell me about mit")
        self.assertGreaterEqual(len(pre.detected_schools), 1)


class TestDefaultClassification(unittest.TestCase):
    """Test that non-greeting, non-essay queries return None query_type."""

    def setUp(self):
        self.router = QueryRouter()

    def test_regular_question_returns_none_type(self):
        pre = self.router.classify("What is MIT's acceptance rate?")
        self.assertIsNone(pre.query_type)

    def test_regular_question_extracts_schools(self):
        pre = self.router.classify("What is MIT's acceptance rate?")
        self.assertGreaterEqual(len(pre.detected_schools), 1)


if __name__ == "__main__":
    unittest.main()
