"""P1 (answer question and the search-original flag) and P2 (the clarify guard), INSTRUCTIONS_27.

The guard tests replay RECORDED practice outputs of the clarify step (tests/fixtures/
clarify_practice_outputs.jsonl, copied from eval/results/clarify-check__*). No evaluation question
text is used. No Groq, no Voyage, no database.
"""

import json
from pathlib import Path

import httpx

from app.config import Settings
from app.generation.llm import LLMClient
from app.memory import SessionState, Turn
from app.services.chat import ChatService
from app.services.clarify import ClarifyingPreparer, decide, named_terms
from tests.fakes import FakeAnswerer
from tests.test_clarify import FakeMemory, reply
from tests.test_llm_client import Server, ok_body, resp, role

FIXTURE = Path(__file__).parent / "fixtures" / "clarify_practice_outputs.jsonl"
ROWS = [json.loads(line) for line in FIXTURE.read_text().splitlines()[1:]]


def turns_of(row) -> list[Turn]:
    return [Turn(t["user"], t["assistant"], "answered") for t in row["history"]]


def final_action(row, guard: bool) -> str:
    """What the app decides from the recorded model reply."""
    if row["model_action"] != "clarify":
        return row["model_action"]
    data = {
        "action": "clarify",
        "query": row["query"],
        "clarifying_question": row["clarifying_question"],
    }
    return decide(data, None, SessionState(turns=turns_of(row)), row["question"], guard).action


def count(rows, guard):
    out = {}
    for name in ("tuning", "heldout"):
        sel = [r for r in rows if r["set"] == name]
        clear = [r for r in sel if r["group"] == "clear"]
        vague = [r for r in sel if r["group"] == "vague"]
        follow = [r for r in sel if r["group"] == "follow-up"]
        out[name] = {
            "clear_wrong": sum(final_action(r, guard) == "clarify" for r in clear),
            "vague_caught": sum(final_action(r, guard) == "clarify" for r in vague),
            "vague": len(vague),
            "follow_right": sum(final_action(r, guard) == r["expected_action"] for r in follow),
            "follow": len(follow),
        }
    return out


# ----------------------------------------------------------------------------- the pure function


def test_a_clarify_about_a_term_the_message_already_names_is_ignored():
    assert named_terms(
        "Which type of specialist are you asking about?", "refer me to a specialist", []
    ) == ["specialist"]


def test_plural_and_possessive_forms_count_as_the_same_term():
    assert named_terms("Which dentist are you referring to?", "I switched dentists", []) == [
        "dentist"
    ]
    assert named_terms("Which dentist's schedule?", "I like the dentist schedules", []) == [
        "dentist",
        "schedule",
    ]


def test_a_clarify_about_something_missing_is_kept():
    assert (
        named_terms("Which treatment are you asking about?", "how long does it take?", []) is None
    )
    # one of two asked terms is missing: keep the clarify
    assert (
        named_terms("Which dentist or staff member?", "who looks after braces, a dentist?", [])
        is None
    )


def test_terms_named_only_in_the_history_count_as_supplied():
    history = [Turn("tell me about sedation", "Nitrous oxide or oral sedation.", "answered")]
    question = "Which sedation are you asking about?"
    assert named_terms(question, "which one is better for a child", history) == ["sedation"]
    assert named_terms(question, "which one is better for a child", []) is None


def test_a_clarify_with_no_content_term_is_left_alone():
    assert named_terms("Which one are you asking about?", "can I book it", []) is None


# ----------------------------------------------------------------------------- decide()


def clarify_data(question, query="q"):
    return {"action": "clarify", "query": query, "clarifying_question": question}


def test_decide_turns_a_guarded_clarify_into_a_search_and_says_which_terms():
    d = decide(
        clarify_data("Which dentist are you referring to?"),
        None,
        SessionState(),
        "my dentists moved away, can you transfer my x-rays?",
    )
    assert d.action == "search" and d.clarifying_question is None
    assert d.guard_override == ["dentist"] and any("guard" in n for n in d.notes)


def test_decide_with_the_guard_off_keeps_the_clarify():
    d = decide(
        clarify_data("Which dentist are you referring to?"),
        None,
        SessionState(),
        "my dentists moved away",
        guard_enabled=False,
    )
    assert d.action == "clarify" and d.guard_override is None


def test_the_guard_never_touches_a_plain_search():
    d = decide(
        {"action": "search", "query": "parking", "clarifying_question": ""},
        None,
        SessionState(),
        "parking?",
    )
    assert d.action == "search" and d.guard_override is None


# ------------------------------------------------------------------- recorded practice outputs


def test_the_fixture_is_practice_data_of_the_expected_size():
    assert len(ROWS) == 90
    assert {r["set"] for r in ROWS} == {"tuning", "heldout"}


def test_without_the_guard_the_recorded_numbers_are_the_known_baseline():
    c = count(ROWS, guard=False)
    assert c["tuning"] == {
        "clear_wrong": 1,
        "vague_caught": 16,
        "vague": 16,
        "follow_right": 6,
        "follow": 8,
    }
    assert c["heldout"] == {
        "clear_wrong": 0,
        "vague_caught": 8,
        "vague": 8,
        "follow_right": 1,
        "follow": 4,
    }


def test_with_the_guard_every_practice_vague_item_is_still_clarified():
    c = count(ROWS, guard=True)
    assert c["tuning"]["vague_caught"] == 16 and c["heldout"]["vague_caught"] == 8


def test_with_the_guard_the_clear_item_that_was_wrongly_stopped_is_fixed_and_none_is_added():
    c = count(ROWS, guard=True)
    assert c["tuning"]["clear_wrong"] == 0 and c["heldout"]["clear_wrong"] == 0
    s36 = next(r for r in ROWS if r["qid"] == "S36")
    assert s36["history"] == [] and final_action(s36, True) == "search"  # empty history case
    assert final_action(s36, False) == "clarify"


def test_with_the_guard_the_practice_follow_ups_go_from_7_to_8_of_12():
    off, on = count(ROWS, guard=False), count(ROWS, guard=True)
    assert off["tuning"]["follow_right"] + off["heldout"]["follow_right"] == 7
    assert on["tuning"]["follow_right"] + on["heldout"]["follow_right"] == 8
    assert on["tuning"]["follow"] + on["heldout"]["follow"] == 12


def test_the_guard_changes_exactly_the_four_practice_items_the_plan_listed():
    changed = sorted(r["qid"] for r in ROWS if final_action(r, True) != final_action(r, False))
    assert changed == ["F05", "F08", "HF04", "S36"]


# ----------------------------------------------------------------------------- P1a and P1b


def make_preparer(text, turns=(), **flags):
    llm = LLMClient(
        role(name="rewrite", model="small"),
        transport=httpx.MockTransport(Server(resp(body=ok_body(text)))),
        sleep=lambda s: None,
    )
    state = SessionState(turns=list(turns), last_status="answered" if turns else None)
    return ClarifyingPreparer(llm, "SYSTEM", "clarify_t", FakeMemory(state), **flags)


HISTORY = [Turn("what are your hours on friday", "We close at 2 pm on Fridays.", "answered")]


def test_p1a_with_no_history_the_answer_model_gets_the_original_message():
    p = make_preparer(reply(query="weekday closing time")).prepare(
        "s", "When do you close, Oakline Dental?"
    )
    assert p.answer_question == "When do you close, Oakline Dental?"
    assert p.search_query == "weekday closing time" and p.rewrite_query == "weekday closing time"


def test_p1a_with_history_the_answer_model_gets_the_standalone_rewrite():
    p = make_preparer(reply(query="Saturday opening hours"), HISTORY).prepare(
        "s", "what about sundays?"
    )
    assert (
        p.answer_question == "Saturday opening hours" and p.search_query == "Saturday opening hours"
    )


def test_p1b_flag_on_searches_the_original_words_when_there_is_no_history():
    p = make_preparer(reply(query="rewritten"), search_original_when_no_history=True).prepare(
        "s", "my own words"
    )
    assert p.search_query == "my own words" and p.answer_question == "my own words"
    assert p.rewrite_query == "rewritten"  # the rewrite is still recorded


def test_p1b_flag_on_still_uses_the_rewrite_with_history():
    p = make_preparer(
        reply(query="rewritten"), HISTORY, search_original_when_no_history=True
    ).prepare("s", "what about sundays?")
    assert p.search_query == "rewritten" and p.answer_question == "rewritten"


def test_the_flags_default_to_off_for_p1b_and_on_for_the_guard():
    s = Settings(_env_file=None)
    assert s.search_original_when_no_history is False and s.clarify_guard_enabled is True


def test_the_preparer_applies_the_guard_and_records_the_override_in_the_usage():
    text = reply(action="clarify", clarifying_question="Which dentist are you referring to?")
    p = make_preparer(text).prepare("s", "I switched dentists, how do I move my x-rays?")
    assert p.action == "search" and p.clarifying_question is None
    assert p.usage["guard_override"] == ["dentist"]
    off = make_preparer(text, guard_enabled=False).prepare(
        "s", "I switched dentists, how do I move my x-rays?"
    )
    assert off.action == "clarify" and "guard_override" not in off.usage


def test_a_failed_open_search_uses_the_message_for_both_search_and_answer():
    p = make_preparer("not json").prepare("s", "hello there")
    assert p.action == "search_failed_open" and p.search_query == "hello there"
    assert (p.answer_question or p.search_query) == "hello there"


# ----------------------------------------------------------------------------- through ChatService


class _Embedder:
    def embed_query(self, text, max_wait=None):
        return [0.0, 1.0]


def test_chat_service_gives_the_answerer_the_prepared_answer_question(monkeypatch):
    from app.services import chat as chat_module
    from app.services.chat import PreparedQuery

    seen = {}

    class P:
        prompt_version = "t"

        def prepare(self, session_id, message):
            return PreparedQuery(
                "search text",
                action="search",
                answer_question="original text",
                rewrite_query="rewrite text",
            )

    answerer = FakeAnswerer()
    original = answerer.answer

    def spy(question, hits):
        seen["question"] = question
        return original(question, hits)

    answerer.answer = spy
    monkeypatch.setattr(chat_module, "connect", _FakeConnect)
    monkeypatch.setattr(chat_module, "search", lambda conn, v, top_k, embedding_model: [_hit()])
    service = ChatService(
        Settings(_env_file=None, similarity_threshold=0.4), _Embedder(), answerer, P()
    )
    result = service.chat(None, "original text")
    assert seen["question"] == "original text"
    assert result.standalone_query == "rewrite text" and result.searched_query == "search text"


class _FakeConnect:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hit():
    from app.retrieval.search import Hit

    return Hit(1, "d", "faq.md", None, "Hours", "We open at 8.", 0.9)
