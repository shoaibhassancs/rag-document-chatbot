"""The evaluation runner with a fake pipeline, judge and clock: no network, no database."""

import json

import pytest

from app.config import Settings
from app.services.chat import ChatResult
from eval import run_eval
from eval.fakes import FakeClock, FakeJudge, FakePipeline
from eval.judge import JudgeError, Verdict
from eval.questions import load_questions
from eval.state import ConfigMismatch, State, StateError

SENTINEL = "fake-eval-secret-xyz"
ALL = load_questions(run_eval.DEFAULT_QUESTIONS)[1]


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    state, results = tmp_path / "real_state", tmp_path / "real_results"
    monkeypatch.setattr(run_eval, "STATE_DIR", state)
    monkeypatch.setattr(run_eval, "RESULTS_DIR", results)
    return state, results


def make_settings(**kw) -> Settings:
    values = dict(
        _env_file=None,
        groq_api_key=SENTINEL,
        voyage_api_key="fake-voyage-key",
        similarity_threshold=0.4,
    )
    values.update(kw)
    return Settings(**values)


class Harness:
    def __init__(self, settings=None, pipeline=None, judge=None):
        self.settings = settings or make_settings()
        self.pipeline = pipeline or FakePipeline(ALL)
        self.judge = judge or FakeJudge()
        self.clock = FakeClock()
        self.lines: list[str] = []
        self.factory_calls = 0

    def run(self, *args, settings=None):
        def pipeline_factory(s):
            self.factory_calls += 1
            return self.pipeline

        return run_eval.main(
            list(args),
            settings=settings or self.settings,
            pipeline_factory=pipeline_factory,
            judge_factory=lambda s: self.judge,
            clock=self.clock,
            sleep=self.clock.sleep,
            out=self.lines.append,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


LIVE = ("--confirm-live", "--allow-draft")


def read_state(dirs, run_id):
    return State(dirs[0] / f"{run_id}.jsonl")


def the_run_id(h):
    return run_eval.run_id_for(
        run_eval.config_snapshot(h.settings, run_eval.DEFAULT_QUESTIONS, 2), None
    )


# ----------------------------------------------------------------------------- guards


def test_a_live_run_without_confirm_live_refuses_and_touches_nothing(dirs):
    h = Harness()
    assert h.run("--allow-draft") == 2
    assert "REFUSING" in h.text and "--confirm-live" in h.text and "Would make about" in h.text
    assert h.factory_calls == 0 and not dirs[0].exists() and not dirs[1].exists()


def draft_copy(tmp_path, header_too=False):
    """A temp copy of the shipped file with ONE question (E02, not the first) set back to DRAFT."""
    lines = run_eval.DEFAULT_QUESTIONS.read_text().splitlines()
    marker = '"status": "APPROVED"'
    lines = [
        line.replace(marker, '"status": "DRAFT"') if '"id": "E02"' in line else line
        for line in lines
    ]
    if header_too:
        lines[0] = lines[0].replace("APPROVED by the owner", "DRAFT: not approved by the owner")
    path = tmp_path / "questions.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_shipped_questions_run_live_without_allow_draft(dirs):
    # Added (INSTRUCTIONS_17): an all-APPROVED file needs --confirm-live and nothing else.
    h = Harness()
    assert h.run("--confirm-live", "--limit", "2") == 0
    assert "APPROVED by the owner" in h.lines[0] and "DRAFT" not in h.lines[0]
    assert h.factory_calls == 1


def test_a_question_set_back_to_draft_needs_allow_draft_for_a_live_run(dirs, tmp_path):
    # Changed deliberately (INSTRUCTIONS_17): this used the shipped file, which was all DRAFT;
    # it now uses a temp copy with one question set back to DRAFT. The rule is unchanged.
    path = draft_copy(tmp_path)
    for extra in ([], ["--limit", "1"], ["--ids", "E01"]):  # a subset cannot hide the DRAFT one
        h = Harness()
        assert h.run("--confirm-live", "--questions", str(path), *extra) == 2
        assert "DRAFT" in h.text and "--allow-draft" in h.text
        assert h.factory_calls == 0
    assert not dirs[0].exists()
    h = Harness()
    assert h.run("--confirm-live", "--allow-draft", "--questions", str(path), "--limit", "2") == 0
    assert "DRAFT (not approved by the owner)" in h.lines[0]
    summary = next(p for p in dirs[1].glob("*.md") if "handcheck" not in p.name).read_text()
    assert "DRAFT questions: not approved by the owner" in summary


def test_a_draft_header_alone_also_needs_allow_draft(dirs, tmp_path):
    lines = run_eval.DEFAULT_QUESTIONS.read_text().splitlines()
    lines[0] = lines[0].replace("APPROVED by the owner", "DRAFT: not approved by the owner")
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    h = Harness()
    assert h.run("--confirm-live", "--questions", str(path)) == 2 and "--allow-draft" in h.text


def test_confirm_live_is_still_required_when_everything_is_approved(dirs):
    h = Harness()
    assert h.run() == 2
    assert "REFUSING" in h.text and "--confirm-live" in h.text
    assert h.factory_calls == 0 and not dirs[0].exists() and not dirs[1].exists()


def test_a_missing_key_is_reported_by_setting_name_only(dirs):
    h = Harness(settings=make_settings(groq_api_key=None, voyage_api_key=None))
    assert h.run(*LIVE) == 2
    assert "GROQ_API_KEY" in h.text and "VOYAGE_API_KEY" in h.text and "no value" in h.text
    assert h.factory_calls == 0


def test_no_key_value_is_ever_printed_logged_or_written(dirs):
    h = Harness()
    assert h.run(*LIVE, "--limit", "6") == 0
    assert SENTINEL not in h.text
    for root in dirs:
        for path in root.rglob("*"):
            if path.is_file():
                assert SENTINEL not in path.read_text(), path


def test_nothing_runs_when_no_question_is_selected(dirs):
    h = Harness()
    assert h.run(*LIVE, "--ids", "NOPE") == 2 and "Nothing to run" in h.text


def test_a_broken_question_file_stops_the_run(dirs, tmp_path):
    bad = tmp_path / "q.jsonl"
    bad.write_text('{"id": "X1"}\n')
    h = Harness()
    assert h.run(*LIVE, "--questions", str(bad)) == 2 and "Question file problems" in h.text


# ----------------------------------------------------------------------------- banner


def test_the_startup_banner_names_every_model_prompt_and_the_threshold(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "2")
    banner = h.lines[0]
    for needle in (
        "openai/gpt-oss-20b",
        "answer_v2",
        "clarify_v2",
        "qwen/qwen3.8-27b",
        "judge_correctness_v1",
        "0.4",
        "APPROVED by the owner",  # changed deliberately (INSTRUCTIONS_17): was the DRAFT label
        "BOTH gpt-oss-20b and gpt-oss-120b",
        "openai/gpt-oss-120b",  # the fallback
    ):
        assert needle in banner, needle
    assert SENTINEL not in banner


def test_the_banner_and_config_are_recorded_in_the_state_and_the_summary(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "2")
    state = read_state(dirs, the_run_id(h))
    assert state.header["answer_model"] == "openai/gpt-oss-20b"
    assert state.header["answer_prompt_version"] == "answer_v2"
    summary = (dirs[1] / f"{the_run_id(h)}.md").read_text()
    assert "openai/gpt-oss-20b" in summary and "answer_v2" in summary
    # Changed deliberately (INSTRUCTIONS_17): the shipped file is approved, so no DRAFT warning.
    assert "DRAFT questions" not in summary and "APPROVED by the owner" in summary


# ----------------------------------------------------------------------------- dry run


def test_a_dry_run_never_writes_to_the_real_directories_and_says_so_on_the_first_line(dirs):
    h = Harness()
    assert h.run("--dry-run", "--limit", "6") == 0
    assert h.lines[0].splitlines()[0] == run_eval.DRY_RUN_LINE  # the first line of the banner
    assert not dirs[0].exists() and not dirs[1].exists()
    written = [line.split("Wrote ")[1] for line in h.lines if line.startswith("Wrote ")]
    assert len(written) == 3
    for path in written:
        assert str(dirs[1]) not in path and "eval-dryrun-results-" in path
    summary = next(p for p in written if p.endswith(".md") and "handcheck" not in p)
    assert open(summary).read().startswith(run_eval.DRY_RUN_LINE)
    assert (
        open(next(p for p in written if "handcheck" in p)).read().startswith(run_eval.DRY_RUN_LINE)
    )
    rows = [json.loads(line) for line in open(next(p for p in written if p.endswith(".jsonl")))]
    assert rows and all(r["dry_run"] for r in rows)


def test_a_dry_run_needs_no_keys_and_makes_no_real_pipeline(dirs):
    h = Harness(settings=make_settings(groq_api_key=None, voyage_api_key=None))
    assert h.run("--dry-run", "--limit", "3") == 0 and h.factory_calls == 0


# ----------------------------------------------------------------------------- a full run


def test_a_full_run_with_fakes_writes_state_results_and_a_summary(dirs):
    h = Harness()
    assert h.run(*LIVE) == 0
    run_id = the_run_id(h)
    state = read_state(dirs, run_id)
    answers = [r for r in state.records if r["kind"] == "answer"]
    judged = [r for r in state.records if r["kind"] == "judge"]
    # Changed deliberately (INSTRUCTIONS_16): E31 became answerable, so 29 judged, 58 judge calls.
    assert len(answers) == 40 and len(judged) == 29
    assert all(r["run"] == 1 for r in answers)
    assert set(h.judge.calls) == {"correctness", "faithfulness"} and len(h.judge.calls) == 58
    summary = (dirs[1] / f"{run_id}.md").read_text()
    assert "| Answer correctness | 100.0% | 29 | 85% | yes |" in summary
    assert "| Refusal accuracy | 100.0% | 36 | 95% | yes |" in summary
    raw = (dirs[1] / f"{run_id}.jsonl").read_text().splitlines()
    assert len(raw) == 40 and (dirs[1] / f"{run_id}-handcheck.md").exists()


def test_the_follow_up_questions_send_their_setup_turn_first_in_the_same_session(dirs):
    h = Harness()
    h.run(*LIVE, "--ids", "E25,E26")
    calls = h.pipeline.calls
    assert [m for _, m in calls] == [
        "what are your opening times during the week?",
        "and saturdays?",
        "how much is a one surface tooth colored filling",
        "and a crown?",
    ]
    assert calls[0][0] == calls[1][0] and calls[2][0] == calls[3][0] and calls[0][0] != calls[2][0]
    assert calls[0][0].startswith("eval-")


def test_pacing_is_simulated_and_respects_the_token_budget(dirs):
    h = Harness()
    start = h.clock.now
    h.run(*LIVE, "--limit", "12", "--phase", "answer")
    assert h.clock.now - start > 60  # 12 questions x 2,900 tokens cannot fit in one minute
    state = read_state(dirs, the_run_id(h))
    assert any(r["kind"] == "usage" and r["model"] == "voyage" for r in state.records)


def test_ids_and_limit_select_questions(dirs):
    h = Harness()
    h.run(*LIVE, "--ids", "E03,E01", "--phase", "answer")
    answers = [r["qid"] for r in read_state(dirs, the_run_id(h)).records if r["kind"] == "answer"]
    assert answers == ["E01", "E03"]


def test_phases_can_run_separately_and_the_judge_phase_needs_no_pipeline_calls(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "5", "--phase", "answer")
    state = read_state(dirs, the_run_id(h))
    assert not [r for r in state.records if r["kind"] == "judge"]
    asked = len(h.pipeline.calls)
    h.run(*LIVE, "--limit", "5", "--phase", "judge")
    state = read_state(dirs, the_run_id(h))
    assert len([r for r in state.records if r["kind"] == "judge"]) == 5
    assert len(h.pipeline.calls) == asked


def test_judge_calls_one_uses_a_single_combined_call_per_answer(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "5", "--judge-calls", "1")
    assert h.judge.calls == ["both"] * 5


def test_repeat_runs_the_questions_again_and_reports_the_spread(dirs):
    h = Harness()
    assert h.run(*LIVE, "--limit", "4", "--repeat", "2") == 0
    state = read_state(dirs, the_run_id(h))
    assert sorted({r["run"] for r in state.records if r["kind"] == "answer"}) == [1, 2]
    assert "Spread over repeated runs" in (dirs[1] / f"{the_run_id(h)}.md").read_text()


# ----------------------------------------------------------------------------- resume and stops


class StopsAt(FakePipeline):
    def __init__(self, questions, stop_on_call: int, how: str):
        super().__init__(questions)
        self.stop_on_call, self.how, self.count = stop_on_call, how, 0

    def ask(self, session_id, message):
        self.count += 1
        result = super().ask(session_id, message)
        if self.count == self.stop_on_call:
            if self.how == "fallback":
                result.llm_usage = {"model": "openai/gpt-oss-120b", "fallback_used": True}
            elif self.how == "busy":
                return ChatResult("busy", "error", error="LLMBusyError")
        return result


@pytest.mark.parametrize("how", ["fallback", "busy"])
def test_a_fallback_or_a_long_rate_limit_stops_the_run_with_the_state_saved(dirs, how):
    h = Harness(pipeline=StopsAt(ALL, 4, how))
    assert h.run(*LIVE, "--limit", "8", "--phase", "answer") == 3
    assert "STOPPED" in h.text and "run the same command to continue" in h.text
    answers = [r["qid"] for r in read_state(dirs, the_run_id(h)).records if r["kind"] == "answer"]
    assert answers == ["E01", "E02", "E03"]  # the unit that hit the problem is not recorded


def test_resuming_finishes_only_the_missing_work_and_matches_a_clean_run(
    dirs, tmp_path, monkeypatch
):
    stopped = Harness(pipeline=StopsAt(ALL, 5, "fallback"))
    assert stopped.run(*LIVE, "--limit", "8") == 3
    resumed = Harness()
    assert resumed.run(*LIVE, "--limit", "8") == 0
    asked_after_resume = {m for _, m in resumed.pipeline.calls}
    assert "is there a bus stop close to you?" not in asked_after_resume  # E01 was already done
    rid = the_run_id(resumed)
    state = read_state(dirs, rid)
    answers = [r["qid"] for r in state.records if r["kind"] == "answer"]
    assert answers == [f"E0{n}" for n in range(1, 9)]  # no duplicates, none missing
    # the same run done in one go gives the same rows
    clean_dirs = (tmp_path / "s2", tmp_path / "r2")
    monkeypatch.setattr(run_eval, "STATE_DIR", clean_dirs[0])
    monkeypatch.setattr(run_eval, "RESULTS_DIR", clean_dirs[1])
    clean = Harness()
    assert clean.run(*LIVE, "--limit", "8") == 0
    first = (dirs[1] / f"{rid}.jsonl").read_text().splitlines()
    second = (clean_dirs[1] / f"{rid}.jsonl").read_text().splitlines()

    def essentials(line):  # fake chunk ids and latencies depend on call order, the rest must match
        row = json.loads(line)
        return {k: row[k] for k in ("qid", "run", "status", "answer", "sources", "setup_status")}

    assert [essentials(x) for x in first] == [essentials(x) for x in second]
    summary_a = (dirs[1] / f"{rid}.md").read_text().split("```")[-1]
    summary_b = (clean_dirs[1] / f"{rid}.md").read_text().split("```")[-1]
    assert summary_a == summary_b  # same metrics once the banner is set aside


def test_a_second_complete_run_does_nothing_new(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "4")
    asked = len(h.pipeline.calls)
    judged = len(h.judge.calls)
    h.run(*LIVE, "--limit", "4")
    assert len(h.pipeline.calls) == asked and len(h.judge.calls) == judged


def test_a_half_written_last_line_is_ignored_and_repaired(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "3", "--phase", "answer")
    path = dirs[0] / f"{the_run_id(h)}.jsonl"
    with path.open("a") as handle:
        handle.write('{"kind": "answer", "qid": "E0')  # a crash mid-write
    assert h.run(*LIVE, "--limit", "5", "--phase", "answer") == 0
    answers = [r["qid"] for r in read_state(dirs, the_run_id(h)).records if r["kind"] == "answer"]
    assert answers == ["E01", "E02", "E03", "E04", "E05"]


def test_a_changed_configuration_refuses_to_resume_unless_forced(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "2", "--phase", "answer")
    changed = Harness(settings=make_settings(similarity_threshold=0.55))
    assert changed.run(*LIVE, "--limit", "2", "--phase", "answer") == 2
    assert "different configuration" in changed.text and "similarity_threshold" in changed.text
    forced = Harness(settings=make_settings(similarity_threshold=0.55))
    assert forced.run(*LIVE, "--limit", "3", "--phase", "answer", "--force-config") == 0
    assert "Resuming despite a changed configuration" in forced.text


def test_fresh_sets_the_old_state_aside_instead_of_deleting_it(dirs):
    h = Harness()
    h.run(*LIVE, "--limit", "2", "--phase", "answer")
    h.run(*LIVE, "--limit", "2", "--phase", "answer", "--fresh")
    assert len(list(dirs[0].glob("*.superseded-1.jsonl"))) == 1
    assert len([r for r in read_state(dirs, the_run_id(h)).records if r["kind"] == "answer"]) == 2


def test_the_daily_budget_stops_the_run_and_a_later_day_can_continue(dirs):
    settings = make_settings(groq_tokens_per_day=20_000)  # 18,000 usable: about 6 questions
    h = Harness(settings=settings)
    assert h.run(*LIVE, "--limit", "20", "--phase", "answer") == 3
    assert "daily" in h.text
    saved = [r for r in read_state(dirs, the_run_id(h)).records if r["kind"] == "answer"]
    assert 3 <= len(saved) < 20


# ----------------------------------------------------------------------------- setup turns


class SetupClarifies(FakePipeline):
    def ask(self, session_id, message):
        if message == "what are your opening times during the week?":
            return ChatResult("Which day?", "clarify", llm_usage={"fallback_used": False})
        return super().ask(session_id, message)


def test_a_setup_turn_that_was_not_answered_flags_the_follow_up(dirs):
    h = Harness(pipeline=SetupClarifies(ALL))
    h.run(*LIVE, "--ids", "E25,E26")
    state = read_state(dirs, the_run_id(h))
    rows = {r["qid"]: r for r in state.records if r["kind"] == "answer"}
    assert rows["E25"]["setup_status"] == "clarify" and rows["E25"]["setup_not_answered"] is True
    assert rows["E26"]["setup_status"] == "answered" and rows["E26"]["setup_not_answered"] is False
    summary = (dirs[1] / f"{the_run_id(h)}.md").read_text()
    assert "setup_not_answered" in summary and "['E25']" in summary


# ----------------------------------------------------------------------------- faithfulness basis


class Basis(FakePipeline):
    def __init__(self, questions, mode):
        super().__init__(questions)
        self.mode = mode

    def ask(self, session_id, message):
        result = super().ask(session_id, message)
        if result.status == "answered" and result.retrieved:
            if self.mode == "empty":
                result.used_chunk_ids = []
            elif self.mode == "fallback":
                result.warnings = [
                    "answerable but no valid used_chunks; cited the top-scoring chunk only"
                ]
            elif self.mode == "noretrieved":
                result.retrieved, result.used_chunk_ids = [], []
        return result


class Gated(Basis):
    """Five retrieved chunks; only the first three pass the 0.4 threshold."""

    def ask(self, session_id, message):
        result = super().ask(session_id, message)
        if result.status == "answered":
            sims = [0.8, 0.6, 0.45, 0.39, 0.2]
            result.retrieved = [
                {"chunk_id": 500 + n, "file": "f.md", "page": None, "similarity": sim}
                for n, sim in enumerate(sims)
            ]
            result.used_chunk_ids = [500]  # the model claims only the first
        return result


def test_the_judge_sees_every_chunk_in_the_prompt_not_only_the_ones_the_model_claims(dirs):
    h = Harness(pipeline=Gated(ALL, "normal"))
    h.run(*LIVE, "--ids", "E01")
    state = read_state(dirs, the_run_id(h))
    answer = next(r for r in state.records if r["kind"] == "answer")
    verdict = next(r for r in state.records if r["kind"] == "judge")
    assert answer["faithfulness_basis"] == "context_chunks"
    assert [c["chunk_id"] for c in answer["basis_chunks"]] == [500, 501, 502]  # above 0.4 only
    assert answer["used_chunk_ids"] == [500]  # the model's own claim is unchanged
    assert verdict["faithfulness"]["basis"] == "context_chunks"
    assert verdict["faithfulness"]["chunk_ids"] == [500, 501, 502]


@pytest.mark.parametrize("mode", ["normal", "empty", "fallback"])
def test_the_basis_does_not_depend_on_what_the_model_claims_it_used(dirs, mode):
    h = Harness(pipeline=Basis(ALL, mode))
    h.run(*LIVE, "--ids", "E01")
    state = read_state(dirs, the_run_id(h))
    answer = next(r for r in state.records if r["kind"] == "answer")
    verdict = next(r for r in state.records if r["kind"] == "judge")
    assert answer["faithfulness_basis"] == "context_chunks"
    assert len(answer["basis_chunks"]) == 1 and verdict["faithfulness"]["score"] == 1
    if mode == "empty":
        assert answer["used_chunk_ids"] == []


def test_no_chunk_in_the_prompt_means_no_basis_and_no_faithfulness_score(dirs):
    h = Harness(pipeline=Basis(ALL, "noretrieved"))
    h.run(*LIVE, "--ids", "E01")
    state = read_state(dirs, the_run_id(h))
    answer = next(r for r in state.records if r["kind"] == "answer")
    verdict = next(r for r in state.records if r["kind"] == "judge")
    assert answer["faithfulness_basis"] == "none" and answer["basis_chunks"] == []
    assert verdict["faithfulness"]["score"] is None
    assert verdict["faithfulness"]["error"] == "no chunks to judge against"


def test_the_handcheck_file_shows_the_basis_and_the_judged_chunk_ids(dirs):
    h = Harness(pipeline=Gated(ALL, "normal"))
    h.run(*LIVE, "--ids", "E01")
    text = next(dirs[1].glob("*-handcheck.md")).read_text()
    assert "basis: context_chunks" in text
    assert "chunk ids judged: [500, 501, 502]" in text and "model's claim): [500]" in text


def test_a_state_file_written_under_used_chunks_refuses_to_resume(dirs):
    h = Harness()
    h.run(*LIVE, "--ids", "E01")
    path = next(dirs[0].glob("*.jsonl"))
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    del header["header"]["faithfulness_basis"]  # what a pre-D-080 state file looks like
    path.write_text("\n".join([json.dumps(header)] + lines[1:]) + "\n")
    again = Harness()
    assert again.run(*LIVE, "--ids", "E01") == 2
    assert "REFUSING" in again.text and "faithfulness_basis" in again.text
    assert "used_chunks" in again.text and "new --tag" in again.text


def test_the_dry_run_still_says_fake_data_with_the_new_basis():
    h = Harness()
    assert h.run("--dry-run", "--limit", "3") == 0
    assert "DRY RUN: FAKE DATA, NOT A RESULT" in h.text


# ----------------------------------------------------------------------------- judge failures


class FlakyJudge(FakeJudge):
    def correctness(self, question, expected, answer):
        if "bitcoin" in question:
            raise JudgeError("unusable judge reply twice (not json)")
        return super().correctness(question, expected, answer)

    def faithfulness(self, answer, chunks):
        return Verdict(0, "invented", 1)


def test_a_judge_failure_is_recorded_as_an_error_and_never_as_a_score(dirs):
    h = Harness(judge=FlakyJudge())
    h.run(*LIVE, "--ids", "E17,E18")
    state = read_state(dirs, the_run_id(h))
    verdicts = {r["qid"]: r for r in state.records if r["kind"] == "judge"}
    assert verdicts["E18"]["correctness"]["score"] is None
    assert "unusable judge reply" in verdicts["E18"]["correctness"]["error"]
    assert verdicts["E17"]["correctness"]["score"] == 1
    assert verdicts["E18"]["faithfulness"]["score"] == 0  # the other verdict still counts
    summary = (dirs[1] / f"{the_run_id(h)}.md").read_text()
    assert "correctness ['E18']" in summary and "| Answer correctness | 100.0% | 1 |" in summary


# ----------------------------------------------------------------------------- state file


def test_the_state_file_checks_its_header_and_rejects_damage(tmp_path):
    path = tmp_path / "s.jsonl"
    state = State(path)
    state.start({"answer_model": "a"})
    state.append({"kind": "answer", "qid": "E01", "run": 1})
    again = State(path)
    assert again.header == {"answer_model": "a"} and again.done("answer") == {("E01", 1)}
    assert again.check_config({"answer_model": "a"}) == []
    with pytest.raises(ConfigMismatch):
        again.check_config({"answer_model": "b"})
    assert again.check_config({"answer_model": "b"}, force=True)
    path.write_text('{"header": {}}\nnot json\n{"kind": "x"}\n')
    with pytest.raises(StateError, match="line 2"):
        State(path)


def test_appending_before_the_header_is_an_error(tmp_path):
    with pytest.raises(StateError):
        State(tmp_path / "s.jsonl").append({"kind": "answer"})


def test_daily_usage_is_restored_from_the_state_for_today_only(tmp_path):
    state = State(tmp_path / "s.jsonl")
    state.start({})
    state.append(
        {"kind": "usage", "date": "2026-01-01", "model": "m", "tokens": 100, "requests": 1}
    )
    state.append({"kind": "usage", "date": "2026-01-02", "model": "m", "tokens": 50, "requests": 1})
    state.append({"kind": "usage", "date": "2026-01-02", "model": "m", "tokens": 25, "requests": 1})
    assert state.daily_used("2026-01-02") == {"m": (75, 2)} and state.daily_used("x") == {}


# ----------------------------------------------------------------------------- misc


def test_the_plan_estimate_and_helpers():
    cfg = run_eval.config_snapshot(make_settings(), run_eval.DEFAULT_QUESTIONS, 2)
    text = run_eval.estimate_plan(cfg, ALL, 1, 2)
    # Changed deliberately (INSTRUCTIONS_16): 29 answerable questions now, so 58 judge calls.
    assert "44 chat calls" in text and "58 judge calls" in text
    assert run_eval.slug("openai/gpt-oss-20b") == "openai_gpt-oss-20b"
    assert run_eval.run_id_for(cfg, "t1").endswith("__t1")
    assert run_eval.missing_keys(make_settings()) == []
    assert run_eval.missing_keys(make_settings(groq_api_key=None)) == ["GROQ_API_KEY"]
