"""The owner-run verification scripts V1 (check_rewrite_retrieval) and V4 (check_answerer).

Fakes only: no Groq, no Voyage, no database. Neither script may touch the project's results or
state folders in these tests.
"""

import json

import pytest

from app.config import Settings
from app.retrieval.search import Hit
from eval.fakes import FakeClarifyClient, FakeClock, FakeGroundedAnswerer, FakeQueryEmbedder
from scripts import check_answerer as v4
from scripts import check_clarify as cc
from scripts import check_rewrite_retrieval as v1
from scripts.calibrate_threshold import DEFAULT_QUESTIONS, load_questions


def make_settings(**kw) -> Settings:
    values = dict(_env_file=None, groq_api_key="fake-groq-key", voyage_api_key="fake-voyage-key")
    values.update(kw)
    return Settings(**values)


def hit(file, similarity, n=1):
    return Hit(n, "d", file, None, None, "text", similarity)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    for module in (v1, v4):
        monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(module, "RESULTS_DIR", tmp_path / "results")
    return tmp_path / "state", tmp_path / "results"


PRACTICE = load_questions(DEFAULT_QUESTIONS)
ANSWERABLE = [q for q in PRACTICE if q["category"] == "answerable"]


# ----------------------------------------------------------------------------- V1: pure parts


def row(expected, orig, rewrite, qid="A01", category="answerable"):
    q = {"id": qid, "category": category, "expected_sources": expected}
    return v1.compare_row(q, {"actual": "search", "query": "rw"}, orig, rewrite)


def test_compare_row_gives_the_rank_of_every_expected_source_and_whether_all_are_kept():
    r = row(
        ["a.md", "b.md"], [hit("x.md", 0.6), hit("a.md", 0.5), hit("b.md", 0.4)], [hit("a.md", 0.5)]
    )
    assert r["orig_ranks"] == {"a.md": 2, "b.md": 3} and r["orig_keeps_all"] is True
    assert r["rewrite_ranks"] == {"a.md": 1, "b.md": None} and r["rewrite_keeps_all"] is False
    assert r["orig_top1"] == 0.6 and r["orig_best"] == 0.5


def test_the_rule_passes_only_when_the_original_keeps_every_answerable():
    good = [row(["a.md"], [hit("a.md", 0.5)], [hit("z.md", 0.5)], qid=f"A{i}") for i in range(3)]
    assert v1.rule_result(good)["pass"] is True
    bad = good + [row(["a.md"], [hit("z.md", 0.5)], [hit("a.md", 0.5)], qid="A9")]
    result = v1.rule_result(bad)
    assert result["pass"] is False and result["lost_by_original"] == ["A9"]
    assert result["rewrite_keeps_all"] == 1


def test_out_of_scope_rows_do_not_count_towards_the_rule():
    rows = [
        row(["a.md"], [hit("a.md", 0.5)], [], qid="A1"),
        row([], [hit("q.md", 0.3)], [], "O1", "out_of_scope"),
    ]
    assert v1.rule_result(rows)["answerable"] == 1 and v1.rule_result(rows)["pass"] is True


def test_the_report_states_the_rule_and_the_result():
    text = v1.report_markdown(
        "BANNER", [row(["a.md"], [hit("z.md", 0.5)], [hit("a.md", 0.5)])], False
    )
    assert (
        "RULE:" in text
        and "RESULT: FAIL" in text
        and "Keep SEARCH_ORIGINAL_WHEN_NO_HISTORY=false" in text
    )
    assert "DRY RUN" not in text


# ----------------------------------------------------------------------------- V1: the run


class FileSearcher:
    """A fake search: returns the expected file of the question first when `good` is true."""

    def __init__(self, by_vector, good=True):
        self.by_vector = by_vector
        self.good = good

    def __call__(self, vector):
        files = self.by_vector.get(tuple(vector), [])
        return [hit(f, 0.7 - 0.05 * n, n + 1) for n, f in enumerate(files)]


def live_v1(dirs, good=True, **flags):
    questions = ANSWERABLE
    items = [{**q, "expected_action": "search"} for q in PRACTICE]
    client = FakeClarifyClient(items)
    embedder = FakeQueryEmbedder()
    lines: list[str] = []
    clock = FakeClock()

    def search_factory(settings):
        class Searcher:
            def __call__(self, vector):
                # every question's expected files come back first only for ORIGINAL texts when good
                return [hit("none.md", 0.5)]

        return Searcher(), (lambda: None)

    code = v1.main(
        ["--confirm-live", *flags.get("args", [])],
        settings=make_settings(),
        client_factory=lambda s: client,
        embedder_factory=lambda s: embedder,
        search_factory=search_factory,
        clock=clock,
        sleep=clock.sleep,
        out=lines.append,
    )
    return code, "\n".join(lines), embedder, client, questions


def test_v1_dry_run_prints_the_label_and_touches_no_project_folder(dirs, capsys):
    lines: list[str] = []
    code = v1.main(["--dry-run"], settings=make_settings(), out=lines.append)
    assert code == 0 and "DRY RUN: FAKE DATA" in "\n".join(lines)
    assert not dirs[0].exists() and not dirs[1].exists()


def test_v1_refuses_a_live_run_without_confirm_live(dirs):
    lines: list[str] = []
    assert v1.main([], settings=make_settings(), out=lines.append) == 2
    assert "--confirm-live" in "\n".join(lines) and not dirs[0].exists()


def test_v1_live_run_embeds_originals_and_rewrites_in_one_request_and_writes_results(dirs):
    code, text, embedder, client, _ = live_v1(dirs)
    assert code == 0
    assert embedder.requests == 1 and len(embedder.texts) == 2 * len(PRACTICE)
    assert len(client.calls) == len(PRACTICE)
    assert (dirs[1] / "rewrite-retrieval__clarify_v2.md").exists()
    assert "RESULT:" in text and "RULE:" in text
    # the clarify calls are saved, so a second run makes no new Groq call
    state = [
        json.loads(line)
        for line in (dirs[0] / "rewrite-retrieval__clarify_v2.jsonl").read_text().splitlines()
    ]
    assert sum(1 for r in state if r.get("kind") == "clarify") == len(PRACTICE)


def test_v1_resumes_without_repeating_clarify_calls(dirs):
    code, _, _, client, _ = live_v1(dirs)
    first = len(client.calls)
    lines: list[str] = []
    again = FakeClarifyClient([{**q, "expected_action": "search"} for q in PRACTICE])
    v1.main(
        ["--confirm-live"],
        settings=make_settings(),
        client_factory=lambda s: again,
        embedder_factory=lambda s: FakeQueryEmbedder(),
        search_factory=lambda s: ((lambda v: [hit("x.md", 0.5)]), (lambda: None)),
        clock=FakeClock(),
        sleep=lambda s: None,
        out=lines.append,
    )
    assert first == len(PRACTICE) and again.calls == []


# ----------------------------------------------------------------------------- V4


def entity_items():
    return load_questions(v4.ENTITY_QUESTIONS)


def test_the_entity_items_are_six_draft_out_of_scope_items_that_overlap_nothing():
    items = entity_items()
    assert len(items) == 6 and all(i["category"] == "out_of_scope" for i in items)
    assert all(i["kind"] == "entity_mismatch" and i["status"] == "DRAFT" for i in items)
    assert all("Run A" in i["note"] for i in items)
    for other in (
        cc.EVAL_QUESTIONS,
        cc.THRESHOLD_SET,
        cc.DEFAULT_SET,
        cc.HELDOUT_SET,
        cc.EXTRA_SET,
    ):
        texts = cc.texts_in_file(other)
        assert not [i["id"] for i in items if cc.normalize(i["question"]) in texts]
    eval_words = set()
    for text in cc.texts_in_file(cc.EVAL_QUESTIONS):
        eval_words |= set(text.split())
    # the invented names must not be words that appear in any evaluation question
    for name in ("northgate", "sunrise", "nakamura", "oakline"):
        assert name not in eval_words


def test_select_questions_takes_near_miss_injection_and_entity_items():
    chosen = v4.select_questions(PRACTICE, entity_items(), include_answerable=False)
    kinds = {q.get("kind") for q in chosen}
    assert kinds == {"near_miss", "injection", "entity_mismatch"} and len(chosen) == 12
    with_answerable = v4.select_questions(PRACTICE, entity_items(), include_answerable=True)
    assert len(with_answerable) == 12 + len(ANSWERABLE)


def v4_run(dirs, answerer, args=()):
    lines: list[str] = []
    clock = FakeClock()
    embedder = FakeQueryEmbedder()
    code = v4.main(
        ["--confirm-live", "--gate", "0.2", *args],
        settings=make_settings(),
        answerer_factory=lambda s: answerer,
        embedder_factory=lambda s: embedder,
        search_factory=lambda s: ((lambda v: [hit("faq.md", 0.55)]), (lambda: None)),
        clock=clock,
        sleep=clock.sleep,
        out=lines.append,
    )
    return code, "\n".join(lines), embedder


def test_v4_p4_is_triggered_when_an_entity_item_is_answered(dirs):
    code, text, embedder = v4_run(dirs, FakeGroundedAnswerer(answer_when=("Northgate",)))
    assert code == 0 and embedder.requests == 1
    assert "RESULT P4: TRIGGERED" in text and "N01" in text


def test_v4_p4_is_not_triggered_when_every_entity_item_is_refused(dirs):
    code, text, _ = v4_run(dirs, FakeGroundedAnswerer())
    assert code == 0 and "RESULT P4: NOT triggered" in text and "Do not change the prompt" in text


def test_v4_gate_tripwire_needs_more_than_one_extra_question_answered():
    rows = [
        {"id": "O1", "category": "out_of_scope", "outcome": "answered", "top1": 0.36},
        {"id": "O2", "category": "out_of_scope", "outcome": "answered", "top1": 0.37},
        {"id": "O3", "category": "out_of_scope", "outcome": "answered", "top1": 0.55},
    ]
    assert v4.summarize(rows, 0.33)["extra_answered"] == ["O1", "O2"]
    assert "TRIPPED" in v4.report_markdown("B", rows, 0.33, False).split("RESULT gate:")[1]
    one = rows[:1] + rows[2:]
    assert "not tripped" in v4.report_markdown("B", one, 0.33, False).split("RESULT gate:")[1]


def test_v4_marks_an_unanswered_answerable_and_an_answered_out_of_scope_as_wrong():
    assert v4.is_wrong({"category": "answerable", "outcome": "model"})
    assert not v4.is_wrong({"category": "answerable", "outcome": "answered"})
    assert v4.is_wrong({"category": "out_of_scope", "outcome": "answered"})
    assert not v4.is_wrong({"category": "out_of_scope", "outcome": "gate"})


def test_v4_refuses_without_confirm_live_and_its_dry_run_touches_no_project_folder(dirs):
    lines: list[str] = []
    settings = make_settings(similarity_threshold=0.4)
    assert v4.main([], settings=settings, out=lines.append) == 2
    assert "--confirm-live" in "\n".join(lines)
    lines.clear()
    assert v4.main(["--dry-run"], settings=settings, out=lines.append) == 0
    assert "DRY RUN: FAKE DATA" in "\n".join(lines) and not dirs[0].exists()


def test_v4_runs_the_answer_model_without_a_fallback_and_with_the_chosen_gate_and_prompt(dirs):
    lines: list[str] = []
    seen = {}

    def factory(s):
        seen["fallback"] = s.answer_fallback_enabled
        seen["gate"] = s.similarity_threshold
        seen["prompt"] = s.answer_prompt_version
        seen["model"] = s.answer_llm_model
        return FakeGroundedAnswerer()

    clock = FakeClock()
    v4.main(
        [
            "--confirm-live",
            "--gate",
            "0.33",
            "--model",
            "openai/gpt-oss-20b",
            "--prompt",
            "answer_v2",
        ],
        settings=make_settings(),
        answerer_factory=factory,
        embedder_factory=lambda s: FakeQueryEmbedder(),
        search_factory=lambda s: ((lambda v: [hit("faq.md", 0.55)]), (lambda: None)),
        clock=clock,
        sleep=clock.sleep,
        out=lines.append,
    )
    assert seen == {
        "fallback": False,
        "gate": 0.33,
        "prompt": "answer_v2",
        "model": "openai/gpt-oss-20b",
    }
