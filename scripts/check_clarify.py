"""Check ONLY the clarify step on the clarify-tuning set (no embedding, search, answer or judge).

  uv run python -m scripts.check_clarify --dry-run                       # fakes, NOT a result
  uv run python -m scripts.check_clarify --confirm-live --prompt-version clarify_v2

For every item of data/calibration/clarify_questions.jsonl it makes ONE call to the rewrite model
(gpt-oss-20b by default) with the chosen prompt version, applies the same code-side rules as the
app (`decide` in app/services/clarify.py), and compares the action with the expected one
(search or clarify). It prints the counts and every miss and writes a small results file under
eval/results/ named for the prompt version. It paces itself under the free-tier limits, saves every
finished item to a git-ignored state file (re-run the same command to resume) and never prints a
key. The tuning set is a DRAFT written by Claude, kept apart from the approved evaluation set.
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
from app.generation.answer import AnswerFormatError, parse_model_json
from app.generation.llm import LLMBusyError, LLMClient, LLMConfigError, LLMError
from app.memory import SessionState, Turn
from app.services.clarify import build_clarify_message, decide
from eval.fakes import FakeClarifyClient, FakeClock
from eval.pacing import (
    CallTooLarge,
    DailyBudgetExceeded,
    Limits,
    Pacer,
    estimate_tokens_for_text,
)
from eval.state import ConfigMismatch, State, StateError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = ROOT / "data" / "calibration" / "clarify_questions.jsonl"
STATE_DIR = ROOT / "eval" / ".state"  # git-ignored
RESULTS_DIR = ROOT / "eval" / "results"
EVAL_QUESTIONS = ROOT / "eval" / "questions.jsonl"
THRESHOLD_SET = ROOT / "data" / "calibration" / "questions.jsonl"
DRY_RUN_LINE = "DRY RUN: FAKE DATA, NOT A RESULT"
ACTIONS = {"search", "clarify"}
REQUIRED = ("id", "expected_action", "kind", "question")
NOTE = (
    "Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not "
    "approved by the owner) and is NOT the evaluation set. These numbers are not evaluation "
    "results and must not be quoted as such."
)


class TuningSetError(Exception):
    pass


# ----------------------------------------------------------------------------- the tuning set


def normalize(text: str) -> str:
    """Lowercase, punctuation to spaces, spaces collapsed: for comparing questions."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def load_items(path: Path) -> tuple[dict, list[dict]]:
    header: dict = {}
    items: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TuningSetError(f"Cannot read {path}: {type(exc).__name__}") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise TuningSetError(f"{path}: line {number} is not valid JSON") from None
        if not isinstance(item, dict):
            raise TuningSetError(f"{path}: line {number} is not a JSON object")
        if "_header" in item:
            header = item
        else:
            items.append(item)
    return header, items


def texts_of(item: dict) -> list[str]:
    """Every piece of user-written text of an item: the question and the setup/history turns."""
    texts = [item.get("question", "")]
    if item.get("setup"):
        texts.append(item["setup"])
    for turn in item.get("history") or []:
        texts += [turn.get("user", ""), turn.get("assistant", "")]
    return [t for t in texts if t]


def validate_items(items: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for item in items:
        qid = item.get("id", "?")
        for field in REQUIRED:
            if not item.get(field):
                problems.append(f"{qid}: missing field '{field}'")
        if qid in seen:
            problems.append(f"{qid}: duplicate id")
        seen.add(qid)
        if item.get("expected_action") not in ACTIONS:
            problems.append(f"{qid}: expected_action must be search or clarify")
        for turn in item.get("history") or []:
            if not (isinstance(turn, dict) and turn.get("user") and turn.get("assistant")):
                problems.append(f"{qid}: each history turn needs user and assistant text")
    return problems


def overlap_with(items: list[dict], other_texts: set[str]) -> list[str]:
    """Ids of items one of whose texts equals (normalised) a text in `other_texts`."""
    return [
        item["id"] for item in items if any(normalize(t) in other_texts for t in texts_of(item))
    ]


def texts_in_file(path: Path) -> set[str]:
    """Normalised question and setup texts of an evaluation or threshold-calibration file."""
    _, items = load_items(path)
    return {normalize(t) for item in items for t in texts_of(item)}


def group_of(item: dict) -> str:
    """clear (must search), vague (must clarify) or follow-up (has history)."""
    if item.get("history"):
        return "follow-up"
    return "clear" if item["expected_action"] == "search" else "vague"


# ----------------------------------------------------------------------------- one item


def turns_of(item: dict) -> list[Turn]:
    return [Turn(t["user"], t["assistant"], "answered") for t in item.get("history") or []]


def classify(client, system: str, item: dict, max_chars: int) -> dict:
    """One clarify call. Returns {"actual", "query", "clarifying_question", "error", "usage"}.
    actual is "search", "clarify" or "error" (the app would fail open to a search; here it is
    reported on its own). Raises LLMBusyError so the caller can stop; never returns a key."""
    turns = turns_of(item)
    user = build_clarify_message(turns, item["question"], max_chars)
    out = {
        "actual": "error",
        "query": None,
        "clarifying_question": None,
        "error": None,
        "usage": {},
    }
    try:
        result = client.complete(system, user)
    except LLMBusyError:
        raise
    except LLMError as exc:
        out["error"] = type(exc).__name__
        return out
    out["usage"] = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "latency_ms": result.latency_ms,
    }
    try:
        if result.finish_reason == "length":
            raise AnswerFormatError("reply cut off")
        data = parse_model_json(result.text)
    except AnswerFormatError as exc:
        out["error"] = f"unusable reply ({exc})"
        return out
    decision = decide(data, None, SessionState(turns=turns), item["question"])
    out["actual"] = decision.action
    out["query"] = decision.query
    out["clarifying_question"] = decision.clarifying_question
    return out


# ----------------------------------------------------------------------------- scoring


def summarize(rows: list[dict]) -> dict:
    """Counts and misses from finished rows (each has group, expected_action, actual)."""

    def count(group: str):
        sel = [r for r in rows if r["group"] == group]
        return sel, [r for r in sel if r["actual"] != r["expected_action"]]

    clear, clear_miss = count("clear")
    vague, vague_miss = count("vague")
    follow, follow_miss = count("follow-up")
    return {
        "items": len(rows),
        "clear": len(clear),
        "clear_wrongly_clarified": sum(1 for r in clear if r["actual"] == "clarify"),
        "vague": len(vague),
        "vague_caught": sum(1 for r in vague if r["actual"] == "clarify"),
        "vague_wrongly_searched": sum(1 for r in vague if r["actual"] == "search"),
        "follow_ups": len(follow),
        "follow_ups_right": len(follow) - len(follow_miss),
        "errors": sum(1 for r in rows if r["actual"] == "error"),
        "misses": [r for r in clear_miss + vague_miss + follow_miss],
    }


def report_markdown(banner_text: str, rows: list[dict], dry_run: bool) -> str:
    s = summarize(rows)
    lines = [DRY_RUN_LINE, ""] if dry_run else []
    lines += [
        "# Clarify check",
        "",
        "```",
        banner_text,
        "```",
        "",
        NOTE,
        "",
        "| Measure | Count |",
        "| --- | --- |",
        f"| Clear questions wrongly clarified (should search) | "
        f"{s['clear_wrongly_clarified']} of {s['clear']} |",
        f"| Vague questions caught (clarified) | {s['vague_caught']} of {s['vague']} |",
        f"| Vague questions wrongly searched | {s['vague_wrongly_searched']} of {s['vague']} |",
        f"| Follow-ups handled as expected | {s['follow_ups_right']} of {s['follow_ups']} |",
        f"| Items with a model or format error | {s['errors']} of {s['items']} |",
        "",
        "## Every miss",
        "",
    ]
    if not s["misses"]:
        lines.append("None.")
    for r in s["misses"]:
        lines.append(
            f"- {r['qid']} ({r['group']}): expected {r['expected_action']}, got {r['actual']}"
            + (f" [{r['error']}]" if r.get("error") else "")
            + f": {r['question']!r}"
            + (f" -> asked {r['clarifying_question']!r}" if r.get("clarifying_question") else "")
        )
    lines += ["", "## Every item", ""]
    for r in rows:
        mark = "ok" if r["actual"] == r["expected_action"] else "MISS"
        lines.append(f"- {r['qid']} {mark}: expected {r['expected_action']}, got {r['actual']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- the run


def estimate_call(system: str, item: dict, max_chars: int, output_cap: int) -> int:
    user = build_clarify_message(turns_of(item), item["question"], max_chars)
    return estimate_tokens_for_text(system) + estimate_tokens_for_text(user) + output_cap


def banner(cfg: dict, run_id: str, dry_run: bool, calls: int, tokens: int, minutes: int) -> str:
    lines = [DRY_RUN_LINE] if dry_run else []
    lines += [
        "CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)",
        f"  run id             : {run_id}",
        f"  rewrite model      : {cfg['rewrite_model']}",
        f"  clarify prompt     : {cfg['clarify_prompt_version']}",
        f"  tuning set         : {cfg['tuning_set']} (DRAFT, not approved by the owner)",
        f"  estimate           : about {calls} calls and about {tokens:,} reserved tokens "
        f"(real use is lower), about {minutes} minutes (paced by the tokens-per-minute limit)",
        "  note               : the rewrite model's daily token budget is shared with every "
        "other use of it today",
    ]
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: Callable[[Settings], object] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    import time

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="fakes only; not a result")
    parser.add_argument("--confirm-live", action="store_true", help="required for a live run")
    parser.add_argument("--prompt-version", default=None, help="e.g. clarify_v1 or clarify_v2")
    parser.add_argument("--items", type=Path, default=DEFAULT_SET)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--fresh", action="store_true", help="set saved state aside, start over")
    parser.add_argument("--tag", default=None, help="extra label for the run id")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    version = args.prompt_version or settings.clarify_prompt_version
    clock = clock or time.monotonic
    sleep = sleep or time.sleep

    try:
        header, items = load_items(args.items)
    except TuningSetError as exc:
        out(f"Tuning set problem: {exc}")
        return 2
    problems = validate_items(items)
    if problems:
        out("Tuning set problems:\n  " + "\n  ".join(problems))
        return 2
    if args.limit is not None:
        items = items[: args.limit]
    if not items:
        out("Nothing to run.")
        return 2

    prompt_path = settings.prompts_dir / f"{version}.txt"
    try:
        system = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        out(f"REFUSING to start: there is no prompt file {prompt_path.name}.")
        return 2

    role = settings.llm_role("rewrite")
    max_chars = settings.history_message_max_chars
    est_tokens = sum(estimate_call(system, i, max_chars, role.max_output_tokens) for i in items)
    cfg = {
        "rewrite_model": role.model,
        "clarify_prompt_version": version,
        "tuning_set": args.items.name,
        "items": len(items),
    }
    run_id = "clarify-check__" + re.sub(r"[^A-Za-z0-9._-]+", "_", version)
    if args.tag:
        run_id += "__" + re.sub(r"[^A-Za-z0-9._-]+", "_", args.tag)
    per_minute = max(1, int(settings.groq_tokens_per_minute * settings.eval_limit_margin))
    minutes = max(1, -(-est_tokens // per_minute))
    banner_text = banner(cfg, run_id, args.dry_run, len(items), est_tokens, minutes)
    out(banner_text)

    if not args.dry_run:
        if not args.confirm_live:
            out("\nREFUSING to start: a live run needs --confirm-live.")
            return 2
        if role.api_key is None or not role.api_key.get_secret_value():
            out("\nREFUSING to start: the rewrite model's API key setting has no value.")
            return 2

    if args.dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="clarify-dryrun-state-"))
        results_dir = Path(tempfile.mkdtemp(prefix="clarify-dryrun-results-"))
        fake = FakeClock()
        clock, sleep = fake, fake.sleep
        client = FakeClarifyClient(items)
    else:
        state_dir, results_dir = STATE_DIR, RESULTS_DIR
        try:
            client = (client_factory or _real_client)(settings)
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
        for item in items:
            if (item["id"], 1) in done:
                continue
            tokens = estimate_call(system, item, max_chars, role.max_output_tokens)
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
            result = classify(client, system, item, max_chars)
            state.append(
                {
                    "kind": "clarify",
                    "qid": item["id"],
                    "run": 1,
                    "group": group_of(item),
                    "question": item["question"],
                    "expected_action": item["expected_action"],
                    **result,
                }
            )
            out(f"  {item['id']}: expected {item['expected_action']}, got {result['actual']}")
    except (LLMBusyError, DailyBudgetExceeded, CallTooLarge) as exc:
        out(
            f"\nSTOPPED: {type(exc).__name__}: {exc}. Progress is saved in {state_path.name}; "
            "run the same command to continue."
        )
        return 3

    rows = [r for r in state.records if r.get("kind") == "clarify"]
    results_dir.mkdir(parents=True, exist_ok=True)
    raw = results_dir / f"{run_id}.jsonl"
    summary = results_dir / f"{run_id}.md"
    raw.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    summary.write_text(report_markdown(banner_text, rows, args.dry_run) + "\n", encoding="utf-8")
    s = summarize(rows)
    out(
        f"\nClear questions wrongly clarified: {s['clear_wrongly_clarified']} of {s['clear']}\n"
        f"Vague questions caught: {s['vague_caught']} of {s['vague']}"
        f" (wrongly searched: {s['vague_wrongly_searched']})\n"
        f"Follow-ups handled as expected: {s['follow_ups_right']} of {s['follow_ups']}\n"
        f"Items with a model or format error: {s['errors']}"
    )
    for r in s["misses"]:
        out(
            f"  MISS {r['qid']} ({r['group']}): expected {r['expected_action']}, got "
            f"{r['actual']}: {r['question']!r}"
        )
    out(f"Wrote {raw}\nWrote {summary}")
    return 0


def _real_client(settings: Settings) -> LLMClient:
    # Same construction as build_preparer in app/services/clarify.py: the rewrite role, no fallback.
    return LLMClient(
        settings.llm_role("rewrite"),
        fallback_model=None,
        max_retries=settings.llm_max_retries,
        max_wait_seconds=settings.llm_max_wait_seconds,
        timeout=settings.llm_timeout_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
