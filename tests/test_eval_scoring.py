import pytest

from eval import scoring
from eval.scoring import compute_metrics, hit_all, hit_any, meets_target, percentile, spread


def ret(*files):
    return [
        {"chunk_id": i, "file": f, "page": None, "similarity": 0.5} for i, f in enumerate(files)
    ]


def row(qid, category, status, expected_status=None, sources=(), retrieved=(), latency=1000, **kw):
    expected = (
        expected_status
        or {"answerable": "answered", "out_of_scope": "refused", "vague": "clarify"}[category]
    )
    return {
        "qid": qid,
        "category": category,
        "status": status,
        "expected_status": expected,
        "expected_sources": list(sources),
        "retrieved": ret(*retrieved),
        "latency_ms": latency,
        **kw,
    }


def verdict(score, error=None):
    return {"score": score, "reason": "r", "error": error}


def test_retrieval_hit_all_needs_every_expected_source_any_needs_one():
    both = row(
        "a", "answerable", "answered", sources=["x.md", "y.md"], retrieved=["x.md", "y.md", "z.md"]
    )
    one = row("b", "answerable", "answered", sources=["x.md", "y.md"], retrieved=["x.md"])
    none = row("c", "answerable", "answered", sources=["x.md"], retrieved=["z.md"])
    assert (hit_all(both), hit_all(one), hit_all(none)) == (True, False, False)
    assert (hit_any(both), hit_any(one), hit_any(none)) == (True, True, False)
    m = compute_metrics([both, one, none])
    assert m["retrieval_hit_all"]["good"] == 1 and m["retrieval_hit_any"]["good"] == 2
    assert m["retrieval_hit_all"]["n"] == 3


def test_correctness_counts_refused_and_clarified_answerables_as_wrong_not_excluded():
    rows = [
        row("a", "answerable", "answered", correctness=verdict(1), faithfulness=verdict(1)),
        row("b", "answerable", "answered", correctness=verdict(0), faithfulness=verdict(0)),
        row("c", "answerable", "refused"),
        row("d", "answerable", "clarify"),
    ]
    m = compute_metrics(rows)
    assert (m["correctness"]["good"], m["correctness"]["n"]) == (1, 4)
    assert m["correctness"]["value"] == 0.25 and m["correctness"]["unjudged"] == []
    assert (m["faithfulness"]["good"], m["faithfulness"]["n"]) == (1, 2)


def test_unjudged_answers_are_excluded_and_counted_never_scored_as_zero_or_one():
    rows = [
        row("a", "answerable", "answered", correctness=verdict(1), faithfulness=verdict(1)),
        row(
            "b",
            "answerable",
            "answered",
            correctness=verdict(None, "unusable judge reply twice"),
            faithfulness=verdict(None, "unusable judge reply twice"),
        ),
        row("c", "answerable", "answered"),  # not judged yet
    ]
    m = compute_metrics(rows)
    assert (m["correctness"]["good"], m["correctness"]["n"]) == (1, 1)
    assert m["correctness"]["unjudged"] == ["b", "c"] and m["faithfulness"]["unjudged"] == [
        "b",
        "c",
    ]
    assert m["faithfulness"]["value"] == 1.0


def test_a_score_outside_zero_and_one_is_not_counted():
    rows = [row("a", "answerable", "answered", correctness=verdict(2), faithfulness=verdict(True))]
    m = compute_metrics(rows)
    assert m["correctness"]["unjudged"] == ["a"]


def test_refusal_accuracy_covers_answerable_and_out_of_scope_only():
    rows = [
        row("a", "answerable", "answered"),
        row("b", "answerable", "refused"),  # wrongly refused
        row("c", "answerable", "clarify"),  # not refused: counts as fine for refusal accuracy
        row("d", "out_of_scope", "refused"),
        row("e", "out_of_scope", "answered"),  # wrongly answered
        row("f", "vague", "refused"),  # ignored here
    ]
    m = compute_metrics(rows)
    assert (m["refusal_accuracy"]["good"], m["refusal_accuracy"]["n"]) == (3, 5)


def test_status_accuracy_overall_and_per_category():
    rows = [
        row("a", "answerable", "answered"),
        row("b", "answerable", "clarify"),
        row("c", "out_of_scope", "refused"),
        row("d", "vague", "clarify"),
        row("e", "vague", "answered"),
    ]
    m = compute_metrics(rows)
    assert (m["status_accuracy"]["good"], m["status_accuracy"]["n"]) == (3, 5)
    by = m["status_accuracy_by_category"]
    assert by["answerable"]["value"] == 0.5 and by["vague"]["value"] == 0.5
    assert by["out_of_scope"]["value"] == 1.0
    assert m["clarify_on_answerable"] == ["b"]


def test_latency_percentiles_use_all_questions():
    rows = [
        row(str(i), "answerable", "answered", latency=ms)
        for i, ms in enumerate([100, 200, 300, 400, 1000])
    ]
    lat = compute_metrics(rows)["latency_ms"]
    assert (lat["p50"], lat["p95"], lat["max"], lat["n"]) == (300, 1000, 1000, 5)


def test_percentile_edge_cases():
    assert percentile([], 95) is None
    assert percentile([5], 50) == 5 and percentile([1, 2, 3, 4], 50) == 2
    assert percentile(list(range(1, 101)), 95) == 95


def test_errors_and_setup_not_answered_are_listed():
    rows = [
        row("a", "answerable", "error"),
        row("b", "answerable", "answered", setup_not_answered=True),
    ]
    m = compute_metrics(rows)
    assert m["errors"] == ["a"] and m["setup_not_answered"] == ["b"]


def test_empty_input_gives_undefined_values_not_a_crash():
    m = compute_metrics([])
    assert m["correctness"]["value"] is None and m["latency_ms"]["p50"] is None


def test_spread_ignores_undefined_runs():
    assert spread([0.8, 0.9, None]) == {
        "mean": pytest.approx(0.85),
        "min": 0.8,
        "max": 0.9,
        "runs": 3,
    }
    assert spread([None])["mean"] is None


def test_targets_come_from_the_spec_and_latency_is_strictly_under_three_seconds():
    assert scoring.TARGETS == {
        "retrieval_hit_all": 0.90,
        "faithfulness": 0.90,
        "correctness": 0.85,
        "refusal_accuracy": 0.95,
        "latency_p50_ms": 3000,
    }
    assert meets_target("correctness", 0.85) is True and meets_target("correctness", 0.84) is False
    assert (
        meets_target("latency_p50_ms", 2999) is True
        and meets_target("latency_p50_ms", 3000) is False
    )
    assert (
        meets_target("status_accuracy", 1.0) is None and meets_target("correctness", None) is None
    )
