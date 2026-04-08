"""Tests for the LLM query classifier."""

import json
import unittest
from unittest.mock import patch, MagicMock

from college_ai.rag.classifier import (
    QueryIntent,
    classify_query,
    VALID_QUERY_TYPES,
    RANKING_CATEGORIES,
    SCHOOL_DATA_CATEGORIES,
)


def _mock_openai_response(content):
    """Build a mock OpenAI chat completion response."""
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    return mock_resp


class TestQueryIntent(unittest.TestCase):
    """Test QueryIntent dataclass."""

    def test_defaults(self):
        intent = QueryIntent()
        self.assertEqual(intent.query_type, "qa")
        self.assertEqual(intent.complexity, "complex")
        self.assertEqual(intent.categories, [])
        self.assertEqual(intent.niche_categories, [])

    def test_custom_values(self):
        intent = QueryIntent(
            query_type="ranking",
            complexity="complex",
            categories=["admissions"],
            niche_categories=["academics"],
        )
        self.assertEqual(intent.query_type, "ranking")
        self.assertEqual(intent.niche_categories, ["academics"])

    def test_repr(self):
        intent = QueryIntent(query_type="qa", complexity="simple")
        self.assertIn("qa", repr(intent))
        self.assertIn("simple", repr(intent))


class TestClassifyQuery(unittest.TestCase):
    """Test classify_query with mocked OpenAI."""

    def _patch_client(self, response_content):
        """Patch the OpenAI client to return a given response."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_openai_response(
            response_content
        )
        return patch("college_ai.rag.classifier._get_client", return_value=mock_client)

    def test_valid_qa_simple(self):
        payload = json.dumps({
            "query_type": "qa",
            "complexity": "simple",
            "categories": ["admissions"],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("What is MIT's acceptance rate?")
        self.assertEqual(intent.query_type, "qa")
        self.assertEqual(intent.complexity, "simple")
        self.assertEqual(intent.categories, ["admissions"])
        self.assertEqual(intent.niche_categories, [])

    def test_valid_ranking(self):
        payload = json.dumps({
            "query_type": "ranking",
            "complexity": "complex",
            "categories": ["admissions", "student"],
            "niche_categories": ["academics", "value"],
        })
        with self._patch_client(payload):
            intent = classify_query("Rank the top CS schools")
        self.assertEqual(intent.query_type, "ranking")
        self.assertEqual(intent.niche_categories, ["academics", "value"])

    def test_ranking_empty_niche_defaults_to_academics(self):
        payload = json.dumps({
            "query_type": "ranking",
            "complexity": "complex",
            "categories": [],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("Best schools overall")
        self.assertEqual(intent.niche_categories, ["academics"])

    def test_non_ranking_niche_categories_ignored(self):
        payload = json.dumps({
            "query_type": "qa",
            "complexity": "simple",
            "categories": ["admissions"],
            "niche_categories": ["academics"],  # should be ignored
        })
        with self._patch_client(payload):
            intent = classify_query("What's MIT's SAT range?")
        self.assertEqual(intent.niche_categories, [])

    def test_invalid_query_type_falls_back(self):
        payload = json.dumps({
            "query_type": "invalid_type",
            "complexity": "simple",
            "categories": [],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("Something weird")
        self.assertEqual(intent.query_type, "qa")

    def test_invalid_complexity_falls_back(self):
        payload = json.dumps({
            "query_type": "qa",
            "complexity": "medium",  # invalid
            "categories": [],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("Test")
        self.assertEqual(intent.complexity, "complex")

    def test_invalid_categories_filtered(self):
        payload = json.dumps({
            "query_type": "qa",
            "complexity": "simple",
            "categories": ["admissions", "bogus_category", "cost"],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("Test")
        self.assertEqual(intent.categories, ["admissions", "cost"])

    def test_code_fence_stripping(self):
        payload = '```json\n{"query_type": "qa", "complexity": "simple", "categories": [], "niche_categories": []}\n```'
        with self._patch_client(payload):
            intent = classify_query("Test")
        self.assertEqual(intent.query_type, "qa")
        self.assertEqual(intent.complexity, "simple")

    def test_exception_returns_defaults(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("API error")
        with patch("college_ai.rag.classifier._get_client", return_value=mock_client):
            intent = classify_query("Test")
        self.assertEqual(intent.query_type, "qa")
        self.assertEqual(intent.complexity, "complex")
        self.assertEqual(intent.categories, [])

    def test_empty_response_returns_defaults(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = []
        mock_client.chat.completions.create.return_value = mock_resp
        with patch("college_ai.rag.classifier._get_client", return_value=mock_client):
            intent = classify_query("Test")
        self.assertEqual(intent.query_type, "qa")

    def test_malformed_json_returns_defaults(self):
        with self._patch_client("not json at all"):
            intent = classify_query("Test")
        self.assertEqual(intent.query_type, "qa")
        self.assertEqual(intent.complexity, "complex")

    def test_comparison_type(self):
        payload = json.dumps({
            "query_type": "comparison",
            "complexity": "complex",
            "categories": ["admissions", "cost"],
            "niche_categories": [],
        })
        with self._patch_client(payload):
            intent = classify_query("MIT vs Stanford for CS")
        self.assertEqual(intent.query_type, "comparison")


class TestConstants(unittest.TestCase):
    """Test that valid constants are well-formed."""

    def test_valid_query_types(self):
        expected = {"qa", "essay_ideas", "essay_review",
                    "admission_prediction", "ranking", "comparison"}
        self.assertEqual(VALID_QUERY_TYPES, expected)

    def test_ranking_categories_non_empty(self):
        self.assertGreater(len(RANKING_CATEGORIES), 0)
        self.assertIn("academics", RANKING_CATEGORIES)
        self.assertIn("other", RANKING_CATEGORIES)

    def test_school_data_categories_non_empty(self):
        self.assertGreater(len(SCHOOL_DATA_CATEGORIES), 0)
        self.assertIn("admissions", SCHOOL_DATA_CATEGORIES)
        self.assertIn("cost", SCHOOL_DATA_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
