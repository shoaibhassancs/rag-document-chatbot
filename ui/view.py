"""Display logic for the chat UI, as pure functions (no Streamlit import), so it is testable
without a browser.

Rules that matter:
  * Text that came from a model or a document (answers, clarifying questions, source names, document
    names) is only ever shown as PLAIN TEXT blocks. It is never put into markdown or HTML, so an
    answer containing `![x](http://...)`, `[link](...)` or `<img src=...>` cannot make the browser
    fetch anything or draw a link. Only badges (built from constants below) are HTML.
  * Every message is friendly: no status codes, no raw server text, no key.
  * Each outcome has its own badge, distinguished by text and symbol as well as colour.
"""

import re
import secrets
from dataclasses import dataclass

from ui.api_client import ChatOutcome

MAX_MESSAGE_CHARS = 1000  # the API's limit for /chat
MAX_SHOWN_CHARS = 4000  # a model answer is capped at 600 tokens; this is a safety limit

PRIVACY_NOTE = (
    "Demo only. Please do not enter personal information such as names, phone numbers, "
    "email addresses or health details."
)
SCOPE_NOTE = "Answers come only from the clinic's documents."
NO_DOCUMENTS = "No documents yet."

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Badge:
    label: str
    symbol: str
    foreground: str
    background: str
    help: str


BADGES: dict[str, Badge] = {
    "answered": Badge("Answered", "✔", "#0f5132", "#d1e7dd", "Found in our documents"),
    "refused": Badge(
        "Not in our documents", "⚠", "#664d03", "#fff3cd", "Outside what our documents cover"
    ),
    "clarify": Badge("One quick question", "?", "#055160", "#cff4fc", "More detail needed"),
    "error": Badge("Service busy", "✖", "#842029", "#f8d7da", "Please try again in a minute"),
    "unreachable": Badge(
        "Can't reach the assistant", "✖", "#842029", "#f8d7da", "The service is not responding"
    ),
}

FRIENDLY = {
    "unreachable": "I can't reach the assistant right now. Please try again in a moment.",
    "error": "The assistant is busy right now. Please try again in a minute.",
    "unexpected": "Something went wrong on our side. Please try again in a moment.",
    "bad_input": f"Please type a question (up to {MAX_MESSAGE_CHARS} characters).",
    "empty": "Please type a question first.",
    "too_long": f"That message is too long. Please keep it under {MAX_MESSAGE_CHARS} characters.",
    # documents
    "docs_unreachable": "I can't load the document list right now. Please try again in a moment.",
    "docs_unexpected": "The document list is not available right now.",
    "no_key": "Uploading and deleting need the APP_API_KEY setting, which is not set for this UI.",
    "unauthorized": "The upload key was not accepted, so nothing was changed.",
    "unavailable": "The server is not set up for uploads yet, so nothing was changed.",
    "too_many": "Too many files at once. Please upload fewer files.",
    "not_found": "That document is already gone.",
    "conflict": "This document is still being processed. Try again when it is ready or failed.",
    "ingest_unreachable": "I can't reach the server, so nothing was uploaded.",
    "delete_unreachable": "I can't reach the server, so nothing was deleted.",
}

INGEST_ACCEPTED = (
    "Accepted. Embedding continues in the background and can take a few minutes. "
    "Press Refresh to see progress."
)

# Per-file reasons from POST /ingest, in plain words. Unknown codes fall back to a generic line.
REASONS = {
    "unsupported_type": "this file type is not supported",
    "empty": "the file is empty",
    "too_large": "the file is too large",
    "unreadable_pdf": "the PDF could not be read",
    "private_url": "that address is not allowed",
    "not_html": "that address is not a web page",
    "bad_name": "the file has no name",
    "conflict": "another upload of this name is in progress",
}


def sanitize(text: str) -> str:
    """Plain text for display: control characters removed, length capped. Not markdown-escaped,
    because it is only ever shown through a plain-text element."""
    cleaned = _CONTROL.sub("", text or "")
    return cleaned[:MAX_SHOWN_CHARS]


def badge_html(key: str) -> str:
    """A coloured label with a symbol. Built only from the constants above."""
    b = BADGES[key]
    return (
        f'<span title="{b.help}" style="display:inline-block;padding:2px 10px;border-radius:12px;'
        f'font-weight:600;font-size:0.9rem;color:{b.foreground};background:{b.background}">'
        f"{b.symbol} {b.label}</span>"
    )


def format_source(source: dict) -> str:
    """'file, page N' for PDFs, 'file, section X' for Markdown, text and web pages, else file."""
    name = sanitize(str(source.get("file") or "unknown document"))
    page, section = source.get("page"), source.get("section")
    if isinstance(page, int) and not isinstance(page, bool):
        return f"{name}, page {page}"
    if isinstance(section, str) and section.strip():
        return f"{name}, section {sanitize(section.strip())}"
    return name


@dataclass(frozen=True)
class Block:
    kind: str  # "badge" | "text" | "heading" | "source" | "caption"
    text: str = ""
    badge: str | None = None  # key into BADGES for kind == "badge"


def validate_message(text: str) -> str | None:
    """None if the message may be sent, else a friendly reason."""
    if not text or not text.strip():
        return FRIENDLY["empty"]
    if len(text.strip()) > MAX_MESSAGE_CHARS:
        return FRIENDLY["too_long"]
    return None


def render_blocks(outcome: ChatOutcome) -> list[Block]:
    """What to draw for one reply, as blocks. Model text is always a 'text' block."""
    if outcome.kind == "unreachable":
        return [Block("badge", badge="unreachable"), Block("text", FRIENDLY["unreachable"])]
    if outcome.kind == "bad_input":
        return [Block("badge", badge="error"), Block("text", FRIENDLY["bad_input"])]
    if outcome.kind != "ok":
        return [Block("badge", badge="error"), Block("text", FRIENDLY["unexpected"])]

    status = outcome.status or "error"
    answer = sanitize(outcome.answer).strip()
    blocks = [Block("badge", badge=status)]
    if status == "error":
        blocks.append(Block("text", answer or FRIENDLY["error"]))
    elif status == "answered":
        blocks.append(Block("text", answer or FRIENDLY["unexpected"]))
        sources = [format_source(s) for s in outcome.sources]
        if sources:
            blocks.append(Block("heading", "Sources"))
            blocks += [Block("source", s) for s in dict.fromkeys(sources)]
        if outcome.latency_ms is not None:
            blocks.append(Block("caption", f"answered in {outcome.latency_ms / 1000:.1f} s"))
    else:  # refused or clarify: the text only, never sources
        blocks.append(Block("text", answer or FRIENDLY["unexpected"]))
    return blocks


def documents_sentence(count: int) -> str:
    """The one plain sentence at the top of the sidebar."""
    if count <= 0:
        return NO_DOCUMENTS
    if count == 1:
        return "The assistant answers only from this clinic document."
    return f"The assistant answers only from these {count} clinic documents."


def documents_title(count: int) -> str:
    """Title of the collapsed list, with the count."""
    return f"See the {count} document" + ("" if count == 1 else "s")


def format_document(doc: dict) -> str:
    """One plain-text line per document: name and type, plus progress or the failure reason
    when the document is not ready."""
    name = sanitize(str(doc.get("name") or "unnamed"))
    kind = sanitize(str(doc.get("source_type") or "?"))
    status = str(doc.get("status") or "?")
    line = f"{name} [{kind}]"
    chunks, done = doc.get("chunks"), doc.get("chunks_done")
    if status == "ready":
        return line
    if status == "ingesting" and isinstance(chunks, int) and isinstance(done, int):
        return f"{line} - embedding {done}/{chunks}"
    if status == "failed":
        error = doc.get("error")
        reason = sanitize(error)[:120] if isinstance(error, str) and error else "unknown reason"
        return f"{line} - failed ({reason})"
    return f"{line} - {sanitize(status)}"


_MARKDOWN_CHARS = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>$:])")


def picker_label(doc: dict) -> str:
    """Label for the delete picker. A select box may render its labels as markdown, so every
    markdown character in a document name is escaped; it stays readable text."""
    name = sanitize(str(doc.get("name") or "unnamed")).replace("\n", " ")
    kind = sanitize(str(doc.get("source_type") or "?")).replace("\n", " ")
    return _MARKDOWN_CHARS.sub(r"\\\1", f"{name} [{kind}]")


def ingest_lines(results: list[dict]) -> list[str]:
    """One line per item of an ingest response, in plain words."""
    lines = []
    for r in results:
        name = sanitize(str(r.get("name") or "item"))
        status = r.get("status")
        if status == "queued":
            lines.append(f"{name}: accepted ({r.get('chunks', '?')} chunks to embed)")
        elif status == "unchanged":
            lines.append(f"{name}: already up to date")
        elif status == "duplicate":
            lines.append(
                f"{name}: same text as {sanitize(str(r.get('duplicate_of') or 'another file'))}"
            )
        else:
            reason = REASONS.get(str(r.get("reason_code")), "it could not be added")
            lines.append(f"{name}: not added, {reason}")
    return lines


def ingest_error(kind: str) -> str:
    """Friendly text for a failed ingest outcome kind."""
    if kind == "unreachable":
        return FRIENDLY["ingest_unreachable"]
    return FRIENDLY.get(kind, FRIENDLY["unexpected"])


def delete_error(kind: str) -> str:
    """Friendly text for a failed delete outcome kind."""
    if kind == "unreachable":
        return FRIENDLY["delete_unreachable"]
    if kind == "bad_input":
        return FRIENDLY["not_found"]
    return FRIENDLY.get(kind, FRIENDLY["unexpected"])


def docs_error(kind: str) -> str:
    """Friendly text when the document list cannot be loaded."""
    return FRIENDLY["docs_unreachable"] if kind == "unreachable" else FRIENDLY["docs_unexpected"]


def new_session_id() -> str:
    """Matches the API's session id pattern ^[A-Za-z0-9_-]{1,64}$."""
    return "ui-" + secrets.token_hex(8)
