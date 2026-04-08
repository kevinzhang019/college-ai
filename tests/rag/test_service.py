"""Tests for RAG service helper methods (no external API calls)."""

import unittest

from college_ai.rag.service import CollegeRAG


class TestVerifyCitations(unittest.TestCase):
    """Test _verify_citations."""

    def test_valid_citations_preserved(self):
        answer = "MIT has a 4% acceptance rate [1] and strong CS [2]."
        result = CollegeRAG._verify_citations(answer, num_sources=3)
        self.assertIn("[1]", result)
        self.assertIn("[2]", result)

    def test_invalid_citations_stripped(self):
        answer = "MIT is great [1] and also [5] for CS."
        result = CollegeRAG._verify_citations(answer, num_sources=3)
        self.assertIn("[1]", result)
        self.assertNotIn("[5]", result)

    def test_zero_citation_stripped(self):
        answer = "Something [0] here."
        result = CollegeRAG._verify_citations(answer, num_sources=3)
        self.assertNotIn("[0]", result)

    def test_warning_appended_when_no_valid_citations(self):
        answer = "MIT is a great school [10]."
        result = CollegeRAG._verify_citations(answer, num_sources=3)
        self.assertIn("may not be fully grounded", result)

    def test_no_warning_when_zero_sources(self):
        answer = "Hello, how can I help?"
        result = CollegeRAG._verify_citations(answer, num_sources=0)
        self.assertNotIn("may not be fully grounded", result)

    def test_school_data_tags_stripped(self):
        answer = "MIT data [SCHOOL DATA] MIT\nAcceptance rate: 4%"
        result = CollegeRAG._verify_citations(answer, num_sources=0)
        self.assertNotIn("[SCHOOL DATA]", result)

    def test_placeholder_n_stripped(self):
        answer = "MIT is great [N] for CS."
        result = CollegeRAG._verify_citations(answer, num_sources=3)
        self.assertNotIn("[N]", result)


class TestComputeConfidence(unittest.TestCase):
    """Test _compute_confidence."""

    def test_empty_hits_low(self):
        self.assertEqual(CollegeRAG._compute_confidence([]), "low")

    def test_high_confidence_rerank(self):
        hits = [{"rerank_score": 0.8} for _ in range(5)]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "high")

    def test_medium_confidence_rerank(self):
        hits = [{"rerank_score": 0.3} for _ in range(3)]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "medium")

    def test_low_confidence_rerank(self):
        hits = [{"rerank_score": 0.1}]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "low")

    def test_high_confidence_rrf(self):
        """Without rerank_score, falls back to distance."""
        hits = [{"distance": 0.8} for _ in range(5)]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "high")

    def test_medium_confidence_rrf(self):
        hits = [{"distance": 0.5} for _ in range(3)]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "medium")

    def test_low_confidence_rrf(self):
        hits = [{"distance": 0.2}]
        self.assertEqual(CollegeRAG._compute_confidence(hits), "low")


class TestSelectModel(unittest.TestCase):
    """Test _select_model."""

    def setUp(self):
        self.rag = CollegeRAG.__new__(CollegeRAG)
        self.rag.model_simple = "gpt-4.1-nano"
        self.rag.model_standard = "gpt-5.4-mini"

    def test_simple_qa_gets_simple_model(self):
        model = self.rag._select_model("qa", "simple")
        self.assertEqual(model, "gpt-4.1-nano")

    def test_complex_qa_gets_standard_model(self):
        model = self.rag._select_model("qa", "complex")
        self.assertEqual(model, "gpt-5.4-mini")

    def test_ranking_gets_standard_model(self):
        model = self.rag._select_model("ranking", "complex")
        self.assertEqual(model, "gpt-5.4-mini")

    def test_essay_gets_standard_model(self):
        model = self.rag._select_model("essay_ideas", "complex")
        self.assertEqual(model, "gpt-5.4-mini")


class TestBuildContextSnippets(unittest.TestCase):
    """Test _build_context_snippets scaling."""

    def _make_hits(self, n, content_len=3000):
        return [
            {
                "college_name": f"School{i}",
                "title": f"Title{i}",
                "url": f"https://school{i}.edu",
                "crawled_at": "2024-01-01",
                "content": "x" * content_len,
            }
            for i in range(n)
        ]

    def test_empty_hits(self):
        result = CollegeRAG._build_context_snippets([])
        self.assertEqual(result, "")

    def test_few_hits_longer_snippets(self):
        """<=3 hits should use 2500 char max."""
        hits = self._make_hits(2)
        result = CollegeRAG._build_context_snippets(hits)
        # Each snippet should contain up to 2500 chars of content
        self.assertIn("[1]", result)
        self.assertIn("[2]", result)

    def test_many_hits_shorter_snippets(self):
        """7+ hits should use 1500 char max."""
        hits = self._make_hits(8, content_len=3000)
        result = CollegeRAG._build_context_snippets(hits)
        # Snippets should be truncated to 1500 chars
        self.assertIn("[8]", result)
        # Content should be truncated with "..."
        self.assertIn("...", result)

    def test_snippet_format(self):
        hits = self._make_hits(1)
        result = CollegeRAG._build_context_snippets(hits)
        self.assertIn("[1] School0", result)
        self.assertIn("URL:", result)
        self.assertIn("crawled:", result)


class TestGetMaxTokens(unittest.TestCase):
    """Test _get_max_tokens."""

    def test_response_length_override(self):
        self.assertEqual(CollegeRAG._get_max_tokens("XS", "qa"), 200)
        self.assertEqual(CollegeRAG._get_max_tokens("S", "qa"), 400)
        self.assertEqual(CollegeRAG._get_max_tokens("M", "qa"), 700)
        self.assertEqual(CollegeRAG._get_max_tokens("L", "qa"), 1200)
        self.assertEqual(CollegeRAG._get_max_tokens("XL", "qa"), 1800)

    def test_default_qa(self):
        self.assertEqual(CollegeRAG._get_max_tokens(None, "qa"), 700)

    def test_default_essay(self):
        self.assertEqual(CollegeRAG._get_max_tokens(None, "essay_ideas"), 1200)
        self.assertEqual(CollegeRAG._get_max_tokens(None, "essay_review"), 1200)

    def test_response_length_overrides_query_type(self):
        """Even for essay types, explicit response_length wins."""
        self.assertEqual(CollegeRAG._get_max_tokens("XS", "essay_ideas"), 200)


class TestGetTemperature(unittest.TestCase):
    """Test _get_temperature."""

    def setUp(self):
        self.rag = CollegeRAG.__new__(CollegeRAG)

    def test_qa_temperature(self):
        self.assertAlmostEqual(self.rag._get_temperature("qa"), 0.2)

    def test_essay_ideas_temperature(self):
        self.assertAlmostEqual(self.rag._get_temperature("essay_ideas"), 0.4)

    def test_essay_review_temperature(self):
        self.assertAlmostEqual(self.rag._get_temperature("essay_review"), 0.3)

    def test_ranking_temperature(self):
        self.assertAlmostEqual(self.rag._get_temperature("ranking"), 0.2)


class TestFormatHelpers(unittest.TestCase):
    """Test static formatting helpers."""

    def test_college_focus_single(self):
        result = CollegeRAG._format_college_focus(["MIT"])
        self.assertIn("**MIT**", result)
        self.assertIn("Focus on:", result)

    def test_college_focus_multiple(self):
        result = CollegeRAG._format_college_focus(["MIT", "Stanford", "Harvard"])
        self.assertIn("**MIT**", result)
        self.assertIn("**Stanford**", result)
        self.assertIn("and **Harvard**", result)

    def test_college_focus_empty(self):
        result = CollegeRAG._format_college_focus([])
        self.assertEqual(result, "")

    def test_school_context_single(self):
        result = CollegeRAG._format_school_context(["MIT"])
        self.assertIn("School of interest:", result)

    def test_school_context_multiple(self):
        result = CollegeRAG._format_school_context(["MIT", "Stanford"])
        self.assertIn("Schools of interest:", result)

    def test_school_context_empty(self):
        result = CollegeRAG._format_school_context([])
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
