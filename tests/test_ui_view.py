"""The UI's display logic. Pure functions: no Streamlit, no network."""

import re

import pytest

from ui import view
from ui.api_client import ChatOutcome
from ui.view import Block

EVIL = (
    "![x](http://example.invalid/x.png) and [link](http://example.invalid) and "
    '<img src="http://example.invalid/x.png"> <script>alert(1)</script> :smile: **bold**'
)


def reply(status, answer="An answer.", sources=None, latency=None, kind="ok"):
    return ChatOutcome(kind, status, answer, sources or [], latency)


def kinds(blocks):
    return [b.kind for b in blocks]


# ----------------------------------------------------------------------------- badges


def test_every_outcome_has_its_own_badge_label_symbol_and_colours():
    assert set(view.BADGES) == {"answered", "refused", "clarify", "error", "unreachable"}
    labels = [b.label for b in view.BADGES.values()]
    assert len(set(labels)) == len(labels)
    visible = {(b.foreground, b.background) for k, b in view.BADGES.items() if k != "unreachable"}
    assert len(visible) == 4  # error and unreachable intentionally share the red; the rest differ
    for key, badge in view.BADGES.items():
        html = view.badge_html(key)
        assert badge.label in html and badge.symbol in html and badge.background in html


def test_the_badge_text_does_not_rely_on_colour_alone():
    for badge in view.BADGES.values():
        assert badge.label.strip() and badge.symbol.strip()


def test_badge_html_is_built_only_from_constants():
    for key in view.BADGES:
        html = view.badge_html(key)
        assert re.fullmatch(r'<span title="[^"<>]*" style="[^"<>]*">[^<>]+</span>', html), html


# ----------------------------------------------------------------------------- blocks per status


def test_an_answered_reply_shows_the_badge_the_answer_and_the_sources():
    out = reply(
        "answered",
        "Yes, we do.",
        [
            {"file": "a.pdf", "page": 2, "section": None},
            {"file": "b.md", "page": None, "section": "Fees"},
        ],
        latency=1840,
    )
    blocks = view.render_blocks(out)
    assert blocks[0] == Block("badge", badge="answered") and blocks[1] == Block(
        "text", "Yes, we do."
    )
    assert [b.text for b in blocks if b.kind == "source"] == ["a.pdf, page 2", "b.md, section Fees"]
    assert Block("heading", "Sources") in blocks
    assert blocks[-1] == Block("caption", "answered in 1.8 s")


def test_duplicate_sources_are_listed_once():
    out = reply("answered", sources=[{"file": "a.pdf", "page": 1}, {"file": "a.pdf", "page": 1}])
    assert [b.text for b in view.render_blocks(out) if b.kind == "source"] == ["a.pdf, page 1"]


def test_a_clarify_reply_shows_the_question_and_never_any_sources():
    out = reply("clarify", "Which treatment do you mean?", [{"file": "a.pdf", "page": 1}], 900)
    blocks = view.render_blocks(out)
    assert kinds(blocks) == ["badge", "text"] and blocks[0].badge == "clarify"
    assert blocks[1].text == "Which treatment do you mean?"


def test_a_refused_reply_shows_the_fixed_message_and_no_sources():
    out = reply("refused", "I couldn't find that in our documents.", [{"file": "a.pdf"}])
    blocks = view.render_blocks(out)
    assert kinds(blocks) == ["badge", "text"] and blocks[0].badge == "refused"


def test_an_error_reply_shows_the_busy_message_or_a_friendly_fallback():
    blocks = view.render_blocks(reply("error", "The assistant is busy right now."))
    assert blocks[0].badge == "error" and blocks[1].text == "The assistant is busy right now."
    assert view.render_blocks(reply("error", "   "))[1].text == view.FRIENDLY["error"]


@pytest.mark.parametrize(
    "outcome, badge, friendly",
    [
        (ChatOutcome("unreachable"), "unreachable", "unreachable"),
        (ChatOutcome("bad_input"), "error", "bad_input"),
        (ChatOutcome("unexpected"), "error", "unexpected"),
    ],
)
def test_transport_problems_get_a_badge_and_a_friendly_text(outcome, badge, friendly):
    blocks = view.render_blocks(outcome)
    assert blocks[0].badge == badge and blocks[1] == Block("text", view.FRIENDLY[friendly])


def test_friendly_messages_contain_no_codes_urls_or_technical_words():
    for key, text in view.FRIENDLY.items():
        assert not re.search(r"\b(HTTP|50\d|40\d|traceback|exception|json|httpx)\b", text, re.I), (
            key
        )


# ----------------------------------------------------------------------------- no network loads


@pytest.mark.parametrize("status", ["answered", "refused", "clarify", "error"])
def test_hostile_markdown_and_html_stay_in_plain_text_blocks_only(status):
    blocks = view.render_blocks(reply(status, EVIL, [{"file": EVIL, "page": None}]))
    carrying = [b for b in blocks if "example.invalid" in b.text]
    assert carrying and all(b.kind in ("text", "source") for b in carrying)
    assert all(
        b.kind != "badge" or "example.invalid" not in view.badge_html(b.badge) for b in blocks
    )
    for b in blocks:
        if b.kind == "badge":
            assert "<img" not in view.badge_html(b.badge) and "<a " not in view.badge_html(b.badge)


def test_the_answer_text_is_kept_character_for_character_apart_from_control_characters():
    blocks = view.render_blocks(reply("answered", EVIL))
    assert blocks[1].text == EVIL.strip()
    assert view.sanitize("a\x00b\x07c\x1bd\ne\tf") == "abcd\ne\tf"


def test_very_long_answers_are_capped():
    assert len(view.sanitize("x" * 10_000)) == view.MAX_SHOWN_CHARS


def test_source_and_document_names_are_sanitised_plain_text():
    assert "\x00" not in view.format_source({"file": "a\x00.pdf", "page": 1})
    line = view.format_document({"name": "n\x07.md", "source_type": "markdown", "status": "ready"})
    assert "\x07" not in line


# ----------------------------------------------------------------------------- sources


@pytest.mark.parametrize(
    "source, expected",
    [
        ({"file": "p.pdf", "page": 3, "section": None}, "p.pdf, page 3"),
        ({"file": "p.pdf", "page": 3, "section": "Fees"}, "p.pdf, page 3"),
        ({"file": "m.md", "page": None, "section": "Visits"}, "m.md, section Visits"),
        ({"file": "m.md", "page": None, "section": "  "}, "m.md"),
        ({"file": "m.md"}, "m.md"),
        ({"file": "m.md", "page": True}, "m.md"),
        ({}, "unknown document"),
    ],
)
def test_format_source(source, expected):
    assert view.format_source(source) == expected


# ----------------------------------------------------------------------------- messages and ids


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_a_blank_message_is_not_sent(text):
    assert view.validate_message(text) == view.FRIENDLY["empty"]


def test_a_message_over_1000_characters_is_not_sent_but_1000_is():
    assert view.validate_message("x" * 1001) == view.FRIENDLY["too_long"]
    assert view.validate_message("x" * 1000) is None and view.validate_message("hello") is None


def test_session_ids_match_the_api_pattern_and_differ():
    ids = {view.new_session_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", i) and i.startswith("ui-") for i in ids)


def test_the_privacy_note_asks_for_no_personal_information():
    assert "do not enter personal information" in view.PRIVACY_NOTE


# ----------------------------------------------------------------------------- documents


def test_document_lines_show_name_and_type_and_only_say_more_when_not_ready():
    # Changed deliberately (Nov 17, 2026): the old line also said "ready, 3 chunks" for every
    # document. Now a ready document is just its name and type.
    ready = {"name": "a.pdf", "source_type": "pdf", "status": "ready", "chunks": 3}
    assert view.format_document(ready) == "a.pdf [pdf]"
    ingesting = {"name": "a.pdf", "source_type": "pdf", "status": "ingesting"}
    assert (
        view.format_document({**ingesting, "chunks": 4, "chunks_done": 1})
        == "a.pdf [pdf] - embedding 1/4"
    )
    failed = view.format_document(
        {"name": "a.pdf", "source_type": "pdf", "status": "failed", "chunks": 4, "error": "x" * 500}
    )
    assert failed.startswith("a.pdf [pdf] - failed (x") and len(failed) < 200
    no_reason = {"name": "a.pdf", "source_type": "pdf", "status": "failed"}
    assert view.format_document(no_reason) == "a.pdf [pdf] - failed (unknown reason)"
    assert view.format_document({**ingesting, "status": "queued"}) == "a.pdf [pdf] - queued"


def test_the_documents_sentence_for_none_one_and_many():
    assert view.documents_sentence(0) == "No documents yet."
    assert view.documents_sentence(1) == "The assistant answers only from this clinic document."
    assert view.documents_sentence(2) == "The assistant answers only from these 2 clinic documents."
    assert "29 clinic documents" in view.documents_sentence(29)


def test_the_expander_title_has_the_count():
    assert view.documents_title(1) == "See the 1 document"
    assert view.documents_title(29) == "See the 29 documents"


def test_the_picker_label_escapes_markdown_and_html_in_document_names():
    label = view.picker_label(
        {"name": "![x](http://e.invalid/a.png) <img src=x>.md", "source_type": "md"}
    )
    # every markdown/HTML special character is preceded by a backslash (none is bare)
    assert re.search(r"(?<!\\)[<>\[\]()!`*_]", label) is None
    assert "\x07" not in view.picker_label({"name": "a\x07b.md", "source_type": "markdown"})
    assert "\n" not in view.picker_label({"name": "a\nb.md", "source_type": "markdown"})
    assert view.picker_label({"name": "faq", "source_type": "markdown"}) == "faq \\[markdown\\]"


def test_ingest_lines_explain_each_result_in_plain_words():
    lines = view.ingest_lines(
        [
            {"name": "a.md", "status": "queued", "chunks": 2},
            {"name": "b.md", "status": "unchanged"},
            {"name": "c.md", "status": "duplicate", "duplicate_of": "a.md"},
            {"name": "d.csv", "status": "rejected", "reason_code": "unsupported_type"},
            {"name": "e.pdf", "status": "rejected", "reason_code": "something_new"},
        ]
    )
    assert lines == [
        "a.md: accepted (2 chunks to embed)",
        "b.md: already up to date",
        "c.md: same text as a.md",
        "d.csv: not added, this file type is not supported",
        "e.pdf: not added, it could not be added",
    ]


def test_ingest_delete_and_docs_errors_map_to_friendly_text():
    assert view.ingest_error("unauthorized") == view.FRIENDLY["unauthorized"]
    assert view.ingest_error("unreachable") == view.FRIENDLY["ingest_unreachable"]
    assert view.ingest_error("weird") == view.FRIENDLY["unexpected"]
    assert view.delete_error("conflict") == view.FRIENDLY["conflict"]
    assert view.delete_error("bad_input") == view.FRIENDLY["not_found"]
    assert view.delete_error("unreachable") == view.FRIENDLY["delete_unreachable"]
    assert view.docs_error("unreachable") == view.FRIENDLY["docs_unreachable"]
    assert view.docs_error("unexpected") == view.FRIENDLY["docs_unexpected"]
    assert "Refresh" in view.INGEST_ACCEPTED and "background" in view.INGEST_ACCEPTED
