"""scripts/measure_clarify_latency.py with fakes: no network, no database."""

from app.config import Settings
from app.generation.llm import LLMBusyError
from eval.fakes import FakeClarifyClient
from scripts import check_clarify as cc
from scripts import measure_clarify_latency as ml


def make_settings(**kw) -> Settings:
    values = dict(_env_file=None, groq_api_key="fake-groq-key")
    values.update(kw)
    return Settings(**values)


ITEMS = cc.load_items(cc.DEFAULT_SET)[1]


def test_it_picks_ten_clear_questions_without_history_from_the_tuning_set():
    chosen = ml.pick_items(ITEMS)
    assert len(chosen) == 10
    assert all(cc.group_of(i) == "clear" and not i.get("history") for i in chosen)


def test_the_dry_run_says_fake_data_uses_no_client_and_prints_both_models():
    lines: list[str] = []
    calls: list[str] = []
    code = ml.main(
        ["--dry-run"],
        settings=make_settings(),
        client_factory=lambda s, m: calls.append(m),
        sleep=lambda s: None,
        out=lines.append,
    )
    text = "\n".join(lines)
    assert code == 0 and lines[0].startswith("DRY RUN: FAKE DATA") and calls == []
    assert "openai/gpt-oss-20b: median 0.50 s, slowest 0.50 s" in text
    assert "openai/gpt-oss-120b: median 0.90 s" in text and "about 20 calls" in text


def test_a_live_run_needs_confirm_live_and_a_key():
    lines: list[str] = []
    made: list[str] = []

    def factory(s, m):
        made.append(m)

    assert ml.main([], settings=make_settings(), client_factory=factory, out=lines.append) == 2
    assert "--confirm-live" in "\n".join(lines) and made == []
    no_key = Settings(_env_file=None, groq_api_key=None)
    assert ml.main(["--confirm-live"], settings=no_key, client_factory=factory) == 2
    assert made == []


def test_a_live_run_times_each_call_with_the_gap_and_reports_median_and_slowest():
    clock = [0.0]
    sleeps: list[float] = []
    durations = iter([1.0, 2.0, 3.0] * 7)

    class Timed(FakeClarifyClient):
        def complete(self, system, user, **kw):
            clock[0] += next(durations)
            return super().complete(system, user, **kw)

    lines: list[str] = []
    code = ml.main(
        ["--confirm-live"],
        settings=make_settings(),
        client_factory=lambda s, m: Timed(ITEMS),
        timer=lambda: clock[0],
        sleep=sleeps.append,
        out=lines.append,
    )
    text = "\n".join(lines)
    assert code == 0 and sleeps == [3.0] * 18  # 9 gaps per model
    assert text.count("median") == 2 and "slowest 3.00 s" in text


def test_errors_are_counted_and_do_not_stop_the_other_model():
    class Busy(FakeClarifyClient):
        def complete(self, system, user, **kw):
            raise LLMBusyError("busy")

    lines: list[str] = []
    models: list[str] = []

    def factory(s, m):
        models.append(m)
        return Busy(ITEMS) if m.endswith("-20b") else FakeClarifyClient(ITEMS)

    code = ml.main(
        ["--confirm-live"],
        settings=make_settings(),
        client_factory=factory,
        sleep=lambda s: None,
        out=lines.append,
    )
    text = "\n".join(lines)
    assert code == 0 and models == list(ml.MODELS)
    assert "no call succeeded" in text and "openai/gpt-oss-120b: median" in text


def test_it_never_touches_the_database_or_writes_files():
    source = open(ml.__file__).read()
    for forbidden in ("psycopg", "connect(", "write_text", "open(", "MemoryStore", "ChatLogger"):
        assert forbidden not in source
    assert "print(settings" not in source and "print(role" not in source
