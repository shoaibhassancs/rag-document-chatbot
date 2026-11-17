"""Time ONLY the clarify call, on the 20b and on the 120b (read-only; writes nothing anywhere).

  uv run python -m scripts.measure_clarify_latency --dry-run
  uv run python -m scripts.measure_clarify_latency --confirm-live

Takes the first 10 clear questions (no history) of the clarify tuning set and asks each one to
`openai/gpt-oss-20b` and then to `openai/gpt-oss-120b`, 3 seconds apart, with the clarify prompt
the app uses by default (settings.clarify_prompt_version). It prints the median and the slowest
time per model. About 20 calls. No database, no embedding, no answer, no judge; no file is written.
Never prints a key. The timings include network time and any retry the client made.
"""

import argparse
import statistics
import sys
import time
from collections.abc import Callable

from app.config import Settings, get_settings
from app.generation.llm import LLMClient, LLMConfigError, LLMError
from app.services.clarify import build_clarify_message
from eval.fakes import FakeClarifyClient
from scripts.check_clarify import DEFAULT_SET, group_of, load_items, turns_of

MODELS = ("openai/gpt-oss-20b", "openai/gpt-oss-120b")
CALLS = 10
GAP_SECONDS = 3.0
DRY_RUN_LINE = "DRY RUN: FAKE DATA"


def pick_items(items: list[dict], count: int = CALLS) -> list[dict]:
    return [i for i in items if group_of(i) == "clear" and not i.get("history")][:count]


def summarize(times: list[float]) -> tuple[float, float]:
    return statistics.median(times), max(times)


def _real_client(settings: Settings, model: str) -> LLMClient:
    role = settings.llm_role("rewrite").model_copy(update={"model": model})
    return LLMClient(
        role,
        fallback_model=None,
        max_retries=settings.llm_max_retries,
        max_wait_seconds=settings.llm_max_wait_seconds,
        timeout=settings.llm_timeout_seconds,
    )


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: Callable[[Settings, str], object] | None = None,
    timer: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="fakes only; prints FAKE DATA")
    parser.add_argument("--confirm-live", action="store_true", help="required for live calls")
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    version = settings.clarify_prompt_version
    _, items = load_items(DEFAULT_SET)
    chosen = pick_items(items)
    try:
        system = (settings.prompts_dir / f"{version}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        out(f"REFUSING to start: there is no prompt file {version}.txt.")
        return 2

    if args.dry_run:
        out(DRY_RUN_LINE + ": NOT A RESULT")
    out(
        f"Clarify latency: {len(chosen)} clear questions x {len(MODELS)} models, "
        f"{GAP_SECONDS:.0f} s apart, prompt {version}; about {len(chosen) * len(MODELS)} calls"
    )
    if not args.dry_run:
        if not args.confirm_live:
            out("REFUSING to start: a live run needs --confirm-live.")
            return 2
        role = settings.llm_role("rewrite")
        if role.api_key is None or not role.api_key.get_secret_value():
            out("REFUSING to start: the rewrite model's API key setting has no value.")
            return 2

    fake_now = [0.0]
    if args.dry_run:

        def timer() -> float:  # noqa: F811 - fake time: each call "takes" a fixed amount
            return fake_now[0]

        def sleep(seconds: float) -> None:  # noqa: F811
            fake_now[0] += seconds

    for model in MODELS:
        try:
            client = (
                FakeClarifyClient(chosen)
                if args.dry_run
                else (client_factory or _real_client)(settings, model)
            )
        except LLMConfigError as exc:
            out(f"REFUSING to start: {exc}")
            return 2
        times: list[float] = []
        errors = 0
        for number, item in enumerate(chosen):
            if number:
                sleep(GAP_SECONDS)
            user = build_clarify_message(turns_of(item), item["question"])
            began = timer()
            try:
                client.complete(system, user)
            except LLMError as exc:
                errors += 1
                out(f"  {model} item {number + 1}: {type(exc).__name__}")
                continue
            if args.dry_run:
                fake_now[0] += 0.5 if model.endswith("-20b") else 0.9
            times.append(timer() - began)
        if not times:
            out(f"{model}: no call succeeded ({errors} errors)")
            continue
        median, slowest = summarize(times)
        out(
            f"{model}: median {median:.2f} s, slowest {slowest:.2f} s "
            f"({len(times)} calls ok, {errors} errors)"
        )
    out("Timings are one short sample of the clarify call alone, not an evaluation result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
