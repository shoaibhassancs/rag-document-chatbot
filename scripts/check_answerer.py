"""V4: does the answer model refuse out-of-scope practice questions (and name-mismatch ones)?

  uv run python -m scripts.check_answerer --dry-run
  uv run python -m scripts.check_answerer --confirm-live --model openai/gpt-oss-20b
  uv run python -m scripts.check_answerer --confirm-live --model openai/gpt-oss-20b --gate 0.33

Questions (all PRACTICE data, NOT the evaluation set): the practice near-miss and injection items
of data/calibration/questions.jsonl plus the DRAFT entity-mismatch items of
data/calibration/entity_questions.jsonl (a named other clinic, another business, a named person
who is not on staff). Each is embedded as written (the answer model gets the user's own wording,
P1a), searched, gated, and sent to the answer model with the current answer prompt. Add
--include-answerable to also replay the practice answerables (use that to re-test a new answer
prompt). The answer-model fallback is OFF, so a result never mixes two models.

Decision rules printed with the report:
  * P4 (a new answer_v3 rule) ships ONLY if at least one entity-mismatch item is ANSWERED.
  * Gate tripwire (V3): if MORE THAN ONE practice out-of-scope or near-miss question that passes the
    candidate gate but not 0.40 is ANSWERED, use 0.35 as the gate instead of the candidate.

Paced under the Groq limits, resumable (git-ignored state), never prints a key. 1 Voyage request.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.generation.answer import AnswerFormatError, build_answerer
from app.generation.llm import LLMBusyError, LLMConfigError, LLMError
from app.retrieval.search import apply_threshold
from eval.fakes import FakeClock, FakeGroundedAnswerer, FakeQueryEmbedder, FakeSearcher
from eval.pacing import (
    CallTooLarge,
    DailyBudgetExceeded,
    Limits,
    Pacer,
    estimate_tokens_for_text,
)
from eval.state import ConfigMismatch, State, StateError
from scripts.calibrate_threshold import DEFAULT_QUESTIONS, load_questions
from scripts.compare_prompts import error_row, gate_row, model_row

ROOT = Path(__file__).resolve().parent.parent
ENTITY_QUESTIONS = ROOT / "data" / "calibration" / "entity_questions.jsonl"
STATE_DIR = ROOT / "eval" / ".state"
RESULTS_DIR = ROOT / "eval" / "results"
DRY_RUN_LINE = "DRY RUN: FAKE DATA"
OLD_GATE = 0.40
TRIPWIRE_GATE = 0.35
NOTE = "Practice questions, NOT the evaluation set. Not an evaluation result."
RULES = (
    "RULE P4: ship a new answer_v3 rule ONLY if at least one entity-mismatch item is ANSWERED.\n"
    f"RULE gate: if MORE THAN ONE out-of-scope or near-miss question that passes the gate but "
    f"not {OLD_GATE:.2f} is ANSWERED, use {TRIPWIRE_GATE:.2f} instead of the candidate gate."
)


def select_questions(practice: list[dict], entity: list[dict], include_answerable: bool):
    chosen = [q for q in practice if q.get("kind") in ("near_miss", "injection")] + entity
    if include_answerable:
        chosen = [q for q in practice if q["category"] == "answerable"] + chosen
    return chosen


def is_wrong(row: dict) -> bool:
    """An out-of-scope item that was answered, or an answerable one that was not answered."""
    if row["category"] == "answerable":
        return row["outcome"] != "answered"
    return row["outcome"] == "answered"


def summarize(rows: list[dict], gate: float) -> dict:
    entity = [r for r in rows if r.get("item_kind") == "entity_mismatch"]
    oos = [r for r in rows if r["category"] == "out_of_scope"]
    extra = [r for r in oos if gate <= (r.get("top1") or 0) < OLD_GATE]
    return {
        "entity_answered": [r["id"] for r in entity if r["outcome"] == "answered"],
        "entity": len(entity),
        "oos_answered": [r["id"] for r in oos if r["outcome"] == "answered"],
        "oos": len(oos),
        "extra_through": [r["id"] for r in extra if r["outcome"] != "gate"],
        "extra_answered": [r["id"] for r in extra if r["outcome"] == "answered"],
        "answerable_wrong": [
            r["id"] for r in rows if r["category"] == "answerable" and is_wrong(r)
        ],
        "errors": [r["id"] for r in rows if r["outcome"] == "error"],
    }


def report_markdown(banner_text: str, rows: list[dict], gate: float, dry_run: bool) -> str:
    s = summarize(rows, gate)
    lines = [DRY_RUN_LINE, ""] if dry_run else []
    lines += [
        "# V4: answer model on practice out-of-scope and entity-mismatch questions",
        "",
        "```",
        banner_text,
        "```",
        "",
        NOTE,
        "",
        "| id | kind | top-1 | outcome | wrong? | answer (first 120 characters) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        top = "-" if r.get("top1") is None else f"{r['top1']:.3f}"
        answer = (r.get("answer") or "").replace("|", "/").replace("\n", " ")[:120]
        lines.append(
            f"| {r['id']} | {r.get('item_kind') or r['category']} | {top} | {r['outcome']} | "
            f"{'YES' if is_wrong(r) else ''} | {answer} |"
        )
    p4 = bool(s["entity_answered"])
    lines += [
        "",
        f"Entity-mismatch items answered (should be refused): {len(s['entity_answered'])} of "
        f"{s['entity']}" + (f" ({', '.join(s['entity_answered'])})" if p4 else "") + ".",
        f"Out-of-scope items answered in total: {len(s['oos_answered'])} of {s['oos']}"
        + (f" ({', '.join(s['oos_answered'])})" if s["oos_answered"] else "")
        + ".",
        f"Items that pass gate {gate:.2f} but not {OLD_GATE:.2f}: {len(s['extra_through'])} "
        f"({', '.join(s['extra_through']) or 'none'}); of these answered: "
        f"{len(s['extra_answered'])}.",
    ]
    if s["answerable_wrong"]:
        lines.append(f"Practice answerables not answered: {', '.join(s['answerable_wrong'])}.")
    if s["errors"]:
        lines.append(f"Items with a model error: {', '.join(s['errors'])} (not a verdict).")
    lines += ["", RULES, ""]
    lines.append(
        "RESULT P4: "
        + (
            "TRIGGERED (an entity-mismatch item was answered): tell the owner, then write "
            "prompts/answer_v3.txt."
            if p4
            else "NOT triggered: no entity-mismatch item was answered. Do not change the prompt."
        )
    )
    tripped = len(s["extra_answered"]) > 1
    lines.append(
        "RESULT gate: "
        + (
            f"TRIPPED ({len(s['extra_answered'])} extra questions answered): use "
            f"{TRIPWIRE_GATE:.2f}."
            if tripped
            else f"not tripped ({len(s['extra_answered'])} extra answered): the candidate gate "
            "stands (the owner approves it)."
        )
    )
    return "\n".join(lines)


def banner(run_id: str, dry_run: bool, cfg: dict, n: int, calls: int, tokens: int) -> str:
    lines = [DRY_RUN_LINE] if dry_run else []
    lines += [
        "V4: ANSWER MODEL on practice out-of-scope and entity-mismatch questions",
        f"  run id        : {run_id}",
        f"  answer model  : {cfg['model']} (prompt {cfg['prompt']}, fallback OFF)",
        f"  gate          : {cfg['gate']}",
        f"  questions     : {n} (practice and DRAFT entity items; not the evaluation set)",
        f"  estimate      : up to {calls} answer calls and about {tokens:,} reserved tokens on the "
        "answer model; 1 Voyage request",
        *["  " + line for line in RULES.splitlines()],
    ]
    return "\n".join(lines)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    answerer_factory: Callable[[Settings], object] | None = None,
    embedder_factory: Callable[[Settings], object] | None = None,
    search_factory: Callable[[Settings], tuple[Callable, Callable]] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    import time

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="fakes only; not a result")
    parser.add_argument("--confirm-live", action="store_true", help="required for a live run")
    parser.add_argument("--model", default=None, help="answer model (default: ANSWER_LLM_MODEL)")
    parser.add_argument("--prompt", default=None, help="answer prompt version")
    parser.add_argument(
        "--gate", type=float, default=None, help="similarity gate (default: config)"
    )
    parser.add_argument("--include-answerable", action="store_true")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--entity-questions", type=Path, default=ENTITY_QUESTIONS)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    model = args.model or settings.answer_llm_model
    prompt = args.prompt or settings.answer_prompt_version
    gate = args.gate if args.gate is not None else settings.similarity_threshold
    if gate is None:
        out("REFUSING to start: no gate (SIMILARITY_THRESHOLD is not set; pass --gate).")
        return 2
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    try:
        practice = load_questions(args.questions)
        entity = load_questions(args.entity_questions)
    except (OSError, ValueError) as exc:
        out(f"Cannot read the questions: {exc}")
        return 2
    questions = select_questions(practice, entity, args.include_answerable)
    s = settings.model_copy(
        update={
            "answer_prompt_version": prompt,
            "answer_llm_model": model,
            "answer_fallback_enabled": False,
            "similarity_threshold": gate,
        }
    )
    cfg = {"model": model, "prompt": prompt, "gate": gate, "items": len(questions)}
    run_id = "answerer-check__" + _slug(f"{prompt}__{model}__gate{gate}")
    if args.include_answerable:
        run_id += "__with-answerable"
    if args.tag:
        run_id += "__" + _slug(args.tag)
    out_cap = s.llm_role("answer").max_output_tokens
    est_tokens = len(questions) * (2000 + out_cap)
    banner_text = banner(run_id, args.dry_run, cfg, len(questions), len(questions), est_tokens)
    out(banner_text)

    if not args.dry_run:
        if not args.confirm_live:
            out("\nREFUSING to start: a live run needs --confirm-live.")
            return 2
        role = s.llm_role("answer")
        if role.api_key is None or not role.api_key.get_secret_value():
            out("\nREFUSING to start: the answer model's API key setting has no value.")
            return 2

    closer = None
    if args.dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="v4-dryrun-state-"))
        results_dir = Path(tempfile.mkdtemp(prefix="v4-dryrun-results-"))
        fake = FakeClock()
        clock, sleep = fake, fake.sleep
        answerer = FakeGroundedAnswerer()
        embedder = FakeQueryEmbedder()
        searcher = FakeSearcher(["faq.md", "clinic-website.html", "services-and-prices.pdf"])
    else:
        state_dir, results_dir = STATE_DIR, RESULTS_DIR
        try:
            answerer = (answerer_factory or build_answerer)(s)
        except LLMConfigError as exc:
            out(f"\nREFUSING to start: {exc}")
            return 2
        embedder = searcher = None

    state_path = state_dir / f"{run_id}.jsonl"
    if args.fresh and state_path.exists():
        n = 1
        while (state_dir / f"{run_id}.superseded-{n}.jsonl").exists():
            n += 1
        shutil.move(state_path, state_dir / f"{run_id}.superseded-{n}.jsonl")
    try:
        state = State(state_path)
        state.check_config(cfg)
        state.start(cfg)
    except (ConfigMismatch, StateError) as exc:
        out(f"\nREFUSING to start: {exc}")
        return 2

    try:
        if not args.dry_run:
            searcher, closer = (search_factory or _real_search)(s)
            embedder = (embedder_factory or _real_embedder)(s)
        vectors = embedder.embed([q["question"] for q in questions], "query").vectors
        retrieved = [searcher(v) for v in vectors]
    except Exception as exc:  # embedding or database problem: report, never loop
        out(f"\nSTOPPED: {type(exc).__name__}: {exc}.")
        return 3
    finally:
        if closer:
            closer()
    out(f"Embedded {len(questions)} questions in {embedder.requests} Voyage request(s).")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    limits = Limits(
        s.groq_tokens_per_minute,
        s.groq_requests_per_minute,
        s.groq_tokens_per_day,
        s.groq_requests_per_day,
        s.eval_limit_margin,
    )
    pacer = Pacer({model: limits}, clock, sleep, state.daily_used(today))
    done = state.done("answer")
    try:
        for q, hits in zip(questions, retrieved, strict=True):
            if (q["id"], 1) in done:
                continue
            kept = apply_threshold(hits, gate)
            top1 = hits[0].similarity if hits else None
            if not kept:
                row = gate_row(q)
            else:
                tokens = (
                    estimate_tokens_for_text(" ".join(h.text for h in kept) + q["question"])
                    + 400
                    + out_cap
                )
                pacer.acquire(model, tokens)
                state.append(
                    {
                        "kind": "usage",
                        "date": today,
                        "model": model,
                        "tokens": tokens,
                        "requests": 1,
                    }
                )
                try:
                    row = model_row(q, answerer.answer(q["question"], kept))
                except LLMBusyError:
                    raise
                except (LLMError, AnswerFormatError) as exc:
                    row = error_row(q, exc)
            row.update(item_kind=q.get("kind"), top1=top1)
            row.pop("raw", None)
            state.append({"kind": "answer", "qid": q["id"], "run": 1, **row})
            out(f"  {q['id']}: {row['outcome']}")
    except (LLMBusyError, DailyBudgetExceeded, CallTooLarge) as exc:
        out(
            f"\nSTOPPED: {type(exc).__name__}: {exc}. Progress is saved in {state_path.name}; "
            "run the same command to continue."
        )
        return 3

    rows = []
    for q in questions:
        rec = state.get("answer", q["id"], 1)
        if rec:
            rows.append({**rec, "id": rec["qid"]} if "id" not in rec else rec)
    results_dir.mkdir(parents=True, exist_ok=True)
    raw = results_dir / f"{run_id}.jsonl"
    summary = results_dir / f"{run_id}.md"
    raw.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    text = report_markdown(banner_text, rows, gate, args.dry_run)
    summary.write_text(text + "\n", encoding="utf-8")
    out("\n" + text)
    out(f"\nWrote {raw}\nWrote {summary}")
    return 0


def _real_embedder(settings: Settings):
    from app.ingest.embedder import build_embedder

    return build_embedder(settings)


def _real_search(settings: Settings):
    from app.db import connect
    from app.retrieval.search import search

    conn = connect(settings.database_url)

    def run(vector):
        return search(conn, vector, top_k=settings.top_k, embedding_model=settings.embedding_model)

    return run, conn.close


if __name__ == "__main__":
    sys.exit(main())
