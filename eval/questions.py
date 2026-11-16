"""Load and validate the evaluation question file (JSON Lines).

The first line is a header object `{"_header": "DRAFT: ..."}`. Every question carries its own
`status` ("DRAFT" until the owner approves it). Validation returns a list of problems instead of
raising, so tests and the runner can show all of them at once.
"""

import hashlib
import json
from pathlib import Path

CATEGORIES = {"answerable", "out_of_scope", "vague"}
EXPECTED_STATUS = {"answerable": "answered", "out_of_scope": "refused", "vague": "clarify"}
REQUIRED = ("id", "category", "question", "expected_sources", "expected_answer", "expected_status")
DRAFT = "DRAFT"


class QuestionFileError(Exception):
    pass


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_questions(path: Path) -> tuple[dict, list[dict]]:
    """Return (header, questions). Raises QuestionFileError for unreadable or malformed lines."""
    header: dict = {}
    questions: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QuestionFileError(f"Cannot read {path}: {type(exc).__name__}") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise QuestionFileError(f"{path}: line {number} is not valid JSON") from None
        if not isinstance(item, dict):
            raise QuestionFileError(f"{path}: line {number} is not a JSON object")
        if "_header" in item:
            header = item
        else:
            questions.append(item)
    return header, questions


def validate(questions: list[dict], sample_docs: Path | None = None) -> list[str]:
    """Problems found in the question list (empty list = fine)."""
    problems: list[str] = []
    seen: set[str] = set()
    for q in questions:
        qid = q.get("id", "?")
        for field in REQUIRED:
            if field not in q:
                problems.append(f"{qid}: missing field '{field}'")
        if qid in seen:
            problems.append(f"{qid}: duplicate id")
        seen.add(qid)
        category = q.get("category")
        if category not in CATEGORIES:
            problems.append(f"{qid}: unknown category {category!r}")
            continue
        if q.get("expected_status") != EXPECTED_STATUS[category]:
            problems.append(f"{qid}: expected_status should be {EXPECTED_STATUS[category]!r}")
        if not str(q.get("question", "")).strip():
            problems.append(f"{qid}: empty question")
        sources = q.get("expected_sources")
        if not isinstance(sources, list):
            problems.append(f"{qid}: expected_sources must be a list")
            continue
        if category == "answerable":
            if not sources:
                problems.append(f"{qid}: an answerable question needs expected_sources")
            if not str(q.get("expected_answer", "")).strip():
                problems.append(f"{qid}: an answerable question needs expected_answer")
        elif sources or q.get("expected_answer"):
            problems.append(f"{qid}: {category} questions have no expected sources or answer")
        if sample_docs is not None:
            for name in sources:
                if not (sample_docs / name).is_file():
                    problems.append(f"{qid}: expected source {name!r} is not in {sample_docs}")
        if "status" not in q:
            problems.append(f"{qid}: missing 'status' (DRAFT or APPROVED)")
        if "setup" in q and not str(q["setup"]).strip():
            problems.append(f"{qid}: empty setup message")
    return problems


def is_draft(header: dict, questions: list[dict]) -> bool:
    """True while the header says DRAFT or any question is still marked DRAFT."""
    if str(header.get("_header", "")).upper().startswith(DRAFT):
        return True
    return any(q.get("status") == DRAFT for q in questions)
