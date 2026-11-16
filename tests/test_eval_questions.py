import json
import re
from pathlib import Path

import pytest

from eval import questions as qmod
from eval.questions import (
    QuestionFileError,
    file_sha256,
    is_draft,
    load_questions,
    validate,
)

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.jsonl"
CALIBRATION = ROOT / "data" / "calibration" / "questions.jsonl"
SAMPLE_DOCS = ROOT / "data" / "sample_docs"
README = ROOT / "eval" / "README.md"


@pytest.fixture(scope="module")
def loaded():
    return load_questions(QUESTIONS)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def test_the_file_starts_with_the_draft_header_and_every_question_is_marked_draft(loaded):
    header, items = loaded
    assert header["_header"] == "DRAFT: not approved by the owner"
    assert items and all(q["status"] == "DRAFT" for q in items)
    assert is_draft(header, items)


def test_the_file_passes_validation_against_the_sample_documents(loaded):
    _, items = loaded
    assert validate(items, SAMPLE_DOCS) == []


def test_the_mix_is_28_answerable_8_out_of_scope_and_4_vague(loaded):
    _, items = loaded
    counts = {c: sum(1 for q in items if q["category"] == c) for c in qmod.CATEGORIES}
    assert counts == {"answerable": 28, "out_of_scope": 8, "vague": 4}
    assert len(items) == 40 and len({q["id"] for q in items}) == 40


def test_ids_are_e01_to_e40_and_never_start_like_the_calibration_ids(loaded):
    _, items = loaded
    assert [q["id"] for q in items] == [f"E{n:02d}" for n in range(1, 41)]


def test_no_question_is_in_the_calibration_set_even_ignoring_case_and_punctuation(loaded):
    _, items = loaded
    calibration = [
        json.loads(line) for line in CALIBRATION.read_text().splitlines() if line.strip()
    ]
    banned = {norm(c["question"]) for c in calibration}
    banned |= {norm(c["question"]) for c in calibration if "question" in c}
    mine = {norm(q["question"]) for q in items} | {norm(q["setup"]) for q in items if "setup" in q}
    assert not (banned & mine)


def test_every_expected_source_is_a_real_sample_document(loaded):
    _, items = loaded
    for q in items:
        for name in q["expected_sources"]:
            assert (SAMPLE_DOCS / name).is_file(), (q["id"], name)


def test_the_wording_rules_are_met(loaded):
    _, items = loaded
    answerable = [q for q in items if q["category"] == "answerable"]
    notes = [q.get("note", "") for q in answerable]
    assert sum("terse" in n for n in notes) >= 6
    assert sum("negative fact" in n for n in notes) >= 3
    assert sum("exact" in n for n in notes) >= 4
    assert sum(len(q["expected_sources"]) >= 2 for q in answerable) >= 2
    assert sum("setup" in q for q in answerable) == 4
    assert any(q["question"] == q["question"].lower() for q in answerable)  # lowercase
    assert any("typo" in n for n in notes)
    assert any(len(q["question"]) > 120 for q in answerable)  # a polite longer message


def test_out_of_scope_and_vague_questions_have_no_expected_answer(loaded):
    _, items = loaded
    for q in items:
        if q["category"] != "answerable":
            assert q["expected_sources"] == [] and q["expected_answer"] == ""


def test_out_of_scope_kinds_are_covered(loaded):
    _, items = loaded
    notes = " ".join(q.get("note", "") for q in items if q["category"] == "out_of_scope")
    for kind in ("injection", "another clinic", "private information", "diagnosis"):
        assert kind in notes


def test_the_four_vague_patterns_are_covered(loaded):
    _, items = loaded
    notes = " ".join(q.get("note", "") for q in items if q["category"] == "vague")
    for pattern in ("missing service", "missing time", "missing person", "bare pronoun"):
        assert pattern in notes


def test_the_readme_carries_the_draft_banner_and_the_no_score_statement():
    text = README.read_text()
    assert text.startswith("# Evaluation") and "**DRAFT: not approved by the owner.**" in text
    assert "No score exists yet" in text
    assert "no number may appear" in text.lower()
    assert "Faithfulness basis" in text and "under-reports" in text
    assert "about one day of that model's token budget" in text


def test_validate_reports_every_kind_of_problem():
    items = [
        {
            "id": "X1",
            "category": "answerable",
            "question": "q",
            "expected_sources": [],
            "expected_answer": "",
            "expected_status": "answered",
        },
        {
            "id": "X1",
            "category": "vague",
            "question": "q",
            "expected_sources": ["a.md"],
            "expected_answer": "",
            "expected_status": "answered",
            "status": "DRAFT",
        },
        {"id": "X3", "category": "weird"},
    ]
    problems = " | ".join(validate(items))
    for needle in (
        "needs expected_sources",
        "needs expected_answer",
        "duplicate id",
        "missing 'status'",
        "expected_status should be 'clarify'",
        "no expected sources or answer",
        "unknown category",
    ):
        assert needle in problems


def test_validate_checks_that_source_files_exist(tmp_path):
    items = [
        {
            "id": "X1",
            "category": "answerable",
            "question": "q",
            "expected_sources": ["no.md"],
            "expected_answer": "a",
            "expected_status": "answered",
            "status": "DRAFT",
        }
    ]
    assert "not in" in " ".join(validate(items, tmp_path))


def test_load_rejects_unreadable_and_malformed_files(tmp_path):
    with pytest.raises(QuestionFileError):
        load_questions(tmp_path / "missing.jsonl")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "X"}\nnot json\n')
    with pytest.raises(QuestionFileError, match="line 2"):
        load_questions(bad)
    bad.write_text("[1, 2]\n")
    with pytest.raises(QuestionFileError, match="not a JSON object"):
        load_questions(bad)


def test_is_draft_is_false_only_when_header_and_questions_are_approved():
    assert not is_draft({"_header": "Approved by the owner"}, [{"status": "APPROVED"}])
    assert is_draft({"_header": "Approved"}, [{"status": "DRAFT"}])
    assert is_draft({"_header": "DRAFT: x"}, [{"status": "APPROVED"}])


def test_the_file_hash_changes_when_the_file_changes(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text("a")
    first = file_sha256(path)
    path.write_text("b")
    assert first != file_sha256(path) and len(first) == 64
