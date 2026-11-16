import pytest

from app.generation.answer import Answer
from scripts.compare_prompts import (
    UnstableRun,
    check_clean,
    error_row,
    extra_sources,
    gate_row,
    has_expected_source,
    metrics,
    model_row,
)
from tests.fakes import fake_llm_result
from tests.test_answer import hit

ENTRY = {
    "id": "A01",
    "category": "answerable",
    "question": "q?",
    "expected_sources": ["file1.pdf"],
}


def answered(used, answerable=True, **llm):
    return model_row(
        ENTRY,
        Answer(
            answerable,
            "Some answer." if answerable else "",
            used,
            fake_llm_result('{"x":1}', **llm),
        ),
    )


def test_a_gated_question_has_the_gate_outcome_and_no_llm_data():
    row = gate_row(ENTRY)
    assert row["outcome"] == "gate" and row["raw"] is None and row["llm_ms"] is None


def test_an_answerable_model_reply_is_answered_with_sources_from_metadata():
    row = answered([hit(1)])
    assert row["outcome"] == "answered" and row["sources"] == ["file1.pdf"]
    assert row["raw"] == '{"x":1}' and row["tokens"]["attempts"] == 1


def test_a_model_refusal_has_the_model_outcome_and_no_sources():
    row = answered([], answerable=False)
    assert row["outcome"] == "model" and row["sources"] == [] and row["answer"] == ""


def test_an_llm_failure_becomes_an_error_row():
    row = error_row(ENTRY, ValueError("boom"))
    assert row["outcome"] == "error" and "ValueError: boom" in row["answer"]


def test_expected_source_check_needs_an_answered_row_citing_an_expected_file():
    assert has_expected_source(answered([hit(1)]))
    assert has_expected_source(answered([hit(2), hit(1)]))
    assert not has_expected_source(answered([hit(2)]))
    assert not has_expected_source(answered([], answerable=False))
    assert not has_expected_source(gate_row(ENTRY))


def test_extra_sources_lists_cited_files_beyond_the_expected_ones():
    assert extra_sources(answered([hit(1)])) == []
    assert extra_sources(answered([hit(1), hit(2), hit(3)])) == ["file2.pdf", "file3.pdf"]
    assert extra_sources(gate_row(ENTRY)) == []


def test_metrics_counts_each_category_separately():
    oos = {"category": "out_of_scope", "expected_sources": []}
    vague = {"category": "vague", "expected_sources": []}
    results = [
        answered([hit(1), hit(2)]),
        {**gate_row({**ENTRY, "id": "A02"}), "expected_sources": ["x"]},
        {**answered([hit(1)]), **oos, "id": "O01"},
        {**gate_row(ENTRY), **oos, "id": "O02"},
        {**answered([], answerable=False), **oos, "id": "O03"},
        {**gate_row(ENTRY), **vague, "id": "V01"},
    ]
    m = metrics(results)
    assert m["answerable"] == 2 and m["cited_expected"] == ["A01"]
    assert m["wrongly_refused"] == ["A02"]
    assert m["oos_answered"] == ["O01"] and m["oos_gate"] == 1 and m["oos_model"] == 1
    assert m["vague"] == [("V01", "gate")]
    assert m["over_cited"]["A01"] == ["file2.pdf"]


def test_a_fallback_or_a_retry_stops_the_stability_run():
    check_clean(answered([hit(1)]))
    with pytest.raises(UnstableRun, match="fell back"):
        check_clean(answered([hit(1)], fallback_used=True))
    with pytest.raises(UnstableRun, match="attempts"):
        check_clean(answered([hit(1)], attempts=2))
