"""Compare answer prompts and models on the calibration questions (Milestone 3).

  uv run python -m scripts.compare_prompts retrieve
  uv run python -m scripts.compare_prompts run --prompt answer_v2 --model openai/gpt-oss-20b
  uv run python -m scripts.compare_prompts report

`retrieve` embeds all questions in ONE Voyage request and saves the top-k chunks to
data/calibration/cache/ (git-ignored). `run` replays those saved chunks through the production
logic (threshold gate, then the grounded answerer) with the chosen prompt and model, paced for
Groq's per-minute token limit, and saves one results file. `report` merges the results files into
one table. No Voyage call happens in `run`. The answer-model fallback is OFF here so a result never
mixes two models. These are the calibration questions, NOT the evaluation set.
"""

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.db import connect
from app.generation.answer import AnswerFormatError, build_answerer, build_sources
from app.generation.llm import LLMError
from app.ingest.embedder import EmbeddingError, build_embedder
from app.retrieval.search import Hit, apply_threshold, search
from scripts.calibrate_threshold import DEFAULT_QUESTIONS, load_questions

CACHE_DIR = Path("data/calibration/cache")
RETRIEVAL_FILE = CACHE_DIR / "retrieval.json"


def fingerprint(conn, model: str) -> str:
    """Changes whenever the stored chunks or the embedding model change."""
    rows = conn.execute(
        "SELECT chunk_hash FROM chunks WHERE embedding IS NOT NULL ORDER BY chunk_hash"
    ).fetchall()
    return hashlib.sha256((model + "|" + "|".join(r[0] for r in rows)).encode()).hexdigest()[:16]


def retrieve(settings, questions: list[dict], refresh: bool) -> dict:
    with connect(settings.database_url) as conn:
        current = fingerprint(conn, settings.embedding_model)
        if RETRIEVAL_FILE.exists() and not refresh:
            cached = json.loads(RETRIEVAL_FILE.read_text())
            same = [q["id"] for q in questions] == [x["id"] for x in cached["questions"]]
            texts = [q["question"] for q in questions] == [
                x["question"] for x in cached["questions"]
            ]
            if cached["fingerprint"] == current and same and texts:
                print("Retrieval cache is valid; no Voyage request made.")
                return cached
        embedder = build_embedder(settings)
        vectors = embedder.embed([q["question"] for q in questions], "query").vectors
        print(f"Embedded {len(questions)} questions in {embedder.requests} Voyage request(s).")
        entries = []
        for question, vector in zip(questions, vectors, strict=True):
            hits = search(
                conn, vector, top_k=settings.top_k, embedding_model=settings.embedding_model
            )
            entries.append({**question, "hits": [h.__dict__ for h in hits]})
    data = {"fingerprint": current, "model": settings.embedding_model, "questions": entries}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RETRIEVAL_FILE.write_text(json.dumps(data))
    return data


def base_row(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "category": entry["category"],
        "question": entry["question"],
        "expected_sources": entry["expected_sources"],
    }


def gate_row(entry: dict) -> dict:
    return {
        **base_row(entry),
        "outcome": "gate",
        "answer": "",
        "sources": [],
        "raw": None,
        "llm_ms": None,
        "tokens": None,
    }


def error_row(entry: dict, exc: Exception) -> dict:
    return {
        **base_row(entry),
        "outcome": "error",
        "answer": f"{type(exc).__name__}: {exc}",
        "sources": [],
        "raw": None,
        "llm_ms": None,
        "tokens": None,
    }


def model_row(entry: dict, answer) -> dict:
    """Outcome 'answered' or 'model' (the model said the excerpts do not answer)."""
    return {
        **base_row(entry),
        "outcome": "answered" if answer.answerable else "model",
        "answer": answer.text,
        "sources": [x["file"] for x in build_sources(answer.used)] if answer.answerable else [],
        "raw": answer.llm.text,
        "llm_ms": answer.llm.latency_ms,
        "fallback_used": answer.llm.fallback_used,
        "tokens": {
            "prompt": answer.llm.prompt_tokens,
            "completion": answer.llm.completion_tokens,
            "reasoning": answer.llm.reasoning_tokens,
            "attempts": answer.llm.attempts,
        },
    }


def has_expected_source(row: dict) -> bool:
    """True when an answered row cites at least one of its expected source files."""
    return row["outcome"] == "answered" and bool(set(row["sources"]) & set(row["expected_sources"]))


def extra_sources(row: dict) -> list[str]:
    """Cited files beyond the expected ones (over-citation). Empty unless answered."""
    if row["outcome"] != "answered":
        return []
    return [f for f in row["sources"] if f not in row["expected_sources"]]


def metrics(results: list[dict]) -> dict:
    ans = [x for x in results if x["category"] == "answerable"]
    oos = [x for x in results if x["category"] == "out_of_scope"]
    vag = [x for x in results if x["category"] == "vague"]
    return {
        "answerable": len(ans),
        "cited_expected": [x["id"] for x in ans if has_expected_source(x)],
        "wrongly_refused": [x["id"] for x in ans if x["outcome"] in ("gate", "model")],
        "oos_answered": [x["id"] for x in oos if x["outcome"] == "answered"],
        "oos_gate": sum(1 for x in oos if x["outcome"] == "gate"),
        "oos_model": sum(1 for x in oos if x["outcome"] == "model"),
        "vague": [(x["id"], x["outcome"]) for x in vag],
        "over_cited": {x["id"]: extra_sources(x) for x in results if extra_sources(x)},
    }


class UnstableRun(Exception):
    """The run was stopped because a call fell back to another model or retried after a 429."""


def check_clean(row: dict) -> None:
    tokens = row.get("tokens") or {}
    if row.get("fallback_used"):
        raise UnstableRun(f"{row['id']}: the call fell back to another model")
    if tokens.get("attempts", 1) > 1:
        raise UnstableRun(f"{row['id']}: the call needed {tokens['attempts']} attempts (retry)")


def slug(prompt: str, model: str) -> str:
    return f"{prompt}__{model.replace('/', '_')}"


def run(settings, prompt: str, model: str, pace: float) -> Path:
    if not RETRIEVAL_FILE.exists():
        raise SystemExit("No retrieval cache. Run `retrieve` first.")
    cached = json.loads(RETRIEVAL_FILE.read_text())
    s = settings.model_copy(
        update={
            "answer_prompt_version": prompt,
            "answer_llm_model": model,
            "answer_fallback_enabled": False,
        }
    )
    print(f"prompt={prompt} model={model} threshold={s.similarity_threshold} pace={pace}s")
    answerer = build_answerer(s)
    results = []
    last_call = None
    for entry in cached["questions"]:
        hits = [Hit(**h) for h in entry["hits"]]
        kept = apply_threshold(hits, s.similarity_threshold)
        if not kept:
            row = gate_row(entry)
        else:
            if last_call is not None:
                time.sleep(max(0.0, pace - (time.monotonic() - last_call)))
            last_call = time.monotonic()
            try:
                row = model_row(entry, answerer.answer(entry["question"], kept))
            except (LLMError, AnswerFormatError) as exc:
                row = error_row(entry, exc)
        results.append(row)
        print(f"  {row['id']}: {row['outcome']}")
    path = CACHE_DIR / f"results-{slug(prompt, model)}.json"
    path.write_text(json.dumps({"prompt": prompt, "model": model, "results": results}))
    return path


def stability(settings, prompt: str, model: str, ids: list[str], repeats: int, pace: float):
    """Run the chosen questions several times on cached chunks. Stops (UnstableRun or LLMError)
    on any fallback, retry or error rather than continuing with mixed conditions."""
    cached = json.loads(RETRIEVAL_FILE.read_text())
    s = settings.model_copy(
        update={
            "answer_prompt_version": prompt,
            "answer_llm_model": model,
            "answer_fallback_enabled": False,
        }
    )
    answerer = build_answerer(s)
    entries = [e for e in cached["questions"] if e["id"] in ids]
    print(f"stability prompt={prompt} model={model} questions={ids} repeats={repeats} pace={pace}s")
    runs: list[dict] = []
    last_call = None
    for rep in range(1, repeats + 1):
        for entry in entries:
            hits = apply_threshold([Hit(**h) for h in entry["hits"]], s.similarity_threshold)
            if not hits:
                raise SystemExit(
                    f"{entry['id']} is stopped by the gate; pick questions that reach it"
                )
            if last_call is not None:
                time.sleep(max(0.0, pace - (time.monotonic() - last_call)))
            last_call = time.monotonic()
            try:
                row = model_row(entry, answerer.answer(entry["question"], hits))
                check_clean(row)
            except (LLMError, AnswerFormatError, UnstableRun):
                _save_stability(prompt, model, runs)
                raise
            runs.append({**row, "run": rep})
            print(f"  run {rep} {row['id']}: {row['outcome']}", flush=True)
    return _save_stability(prompt, model, runs)


def _save_stability(prompt: str, model: str, runs: list[dict]) -> Path:
    path = CACHE_DIR / f"stability-{slug(prompt, model)}.json"
    path.write_text(json.dumps({"prompt": prompt, "model": model, "runs": runs}))
    return path


def stability_report() -> None:
    for f in sorted(CACHE_DIR.glob("stability-*.json")):
        data = json.loads(f.read_text())
        print(f"**{data['prompt']} / {data['model']}**")
        by_id: dict[str, list[dict]] = {}
        for r in data["runs"]:
            by_id.setdefault(r["id"], []).append(r)
        for qid, rs in by_id.items():
            refused = sum(1 for r in rs if r["outcome"] == "model")
            answered = sum(1 for r in rs if r["outcome"] == "answered")
            print(
                f"- {qid} {rs[0]['question']!r}: refused {refused}, answered {answered}"
                f" of {len(rs)}"
            )
            for r in rs:
                if r["outcome"] == "answered":
                    print(f"    run {r['run']} raw JSON: {r['raw']}")
        print()


def report() -> None:
    files = sorted(CACHE_DIR.glob("results-*.json"))
    runs = [json.loads(f.read_text()) for f in files]
    if not runs:
        print("No results yet.")
        return
    names = [f"{r['prompt']} / {r['model'].split('/')[-1]}" for r in runs]
    ids = [x["id"] for x in runs[0]["results"]]
    label = {
        "gate": "REFUSED (gate)",
        "model": "REFUSED (model)",
        "answered": "answered",
        "error": "ERROR",
    }
    print("| id | question | " + " | ".join(names) + " |")
    print("|---|---|" + "---|" * len(runs))
    for i, qid in enumerate(ids):
        base = runs[0]["results"][i]
        cells = []
        for r in runs:
            x = r["results"][i]
            cell = label[x["outcome"]]
            if x["outcome"] == "answered":
                cell += f": {x['answer'][:90]} [{', '.join(x['sources'])}]"
            cells.append(cell.replace("|", "/"))
        print(f"| {qid} | {base['question']} | " + " | ".join(cells) + " |")
    print()
    for name, r in zip(names, runs, strict=True):
        res = r["results"]
        m = metrics(res)
        oos = [x for x in res if x["category"] == "out_of_scope"]
        llm_ms = [x["llm_ms"] for x in res if x["llm_ms"]]
        print(f"**{name}**")
        print(
            f"- answerable answered with an expected source cited: {len(m['cited_expected'])} of "
            f"{m['answerable']} (read the answers to judge correctness); wrongly refused: "
            f"{m['wrongly_refused'] or 'none'}"
        )
        print(
            f"- out-of-scope answered (wrong): {m['oos_answered'] or 'none'}; refused at gate: "
            f"{m['oos_gate']}, by model: {m['oos_model']}"
        )
        print(f"- vague: {m['vague']}")
        print(f"- over-cited (files beyond the expected ones): {m['over_cited'] or 'none'}")
        if llm_ms:
            print(
                f"- LLM latency ms: median {statistics.median(llm_ms):.0f}, max {max(llm_ms)}"
                f" (n={len(llm_ms)}); total adds about 1.2 s embed + 0.1 s search"
            )
        for x in oos:
            if x["outcome"] == "answered":
                print(f"  - {x['id']} raw JSON: {x['raw']}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["retrieve", "run", "report", "stability", "stability-report"]
    )
    parser.add_argument("--prompt", default="answer_v1")
    parser.add_argument("--model", default=None)
    parser.add_argument("--pace", type=float, default=30.0, help="seconds between Groq calls")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--ids", default="O02,O04,O05,O08,O09,O10")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "retrieve":
            retrieve(settings, load_questions(args.questions), args.refresh)
        elif args.command == "run":
            path = run(settings, args.prompt, args.model or settings.answer_llm_model, args.pace)
            print(f"Saved {path}")
        elif args.command == "stability":
            model = args.model or settings.answer_llm_model
            path = stability(
                settings, args.prompt, model, args.ids.split(","), args.repeats, args.pace
            )
            print(f"Saved {path}")
        elif args.command == "stability-report":
            stability_report()
        else:
            report()
    except UnstableRun as exc:
        print(f"STOPPED: {exc}")
        return 3
    except EmbeddingError as exc:
        print(f"Embedding stopped: {exc}")
        return 2
    except LLMError as exc:
        print(f"LLM stopped: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
