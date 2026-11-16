import json

import pytest

from app.retrieval.search import Hit
from scripts.calibrate_threshold import load_questions, propose_threshold, render, score


def hit(file, similarity):
    return Hit(1, "d", file, None, None, "t", similarity)


def q(id, category, sources=(), question="?"):
    return {"id": id, "category": category, "question": question, "expected_sources": list(sources)}


def test_the_shipped_question_file_is_valid_and_varied():
    from scripts.calibrate_threshold import DEFAULT_QUESTIONS

    questions = load_questions(DEFAULT_QUESTIONS)
    kinds = {x["kind"] for x in questions}
    assert {"terse", "typo", "multi_part", "two_documents", "near_miss", "paraphrase"} <= kinds
    assert len({x["id"] for x in questions}) == len(questions)
    cats = [x["category"] for x in questions]
    assert cats.count("answerable") >= 14 and cats.count("out_of_scope") >= 6
    assert cats.count("vague") >= 2
    assert all(x["expected_sources"] for x in questions if x["category"] == "answerable")
    assert "injection" in kinds
    assert 1 <= sum(1 for x in questions if x.get("smoke")) <= 10


def test_evidence_is_the_lowest_best_score_over_the_needed_sources():
    row = score(
        q("A", "answerable", ["a.md", "b.md"]),
        [hit("a.md", 0.7), hit("a.md", 0.6), hit("b.md", 0.5), hit("c.md", 0.4)],
    )
    assert row.evidence == 0.5 and row.missing == [] and row.top1 == 0.7


def test_a_needed_source_outside_the_top_k_is_reported_as_missed():
    row = score(q("A", "answerable", ["a.md", "b.md"]), [hit("a.md", 0.7)])
    assert row.missing == ["b.md"] and row.evidence == 0.7


def test_threshold_is_just_below_the_lowest_evidence_and_never_above_it():
    rows = [
        score(q("A1", "answerable", ["a.md"]), [hit("a.md", 0.6234)]),
        score(q("A2", "answerable", ["b.md"]), [hit("b.md", 0.5251)]),
        score(q("O1", "out_of_scope"), [hit("a.md", 0.45)]),
    ]
    threshold, lowest = propose_threshold(rows)
    assert threshold == 0.52 and lowest.question["id"] == "A2"
    assert threshold < 0.5251


def test_no_answerable_evidence_means_no_proposal():
    assert propose_threshold([score(q("O", "out_of_scope"), [hit("a.md", 0.4)])]) == (None, None)


def test_the_report_counts_what_slips_through_and_what_would_be_dropped():
    rows = [
        score(q("A1", "answerable", ["a.md"]), [hit("a.md", 0.60)]),
        score(q("O1", "out_of_scope"), [hit("a.md", 0.58)]),
        score(q("O2", "out_of_scope"), [hit("a.md", 0.30)]),
        score(q("V1", "vague"), [hit("a.md", 0.55)]),
    ]
    threshold, lowest = propose_threshold(rows)
    report = render(rows, threshold, lowest, "voyage-4")
    assert "Proposed threshold: 0.59 (PROVISIONAL)" in report
    assert "slip past the gate: 0 of 2" in report  # 0.58 < 0.59, so it is refused
    assert "SLIPS THROUGH" not in report
    assert report.count("refused at gate") == 3
    rows[1] = score(q("O1", "out_of_scope"), [hit("a.md", 0.595)])
    assert "slip past the gate: 1 of 2" in render(rows, 0.59, lowest, "voyage-4")


def test_bad_question_files_are_reported(tmp_path):
    bad = tmp_path / "q.jsonl"
    bad.write_text(json.dumps({"id": "x"}) + "\n")
    with pytest.raises(ValueError, match="missing 'category'"):
        load_questions(bad)
