"""Tests for the Cohere reranker and boost logic."""

import unittest
from unittest.mock import patch, MagicMock

from college_ai.rag.reranker import Reranker, _GRADE_TO_NUM


class TestRankingBoost(unittest.TestCase):
    """Test _apply_ranking_boost math."""

    def _make_hits(self, names, base_score=0.5):
        return [
            {"college_name": n, "rerank_score": base_score}
            for n in names
        ]

    def _make_intent(self, niche_categories=None):
        intent = MagicMock()
        intent.query_type = "ranking"
        intent.niche_categories = niche_categories or ["academics"]
        return intent

    def test_niche_rank_boost_rank_1(self):
        """Rank 1 gets maximum boost of 0.15."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"niche_rank": 1}}
        intent = self._make_intent()

        Reranker._apply_ranking_boost(hits, intent, school_data)
        boost = hits[0].get("ranking_boost", 0)
        # rank_score = max(0, 1 - (1-1)/500) = 1.0 → 1.0 * 0.15 = 0.15
        self.assertAlmostEqual(boost, 0.15, places=3)

    def test_niche_rank_boost_rank_500(self):
        """Rank 500 gets near-zero boost."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"niche_rank": 500}}
        intent = self._make_intent()

        Reranker._apply_ranking_boost(hits, intent, school_data)
        boost = hits[0].get("ranking_boost", 0)
        # rank_score = max(0, 1 - 499/500) = 0.002 → tiny
        self.assertAlmostEqual(boost, 0.002 * 0.15, places=4)

    def test_niche_rank_skip_for_other_only(self):
        """When niche_categories=['other'], skip niche rank boost."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"niche_rank": 1}}
        intent = self._make_intent(niche_categories=["other"])

        Reranker._apply_ranking_boost(hits, intent, school_data)
        self.assertNotIn("ranking_boost", hits[0])

    def test_acceptance_rate_boost_academics(self):
        """Academics category triggers acceptance rate boost."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"acceptance_rate": 0.04}}
        intent = self._make_intent(niche_categories=["academics"])

        Reranker._apply_ranking_boost(hits, intent, school_data)
        boost = hits[0].get("ranking_boost", 0)
        # (1 - 0.04) * 0.05 = 0.048
        self.assertGreater(boost, 0.04)

    def test_acceptance_rate_no_boost_non_academics(self):
        """Non-academics categories skip acceptance rate boost."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"acceptance_rate": 0.04}}
        intent = self._make_intent(niche_categories=["food"])

        Reranker._apply_ranking_boost(hits, intent, school_data)
        # Only grade boost could apply, but no grade data → no boost
        self.assertNotIn("ranking_boost", hits[0])

    def test_category_grade_boost(self):
        """Category grade boost averages matching grades."""
        hits = self._make_hits(["mit"])
        school_data = {"mit": {"academics_grade": "A+", "value_grade": "B"}}
        intent = self._make_intent(niche_categories=["academics", "value"])

        Reranker._apply_ranking_boost(hits, intent, school_data)
        boost = hits[0].get("ranking_boost", 0)
        # A+ = 4.3/4.3 = 1.0, B = 3.0/4.3 ≈ 0.698
        # avg ≈ 0.849, boost = 0.849 * 0.10 ≈ 0.0849
        self.assertGreater(boost, 0.08)
        self.assertLess(boost, 0.10)

    def test_resort_after_boost(self):
        """Hits are re-sorted by boosted score descending."""
        hits = [
            {"college_name": "mit", "rerank_score": 0.3},
            {"college_name": "stanford", "rerank_score": 0.8},
        ]
        school_data = {
            "mit": {"niche_rank": 1, "academics_grade": "A+"},
            "stanford": {"niche_rank": 100},
        }
        intent = self._make_intent()

        Reranker._apply_ranking_boost(hits, intent, school_data)
        # MIT should get larger boost and may overtake Stanford
        scores = [h["rerank_score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_school_data_no_crash(self):
        """Missing school_data entry doesn't crash."""
        hits = self._make_hits(["unknown_school"])
        school_data = {}
        intent = self._make_intent()

        Reranker._apply_ranking_boost(hits, intent, school_data)
        self.assertNotIn("ranking_boost", hits[0])


class TestPageTypeBoost(unittest.TestCase):
    """Test _apply_page_type_boost."""

    def test_matching_page_types_get_boost(self):
        hits = [
            {"page_type": "about", "rerank_score": 0.5},
            {"page_type": "other", "rerank_score": 0.6},
        ]
        Reranker._apply_page_type_boost(hits, ["about", "academics"])
        self.assertAlmostEqual(hits[0]["rerank_score"], 0.6)
        self.assertAlmostEqual(hits[1]["rerank_score"], 0.6)

    def test_no_match_no_boost(self):
        hits = [{"page_type": "other", "rerank_score": 0.5}]
        Reranker._apply_page_type_boost(hits, ["about"])
        self.assertAlmostEqual(hits[0]["rerank_score"], 0.5)
        self.assertNotIn("page_type_boost", hits[0])

    def test_resort_after_page_type_boost(self):
        hits = [
            {"page_type": "academics", "rerank_score": 0.4},
            {"page_type": "other", "rerank_score": 0.45},
        ]
        Reranker._apply_page_type_boost(hits, ["academics"])
        # academics gets 0.4+0.1=0.5 > other's 0.45
        self.assertEqual(hits[0]["page_type"], "academics")


class TestRerankerFallback(unittest.TestCase):
    """Test Cohere unavailability fallback."""

    def test_single_hit_passthrough(self):
        reranker = Reranker()
        hits = [{"content": "test", "rerank_score": 0.5}]
        result = reranker.rerank("query", hits)
        self.assertEqual(result, hits)

    def test_empty_hits(self):
        reranker = Reranker()
        result = reranker.rerank("query", [])
        self.assertEqual(result, [])

    @patch.dict("os.environ", {"COHERE_API_KEY": ""})
    def test_no_api_key_falls_back(self):
        reranker = Reranker()
        hits = [
            {"content": "a", "title": "A"},
            {"content": "b", "title": "B"},
            {"content": "c", "title": "C"},
        ]
        result = reranker.rerank("query", hits, top_k=2)
        self.assertEqual(len(result), 2)
        # Returns first top_k in original order
        self.assertEqual(result[0]["content"], "a")


class TestLowScoreFiltering(unittest.TestCase):
    """Test that hits below 0.1 are filtered out after reranking."""

    @patch.dict("os.environ", {"COHERE_API_KEY": "test-key"})
    def test_low_scores_removed(self):
        reranker = Reranker()
        # Mock the Cohere client
        mock_client = MagicMock()
        reranker._cohere_client = mock_client
        reranker._available = True

        # Simulate Cohere returning results with varied scores
        mock_result_high = MagicMock()
        mock_result_high.index = 0
        mock_result_high.relevance_score = 0.8

        mock_result_low = MagicMock()
        mock_result_low.index = 1
        mock_result_low.relevance_score = 0.05  # below 0.1 threshold

        mock_response = MagicMock()
        mock_response.results = [mock_result_high, mock_result_low]
        mock_client.rerank.return_value = mock_response

        hits = [
            {"content": "relevant", "title": "Good"},
            {"content": "irrelevant", "title": "Bad"},
        ]

        result = reranker.rerank("test query", hits, top_k=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "relevant")


class TestGradeMapping(unittest.TestCase):
    """Test the grade-to-numeric mapping."""

    def test_all_grades_present(self):
        expected = ["A+", "A", "A-", "B+", "B", "B-",
                    "C+", "C", "C-", "D+", "D", "D-", "F"]
        for grade in expected:
            self.assertIn(grade, _GRADE_TO_NUM)

    def test_a_plus_is_highest(self):
        self.assertEqual(_GRADE_TO_NUM["A+"], 4.3)

    def test_f_is_zero(self):
        self.assertEqual(_GRADE_TO_NUM["F"], 0.0)

    def test_monotonic_decrease(self):
        grades = ["A+", "A", "A-", "B+", "B", "B-",
                  "C+", "C", "C-", "D+", "D", "D-", "F"]
        values = [_GRADE_TO_NUM[g] for g in grades]
        for i in range(len(values) - 1):
            self.assertGreater(values[i], values[i + 1])


if __name__ == "__main__":
    unittest.main()
