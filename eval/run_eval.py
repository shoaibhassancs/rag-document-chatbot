"""The evaluation runner (Milestone 5 harness; questions approved Nov 17, 2026; no scores yet).

  uv run python -m eval.run_eval --dry-run                    # fakes only, output is NOT a result
  uv run python -m eval.run_eval --confirm-live [--allow-draft] [--phase answer|judge|all] ...

It asks every question through the real ChatService (clarify step and memory included), paces
itself under the free-tier limits, saves every finished unit to a resumable state file, has the
judge grade answered questions, and writes a summary table. A live run needs --confirm-live and,
while the questions are still DRAFT, --allow-draft as well. It never prints, logs or writes a key.
"""

import argparse
import json
import random
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.generation.llm import LLMConfigError
from eval import scoring
from eval.fakes import FakeClock, FakeJudge, FakePipeline
from eval.judge import JudgeError, build_judge
from eval.pacing import (
    CallTooLarge,
    DailyBudgetExceeded,
    Limits,
    Pacer,
    estimate_tokens_for_text,
)
from eval.questions import QuestionFileError, file_sha256, is_draft, load_questions, validate
from eval.state import ConfigMismatch, State, StateError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTIONS = ROOT / "eval" / "questions.jsonl"
STATE_DIR = ROOT / "eval" / ".state"  # git-ignored
RESULTS_DIR = ROOT / "eval" / "results"
SAMPLE_DOCS = ROOT / "data" / "sample_docs"
DRY_RUN_LINE = "DRY RUN: FAKE DATA, NOT A RESULT"
M5_REMINDER = (
    "Milestone 5 plan: run the evaluation on BOTH gpt-oss-20b and gpt-oss-120b and choose from "
    "the numbers."
)
# Reserved tokens per call (prompt estimate + output cap), the same planning rule as M3 and M4.
EST_CLARIFY_TOKENS = 800
EST_ANSWER_TOKENS = 2100
EST_VOYAGE_TOKENS = 100
STOP_ERRORS = {"LLMBusyError", "EmbeddingRateLimited"}
HANDCHECK_SAMPLE = 10
HANDCHECK_SEED = 5150


class RunStopped(Exception):
    """The run stopped on purpose (fallback to another model, or a long 429). State is saved."""


# ----------------------------------------------------------------------------- configuration


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def config_snapshot(settings: Settings, questions_path: Path, judge_calls: int) -> dict:
    """Everything that must be the same for two units of work to belong to one result."""
    return {
        "answer_model": settings.answer_llm_model,
        "answer_prompt_version": settings.answer_prompt_version,
        "answer_fallback_enabled": settings.answer_fallback_enabled,
        "answer_fallback_model": settings.answer_fallback_model,
        "rewrite_model": settings.rewrite_llm_model,
        "clarify_enabled": settings.clarify_enabled,
        "clarify_prompt_version": settings.clarify_prompt_version,
        "judge_model": settings.judge_llm_model,
        "judge_prompt_versions": {
            "correctness": settings.judge_correctness_prompt_version,
            "faithfulness": settings.judge_faithfulness_prompt_version,
            "combined": settings.judge_combined_prompt_version,
        },
        "judge_calls": judge_calls,
        "similarity_threshold": settings.similarity_threshold,
        "top_k": settings.top_k,
        "embedding_model": settings.embedding_model,
        "question_file": questions_path.name,
        "questions_sha256": file_sha256(questions_path),
    }


def run_id_for(cfg: dict, tag: str | None) -> str:
    parts = [slug(cfg["answer_model"]), cfg["answer_prompt_version"], cfg["clarify_prompt_version"]]
    if tag:
        parts.append(slug(tag))
    return "__".join(parts)


def banner(cfg: dict, run_id: str, dry_run: bool, draft: bool, repeat: int) -> str:
    lines = []
    if dry_run:
        lines.append(DRY_RUN_LINE)
    lines += [
        f"Evaluation run {run_id}",
        f"  answer model       : {cfg['answer_model']} (prompt {cfg['answer_prompt_version']})",
        f"  answer fallback    : "
        f"{cfg['answer_fallback_model'] if cfg['answer_fallback_enabled'] else 'off'}",
        f"  rewrite model      : {cfg['rewrite_model']} (clarify prompt "
        f"{cfg['clarify_prompt_version']}, {'on' if cfg['clarify_enabled'] else 'OFF'})",
        f"  judge model        : {cfg['judge_model']} (prompts {cfg['judge_prompt_versions']})",
        f"  similarity threshold: {cfg['similarity_threshold']}  top_k: {cfg['top_k']}",
        f"  embedding model    : {cfg['embedding_model']}",
        f"  questions          : {cfg['question_file']} sha256 {cfg['questions_sha256'][:12]}"
        f"  {'DRAFT (not approved by the owner)' if draft else 'APPROVED by the owner'}",
        f"  repeat {repeat}, judge calls per answer {cfg['judge_calls']}",
        f"  {M5_REMINDER}",
    ]
    return "\n".join(lines)


def missing_keys(settings: Settings) -> list[str]:
    """Names of the settings whose key is not set. Never returns or prints a key value."""
    missing: list[str] = []
    for role in ("answer", "rewrite", "judge"):
        key = settings.llm_role(role).api_key
        if key is None or not key.get_secret_value():
            name = getattr(settings, f"{role}_llm_api_key_var")
            if name not in missing:
                missing.append(name)
    if settings.voyage_api_key is None or not settings.voyage_api_key.get_secret_value():
        missing.append("VOYAGE_API_KEY")
    return missing


def build_limits(settings: Settings) -> dict[str, Limits]:
    groq = Limits(
        settings.groq_tokens_per_minute,
        settings.groq_requests_per_minute,
        settings.groq_tokens_per_day,
        settings.groq_requests_per_day,
        settings.eval_limit_margin,
    )
    voyage = Limits(
        settings.voyage_tokens_per_minute,
        settings.voyage_requests_per_minute,
        margin=settings.eval_limit_margin,
    )
    return {
        settings.answer_llm_model: groq,
        settings.rewrite_llm_model: groq,
        settings.judge_llm_model: groq,
        "voyage": voyage,
    }


# ----------------------------------------------------------------------------- the real pipeline


class RealPipeline:
    """The production ChatService, wired like the API wires it, plus a chunk-text lookup."""

    def __init__(self, settings: Settings):
        from app.generation.answer import build_answerer
        from app.ingest.embedder import CachingEmbedder, build_embedder
        from app.memory import MemoryStore
        from app.services.chat import ChatService
        from app.services.chat_log import ChatLogger
        from app.services.clarify import build_preparer

        self._settings = settings
        memory = MemoryStore(
            settings.database_url, settings.history_turns, settings.session_ttl_hours
        )
        self._service = ChatService(
            settings,
            CachingEmbedder(build_embedder(settings)),
            build_answerer(settings),
            build_preparer(settings, memory),
            memory=memory,
            chat_logger=ChatLogger(settings.database_url),
        )

    def ask(self, session_id: str | None, message: str):
        return self._service.chat(session_id, message)

    def chunk_texts(self, ids: list[int]) -> dict[int, str]:
        from app.db import connect

        if not ids:
            return {}
        with connect(self._settings.database_url) as conn:
            rows = conn.execute("SELECT id, text FROM chunks WHERE id = ANY(%s)", [list(ids)])
            return {int(r[0]): r[1] for r in rows.fetchall()}


# ----------------------------------------------------------------------------- the runner


class Runner:
    def __init__(
        self,
        settings: Settings,
        cfg: dict,
        run_id: str,
        questions: list[dict],
        state: State,
        pipeline,
        judge,
        pacer: Pacer,
        today: Callable[[], str],
        dry_run: bool,
        say: Callable[[str], None] = print,
    ):
        self.s = settings
        self.cfg = cfg
        self.run_id = run_id
        self.questions = questions
        self.state = state
        self.pipeline = pipeline
        self.judge = judge
        self.pacer = pacer
        self.today = today
        self.dry_run = dry_run
        self.say = say

    # ---- pacing helper
    def paced(self, model: str, tokens: int) -> None:
        self.pacer.acquire(model, tokens)
        self.state.append(
            {"kind": "usage", "date": self.today(), "model": model, "tokens": tokens, "requests": 1}
        )

    def paced_ask(self, session_id: str, message: str):
        if self.s.clarify_enabled:
            self.paced(self.s.rewrite_llm_model, EST_CLARIFY_TOKENS)
        self.paced(self.s.answer_llm_model, EST_ANSWER_TOKENS)
        self.paced("voyage", EST_VOYAGE_TOKENS)
        result = self.pipeline.ask(session_id, message)
        used = result.llm_usage or {}
        if used.get("fallback_used"):
            raise RunStopped(
                "a call fell back to another model; stopping so one result never mixes models"
            )
        if result.status == "error" and result.error in STOP_ERRORS:
            raise RunStopped(f"stopped on {result.error} (a rate limit or daily cap); state saved")
        return result

    # ---- answer phase
    def run_answers(self, repeat: int) -> int:
        done = self.state.done("answer")
        count = 0
        for run in range(1, repeat + 1):
            for q in self.questions:
                if (q["id"], run) in done:
                    continue
                self.state.append(self.answer_unit(q, run))
                count += 1
                self.say(f"  run {run} {q['id']}: answered unit saved")
        return count

    def answer_unit(self, q: dict, run: int) -> dict:
        session = f"eval-{self.run_id}-{q['id']}-r{run}"[:64]
        setup_status = None
        if q.get("setup"):
            setup = self.paced_ask(session, q["setup"])
            setup_status = setup.status
        result = self.paced_ask(session, q["question"])
        usage = {k: v for k, v in (result.llm_usage or {}).items() if k != "rate_limit"}
        record = {
            "kind": "answer",
            "qid": q["id"],
            "run": run,
            "category": q["category"],
            "question": q["question"],
            "expected_status": q["expected_status"],
            "expected_sources": q["expected_sources"],
            "expected_answer": q["expected_answer"],
            "status": result.status,
            "answer": result.answer,
            "sources": [s["file"] for s in result.sources],
            "retrieved": result.retrieved,
            "used_chunk_ids": result.used_chunk_ids,
            "answer_warnings": result.warnings,
            "latency_ms": result.latency_ms,
            "timings_ms": result.timings_ms,
            "usage": usage,
            "clarify_action": result.clarify_action,
            "standalone_query": result.standalone_query,
            "setup_status": setup_status,
            "setup_not_answered": bool(q.get("setup")) and setup_status != "answered",
            "error": result.error,
            "dry_run": self.dry_run,
        }
        if q["category"] == "answerable" and result.status == "answered":
            record.update(self.faithfulness_basis(record))
        return record

    def faithfulness_basis(self, record: dict) -> dict:
        """Choose and store the chunk texts the faithfulness judge will see (so the judge phase
        needs no database and a later re-ingest cannot change the basis)."""
        fell_back = any("no valid used_chunks" in w for w in record["answer_warnings"])
        if record["used_chunk_ids"] and not fell_back:
            ids, basis = list(record["used_chunk_ids"]), "used_chunks"
        elif record["retrieved"]:
            ids, basis = [record["retrieved"][0]["chunk_id"]], "top_chunk"
        else:
            return {"faithfulness_basis": "none", "basis_chunks": []}
        texts = self.pipeline.chunk_texts(ids)
        return {
            "faithfulness_basis": basis,
            "basis_chunks": [{"chunk_id": i, "text": texts[i]} for i in ids if i in texts],
        }

    # ---- judge phase
    def run_judge(self, repeat: int) -> int:
        judged = self.state.done("judge")
        count = 0
        for rec in list(self.state.records):
            if rec.get("kind") != "answer" or rec["run"] > repeat:
                continue
            if rec["category"] != "answerable" or rec["status"] != "answered":
                continue
            if (rec["qid"], rec["run"]) in judged:
                continue
            self.state.append(self.judge_unit(rec))
            count += 1
            self.say(f"  run {rec['run']} {rec['qid']}: judged")
        return count

    def judge_unit(self, rec: dict) -> dict:
        chunks = [c["text"] for c in rec.get("basis_chunks") or []]
        out = {"kind": "judge", "qid": rec["qid"], "run": rec["run"], "dry_run": self.dry_run}
        blank = {"score": None, "reason": "", "error": None}
        out["correctness"], out["faithfulness"] = dict(blank), dict(blank)
        out["faithfulness"]["basis"] = rec.get("faithfulness_basis")
        if not chunks:
            out["faithfulness"]["error"] = "no chunks to judge against"
        try:
            if self.cfg["judge_calls"] == 1 and chunks:
                self.paced(self.s.judge_llm_model, self.estimate_judge(rec, chunks, "combined"))
                c, f = self.judge.both(
                    rec["question"], rec["expected_answer"], rec["answer"], chunks
                )
                out["correctness"].update(score=c.score, reason=c.reason)
                out["faithfulness"].update(score=f.score, reason=f.reason)
                return out
            self.paced(self.s.judge_llm_model, self.estimate_judge(rec, chunks, "correctness"))
            c = self.judge.correctness(rec["question"], rec["expected_answer"], rec["answer"])
            out["correctness"].update(score=c.score, reason=c.reason)
        except JudgeError as exc:
            out["correctness"]["error"] = str(exc)
        if chunks:
            try:
                self.paced(self.s.judge_llm_model, self.estimate_judge(rec, chunks, "faithfulness"))
                f = self.judge.faithfulness(rec["answer"], chunks)
                out["faithfulness"].update(score=f.score, reason=f.reason)
            except JudgeError as exc:
                out["faithfulness"]["error"] = str(exc)
        return out

    def estimate_judge(self, rec: dict, chunks: list[str], which: str) -> int:
        text = rec["question"] + rec["expected_answer"] + rec["answer"] + "".join(chunks)
        # about 250 tokens of judge prompt plus the data, plus the judge's output cap
        return 250 + estimate_tokens_for_text(text) + self.s.judge_llm_max_output_tokens

    # ---- report
    def rows(self, run: int) -> list[dict]:
        out = []
        for q in self.questions:
            rec = self.state.get("answer", q["id"], run)
            if rec is None:
                continue
            row = dict(rec)
            verdict = self.state.get("judge", q["id"], run)
            if verdict:
                row["correctness"] = verdict["correctness"]
                row["faithfulness"] = verdict["faithfulness"]
            out.append(row)
        return out


# ----------------------------------------------------------------------------- reporting


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def summary_markdown(
    banner_text: str, per_run: list[dict], repeat: int, dry_run: bool, draft: bool
) -> str:
    lines: list[str] = []
    if dry_run:
        lines += [DRY_RUN_LINE, ""]
    lines += ["# Evaluation summary", ""]
    if draft and not dry_run:
        lines += [
            "**DRAFT questions: not approved by the owner. Do not quote these numbers.**",
            "",
        ]
    lines += ["```", banner_text, "```", ""]
    m = per_run[0] if per_run else None
    if m is None:
        return "\n".join(lines + ["No answered questions yet."])
    lines += [
        f"Run 1 of {repeat}. Questions scored: {m['questions']}.",
        "",
        "| Metric | Value | n | Target | Met |",
        "| --- | --- | --- | --- | --- |",
    ]

    def row(label, key, block, target_key=None):
        value = block["value"]
        target = scoring.TARGETS.get(target_key or key)
        met = scoring.meets_target(target_key or key, value)
        lines.append(
            f"| {label} | {fmt_pct(value)} | {block['n']} | "
            f"{'' if target is None else f'{target * 100:.0f}%'} | "
            f"{'n/a' if met is None else ('yes' if met else 'NO')} |"
        )

    row(
        "Retrieval hit rate (all expected sources in top-k)",
        "retrieval_hit_all",
        m["retrieval_hit_all"],
    )
    row("Retrieval hit rate (any expected source)", "retrieval_hit_any", m["retrieval_hit_any"])
    row("Faithfulness", "faithfulness", m["faithfulness"])
    row("Answer correctness", "correctness", m["correctness"])
    row("Refusal accuracy", "refusal_accuracy", m["refusal_accuracy"])
    row("Status accuracy (no target yet)", "status_accuracy", m["status_accuracy"])
    p50 = m["latency_ms"]["p50"]
    met = scoring.meets_target("latency_p50_ms", p50)
    lines += [
        f"| Latency p50 (ms) | {p50} | {m['latency_ms']['n']} | under 3000 | "
        f"{'n/a' if met is None else ('yes' if met else 'NO')} |",
        "",
        f"Latency p95 {m['latency_ms']['p95']} ms, max {m['latency_ms']['max']} ms "
        "(chat call only; pacing sleeps excluded).",
        "",
        "Status accuracy by category: "
        + ", ".join(
            f"{k} {fmt_pct(v['value'])} (n={v['n']})"
            for k, v in m["status_accuracy_by_category"].items()
        ),
        "",
        f"Unjudged answers: correctness {m['correctness']['unjudged'] or 'none'}, "
        f"faithfulness {m['faithfulness']['unjudged'] or 'none'}.",
        f"Errors: {m['errors'] or 'none'}. Answerable questions that got a clarify: "
        f"{m['clarify_on_answerable'] or 'none'}.",
        f"Follow-ups whose setup turn was not answered (`setup_not_answered`; scored as run, "
        f"judge them by hand): {m['setup_not_answered'] or 'none'}.",
        "",
    ]
    if repeat > 1 and len(per_run) > 1:
        lines += [
            "## Spread over repeated runs",
            "",
            "| Metric | mean | min | max | runs |",
            "| --- | --- | --- | --- | --- |",
        ]
        for key in ("retrieval_hit_all", "faithfulness", "correctness", "refusal_accuracy"):
            s = scoring.spread([r[key]["value"] for r in per_run])
            lines.append(
                f"| {key} | {fmt_pct(s['mean'])} | {fmt_pct(s['min'])} | {fmt_pct(s['max'])} "
                f"| {s['runs']} |"
            )
        lines.append("")
    lines += [
        "Where a score is close to its target, repeat the run and report the spread. The judge is "
        "an LLM; the owner hand-checks about 10 verdicts (see the hand-check file).",
    ]
    return "\n".join(lines)


def handcheck_markdown(rows: list[dict], dry_run: bool) -> str:
    judged = [r for r in rows if scoring._score(r.get("correctness")) is not None]
    rng = random.Random(HANDCHECK_SEED)
    chosen = rng.sample(judged, min(HANDCHECK_SAMPLE, len(judged)))
    lines = [DRY_RUN_LINE, ""] if dry_run else []
    lines += ["# Judge verdicts to check by hand", ""]
    for r in sorted(chosen, key=lambda x: x["qid"]):
        c, f = r["correctness"], r.get("faithfulness") or {}
        lines += [
            f"## {r['qid']} (run {r['run']})",
            f"- Question: {r['question']}",
            f"- Expected: {r['expected_answer']}",
            f"- Answer: {r['answer']}",
            f"- Correctness verdict: {c.get('score')} ({c.get('reason')})",
            f"- Faithfulness verdict: {f.get('score')} ({f.get('reason')}); "
            f"basis: {f.get('basis')}",
            "- Your verdict: ",
            "",
        ]
    return "\n".join(lines)


def write_results(
    runner: Runner, banner_text: str, results_dir: Path, repeat: int, draft: bool
) -> list[Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[list[dict]] = [runner.rows(run) for run in range(1, repeat + 1)]
    per_run = [scoring.compute_metrics(rows) for rows in all_rows if rows]
    base = results_dir / runner.run_id
    raw = base.with_suffix(".jsonl")
    summary = base.with_suffix(".md")
    handcheck = results_dir / f"{runner.run_id}-handcheck.md"
    raw.write_text(
        "".join(json.dumps(r) + "\n" for rows in all_rows for r in rows), encoding="utf-8"
    )
    summary.write_text(
        summary_markdown(banner_text, per_run, repeat, runner.dry_run, draft) + "\n",
        encoding="utf-8",
    )
    handcheck.write_text(
        handcheck_markdown([r for rows in all_rows for r in rows], runner.dry_run) + "\n",
        encoding="utf-8",
    )
    return [raw, summary, handcheck]


# ----------------------------------------------------------------------------- the command line


def estimate_plan(cfg: dict, questions: list[dict], repeat: int, judge_calls: int) -> str:
    n = len(questions) * repeat
    setups = sum(1 for q in questions if q.get("setup")) * repeat
    asks = n + setups
    answered = sum(1 for q in questions if q["category"] == "answerable") * repeat
    clarify_tokens = asks * EST_CLARIFY_TOKENS if cfg["clarify_enabled"] else 0
    answer_tokens = asks * EST_ANSWER_TOKENS
    judge_calls_total = answered * (1 if judge_calls == 1 else 2)
    return (
        f"Would make about {asks} chat calls ({n} questions + {setups} setup turns) and up to "
        f"{judge_calls_total} judge calls.\n"
        f"Reserved tokens (estimate): answer model {answer_tokens:,}, rewrite model "
        f"{clarify_tokens:,}, judge about {judge_calls_total * 2000:,}.\n"
        f"Groq allows 200,000 tokens per model per day: one full pass of a model is about one "
        f"day of its budget; the 20b and 120b have separate quotas."
    )


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    pipeline_factory: Callable[[Settings], object] | None = None,
    judge_factory: Callable[[Settings], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="fakes only; the output is not a result"
    )
    parser.add_argument("--confirm-live", action="store_true", help="required for a live run")
    parser.add_argument(
        "--allow-draft", action="store_true", help="allow a live run on DRAFT questions"
    )
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", default=None, help="comma-separated question ids")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--judge-calls", type=int, choices=[1, 2], default=2)
    parser.add_argument("--phase", choices=["answer", "judge", "all"], default="all")
    parser.add_argument(
        "--fresh", action="store_true", help="set the saved state aside and start over"
    )
    parser.add_argument(
        "--force-config", action="store_true", help="resume despite a changed config"
    )
    parser.add_argument("--tag", default=None, help="extra label for the run id")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    try:
        header, questions = load_questions(args.questions)
    except QuestionFileError as exc:
        out(f"Question file problem: {exc}")
        return 2
    problems = validate(questions, SAMPLE_DOCS if SAMPLE_DOCS.is_dir() else None)
    if problems:
        out("Question file problems:\n  " + "\n  ".join(problems))
        return 2
    # Judged on the WHOLE file, before --ids or --limit pick a subset, so choosing only approved
    # questions cannot hide a DRAFT header or a DRAFT question elsewhere in the file.
    draft = is_draft(header, questions)
    if args.ids:
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        questions = [q for q in questions if q["id"] in wanted]
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions or args.repeat < 1:
        out("Nothing to run (no questions selected, or --repeat is below 1).")
        return 2

    cfg = config_snapshot(settings, args.questions, args.judge_calls)
    run_id = run_id_for(cfg, args.tag)
    banner_text = banner(cfg, run_id, args.dry_run, draft, args.repeat)
    out(banner_text)

    if not args.dry_run:
        if not args.confirm_live:
            out(
                "\nREFUSING to start: a live run needs --confirm-live.\n"
                + estimate_plan(cfg, questions, args.repeat, args.judge_calls)
            )
            return 2
        if draft and not args.allow_draft:
            out(
                "\nREFUSING to start: the questions are still DRAFT (not approved by the owner). "
                "Add --allow-draft to run on them anyway."
            )
            return 2
        missing = missing_keys(settings)
        if missing:
            out("\nREFUSING to start: these settings have no value: " + ", ".join(missing))
            return 2

    if args.dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="eval-dryrun-state-"))
        results_dir = Path(tempfile.mkdtemp(prefix="eval-dryrun-results-"))
        fake_clock = FakeClock()
        clock, sleep = fake_clock, fake_clock.sleep
        pipeline = FakePipeline(questions)
        judge = FakeJudge()
    else:
        state_dir, results_dir = STATE_DIR, RESULTS_DIR
        try:
            pipeline = (pipeline_factory or RealPipeline)(settings)
            judge = (judge_factory or build_judge)(settings)
        except LLMConfigError as exc:
            out(f"\nREFUSING to start: {exc}")
            return 2

    state_path = state_dir / f"{run_id}.jsonl"
    if args.fresh and state_path.exists():
        n = 1
        while (state_dir / f"{run_id}.superseded-{n}.jsonl").exists():
            n += 1
        shutil.move(state_path, state_dir / f"{run_id}.superseded-{n}.jsonl")
    try:
        state = State(state_path)
        diffs = state.check_config(cfg, force=args.force_config)
        if diffs:
            out(
                "Resuming despite a changed configuration (--force-config):\n  "
                + "\n  ".join(diffs)
            )
        state.start(cfg)
    except (ConfigMismatch, StateError) as exc:
        out(f"\nREFUSING to start: {exc}")
        return 2

    now = datetime.now(UTC)
    today = lambda: now.strftime("%Y-%m-%d")  # noqa: E731
    pacer = Pacer(build_limits(settings), clock, sleep, state.daily_used(today()))
    runner = Runner(
        settings, cfg, run_id, questions, state, pipeline, judge, pacer, today, args.dry_run, out
    )

    try:
        if args.phase in ("answer", "all"):
            out(f"Answer phase: {len(questions)} questions x {args.repeat} run(s)")
            runner.run_answers(args.repeat)
        if args.phase in ("judge", "all"):
            out("Judge phase")
            runner.run_judge(args.repeat)
    except (RunStopped, DailyBudgetExceeded, CallTooLarge) as exc:
        out(
            f"\nSTOPPED: {exc}. Progress is saved in {state_path.name}; "
            "run the same command to continue."
        )
        return 3
    paths = write_results(runner, banner_text, results_dir, args.repeat, draft)
    for p in paths:
        out(f"Wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
