"""scripts/check_judge.py: the judge sanity check (no live calls)."""

import json
import re
from pathlib import Path

from eval.judge import JudgeError, Verdict
from scripts import check_judge as cj

ROOT = Path(__file__).resolve().parent.parent


class ScriptedJudge:
    model = "fake"
    versions = {"correctness": "c1", "faithfulness": "f1", "combined": "x"}

    def __init__(self, correct=None, faithful=None, default=1, error_ids=()):
        self.correct, self.faithful = correct or {}, faithful or {}
        self.default, self.error_ids = default, set(error_ids)
        self._by_answer = {}

    def correctness(self, question, expected, answer):
        return self._answer("c", answer)

    def faithfulness(self, answer, chunks):
        return self._answer("f", answer)

    def _answer(self, kind, answer):
        if answer in self.error_ids:
            raise JudgeError("LLMBusyError")
        table = self.correct if kind == "c" else self.faithful
        return Verdict(table.get(answer, self.default), "r", 1)


def item(id, role, cand, sc, sf):
    return {
        "id": id, "tests": "t", "role": role, "question": "q", "chunks": ["c"],
        "expected_answer": "e", "candidate_answer": cand,
        "should_be_correct": sc, "should_be_faithful": sf,
    }  # fmt: skip


def run(items, judge):
    rows = [cj.judge_item(judge, i) for i in items]
    return rows, cj.summarize(rows)


def test_controls_file_is_valid_and_covers_every_role():
    items = cj.load_controls()
    assert len(items) == 12 and len({i["id"] for i in items}) == 12
    roles = [i["role"] for i in items]
    assert roles.count("good") == 3 and roles.count("unfaithful") == 3
    assert roles.count("wrong") == 3 and roles.count("strict") == 1 and roles.count("empty") == 2
    assert [i["should_be_faithful"] for i in items if i["role"] == "unfaithful"] == [0, 0, 0]
    assert [i["should_be_correct"] for i in items if i["role"] == "wrong"] == [0, 0, 0]


def test_control_chunks_are_copied_from_the_sample_docs_and_not_from_the_evaluation():
    docs = " ".join(
        re.sub(r"\s+", " ", p.read_text(encoding="utf-8", errors="ignore"))
        for p in (ROOT / "data" / "sample_docs").iterdir()
        if p.suffix in {".md", ".txt"}
    )
    questions = [
        json.loads(line)["question"].lower()
        for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and "question" in json.loads(line)
    ]
    for control in cj.load_controls():
        for chunk in control["chunks"]:
            assert re.sub(r"\s+", " ", chunk) in docs, control["id"]
        text = " ".join([control["question"], control["candidate_answer"]]).lower()
        asked = control["question"].lower()
        for q in questions:
            assert asked != q and q not in text and asked not in q, (control["id"],)


def test_all_good_and_all_caught_gives_no_miss():
    items = [
        item("G", "good", "good", 1, 1),
        item("U", "unfaithful", "unf", 1, 0),
        item("W", "wrong", "wr", 0, None),
    ]
    judge = ScriptedJudge(correct={"wr": 0}, faithful={"unf": 0})
    rows, s = run(items, judge)
    assert (s["negatives_caught"], s["negatives"], s["positives_kept"], s["positives"]) == (
        2,
        2,
        3,
        3,
    )
    assert s["misses"] == [] and "No miss" in cj.summary_line(s)


def test_a_missed_negative_control_is_a_miss_and_the_summary_says_so():
    items = [item("U", "unfaithful", "unf", 1, 0)]
    rows, s = run(items, ScriptedJudge())  # the judge says 1 to everything
    assert rows[0]["faithful_outcome"] == "MISS"
    assert s["negatives_caught"] == 0 and s["negatives"] == 1
    assert "MISS: U faithful" in cj.summary_line(s)


def test_an_errored_judge_call_is_never_a_pass():
    items = [item("G", "good", "boom", 1, 1), item("W", "wrong", "boom2", 0, None)]
    rows, s = run(items, ScriptedJudge(error_ids={"boom", "boom2"}))
    assert rows[0]["correct"]["score"] is None and rows[0]["correct_outcome"] == "ERROR"
    assert s["positives_kept"] == 0 and s["negatives_caught"] == 0 and s["errors"] >= 3
    assert "error" in " ".join(s["misses"]) and "MISS" in cj.summary_line(s)


def test_the_strictness_item_is_reported_not_scored():
    rows, s = run([item("S", "strict", "part", "either", "either")], ScriptedJudge(default=0))
    assert rows[0]["correct_outcome"] == "REPORT" and s["misses"] == []
    assert s["strict"] == (0, 0) and "(0, 0)" in cj.summary_line(s)


def test_a_live_run_needs_confirm_live_and_the_dry_run_uses_fakes(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cj, "RESULTS_DIR", tmp_path)
    settings = Settings_none()
    assert cj.main([], settings=settings) == 2
    assert "--confirm-live" in capsys.readouterr().out
    assert cj.main(["--dry-run"], settings=settings) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: FAKE DATA" in out and "MISS" in out  # the fake judge says 1 to everything
    assert list(tmp_path.iterdir()) == []  # a dry run touches no project folder


def test_a_live_run_with_a_scripted_judge_writes_the_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cj, "RESULTS_DIR", tmp_path)
    judge = ScriptedJudge()
    assert (
        cj.main(["--confirm-live"], settings=Settings_none(), judge=judge, sleep=lambda s: None)
        == 0
    )
    assert (tmp_path / "judge-check__c1__f1.md").exists()
    assert len((tmp_path / "judge-check__c1__f1.jsonl").read_text().splitlines()) == 12


def Settings_none():
    from app.config import Settings

    return Settings(_env_file=None, groq_api_key="fake-groq-key")
