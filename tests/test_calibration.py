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


# ----------------------------------------------------------------------------- the V3 gate rule


def _row(id, category, kind, top1, evidence=None, marked=False):
    from scripts.calibrate_threshold import Row

    question = {"id": id, "category": category, "kind": kind, "question": "?"}
    if marked:
        question["gate_should_refuse"] = True
    return Row(question, [hit("x.md", top1)], top1, evidence=evidence)


def test_the_gate_rule_uses_the_highest_unrelated_or_vague_top1_plus_0_03_rounded_up():
    from scripts.calibrate_threshold import gate_rule

    rows = [
        _row("A1", "answerable", "direct", 0.62, evidence=0.62),
        _row("O01", "out_of_scope", "unrelated", 0.300, marked=True),  # the ceiling
        _row("V01", "vague", "vague", 0.294, marked=True),
        _row("O07", "out_of_scope", "near_miss", 0.369),  # newly reaches the answer model
        _row("O02", "out_of_scope", "near_miss", 0.570),  # already reached it at 0.40
    ]
    rule = gate_rule(rows)
    assert rule["ceiling"] == 0.300 and rule["candidate"] == 0.33
    assert [e[0] for e in rule["extra"]] == ["O07"] and rule["already_through"] == 1
    assert rule["quiet_refused"] == 2 and rule["answerable_kept"] == 1


def test_the_gate_rule_rounds_up_not_to_nearest():
    from scripts.calibrate_threshold import gate_rule

    rows = [_row("O01", "out_of_scope", "unrelated", 0.301, marked=True)]
    assert gate_rule(rows)["candidate"] == 0.34  # 0.331 rounds UP to 0.34


def test_the_gate_report_names_the_tripwire_and_keeps_the_default():
    from scripts.calibrate_threshold import gate_rule, render_gate_rule

    text = render_gate_rule(
        gate_rule([_row("O01", "out_of_scope", "unrelated", 0.300, marked=True)])
    )
    assert "0.33" in text and "TRIPWIRE" in text and "0.35" in text and "old gate was 0.40" in text


def test_calibration_refuses_a_live_run_without_confirm_live_and_dry_run_prints_the_label(
    capsys,
):
    from app.config import Settings
    from scripts import calibrate_threshold as ct

    settings = Settings(_env_file=None)  # never the developer's real .env
    assert ct.main([], settings=settings) == 2
    assert "--confirm-live" in capsys.readouterr().out
    assert ct.main(["--dry-run"], settings=settings) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: FAKE DATA" in out and "Gate rule" in out


def test_the_gate_rule_on_the_saved_real_v3_table_gives_0_33_not_0_50():
    """The real V3 table (practice data). The dental-sounding 'unrelated' question (0.466) must not
    raise the ceiling: only the items marked gate_should_refuse count."""
    from pathlib import Path

    from scripts.calibrate_threshold import Row, gate_rule, render_gate_rule

    path = Path(__file__).parent / "fixtures" / "calibration_v3_table.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        question = {"id": d["id"], "category": d["category"], "kind": d["kind"], "question": "?"}
        if d["gate_should_refuse"]:
            question["gate_should_refuse"] = True
        rows.append(Row(question, [hit("x.md", d["top1"])], d["top1"], evidence=d["evidence"]))
    rule = gate_rule(rows)
    assert rule["ceiling"] == 0.300 and rule["candidate"] == 0.33
    assert rule["quiet"] == 5 and rule["quiet_refused"] == 5
    assert rule["answerable_kept"] == 16
    assert [e[0] for e in rule["extra"]] == ["O07"]
    text = render_gate_rule(rule)
    assert "0.50" not in text and "O07" in text and "TRIPWIRE" in text


def test_the_marked_practice_items_are_exactly_the_unrelated_and_vague_ones():
    from scripts.calibrate_threshold import DEFAULT_QUESTIONS

    marked = {x["id"] for x in load_questions(DEFAULT_QUESTIONS) if x.get("gate_should_refuse")}
    assert marked == {"O01", "O03", "O06", "V01", "V02"}
