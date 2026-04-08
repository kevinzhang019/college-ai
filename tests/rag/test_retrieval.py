"""Tests for the hybrid retrieval engine logic."""

import unittest
from unittest.mock import MagicMock

from college_ai.rag.retrieval import HybridRetriever


class TestDedupeByUrl(unittest.TestCase):
    """Test _dedupe_by_url."""

    def test_respects_max_chunks_per_url(self):
        hits = [
            {"url": "https://mit.edu/a", "content": "chunk1"},
            {"url": "https://mit.edu/a", "content": "chunk2"},
            {"url": "https://mit.edu/a", "content": "chunk3"},  # should be dropped
            {"url": "https://stanford.edu/b", "content": "chunk4"},
        ]
        result = HybridRetriever._dedupe_by_url(hits, top_k=10)
        # MAX_CHUNKS_PER_URL defaults to 2
        urls = [r["url"] for r in result]
        self.assertEqual(urls.count("https://mit.edu/a"), 2)
        self.assertEqual(len(result), 3)

    def test_no_url_always_kept(self):
        hits = [
            {"url": "", "content": "a"},
            {"url": "", "content": "b"},
            {"url": "", "content": "c"},
        ]
        result = HybridRetriever._dedupe_by_url(hits, top_k=10)
        self.assertEqual(len(result), 3)

    def test_respects_top_k(self):
        hits = [
            {"url": f"https://school{i}.edu", "content": f"chunk{i}"}
            for i in range(20)
        ]
        result = HybridRetriever._dedupe_by_url(hits, top_k=5)
        self.assertEqual(len(result), 5)

    def test_empty_input(self):
        result = HybridRetriever._dedupe_by_url([], top_k=10)
        self.assertEqual(result, [])

    def test_mixed_url_and_no_url(self):
        hits = [
            {"url": "https://mit.edu/a", "content": "1"},
            {"url": "", "content": "2"},
            {"url": "https://mit.edu/a", "content": "3"},
            {"url": "https://mit.edu/a", "content": "4"},  # 3rd from same URL
        ]
        result = HybridRetriever._dedupe_by_url(hits, top_k=10)
        self.assertEqual(len(result), 3)  # 2 from mit.edu + 1 empty


class TestSchoolBoost(unittest.TestCase):
    """Test _apply_school_boost."""

    def test_target_schools_get_boost(self):
        hits = [
            {"college_name": "MIT", "distance": 0.5},
            {"college_name": "Stanford", "distance": 0.6},
            {"college_name": "Harvard", "distance": 0.4},
        ]
        result = HybridRetriever._apply_school_boost(hits, ["MIT"])
        # MIT should get +0.15 boost
        self.assertAlmostEqual(result[0]["_boosted_score"], 0.65)

    def test_non_target_no_boost(self):
        hits = [
            {"college_name": "MIT", "distance": 0.5},
            {"college_name": "Stanford", "distance": 0.6},
        ]
        result = HybridRetriever._apply_school_boost(hits, ["MIT"])
        stanford = [h for h in result if h["college_name"] == "Stanford"][0]
        self.assertAlmostEqual(stanford["_boosted_score"], 0.6)

    def test_case_insensitive(self):
        hits = [{"college_name": "Massachusetts Institute of Technology", "distance": 0.5}]
        result = HybridRetriever._apply_school_boost(
            hits, ["massachusetts institute of technology"]
        )
        self.assertAlmostEqual(result[0]["_boosted_score"], 0.65)

    def test_sorted_descending(self):
        hits = [
            {"college_name": "A", "distance": 0.3},
            {"college_name": "B", "distance": 0.8},
            {"college_name": "C", "distance": 0.1},
        ]
        result = HybridRetriever._apply_school_boost(hits, ["C"])
        scores = [h["_boosted_score"] for h in result]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestFlattenHit(unittest.TestCase):
    """Test _flatten_hit across pymilvus Hit variants."""

    def test_dict_with_entity(self):
        item = {
            "entity": {
                "college_name": "MIT",
                "url": "https://mit.edu",
                "title": "CS Program",
                "content": "Content here",
                "page_type": "academics",
                "crawled_at": "2024-01-01",
                "url_canonical": "https://mit.edu",
            },
            "distance": 0.95,
        }
        result = HybridRetriever._flatten_hit(item)
        self.assertEqual(result["college_name"], "MIT")
        self.assertAlmostEqual(result["distance"], 0.95)

    def test_flat_dict(self):
        item = {
            "college_name": "Stanford",
            "url": "https://stanford.edu",
            "title": "About",
            "content": "Text",
            "page_type": "about",
            "crawled_at": "2024-01-01",
            "url_canonical": "https://stanford.edu",
            "distance": 0.8,
        }
        result = HybridRetriever._flatten_hit(item)
        self.assertEqual(result["college_name"], "Stanford")
        self.assertAlmostEqual(result["distance"], 0.8)

    def test_object_with_entity_attr(self):
        entity = MagicMock()
        entity.get = lambda k, default=None: {
            "college_name": "Harvard",
            "url": "https://harvard.edu",
            "title": "Title",
            "content": "Text",
            "page_type": "about",
            "crawled_at": "2024-01-01",
            "url_canonical": "https://harvard.edu",
        }.get(k, default)
        item = MagicMock()
        item.entity = entity
        item.get = lambda k, default=None: {"distance": 0.7}.get(k, default)
        # Ensure hasattr checks work
        type(item).distance = 0.7
        result = HybridRetriever._flatten_hit(item)
        self.assertEqual(result["college_name"], "Harvard")

    def test_missing_distance_defaults_to_zero(self):
        item = {"college_name": "Test", "url": "", "title": "",
                "content": "", "page_type": "", "crawled_at": "",
                "url_canonical": ""}
        result = HybridRetriever._flatten_hit(item)
        self.assertAlmostEqual(result["distance"], 0.0)


class TestNormalizeResults(unittest.TestCase):
    """Test _normalize_results."""

    def test_empty_input(self):
        result = HybridRetriever._normalize_results([])
        self.assertEqual(result, [])

    def test_none_input(self):
        result = HybridRetriever._normalize_results(None)
        self.assertEqual(result, [])

    def test_nested_groups(self):
        """Results may come as [[hit1, hit2], [hit3]]."""
        hit1 = {"college_name": "MIT", "url": "", "title": "",
                "content": "", "page_type": "", "crawled_at": "",
                "url_canonical": "", "distance": 0.9}
        hit2 = {"college_name": "Stanford", "url": "", "title": "",
                "content": "", "page_type": "", "crawled_at": "",
                "url_canonical": "", "distance": 0.8}
        results = [[hit1, hit2]]
        normalized = HybridRetriever._normalize_results(results)
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["college_name"], "MIT")


if __name__ == "__main__":
    unittest.main()
