"""Judge sanity check with negative controls (INSTRUCTIONS_29).

A judge that scores 100% faithful might never say 0. This feeds the judge model (and ONLY the judge
model: no Voyage, no database, no answer model) 12 hand-written items built from the sample
documents: good answers, unfaithful answers, wrong answers, one strictness item and refusal-like
answers. Each check has an expected verdict; the table shows PASS or MISS. It checks the judge, not
the chatbot, and its output is not an evaluation result.

  uv run python -m scripts.check_judge --dry-run          (fake judge; the numbers mean nothing)
  uv run python -m scripts.check_judge --confirm-live     (about 24 judge calls)

An errored judge call is ERROR, never a pass. Nothing changes automatically from the result.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.generation.llm import LLMConfigError
from eval.judge import JudgeError, build_judge

ROOT = Path(__file__).resolve().parent.parent
CONTROLS = ROOT / "data" / "calibration" / "judge_controls.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
DRY_RUN_LINE = "DRY RUN: FAKE DATA"
NOTE = "Hand-written controls from the sample documents, NOT the evaluation set. Not a result."
TOKENS_PER_CALL = 700  # rough: prompt about 500 + reply cap


def load_controls(path: Path = CONTROLS) -> list[dict]:
    items = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        for key in ("id", "question", "chunks", "expected_answer", "candidate_answer", "role"):
            if key not in item:
                raise ValueError(f"{path}:{number}: missing '{key}'")
        items.append(item)
    return items


def judge_item(judge, item: dict) -> dict:
    """Both verdicts for one control. A JudgeError is recorded as an error, never as 0 or 1."""
    row = {"id": item["id"], "tests": item.get("tests", ""), "role": item["role"]}
    for name, call in (
        (
            "correct",
            lambda: judge.correctness(
                item["question"], item["expected_answer"], item["candidate_answer"]
            ),
        ),
        ("faithful", lambda: judge.faithfulness(item["candidate_answer"], item["chunks"])),
    ):
        try:
            verdict = call()
            row[name] = {"score": verdict.score, "reason": verdict.reason, "error": None}
        except JudgeError as exc:
            row[name] = {"score": None, "reason": "", "error": str(exc)}
    row["should_be_correct"] = item.get("should_be_correct")
    row["should_be_faithful"] = item.get("should_be_faithful")
    return row


def outcome(should_be, got: dict) -> str:
    """PASS / MISS for a scored check, ERROR for a failed call, REPORT for an unscored one."""
    if got["error"] is not None or got["score"] is None:
        return "ERROR"
    if should_be not in (0, 1):
        return "REPORT"
    return "PASS" if got["score"] == should_be else "MISS"


def summarize(rows: list[dict]) -> dict:
    neg_total = neg_caught = pos_total = pos_kept = errors = 0
    misses: list[str] = []
    strict = None
    for row in rows:
        for key, want_key in (("correct", "should_be_correct"), ("faithful", "should_be_faithful")):
            want, got = row[want_key], row[key]
            result = outcome(want, got)
            row[f"{key}_outcome"] = result
            if result == "ERROR":
                errors += 1
                if want in (0, 1):
                    if want == 0:
                        neg_total += 1
                    else:
                        pos_total += 1
                    misses.append(f"{row['id']} {key} (error)")
                continue
            if want == 0:
                neg_total += 1
                neg_caught += result == "PASS"
            elif want == 1:
                pos_total += 1
                pos_kept += result == "PASS"
            if result == "MISS":
                misses.append(f"{row['id']} {key}")
        if row["role"] == "strict":
            strict = (row["correct"]["score"], row["faithful"]["score"])
    return {
        "negatives_caught": neg_caught,
        "negatives": neg_total,
        "positives_kept": pos_kept,
        "positives": pos_total,
        "errors": errors,
        "misses": misses,
        "strict": strict,
    }


def summary_line(s: dict) -> str:
    line = (
        f"Negatives caught {s['negatives_caught']} of {s['negatives']}; "
        f"positives kept {s['positives_kept']} of {s['positives']}; "
        f"errored calls {s['errors']}; "
        f"strictness item (correct, faithful): "
        f"{'not run' if s['strict'] is None else s['strict']}."
    )
    if s["misses"]:
        line += " MISS: " + ", ".join(s["misses"]) + ". Bring this table to the reviewer."
    else:
        line += " No miss."
    return line


def _cell(got: dict) -> str:
    if got["error"] is not None:
        return f"error ({got['error']})"
    return str(got["score"])


def render(rows: list[dict], summary: dict, header: list[str]) -> str:
    out = [
        *header,
        "",
        "| id | tests | check | expected | judge | result | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        for key, want_key in (("correct", "should_be_correct"), ("faithful", "should_be_faithful")):
            want = row[want_key]
            reason = (row[key]["reason"] or "").replace("|", "/").replace("\n", " ")[:160]
            out.append(
                f"| {row['id']} | {row['tests']} | {key} | "
                f"{'not scored' if want is None else want} | {_cell(row[key])} | "
                f"{row[key + '_outcome']} | {reason} |"
            )
    out += ["", f"**{summary_line(summary)}**", ""]
    return "\n".join(out)


def main(argv=None, *, settings: Settings | None = None, judge=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Judge sanity check with negative controls.")
    parser.add_argument("--controls", type=Path, default=CONTROLS)
    parser.add_argument("--dry-run", action="store_true", help="fake judge; not a result")
    parser.add_argument("--confirm-live", action="store_true", help="required for a live run")
    parser.add_argument("--pace", type=float, default=15.0, help="seconds between items")
    args = parser.parse_args(argv)

    try:
        items = load_controls(args.controls)
    except (OSError, ValueError) as exc:
        print(f"Cannot read the controls: {exc}")
        return 1
    settings = settings or get_settings()
    dry = args.dry_run
    if not dry and not args.confirm_live:
        print(
            "REFUSING to start: this makes about "
            f"{2 * len(items)} judge calls (about {2 * len(items) * TOKENS_PER_CALL // 1000}K "
            "tokens on the judge model only); add --confirm-live (or --dry-run for fakes)."
        )
        return 2
    if dry:
        from eval.fakes import FakeJudge

        judge = FakeJudge()
        pace = 0.0
        results_dir = Path(tempfile.mkdtemp(prefix="judge-check-dry-"))
        print(f"{DRY_RUN_LINE} (fake judge, nothing is sent anywhere; the verdicts mean nothing)")
    else:
        pace = args.pace
        results_dir = RESULTS_DIR
        if judge is None:
            try:
                judge = build_judge(settings)
            except LLMConfigError as exc:
                print(f"Cannot judge: {exc}")
                return 1
    versions = "__".join(judge.versions[k] for k in ("correctness", "faithfulness"))
    run_id = f"judge-check__{versions}"
    print(
        f"JUDGE SANITY CHECK: {len(items)} controls, judge model {judge.model}, prompts {versions}"
    )
    print(
        f"estimate: {2 * len(items)} judge calls, about "
        f"{2 * len(items) * TOKENS_PER_CALL // 1000}K tokens on the judge model only; "
        f"pace {pace:.0f}s between items"
    )
    print("RULE: all unfaithful and wrong controls must score 0; all good controls must score 1.")
    rows = []
    for number, item in enumerate(items):
        if number and pace:
            sleep(pace)
        rows.append(judge_item(judge, item))
    summary = summarize(rows)
    header = [
        f"# Judge check: {run_id}",
        "",
        NOTE,
        "",
        f"- Run: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        + (f" ({DRY_RUN_LINE})" if dry else ""),
        f"- Judge model: {judge.model}; prompts: {versions}",
    ]
    text = render(rows, summary, header)
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{run_id}.md").write_text(text, encoding="utf-8")
    (results_dir / f"{run_id}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(text)
    print(f"Files: {results_dir / (run_id + '.md')} and .jsonl")
    if dry:
        shutil.rmtree(results_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
