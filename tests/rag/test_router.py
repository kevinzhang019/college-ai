"""Tests for the query router: greeting detection and school extraction."""

import unittest

from college_ai.rag.router import (
    QueryRouter,
    GREETING,
    ESSAY_IDEAS,
    ESSAY_REVIEW,
    expand_query_shorthand,
)


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


class TestShorthandExpansion(unittest.TestCase):
    """Test the second-pass shorthand expansion in extract_schools."""

    def setUp(self):
        self.router = QueryRouter()

    # ---- expand_query_shorthand unit tests ----

    def test_expand_u_of_ca(self):
        self.assertEqual(
            expand_query_shorthand("U of CA"),
            "university of California",
        )

    def test_expand_univ_of_mich(self):
        self.assertEqual(
            expand_query_shorthand("how is univ of mich"),
            "how is university of michigan",
        )

    def test_expand_bama(self):
        self.assertEqual(
            expand_query_shorthand("Tell me about bama"),
            "Tell me about alabama",
        )

    def test_expand_uppercase_state_codes_only(self):
        # Lowercase "or" must NOT expand (would collide with conjunction).
        self.assertEqual(
            expand_query_shorthand("Should I pick MIT or Stanford?"),
            "Should I pick MIT or Stanford?",
        )

    def test_expand_no_change_returns_original(self):
        self.assertEqual(
            expand_query_shorthand("What is MIT's acceptance rate?"),
            "What is MIT's acceptance rate?",
        )

    def test_expand_safe_lowercase_state_code(self):
        # "ca" is in the safe lowercase set; standalone token should expand.
        self.assertIn(
            "California",
            expand_query_shorthand("how is ca like"),
        )

    # ---- end-to-end extract_schools tests ----

    def test_extract_u_of_ca_berkeley(self):
        pre = self.router.classify("How is U of CA Berkeley for engineering?")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("california" in s and "berkeley" in s for s in schools),
            f"Expected UC Berkeley match, got: {pre.detected_schools}",
        )

    def test_extract_bama_with_university_context(self):
        # State-word shorthands like "bama" only resolve when combined
        # with enough context to form a full canonical name. "univ of bama"
        # expands to "university of alabama" which substring-matches.
        pre = self.router.classify("Tell me about univ of bama")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("alabama" in s for s in schools),
            f"Expected Alabama match, got: {pre.detected_schools}",
        )

    def test_extract_univ_of_mich(self):
        pre = self.router.classify("how is univ of mich for cs")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("michigan" in s for s in schools),
            f"Expected Michigan match, got: {pre.detected_schools}",
        )

    def test_extract_ariz_state(self):
        # Two-phase expansion: "ariz" -> "arizona" (phase 1),
        # "state" -> "state university" (phase 2), giving
        # "arizona state university" which substring-matches.
        pre = self.router.classify("ariz state acceptance rate")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("arizona state" in s for s in schools),
            f"Expected Arizona State University match, got: {pre.detected_schools}",
        )

    def test_or_does_not_become_oregon(self):
        pre = self.router.classify("Should I pick MIT or Stanford?")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertFalse(
            any("oregon" in s for s in schools),
            f"Lowercase 'or' should not expand to Oregon, got: {pre.detected_schools}",
        )

    def test_no_regression_on_plain_alias(self):
        # The standard alias path must keep working unchanged.
        pre = self.router.classify("tell me about MIT")
        schools = [s.lower() for s in pre.detected_schools]
        self.assertTrue(
            any("massachusetts" in s for s in schools),
            f"Expected MIT match, got: {pre.detected_schools}",
        )

    def test_cap_still_enforced_after_second_pass(self):
        # First pass already hits the cap; second pass must not exceed it.
        pre = self.router.classify(
            "Compare MIT, Stanford, Harvard, Yale, Princeton, and bama"
        )
        self.assertLessEqual(len(pre.detected_schools), 5)


class TestFlagshipAliases(unittest.TestCase):
    """Test that bare 'University of X' resolves to the flagship campus
    for systems where one campus is clearly dominant."""

    def setUp(self):
        self.router = QueryRouter()

    def _assert_contains(self, schools, needle, query):
        lowered = [s.lower() for s in schools]
        self.assertTrue(
            any(needle in s for s in lowered),
            f"Expected {needle!r} in detected schools for {query!r}, got: {schools}",
        )

    def test_michigan_flagship(self):
        pre = self.router.classify("tell me about university of michigan")
        self._assert_contains(pre.detected_schools, "ann arbor", "university of michigan")

    def test_michigan_via_shorthand(self):
        # "univ of mich" -> "university of michigan" -> flagship alias -> Ann Arbor.
        # Previously fuzzy-matched Michigan State University by accident.
        pre = self.router.classify("univ of mich for cs")
        self._assert_contains(pre.detected_schools, "ann arbor", "univ of mich")

    def test_texas_flagship(self):
        pre = self.router.classify("university of texas acceptance rate")
        self._assert_contains(pre.detected_schools, "austin", "university of texas")

    def test_texas_dallas_still_resolves(self):
        # UT Dallas should still resolve via its own alias; the flagship
        # mapping must not preempt campus-specific disambiguators.
        pre = self.router.classify("tell me about UT Dallas")
        self._assert_contains(pre.detected_schools, "dallas", "UT Dallas")

    def test_illinois_flagship(self):
        pre = self.router.classify("university of illinois")
        self._assert_contains(pre.detected_schools, "urbana", "university of illinois")

    def test_maryland_flagship(self):
        pre = self.router.classify("university of maryland")
        self._assert_contains(pre.detected_schools, "college park", "university of maryland")

    def test_massachusetts_flagship(self):
        pre = self.router.classify("university of massachusetts")
        self._assert_contains(pre.detected_schools, "amherst", "university of massachusetts")

    def test_wisconsin_flagship(self):
        pre = self.router.classify("university of wisconsin")
        self._assert_contains(pre.detected_schools, "madison", "university of wisconsin")

    def test_north_carolina_flagship(self):
        pre = self.router.classify("university of north carolina")
        self._assert_contains(pre.detected_schools, "chapel hill", "university of north carolina")

    def test_tennessee_flagship(self):
        pre = self.router.classify("university of tennessee")
        self._assert_contains(pre.detected_schools, "knoxville", "university of tennessee")

    def test_nebraska_flagship(self):
        pre = self.router.classify("university of nebraska")
        self._assert_contains(pre.detected_schools, "lincoln", "university of nebraska")

    def test_minnesota_flagship(self):
        pre = self.router.classify("university of minnesota")
        self._assert_contains(pre.detected_schools, "twin cities", "university of minnesota")

    def test_colorado_flagship(self):
        pre = self.router.classify("university of colorado")
        self._assert_contains(pre.detected_schools, "boulder", "university of colorado")

    def test_hawaii_flagship(self):
        pre = self.router.classify("university of hawaii")
        self._assert_contains(pre.detected_schools, "manoa", "university of hawaii")

    def test_louisiana_flagship(self):
        pre = self.router.classify("university of louisiana")
        self._assert_contains(pre.detected_schools, "lafayette", "university of louisiana")

    def test_ucla_disambiguator_still_works(self):
        pre = self.router.classify("ucla acceptance rate")
        self._assert_contains(pre.detected_schools, "los angeles", "ucla")


if __name__ == "__main__":
    unittest.main()
