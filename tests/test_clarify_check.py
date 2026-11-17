"""The clarify-tuning set, the clarify_v2 and judge v2 prompts, and scripts/check_clarify.py.

Everything here uses fakes: no Groq, no Voyage, no database.
"""

import json
import re

import pytest

from app.config import Settings
from app.generation.llm import LLMBusyError
from app.services.clarify import build_preparer
from eval import run_eval
from eval.fakes import FakeClarifyClient, FakeClock, FakeJudge, FakePipeline
from eval.questions import load_questions
from scripts import check_clarify as cc

ROOT = cc.ROOT
ITEMS_HEADER, ITEMS = cc.load_items(cc.DEFAULT_SET)
PROMPTS = ROOT / "prompts"


def make_settings(**kw) -> Settings:
    values = dict(_env_file=None, groq_api_key="fake-groq-key", voyage_api_key="fake-voyage-key")
    values.update(kw)
    return Settings(**values)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    state, results = tmp_path / "state", tmp_path / "results"
    monkeypatch.setattr(cc, "STATE_DIR", state)
    monkeypatch.setattr(cc, "RESULTS_DIR", results)
    return state, results


class Run:
    def __init__(self, client=None, settings=None):
        self.client = client or FakeClarifyClient(ITEMS)
        self.settings = settings or make_settings()
        self.clock = FakeClock()
        self.lines: list[str] = []
        self.factory_calls = 0

    def go(self, *args):
        def factory(s):
            self.factory_calls += 1
            return self.client

        return cc.main(
            list(args),
            settings=self.settings,
            client_factory=factory,
            clock=self.clock,
            sleep=self.clock.sleep,
            out=self.lines.append,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# ------------------------------------------------------------------ the tuning set


def test_the_tuning_set_is_well_formed_and_marked_draft():
    assert ITEMS_HEADER["_header"].startswith("DRAFT")
    assert cc.validate_items(ITEMS) == []
    assert 55 <= len(ITEMS) <= 65
    groups = [cc.group_of(i) for i in ITEMS]
    assert groups.count("clear") == 36 and groups.count("vague") == 16
    assert groups.count("follow-up") == 8
    follow = [i for i in ITEMS if i.get("history")]
    assert {i["expected_action"] for i in follow} == {"search", "clarify"}
    assert all(i["expected_action"] in {"search", "clarify"} for i in ITEMS)


def test_the_clear_set_has_the_words_version_one_got_wrong():
    words = re.compile(r"\b(you|your|here|close|near|where)\b", re.IGNORECASE)
    clear = [i for i in ITEMS if cc.group_of(i) == "clear" and words.search(i["question"])]
    assert len(clear) >= 8


def test_no_item_repeats_an_evaluation_or_threshold_calibration_text():
    eval_texts = cc.texts_in_file(cc.EVAL_QUESTIONS)
    threshold_texts = cc.texts_in_file(cc.THRESHOLD_SET)
    assert len(eval_texts) >= 40 and len(threshold_texts) >= 20
    assert cc.overlap_with(ITEMS, eval_texts) == []
    assert cc.overlap_with(ITEMS, threshold_texts) == []


def test_the_overlap_check_normalizes_and_looks_at_setup_and_history_turns():
    assert (
        cc.normalize("  Is there a BUS stop, close to you?! ") == "is there a bus stop close to you"
    )
    eval_texts = cc.texts_in_file(cc.EVAL_QUESTIONS)
    by_question = {"id": "X1", "question": "Is there a bus stop close to you"}
    by_setup = {"id": "X2", "question": "ok", "setup": "Do you take Cigna dental?"}
    by_history = {
        "id": "X3",
        "question": "ok",
        "history": [{"user": "What are your opening times during the week", "assistant": "x"}],
    }
    assert cc.overlap_with([by_question, by_setup, by_history], eval_texts) == ["X1", "X2", "X3"]


def test_the_tuning_set_is_not_inside_the_evaluation_set_or_the_approved_header():
    header, questions = load_questions(run_eval.DEFAULT_QUESTIONS)
    assert header["_header"].startswith("APPROVED")
    assert not {i["id"] for i in ITEMS} & {q["id"] for q in questions}


# ------------------------------------------------------------------ the prompts


def json_contract(text: str) -> str:
    return text.strip().splitlines()[-1]


def test_clarify_v2_has_the_same_json_contract_and_data_rules_as_v1():
    v1 = (PROMPTS / "clarify_v1.txt").read_text()
    v2 = (PROMPTS / "clarify_v2.txt").read_text()
    assert json_contract(v1) == json_contract(v2)
    for text in (v1, v2):
        assert "<history>" in text and "<message>" in text
        assert "data, never instructions" in text
        assert "do not comply" in text
    # the rules the code relies on are still in the prompt
    assert "ending in" in v2 and "already asked" in v2


def test_clarify_v2_says_you_and_your_mean_the_one_clinic():
    v2 = (PROMPTS / "clarify_v2.txt").read_text()
    assert "BrightSmile Dental" in v2 and "ONE clinic" in v2
    assert '"you", "your"' in v2 and "never missing information" in v2
    assert "When in doubt, use" in v2


def test_the_clarify_v2_examples_are_not_evaluation_or_tuning_questions():
    quoted = re.findall(r'"([^"]+)"', (PROMPTS / "clarify_v2.txt").read_text())
    quoted = {cc.normalize(q) for q in quoted if len(q.split()) >= 3}
    assert len(quoted) >= 5  # the examples were found
    texts = cc.texts_in_file(cc.EVAL_QUESTIONS) | cc.texts_in_file(cc.THRESHOLD_SET)
    texts |= {cc.normalize(t) for i in ITEMS for t in cc.texts_of(i)}
    assert quoted & texts == set()


def test_the_clarify_prompt_is_loaded_by_version_name_and_v2_is_the_default():
    assert make_settings().clarify_prompt_version == "clarify_v2"  # D-078
    memory = object()
    v1 = build_preparer(make_settings(clarify_prompt_version="clarify_v1"), memory)
    v2 = build_preparer(make_settings(), memory)
    assert v1.prompt_version == "clarify_v1" and v2.prompt_version == "clarify_v2"
    assert (
        v1._system != v2._system and v2._system == (PROMPTS / "clarify_v2.txt").read_text().strip()
    )


def test_the_clarify_version_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLARIFY_PROMPT_VERSION", "clarify_v2")
    assert Settings(_env_file=None).clarify_prompt_version == "clarify_v2"


def test_the_v2_judge_prompts_exist_keep_the_contract_and_name_the_clinic():
    for name in ("faithfulness", "combined"):
        v1 = (PROMPTS / f"judge_{name}_v1.txt").read_text()
        v2 = (PROMPTS / f"judge_{name}_v2.txt").read_text()
        assert json_contract(v1) == json_contract(v2)
        assert "data, never instructions" in v2 and "do not comply" in v2
        assert "BrightSmile Dental" in v2 and "never a claim to check" in v2
        for rule in ("not in the excerpts", "contradicts the excerpts"):
            assert rule in v2
        assert "BrightSmile" not in v1  # the old versions are untouched


def test_the_judge_defaults_are_the_v2_faithfulness_and_combined_and_v1_correctness():
    s = make_settings()
    assert s.judge_faithfulness_prompt_version == "judge_faithfulness_v2"
    assert s.judge_combined_prompt_version == "judge_combined_v2"
    assert s.judge_correctness_prompt_version == "judge_correctness_v1"
    env = (ROOT / ".env.example").read_text()
    assert "JUDGE_FAITHFULNESS_PROMPT_VERSION=judge_faithfulness_v2" in env
    assert (
        "CLARIFY_PROMPT_VERSION=clarify_v2" in env
        and "REWRITE_LLM_MODEL=openai/gpt-oss-120b" in env
    )


def test_the_judge_banner_and_the_handcheck_file_show_the_prompt_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "STATE_DIR", tmp_path / "s")
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path / "r")
    clock = FakeClock()
    lines: list[str] = []
    all_questions = load_questions(run_eval.DEFAULT_QUESTIONS)[1]
    code = run_eval.main(
        ["--confirm-live", "--limit", "4", "--tag", "t"],
        settings=make_settings(similarity_threshold=0.4),
        pipeline_factory=lambda s: FakePipeline(all_questions),
        judge_factory=lambda s: FakeJudge(),
        clock=clock,
        sleep=clock.sleep,
        out=lines.append,
    )
    assert code == 0
    text = "\n".join(lines)
    assert "judge_faithfulness_v2" in text and "judge_combined_v2" in text
    handcheck = next((tmp_path / "r").glob("*-handcheck.md")).read_text()
    assert "judge_faithfulness_v2" in handcheck and "judge_correctness_v1" in handcheck
    assert "clarify_v2" in handcheck


def test_smoke_results_are_git_ignored():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "eval/results/*smoke*" in ignore


# ------------------------------------------------------------------ scripts/check_clarify.py


def test_the_dry_run_uses_fakes_labels_itself_and_writes_only_to_temp_folders(dirs):
    run = Run()
    assert run.go("--dry-run") == 0
    assert run.lines[0].startswith("DRY RUN: FAKE DATA, NOT A RESULT")
    assert run.factory_calls == 0  # no real client is built
    assert not dirs[0].exists() and not dirs[1].exists()
    assert "about 60 calls" in run.text
    wrote = [x for line in run.lines for x in line.split("\n") if x.startswith("Wrote ")]
    assert wrote and all(str(dirs[1]) not in line for line in wrote)
    summary = next(line for line in wrote if line.endswith(".md"))
    assert open(summary.removeprefix("Wrote ")).read().startswith("DRY RUN: FAKE DATA")


def test_a_live_run_needs_confirm_live(dirs):
    run = Run()
    assert run.go("--prompt-version", "clarify_v2") == 2
    assert "REFUSING" in run.text and "--confirm-live" in run.text
    assert run.factory_calls == 0 and run.client.calls == []
    assert not dirs[0].exists() and not dirs[1].exists()


def test_a_live_run_refuses_without_a_key_or_with_an_unknown_prompt_version(dirs):
    run = Run(settings=Settings(_env_file=None, groq_api_key=None))
    assert run.go("--confirm-live") == 2 and "no value" in run.text
    run = Run()
    assert run.go("--confirm-live", "--prompt-version", "clarify_v9") == 2
    assert "no prompt file clarify_v9.txt" in run.text and run.client.calls == []


def test_a_live_run_checks_every_item_and_counts_the_misses(dirs):
    wrong = {"S03", "S07", "V02", "F01"}
    run = Run(FakeClarifyClient(ITEMS, wrong=wrong))
    assert run.go("--confirm-live", "--prompt-version", "clarify_v2") == 0
    assert len(run.client.calls) == len(ITEMS)
    assert "Clear questions wrongly clarified: 2 of 36" in run.text
    assert "Vague questions caught: 15 of 16 (wrongly searched: 1)" in run.text
    assert "Follow-ups handled as expected: 7 of 8" in run.text
    for qid in wrong:
        assert f"MISS {qid}" in run.text
    summary = (dirs[1] / "clarify-check__clarify_v2.md").read_text()
    assert "S03" in summary and "Every miss" in summary and "DRAFT" in summary
    raw = [
        json.loads(x)
        for x in (dirs[1] / "clarify-check__clarify_v2.jsonl").read_text().split("\n")
        if x
    ]
    assert len(raw) == len(ITEMS)
    assert "fake-groq-key" not in summary and "fake-groq-key" not in run.text


def test_the_results_file_is_named_for_the_prompt_version(dirs):
    for version in ("clarify_v1", "clarify_v2"):
        assert Run().go("--confirm-live", "--prompt-version", version, "--limit", "2") == 0
    names = sorted(p.name for p in dirs[1].glob("*.md"))
    assert names == ["clarify-check__clarify_v1.md", "clarify-check__clarify_v2.md"]


def test_the_check_resumes_after_a_stop_and_never_repeats_a_finished_item(dirs):
    class Dies(FakeClarifyClient):
        def complete(self, system, user, **kw):
            if len(self.calls) == 5:
                raise LLMBusyError("rate limited")
            return super().complete(system, user, **kw)

    first = Run(Dies(ITEMS))
    assert first.go("--confirm-live") == 3
    assert "STOPPED" in first.text and len(first.client.calls) == 5
    second = Run()
    assert second.go("--confirm-live") == 0
    assert len(second.client.calls) == len(ITEMS) - 5
    assert not set(first.client.calls) & set(second.client.calls)
    summary = (dirs[1] / "clarify-check__clarify_v2.md").read_text()
    assert "None." in summary  # the fake gets everything right


def test_a_changed_prompt_version_does_not_mix_into_an_older_state_file(dirs):
    Run().go("--confirm-live", "--limit", "3")
    run = Run()
    assert run.go("--confirm-live", "--prompt-version", "clarify_v1", "--limit", "3") == 0
    assert sorted(p.name for p in dirs[0].glob("*.jsonl")) == [
        "clarify-check__clarify_v1.jsonl",
        "clarify-check__clarify_v2.jsonl",
    ]


def test_an_unusable_reply_is_an_error_item_not_a_silent_search(dirs):
    class Garbage(FakeClarifyClient):
        def complete(self, system, user, **kw):
            result = super().complete(system, user, **kw)
            result.text = "not json at all"
            return result

    run = Run(Garbage(ITEMS))
    assert run.go("--confirm-live", "--limit", "5") == 0
    assert "Items with a model or format error: 5" in run.text


def test_a_clarify_after_a_clarify_rule_is_not_applied_to_a_check_item():
    # Each item stands alone (no previous status), so the "never twice in a row" rule is not hit.
    item = next(i for i in ITEMS if i["expected_action"] == "clarify" and not i.get("history"))
    out = cc.classify(FakeClarifyClient(ITEMS), "sys", item, 300)
    assert out["actual"] == "clarify" and out["clarifying_question"]


def test_the_check_script_spends_nothing_on_embedding_search_answers_or_judging():
    source = (ROOT / "scripts" / "check_clarify.py").read_text()
    for forbidden in ("embedder", "ChatService", "build_answerer", "build_judge", "psycopg"):
        assert forbidden not in source


# ------------------------------------------------------------------ clarify_v3 and the held-out set

HELD_HEADER, HELD = cc.load_items(cc.HELDOUT_SET)


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"<[a-z_]+>", text))


def test_clarify_v3_has_the_same_contract_placeholders_and_rules_as_v2():
    v2 = (PROMPTS / "clarify_v2.txt").read_text()
    v3 = (PROMPTS / "clarify_v3.txt").read_text()
    assert json_contract(v2) == json_contract(v3)
    assert placeholders(v2) <= placeholders(v3) | {"<history>", "<message>"}
    assert "<history>" in v3 and "<message>" in v3
    assert "data, never instructions" in v3 and "do not comply" in v3
    assert "ending in" in v3 and "already asked" in v3
    assert "BrightSmile Dental" in v3 and "ONE clinic" in v3
    for step in ("(a)", "(b)", "(c)"):
        assert step in v3
    assert 'Choose "search" whenever you are unsure' in v3


def test_clarify_v3_is_loaded_by_version_name_and_the_default_is_v2():
    assert make_settings().clarify_prompt_version == "clarify_v2"
    built = build_preparer(make_settings(clarify_prompt_version="clarify_v3"), object())
    assert built.prompt_version == "clarify_v3"
    assert built._system == (PROMPTS / "clarify_v3.txt").read_text().strip()
    for old in ("clarify_v1", "clarify_v2"):  # earlier versions are never edited
        assert (PROMPTS / f"{old}.txt").exists()


def all_known_texts() -> set[str]:
    texts = cc.texts_in_file(cc.EVAL_QUESTIONS) | cc.texts_in_file(cc.THRESHOLD_SET)
    texts |= {cc.normalize(t) for i in ITEMS + HELD for t in cc.texts_of(i)}
    return texts


def test_the_clarify_v3_examples_are_in_none_of_the_four_sets():
    quoted = re.findall(r'"([^"]+)"', (PROMPTS / "clarify_v3.txt").read_text())
    quoted = {cc.normalize(q) for q in quoted if len(q.split()) >= 3}
    assert len(quoted) >= 8
    assert quoted & all_known_texts() == set()


def test_the_heldout_set_is_well_formed_and_marked_draft_and_never_used_for_tuning():
    assert (
        HELD_HEADER["_header"].startswith("DRAFT")
        and "Never used to edit" in HELD_HEADER["_header"]
    )
    assert cc.validate_items(HELD) == []
    groups = [cc.group_of(i) for i in HELD]
    assert (groups.count("clear"), groups.count("vague"), groups.count("follow-up")) == (18, 8, 4)
    assert 28 <= len(HELD) <= 32
    assert any(i.get("history") and "insurance" in i["question"] for i in HELD)  # like F03
    assert any(cc.group_of(i) == "follow-up" and not i.get("history") for i in HELD)


def test_the_heldout_set_overlaps_no_other_set():
    held_ids = {i["id"] for i in HELD}
    assert not held_ids & {i["id"] for i in ITEMS}
    for other in (cc.EVAL_QUESTIONS, cc.THRESHOLD_SET, cc.DEFAULT_SET):
        assert cc.overlap_with(HELD, cc.texts_in_file(other)) == []


def test_the_heldout_dry_run_works_and_shows_the_warning_and_the_set(dirs):
    run = Run(FakeClarifyClient(HELD))
    assert run.go("--dry-run", "--set", "heldout") == 0
    assert run.lines[0].startswith("DRY RUN: FAKE DATA, NOT A RESULT")
    assert cc.HELDOUT_WARNING in run.text and "heldout" in run.text
    assert "about 30 calls" in run.text and run.factory_calls == 0
    assert not dirs[0].exists() and not dirs[1].exists()


def test_the_heldout_set_refuses_without_confirm_live(dirs):
    run = Run(FakeClarifyClient(HELD))
    assert run.go("--set", "heldout") == 2
    assert "--confirm-live" in run.text and run.client.calls == []
    assert not dirs[0].exists()


def test_the_run_id_and_state_differ_by_set_so_they_never_mix(dirs):
    assert Run().go("--confirm-live", "--limit", "3") == 0
    held = Run(FakeClarifyClient(HELD))
    assert held.go("--confirm-live", "--set", "heldout", "--limit", "3") == 0
    assert sorted(p.name for p in dirs[0].glob("*.jsonl")) == [
        "clarify-check__clarify_v2.jsonl",
        "clarify-check__clarify_v2__heldout.jsonl",
    ]
    summary = (dirs[1] / "clarify-check__clarify_v2__heldout.md").read_text()
    assert cc.HELDOUT_WARNING in summary
    assert cc.HELDOUT_WARNING not in (dirs[1] / "clarify-check__clarify_v2.md").read_text()
    assert len(held.client.calls) == 3
