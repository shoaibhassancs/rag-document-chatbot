"""Small live smoke test of /chat's pipeline with the REAL Voyage and Groq (Milestone 3).

  uv run python -m scripts.smoke_chat [--limit N] [--reasoning-experiment]

Runs the questions marked "smoke": true in data/calibration/questions.jsonl through ChatService,
prints status, answer, sources and the latency split (embed, search, LLM, total) and token use
per question, and a summary. It spends real quota (about one Voyage request and one Groq call per
question), so run it sparingly. The active answer model is printed first. Never prints keys.

--reasoning-experiment adds three direct answer-model calls (effort low, high, low) and prints how
much the provider's remaining-tokens-per-minute header dropped after each, next to the reported
usage, to see whether reasoning tokens (and the output cap) count toward the per-minute limit.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.db import connect
from app.generation.answer import GroundedAnswerer, build_answerer
from app.generation.llm import LLMClient, LLMError
from app.ingest.embedder import CachingEmbedder, EmbeddingError, build_embedder
from app.retrieval.search import search
from app.services.chat import ChatService
from scripts.calibrate_threshold import DEFAULT_QUESTIONS, load_questions


def fmt(value) -> str:
    return "-" if value is None else str(value)


def refusal_source(result) -> str:
    if result.status != "refused":
        return ""
    return "REFUSED BY THE MODEL" if result.llm_usage else "REFUSED BY THE GATE (no LLM call)"


def run_questions(settings, service: ChatService, questions: list[dict], pace: float) -> list:
    results = []
    for index, q in enumerate(questions):
        if index and pace:
            time.sleep(pace)  # stay under Voyage's 3 requests/min so the chat wait cap is not hit
        result = service.chat("smoke", q["question"])
        results.append(result)
        usage = result.llm_usage
        print(f"\n[{q['id']}] {q['question']}")
        print(f"  status={result.status} {refusal_source(result)}  answer={result.answer!r}")
        if result.raw_reply is not None:
            print(f"  raw model JSON: {result.raw_reply}")
        for s in result.sources:
            print(f"  source: {s['file']} page={fmt(s['page'])} section={fmt(s['section'])}")
        print(f"  retrieved: {[(r['file'], r['similarity']) for r in result.retrieved]}")
        print(f"  ms: {result.timings_ms}")
        if usage:
            print(
                f"  tokens: prompt={fmt(usage['prompt_tokens'])} "
                f"completion={fmt(usage['completion_tokens'])} "
                f"reasoning={fmt(usage['reasoning_tokens'])} total={fmt(usage['total_tokens'])} "
                f"model={usage['model']} attempts={usage['attempts']} "
                f"fallback={usage['fallback_used']}"
            )
            remaining = usage["rate_limit"].get("x-ratelimit-remaining-tokens")
            print(f"  groq remaining tokens/min after call: {fmt(remaining)}")
        for w in result.warnings:
            print(f"  WARNING: {w}")
    return results


def summarise(results: list) -> None:
    print("\n=== summary ===")
    print(
        "statuses:",
        {s: sum(1 for r in results if r.status == s) for s in {r.status for r in results}},
    )
    for stage in ("embed", "search", "llm", "total"):
        values = [r.timings_ms[stage] for r in results if stage in r.timings_ms]
        if values:
            print(
                f"{stage:>6} ms: median {statistics.median(values):.0f}  "
                f"mean {statistics.mean(values):.0f}  max {max(values)}  (n={len(values)})"
            )
    used = [r.llm_usage for r in results if r.llm_usage]
    if used:
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            values = [u[key] for u in used if u[key] is not None]
            if values:
                print(f"{key:>18}: mean {statistics.mean(values):.0f}  max {max(values)}")


def reasoning_experiment(settings, embedder, question: dict) -> None:
    """Three back-to-back calls; compare the drop in x-ratelimit-remaining-tokens with usage."""
    vector = embedder.embed_query(question["question"])
    with connect(settings.database_url) as conn:
        hits = search(conn, vector, top_k=settings.top_k, embedding_model=settings.embedding_model)
    base_role = settings.llm_role("answer")
    answerer = build_answerer(settings)
    system = answerer._system  # the same prompt the service uses
    limit_tokens = None
    previous_remaining = None
    previous_time = None
    print("\n=== reasoning-token experiment ===")
    for effort, cap in (
        ("low", base_role.max_output_tokens),
        ("high", 2000),
        ("low", base_role.max_output_tokens),
    ):
        role = base_role.model_copy(update={"reasoning_effort": effort, "max_output_tokens": cap})
        llm = LLMClient(role, fallback_model=None, timeout=settings.llm_timeout_seconds)
        probe = GroundedAnswerer(llm, system, "experiment")
        try:
            answer = probe.answer(question["question"], hits)
        except Exception as exc:  # report and stop; the experiment is informational
            print(f"effort={effort}: failed ({type(exc).__name__}: {exc})")
            return
        r = answer.llm
        remaining = r.rate_limit.get("x-ratelimit-remaining-tokens")
        limit_tokens = r.rate_limit.get("x-ratelimit-limit-tokens", limit_tokens)
        now = time.monotonic()
        print(
            f"effort={effort} cap={cap}: prompt={fmt(r.prompt_tokens)} "
            f"completion={fmt(r.completion_tokens)} reasoning={fmt(r.reasoning_tokens)} "
            f"total={fmt(r.total_tokens)} finish={r.finish_reason}  "
            f"remaining-tokens header={fmt(remaining)}  all headers={r.rate_limit}"
        )
        if previous_remaining is not None and remaining is not None:
            dropped = previous_remaining - int(remaining)
            gap = now - previous_time
            refill = (int(limit_tokens) / 60 * gap) if limit_tokens else 0
            print(
                f"   header dropped by {dropped} since the previous call "
                f"(~{refill:.0f} tokens refilled in the {gap:.1f}s gap, "
                f"so about {dropped + refill:.0f} "
                f"consumed). Compare with: prompt+completion={fmt(r.total_tokens)}, "
                f"prompt+completion-reasoning="
                f"{(r.total_tokens - (r.reasoning_tokens or 0)) if r.total_tokens else '-'}, "
                f"prompt+cap={(r.prompt_tokens or 0) + cap}"
            )
        previous_remaining = int(remaining) if remaining is not None else None
        previous_time = now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live smoke test of the chat pipeline.")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--ids", help="comma-separated question ids to run instead of the smoke set"
    )
    parser.add_argument("--pace", type=float, default=21.0, help="seconds between questions")
    parser.add_argument("--reasoning-experiment", action="store_true")
    parser.add_argument(
        "--experiment-only", action="store_true", help="run only the reasoning experiment"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    print(
        f"answer model: {settings.answer_llm_model} (fallback: "
        f"{settings.answer_fallback_model if settings.answer_fallback_enabled else 'off'})"
    )
    print(
        f"embedding model: {settings.embedding_model}; threshold: {settings.similarity_threshold}"
    )
    try:
        everything = load_questions(args.questions)
        if args.ids:
            wanted = args.ids.split(",")
            questions = [q for q in everything if q["id"] in wanted]
        else:
            questions = [q for q in everything if q.get("smoke")]
        if args.limit:
            questions = questions[: args.limit]
        embedder = CachingEmbedder(build_embedder(settings))
        service = ChatService(settings, embedder, build_answerer(settings))
        if not args.experiment_only:
            results = run_questions(settings, service, questions, args.pace)
            summarise(results)
        if args.reasoning_experiment or args.experiment_only:
            pool = questions if not args.experiment_only else everything
            reasoning_experiment(
                settings, embedder, next(q for q in pool if q["category"] == "answerable")
            )
    except (LLMError, EmbeddingError, OSError, ValueError) as exc:
        print(f"Stopped: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
