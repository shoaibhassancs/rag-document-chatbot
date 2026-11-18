"""V1: does searching with the ORIGINAL wording keep the practice answerables' sources?

  uv run python -m scripts.check_rewrite_retrieval --dry-run
  uv run python -m scripts.check_rewrite_retrieval --confirm-live

For each of the 28 practice calibration questions (data/calibration/questions.jsonl, NOT the
evaluation set) it makes ONE clarify call (the rewrite model, current clarify prompt) to get the
rewrite, then embeds all originals and all rewrites in ONE Voyage request, searches the database for
both, and compares the rank and similarity of each answerable question's expected sources.

Decision rule (INSTRUCTIONS_27, P1b): SEARCH_ORIGINAL_WHEN_NO_HISTORY may be switched on only if
the ORIGINAL wording keeps ALL expected sources of ALL 16 practice answerables in the top 5.

Paced under the Groq limits, saves each clarify call to a git-ignored state file (re-run the same
command to resume), never prints a key. A dry run uses fakes and prints "DRY RUN: FAKE DATA".
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
from app.generation.llm import LLMBusyError, LLMConfigError
from eval.fakes import FakeClarifyClient, FakeClock, FakeQueryEmbedder, FakeSearcher
from eval.pacing import CallTooLarge, DailyBudgetExceeded, Limits, Pacer
from eval.state import ConfigMismatch, State, StateError
from scripts.calibrate_threshold import DEFAULT_QUESTIONS, load_questions
from scripts.check_clarify import _real_client, classify, estimate_call

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "eval" / ".state"
RESULTS_DIR = ROOT / "eval" / "results"
DRY_RUN_LINE = "DRY RUN: FAKE DATA"
RULE = (
    "RULE: switch SEARCH_ORIGINAL_WHEN_NO_HISTORY on only if the ORIGINAL wording keeps ALL "
    "expected sources of ALL practice answerables in the top 5."
)
NOTE = "Practice calibration questions, NOT the evaluation set. Not an evaluation result."


def source_ranks(hits: list, expected: list[str]) -> dict[str, int | None]:
    """Best (lowest) rank, 1-based, of each expected source file among the hits, or None."""
    out: dict[str, int | None] = {}
    for source in expected:
        out[source] = next((n for n, h in enumerate(hits, start=1) if h.file == source), None)
    return out


def best_similarity(hits: list, expected: list[str]) -> float | None:
    sims = [h.similarity for h in hits if h.file in expected]
    return max(sims) if sims else None


def compare_row(question: dict, clarify: dict, orig_hits: list, rewrite_hits: list) -> dict:
    expected = question.get("expected_sources", [])
    o = source_ranks(orig_hits, expected)
    r = source_ranks(rewrite_hits, expected)
    return {
        "id": question["id"],
        "category": question["category"],
        "clarify_action": clarify["actual"],
        "rewrite": clarify.get("query"),
        "orig_top1": orig_hits[0].similarity if orig_hits else None,
        "rewrite_top1": rewrite_hits[0].similarity if rewrite_hits else None,
        "expected": expected,
        "orig_ranks": o,
        "rewrite_ranks": r,
        "orig_best": best_similarity(orig_hits, expected),
        "rewrite_best": best_similarity(rewrite_hits, expected),
        "orig_keeps_all": bool(expected) and all(v is not None for v in o.values()),
        "rewrite_keeps_all": bool(expected) and all(v is not None for v in r.values()),
    }


def rule_result(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["category"] == "answerable"]
    kept = [r for r in answerable if r["orig_keeps_all"]]
    return {
        "answerable": len(answerable),
        "original_keeps_all": len(kept),
        "rewrite_keeps_all": sum(1 for r in answerable if r["rewrite_keeps_all"]),
        "lost_by_original": [r["id"] for r in answerable if not r["orig_keeps_all"]],
        "pass": bool(answerable) and len(kept) == len(answerable),
    }


def _f(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _ranks(ranks: dict) -> str:
    return " ".join(f"{k}:{v if v is not None else 'MISS'}" for k, v in ranks.items()) or "-"


def report_markdown(banner_text: str, rows: list[dict], dry_run: bool) -> str:
    res = rule_result(rows)
    lines = [DRY_RUN_LINE, ""] if dry_run else []
    lines += [
        "# V1: original wording vs rewrite (practice questions)",
        "",
        "```",
        banner_text,
        "```",
        "",
        NOTE,
        "",
        "| id | kind | clarify | orig top-1 | orig expected rank | rewrite top-1 | "
        "rewrite expected rank | orig best expected | rewrite best expected |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['category']} | {r['clarify_action']} | {_f(r['orig_top1'])} | "
            f"{_ranks(r['orig_ranks'])} | {_f(r['rewrite_top1'])} | {_ranks(r['rewrite_ranks'])} | "
            f"{_f(r['orig_best'])} | {_f(r['rewrite_best'])} |"
        )
    lines += [
        "",
        f"Answerable questions whose expected sources are ALL in the top 5: "
        f"original {res['original_keeps_all']} of {res['answerable']}, "
        f"rewrite {res['rewrite_keeps_all']} of {res['answerable']}.",
        "",
        RULE,
        f"RESULT: {'PASS' if res['pass'] else 'FAIL'}"
        + (
            f" (lost by the original: {', '.join(res['lost_by_original'])})"
            if res["lost_by_original"]
            else ""
        )
        + ". "
        + (
            "P1b MAY be switched on if the owner approves."
            if res["pass"]
            else "Keep SEARCH_ORIGINAL_WHEN_NO_HISTORY=false."
        ),
        "",
        "Rewrites that were clarifies are shown but a clarified question is never searched.",
    ]
    return "\n".join(lines)


def banner(run_id: str, dry_run: bool, cfg: dict, calls: int, tokens: int, minutes: int) -> str:
    lines = [DRY_RUN_LINE] if dry_run else []
    lines += [
        "V1: ORIGINAL WORDING vs REWRITE on the practice calibration questions",
        f"  run id        : {run_id}",
        f"  rewrite model : {cfg['rewrite_model']} "
        f"(clarify prompt {cfg['clarify_prompt_version']})",
        f"  questions     : {cfg['questions']} ({cfg['count']} items; practice, not the "
        "evaluation set)",
        f"  estimate      : about {calls} Groq calls and about {tokens:,} reserved tokens on the "
        f"rewrite model, about {minutes} minutes; 1 Voyage request (originals and rewrites "
        "together)",
        "  " + RULE,
    ]
    return "\n".join(lines)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: Callable[[Settings], object] | None = None,
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
    parser.add_argument("--prompt-version", default=None)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--fresh", action="store_true", help="set saved state aside, start over")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    version = args.prompt_version or settings.clarify_prompt_version
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    try:
        questions = load_questions(args.questions)
    except (OSError, ValueError) as exc:
        out(f"Cannot read the questions: {exc}")
        return 2
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions:
        out("Nothing to run.")
        return 2
    try:
        system = (settings.prompts_dir / f"{version}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        out(f"REFUSING to start: there is no prompt file {version}.txt.")
        return 2

    role = settings.llm_role("rewrite")
    max_chars = settings.history_message_max_chars
    est = sum(estimate_call(system, q, max_chars, role.max_output_tokens) for q in questions)
    per_minute = max(1, int(settings.groq_tokens_per_minute * settings.eval_limit_margin))
    minutes = max(1, -(-est // per_minute))
    cfg = {
        "rewrite_model": role.model,
        "clarify_prompt_version": version,
        "questions": args.questions.name,
        "count": len(questions),
    }
    run_id = "rewrite-retrieval__" + _slug(version)
    banner_text = banner(run_id, args.dry_run, cfg, len(questions), est, minutes)
    out(banner_text)

    if not args.dry_run:
        if not args.confirm_live:
            out("\nREFUSING to start: a live run needs --confirm-live.")
            return 2
        if role.api_key is None or not role.api_key.get_secret_value():
            out("\nREFUSING to start: the rewrite model's API key setting has no value.")
            return 2

    if args.dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="v1-dryrun-state-"))
        results_dir = Path(tempfile.mkdtemp(prefix="v1-dryrun-results-"))
        fake = FakeClock()
        clock, sleep = fake, fake.sleep
        items = [{**q, "expected_action": "search"} for q in questions]
        client = FakeClarifyClient(items)
        embedder = FakeQueryEmbedder()
        files = sorted({s for q in questions for s in q.get("expected_sources", [])}) or ["a.md"]
        searcher, closer = FakeSearcher(files), (lambda: None)
    else:
        state_dir, results_dir = STATE_DIR, RESULTS_DIR
        try:
            client = (client_factory or _real_client)(settings)
        except LLMConfigError as exc:
            out(f"\nREFUSING to start: {exc}")
            return 2
        embedder = None  # built after the clarify calls, so a missing Voyage key stops early below
        searcher = closer = None

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

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    limits = Limits(
        settings.groq_tokens_per_minute,
        settings.groq_requests_per_minute,
        settings.groq_tokens_per_day,
        settings.groq_requests_per_day,
        settings.eval_limit_margin,
    )
    pacer = Pacer({role.model: limits}, clock, sleep, state.daily_used(today))
    done = state.done("clarify")
    try:
        for q in questions:
            if (q["id"], 1) in done:
                continue
            tokens = estimate_call(system, q, max_chars, role.max_output_tokens)
            pacer.acquire(role.model, tokens)
            state.append(
                {
                    "kind": "usage",
                    "date": today,
                    "model": role.model,
                    "tokens": tokens,
                    "requests": 1,
                }
            )
            result = classify(client, system, q, max_chars)
            state.append({"kind": "clarify", "qid": q["id"], "run": 1, **result})
            out(f"  {q['id']}: {result['actual']}")
    except (LLMBusyError, DailyBudgetExceeded, CallTooLarge) as exc:
        out(
            f"\nSTOPPED: {type(exc).__name__}: {exc}. Progress is saved in {state_path.name}; "
            "run the same command to continue."
        )
        return 3

    clarified = {r["qid"]: r for r in state.records if r.get("kind") == "clarify"}
    texts_orig = [q["question"] for q in questions]
    texts_rw = [(clarified[q["id"]].get("query") or q["question"]) for q in questions]
    try:
        if not args.dry_run:
            if search_factory is not None:
                searcher, closer = search_factory(settings)
            else:
                searcher, closer = _real_search(settings)
            embedder = (embedder_factory or _real_embedder)(settings)
        vectors = embedder.embed(texts_orig + texts_rw, "query").vectors
        rows = []
        n = len(questions)
        for i, q in enumerate(questions):
            rows.append(
                compare_row(q, clarified[q["id"]], searcher(vectors[i]), searcher(vectors[n + i]))
            )
    except Exception as exc:  # embedding or database problem: report, never loop
        out(f"\nSTOPPED: {type(exc).__name__}: {exc}. The clarify calls are saved; run again.")
        return 3
    finally:
        if closer:
            closer()
    out(
        f"Embedded {len(texts_orig) + len(texts_rw)} texts in {embedder.requests} Voyage "
        "request(s)."
    )

    results_dir.mkdir(parents=True, exist_ok=True)
    raw = results_dir / f"{run_id}.jsonl"
    summary = results_dir / f"{run_id}.md"
    raw.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    text = report_markdown(banner_text, rows, args.dry_run)
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
