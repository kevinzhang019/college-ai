"""
RAG Evaluation Runner

Evaluates the RAG pipeline against a golden test set using both
custom metrics and RAGAS metrics (when available).

Usage:
    python -m tests.rag.eval.evaluate [--ragas] [--limit N]

Metrics computed:
  - Classification accuracy: query_type match rate
  - School extraction accuracy: detected vs expected schools
  - Retrieval coverage: % of queries with non-empty retrieval results
  - Citation accuracy: % of responses with valid citations
  - (Optional) RAGAS faithfulness/relevancy/context precision
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

logger = logging.getLogger(__name__)


def load_golden_set(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load the golden test set."""
    if path is None:
        path = str(Path(__file__).parent / "golden_set.json")
    with open(path, "r") as f:
        return json.load(f)


def evaluate_classification(golden_set: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate query classifier accuracy against golden set."""
    from college_ai.rag.classifier import classify_query

    correct_type = 0
    correct_complexity = 0
    total = len(golden_set)
    results = []

    for item in golden_set:
        question = item["question"]
        intent = classify_query(question)

        type_match = intent.query_type == item["expected_query_type"]
        complexity_match = intent.complexity == item["expected_complexity"]

        correct_type += int(type_match)
        correct_complexity += int(complexity_match)

        results.append({
            "question": question[:60],
            "expected_type": item["expected_query_type"],
            "got_type": intent.query_type,
            "type_match": type_match,
            "expected_complexity": item["expected_complexity"],
            "got_complexity": intent.complexity,
            "complexity_match": complexity_match,
        })

    return {
        "type_accuracy": correct_type / total if total else 0,
        "complexity_accuracy": correct_complexity / total if total else 0,
        "total": total,
        "details": results,
    }


def evaluate_school_extraction(golden_set: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate school extraction accuracy against golden set."""
    from college_ai.rag.router import QueryRouter

    router = QueryRouter()
    correct = 0
    total_with_schools = 0
    results = []

    for item in golden_set:
        expected = item.get("expected_schools", [])
        if not expected:
            continue

        total_with_schools += 1
        question = item["question"]
        pre = router.classify(question)
        detected = pre.detected_schools

        # Check if all expected schools were found (case-insensitive partial match)
        found_all = True
        for exp in expected:
            exp_lower = exp.lower()
            if not any(exp_lower in d.lower() or d.lower() in exp_lower
                       for d in detected):
                found_all = False
                break

        correct += int(found_all)
        results.append({
            "question": question[:60],
            "expected": expected,
            "detected": detected,
            "match": found_all,
        })

    return {
        "accuracy": correct / total_with_schools if total_with_schools else 0,
        "total": total_with_schools,
        "details": results,
    }


def evaluate_full_pipeline(
    golden_set: List[Dict[str, Any]],
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the full RAG pipeline and evaluate retrieval + generation quality."""
    from college_ai.rag.service import CollegeRAG

    rag = CollegeRAG()
    items = golden_set[:limit] if limit else golden_set

    retrieval_hits = 0
    citation_valid = 0
    total = len(items)
    results = []

    for item in items:
        question = item["question"]
        try:
            result = rag.answer_question(question, top_k=8)

            has_sources = result.get("source_count", 0) > 0
            retrieval_hits += int(has_sources)

            answer = result.get("answer", "")
            has_citations = bool(
                answer and "[" in answer and "]" in answer
                and "may not be fully grounded" not in answer
            )
            citation_valid += int(has_citations)

            results.append({
                "question": question[:60],
                "query_type": result.get("query_type"),
                "source_count": result.get("source_count", 0),
                "confidence": result.get("confidence"),
                "has_citations": has_citations,
                "answer_length": len(answer),
            })
        except Exception as exc:
            logger.error("Pipeline failed for %r: %s", question[:40], exc)
            results.append({
                "question": question[:60],
                "error": str(exc),
            })

    return {
        "retrieval_coverage": retrieval_hits / total if total else 0,
        "citation_rate": citation_valid / total if total else 0,
        "total": total,
        "details": results,
    }


def evaluate_ragas(
    golden_set: List[Dict[str, Any]],
    limit: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run RAGAS evaluation if the library is available."""
    try:
        from ragas.metrics.collections import Faithfulness, AnswerRelevancy
        from ragas.llms import llm_factory
        from openai import AsyncOpenAI
        import asyncio
    except ImportError:
        logger.warning("ragas not installed. Run: pip install ragas>=0.2.0")
        return None

    from college_ai.rag.service import CollegeRAG

    rag = CollegeRAG()
    items = golden_set[:limit] if limit else golden_set

    client = AsyncOpenAI()
    llm = llm_factory("gpt-4o-mini", client=client)
    faithfulness_scorer = Faithfulness(llm=llm)
    relevancy_scorer = AnswerRelevancy(llm=llm)

    scores = {"faithfulness": [], "relevancy": []}

    async def score_item(item):
        question = item["question"]
        result = rag.answer_question(question, top_k=8)
        answer = result.get("answer", "")
        contexts = [
            s.get("content", "")[:2000]
            for s in result.get("sources", [])
        ]
        if not contexts:
            return None

        try:
            f_result = await faithfulness_scorer.ascore(
                user_input=question,
                response=answer,
                retrieved_contexts=contexts,
            )
            r_result = await relevancy_scorer.ascore(
                user_input=question,
                response=answer,
                retrieved_contexts=contexts,
            )
            return {
                "faithfulness": f_result.value,
                "relevancy": r_result.value,
            }
        except Exception as exc:
            logger.error("RAGAS scoring failed for %r: %s", question[:40], exc)
            return None

    loop = asyncio.new_event_loop()
    for item in items:
        result = loop.run_until_complete(score_item(item))
        if result:
            scores["faithfulness"].append(result["faithfulness"])
            scores["relevancy"].append(result["relevancy"])
    loop.close()

    return {
        "avg_faithfulness": (
            sum(scores["faithfulness"]) / len(scores["faithfulness"])
            if scores["faithfulness"] else None
        ),
        "avg_relevancy": (
            sum(scores["relevancy"]) / len(scores["relevancy"])
            if scores["relevancy"] else None
        ),
        "count": len(scores["faithfulness"]),
    }


def print_summary(classification, extraction, pipeline, ragas=None):
    """Print a formatted evaluation summary."""
    print("\n" + "=" * 60)
    print("RAG EVALUATION SUMMARY")
    print("=" * 60)

    print(f"\n--- Classification Accuracy ({classification['total']} queries) ---")
    print(f"  Query type accuracy:  {classification['type_accuracy']:.1%}")
    print(f"  Complexity accuracy:  {classification['complexity_accuracy']:.1%}")

    # Show misclassified
    misclassed = [d for d in classification["details"] if not d["type_match"]]
    if misclassed:
        print(f"  Misclassified ({len(misclassed)}):")
        for d in misclassed[:5]:
            print(f"    {d['question']}...")
            print(f"      expected={d['expected_type']}, got={d['got_type']}")

    print(f"\n--- School Extraction ({extraction['total']} queries with schools) ---")
    print(f"  Accuracy:             {extraction['accuracy']:.1%}")

    missed = [d for d in extraction["details"] if not d["match"]]
    if missed:
        print(f"  Missed ({len(missed)}):")
        for d in missed[:5]:
            print(f"    {d['question']}...")
            print(f"      expected={d['expected']}, got={d['detected']}")

    if pipeline:
        print(f"\n--- Full Pipeline ({pipeline['total']} queries) ---")
        print(f"  Retrieval coverage:   {pipeline['retrieval_coverage']:.1%}")
        print(f"  Citation rate:        {pipeline['citation_rate']:.1%}")

    if ragas:
        print(f"\n--- RAGAS Metrics ({ragas['count']} scored) ---")
        if ragas.get("avg_faithfulness") is not None:
            print(f"  Avg faithfulness:     {ragas['avg_faithfulness']:.3f}")
        if ragas.get("avg_relevancy") is not None:
            print(f"  Avg relevancy:        {ragas['avg_relevancy']:.3f}")

    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="RAG Evaluation Runner")
    parser.add_argument("--ragas", action="store_true",
                        help="Run RAGAS metrics (requires ragas package)")
    parser.add_argument("--full", action="store_true",
                        help="Run full pipeline evaluation (requires API keys)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of queries for full/RAGAS eval")
    parser.add_argument("--golden-set", type=str, default=None,
                        help="Path to golden set JSON file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    golden_set = load_golden_set(args.golden_set)
    print(f"Loaded {len(golden_set)} golden set entries")

    # Always run classification + extraction (fast, cheap)
    print("\nEvaluating classification...")
    classification = evaluate_classification(golden_set)

    print("Evaluating school extraction...")
    extraction = evaluate_school_extraction(golden_set)

    # Full pipeline only on request (hits vector DB + LLMs)
    pipeline = None
    if args.full:
        print("Running full pipeline evaluation...")
        pipeline = evaluate_full_pipeline(golden_set, limit=args.limit)

    # RAGAS only on request (expensive)
    ragas = None
    if args.ragas:
        print("Running RAGAS evaluation...")
        ragas = evaluate_ragas(golden_set, limit=args.limit or 10)

    print_summary(classification, extraction, pipeline, ragas)


if __name__ == "__main__":
    main()
