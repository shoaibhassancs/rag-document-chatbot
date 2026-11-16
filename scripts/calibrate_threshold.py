"""Work out a starting SIMILARITY_THRESHOLD from real data (Milestone 3).

  uv run python -m scripts.calibrate_threshold [--questions data/calibration/questions.jsonl]

Embeds every calibration question in ONE Voyage request (input_type=query), searches the real
database, and prints every question's similarity scores. The proposal is just below the lowest
score an answerable question needs: for each answerable question, the lowest of the best scores of
its expected source files. The result is PROVISIONAL: the owner confirms it, and Milestone 5 tunes
it against the evaluation set. These questions are NOT the evaluation set.

Exit codes: 0 done, 1 problem with the input or database, 2 stopped by a Voyage account/rate error.
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.db import connect
from app.ingest.embedder import EmbeddingError, EmbeddingNotConfigured, build_embedder
from app.retrieval.search import Hit, search

DEFAULT_QUESTIONS = Path("data/calibration/questions.jsonl")
REPORT_PATH = Path("data/calibration/last_run.md")


@dataclass
class Row:
    question: dict
    hits: list[Hit]
    top1: float
    evidence: float | None = None  # answerable only: lowest best-score over expected sources
    missing: list[str] = field(default_factory=list)  # expected sources not in the top k


def load_questions(path: Path) -> list[dict]:
    questions = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            item = json.loads(line)
            for key in ("id", "category", "question"):
                if key not in item:
                    raise ValueError(f"{path}:{number}: missing '{key}'")
            questions.append(item)
    return questions


def score(question: dict, hits: list[Hit]) -> Row:
    row = Row(question, hits, hits[0].similarity if hits else 0.0)
    if question["category"] == "answerable":
        best: dict[str, float] = {}
        for hit in hits:
            best[hit.file] = max(best.get(hit.file, 0.0), hit.similarity)
        needed = question.get("expected_sources", [])
        row.missing = [f for f in needed if f not in best]
        found = [best[f] for f in needed if f in best]
        row.evidence = min(found) if found else None
    return row


def propose_threshold(rows: list[Row]) -> tuple[float | None, Row | None]:
    scored = [r for r in rows if r.evidence is not None]
    if not scored:
        return None, None
    lowest = min(scored, key=lambda r: r.evidence)
    return math.floor((lowest.evidence - 0.005) * 100) / 100, lowest


def render(rows: list[Row], threshold: float | None, lowest: Row | None, model: str) -> str:
    out = [
        f"# Threshold calibration (PROVISIONAL)\n\nEmbedding model: `{model}`. Cosine similarity.\n"
    ]
    out.append("| id | kind | question | top-1 | needed | rank 1-5 scores | top-1 file | verdict |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        q = r.question
        scores = " ".join(f"{h.similarity:.3f}" for h in r.hits)
        top = f"{r.hits[0].file} p{r.hits[0].page or '-'}" if r.hits else "-"
        if threshold is None:
            verdict = ""
        elif q["category"] == "answerable":
            if r.missing:
                verdict = "MISSED: " + ", ".join(r.missing)
            else:
                verdict = "kept" if r.evidence >= threshold else "WOULD BE DROPPED"
        else:
            verdict = "refused at gate" if r.top1 < threshold else "SLIPS THROUGH to the LLM"
        need = f"{r.evidence:.3f}" if r.evidence is not None else ""
        out.append(
            f"| {q['id']} {q['category']} | {q.get('kind', '')} | {q['question']} | "
            f"{r.top1:.3f} | {need} | {scores} | {top} | {verdict} |"
        )
    out.append("")
    answerable = [r for r in rows if r.question["category"] == "answerable"]
    others = [r for r in rows if r.question["category"] != "answerable"]
    if threshold is not None and lowest is not None:
        slips = [r for r in others if r.top1 >= threshold]
        oos = [r for r in others if r.question["category"] == "out_of_scope"]
        out.append(f"**Proposed threshold: {threshold:.2f} (PROVISIONAL)**\n")
        out.append(
            f"- Lowest score an answerable question needs: {lowest.evidence:.3f} "
            f"({lowest.question['id']}: {lowest.question['question']!r})."
        )
        out.append(
            f"- Answerable questions with every needed source kept: "
            f"{sum(1 for r in answerable if not r.missing and r.evidence >= threshold)}"
            f" of {len(answerable)}; retrieval misses (threshold cannot fix): "
            f"{sum(1 for r in answerable if r.missing)}."
        )
        if oos:
            top_oos = max(oos, key=lambda r: r.top1)
            out.append(
                f"- Out-of-scope questions that slip past the gate: "
                f"{sum(1 for r in oos if r.top1 >= threshold)} of {len(oos)} "
                f"(highest top-1: {top_oos.top1:.3f}, {top_oos.question['id']})."
            )
        vague = [r for r in others if r.question["category"] == "vague"]
        if vague:
            out.append(
                f"- Vague questions that pass the gate: "
                f"{sum(1 for r in vague if r.top1 >= threshold)} of {len(vague)} "
                f"(Milestone 4's clarify step handles these)."
            )
        if slips:
            out.append(
                "- Slipping questions reach the answer model, which must say 'not answerable'."
            )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propose a starting similarity threshold.")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        questions = load_questions(args.questions)
    except (OSError, ValueError) as exc:
        print(f"Cannot read the questions: {exc}")
        return 1
    try:
        embedder = build_embedder(settings)
        vectors = embedder.embed([q["question"] for q in questions], "query").vectors
    except EmbeddingNotConfigured as exc:
        print(f"Cannot embed: {exc}")
        return 1
    except EmbeddingError as exc:
        print(f"Embedding stopped: {exc}")
        return 2
    print(f"Embedded {len(questions)} questions in {embedder.requests} Voyage request(s).")

    rows = []
    with connect(settings.database_url) as conn:
        for question, vector in zip(questions, vectors, strict=True):
            hits = search(
                conn, vector, top_k=settings.top_k, embedding_model=settings.embedding_model
            )
            rows.append(score(question, hits))
    if not any(r.hits for r in rows):
        print("The database has no embedded chunks. Run scripts.ingest_folder first.")
        return 1
    threshold, lowest = propose_threshold(rows)
    report = render(rows, threshold, lowest, settings.embedding_model)
    print(report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"Saved to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
