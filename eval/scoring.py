"""The evaluation metrics, as pure functions over per-question result rows.

A row is one scored question in one run (a dict). Fields used here:
  qid, category, expected_status, expected_sources
  status                 the /chat status that came back
  retrieved              [{"chunk_id", "file", "page", "similarity"}]: the top-k, before the
                         similarity threshold
  latency_ms
  correctness            {"score": 0|1|None, ...}   present only for judged answers
  faithfulness           {"score": 0|1|None, ...}
  setup_not_answered     True when this follow-up's setup turn was a clarify or an error

Definitions (SPEC.md Evaluation; the first four are the targets):
  retrieval hit rate   answerable questions: ALL expected sources are in the retrieved top-k
                       ("any" is reported as well). PROVISIONAL: headline is "all".
  faithfulness         judged answers: share with score 1. Unjudged are excluded and counted.
  correctness          ALL answerable questions: share with score 1. A refused, clarified or errored
                       answerable question counts as 0. Answered but unjudged: excluded and counted.
  refusal accuracy     answerable + out-of-scope: out-of-scope were refused, answerable were not.
  status accuracy      all questions: status equals expected_status (also per category). No target.
  latency              median, 95th percentile and max of latency_ms over all questions.
Nothing here ever turns a missing judge score into 0 or 1.
"""

import statistics

TARGETS = {
    "retrieval_hit_all": 0.90,
    "faithfulness": 0.90,
    "correctness": 0.85,
    "refusal_accuracy": 0.95,
    "latency_p50_ms": 3000,
}


def _share(good: int, total: int) -> dict:
    return {"value": (good / total) if total else None, "good": good, "n": total}


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in 0..100). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without importing math
    return ordered[int(rank) - 1]


def retrieved_files(row: dict) -> set[str]:
    return {r["file"] for r in row.get("retrieved") or []}


def hit_all(row: dict) -> bool:
    expected = set(row.get("expected_sources") or [])
    return bool(expected) and expected <= retrieved_files(row)


def hit_any(row: dict) -> bool:
    expected = set(row.get("expected_sources") or [])
    return bool(expected & retrieved_files(row))


def _score(block) -> int | None:
    """The judge score (0 or 1) if there is a valid one, else None."""
    if isinstance(block, dict) and block.get("score") in (0, 1) and not block.get("error"):
        return block["score"]
    return None


def compute_metrics(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["category"] == "answerable"]
    oos = [r for r in rows if r["category"] == "out_of_scope"]

    judged_correct = [r for r in answerable if r["status"] == "answered"]
    correct_good = sum(1 for r in answerable if _score(r.get("correctness")) == 1)
    correct_unjudged = [r["qid"] for r in judged_correct if _score(r.get("correctness")) is None]
    # Denominator: every answerable question except answered ones the judge could not grade.
    correct_n = len(answerable) - len(correct_unjudged)

    faith_pool = [r for r in judged_correct if _score(r.get("faithfulness")) is not None]
    faith_unjudged = [r["qid"] for r in judged_correct if _score(r.get("faithfulness")) is None]
    faith_good = sum(1 for r in faith_pool if _score(r.get("faithfulness")) == 1)

    refusal_good = sum(1 for r in oos if r["status"] == "refused") + sum(
        1 for r in answerable if r["status"] != "refused"
    )

    per_category = {}
    for category in ("answerable", "out_of_scope", "vague"):
        subset = [r for r in rows if r["category"] == category]
        per_category[category] = _share(
            sum(1 for r in subset if r["status"] == r["expected_status"]), len(subset)
        )

    latencies = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    return {
        "questions": len(rows),
        "retrieval_hit_all": _share(sum(1 for r in answerable if hit_all(r)), len(answerable)),
        "retrieval_hit_any": _share(sum(1 for r in answerable if hit_any(r)), len(answerable)),
        "faithfulness": {**_share(faith_good, len(faith_pool)), "unjudged": faith_unjudged},
        "correctness": {**_share(correct_good, correct_n), "unjudged": correct_unjudged},
        "refusal_accuracy": _share(refusal_good, len(answerable) + len(oos)),
        "status_accuracy": _share(
            sum(1 for r in rows if r["status"] == r["expected_status"]), len(rows)
        ),
        "status_accuracy_by_category": per_category,
        "latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
            "n": len(latencies),
        },
        "errors": [r["qid"] for r in rows if r["status"] == "error"],
        "clarify_on_answerable": [r["qid"] for r in answerable if r["status"] == "clarify"],
        "setup_not_answered": [r["qid"] for r in rows if r.get("setup_not_answered")],
    }


def spread(values: list[float | None]) -> dict:
    """Mean, min and max over repeated runs (ignores runs where the metric is undefined)."""
    present = [v for v in values if v is not None]
    if not present:
        return {"mean": None, "min": None, "max": None, "runs": len(values)}
    return {
        "mean": sum(present) / len(present),
        "min": min(present),
        "max": max(present),
        "runs": len(values),
    }


def meets_target(metric: str, value: float | None) -> bool | None:
    """True/False against the SPEC target, None when there is no value or no target."""
    if value is None or metric not in TARGETS:
        return None
    if metric == "latency_p50_ms":
        return value < TARGETS[metric]
    return value >= TARGETS[metric]
