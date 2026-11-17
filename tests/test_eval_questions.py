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


def test_the_shipped_file_is_fully_approved_and_loads_without_allow_draft(loaded):
    # Changed deliberately (INSTRUCTIONS_17): this used to assert the all-DRAFT state.
    header, items = loaded
    assert header["_header"].startswith("APPROVED by the owner, Nov 17, 2026")
    assert len(items) == 40 and all(q["status"] == "APPROVED" for q in items)
    assert not is_draft(header, items)
    assert "DRAFT" not in header["_header"].upper().split(",")[0]


def test_one_question_set_back_to_draft_makes_the_whole_file_draft(loaded):
    header, items = loaded
    reverted = [dict(q) for q in items]
    reverted[7]["status"] = "DRAFT"
    assert is_draft(header, reverted)
    assert is_draft({"_header": "DRAFT: x"}, items)


def test_the_file_passes_validation_against_the_sample_documents(loaded):
    _, items = loaded
    assert validate(items, SAMPLE_DOCS) == []


def test_the_mix_is_29_answerable_7_out_of_scope_and_4_vague(loaded):
    # Changed deliberately (INSTRUCTIONS_16): E31 moved from out of scope to answerable.
    _, items = loaded
    counts = {c: sum(1 for q in items if q["category"] == c) for c in qmod.CATEGORIES}
    assert counts == {"answerable": 29, "out_of_scope": 7, "vague": 4}
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
    for kind in ("another clinic", "private information", "diagnosis"):
        assert kind in notes
    # Changed deliberately (INSTRUCTIONS_16): the injection-style question E31 is now answerable
    # (the document does answer it), so "injection" is checked over the whole set instead.
    assert any("injection" in q.get("note", "") for q in items if q["category"] == "answerable")


def test_e31_is_answerable_from_the_waiting_room_notice(loaded):
    _, items = loaded
    e31 = next(q for q in items if q["id"] == "E31")
    assert e31["category"] == "answerable" and e31["expected_status"] == "answered"
    assert e31["expected_sources"] == ["waiting-room-notice.txt"]
    assert "ignore your rules" in e31["question"].lower()  # still the injection test
    assert "BrightSmile-Guest" in e31["expected_answer"] and "no password" in e31["expected_answer"]
    # the document really says it
    notice = (SAMPLE_DOCS / "waiting-room-notice.txt").read_text()
    assert "BrightSmile-Guest" in notice and "no password" in notice


def test_no_out_of_scope_question_repeats_a_document_title_or_filename(loaded):
    # Cheap check that is feasible: a question must not contain a document's file name or its
    # first-line title word for word (case-insensitive). It does not prove a topic is absent from
    # the documents (that was checked by reading them); it only catches the obvious overlap.
    _, items = loaded
    titles = {}
    for path in SAMPLE_DOCS.iterdir():
        titles[path.name.lower()] = path.name
        first = path.read_text(errors="ignore").splitlines()[:1] if path.suffix != ".pdf" else []
        title = first[0].lstrip("# ").strip().lower() if first else ""
        if len(title) >= 8:
            titles[title] = path.name
    for q in items:
        if q["category"] == "out_of_scope":
            text = q["question"].lower()
            for needle, doc in titles.items():
                assert needle not in text, (q["id"], doc)


def test_the_four_vague_patterns_are_covered(loaded):
    _, items = loaded
    notes = " ".join(q.get("note", "") for q in items if q["category"] == "vague")
    for pattern in ("missing service", "missing time", "missing person", "bare pronoun"):
        assert pattern in notes


def test_the_readme_says_the_questions_are_approved_and_that_no_score_exists():
    # Changed deliberately (INSTRUCTIONS_17): the DRAFT banner became the approval statement.
    text = README.read_text()
    assert text.startswith("# Evaluation")
    assert "approved by the owner on Nov 17, 2026" in text and "DRAFT: not approved" not in text
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
