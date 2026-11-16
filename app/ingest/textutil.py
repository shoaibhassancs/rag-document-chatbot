"""Small text helpers shared by the loaders, chunker and embedder."""

import hashlib
import math
import re

CHARS_PER_TOKEN = 4  # rough English average; Voyage's real tokenizer is not available offline


def estimate_tokens(text: str) -> int:
    """Estimated token count (characters / 4, rounded up). Never zero for non-empty text."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def clean_text(text: str) -> str:
    """Light cleanup that keeps paragraph structure: unify newlines, trim spaces, cap blanks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_for_hash(text: str) -> str:
    """Collapse all whitespace so a re-saved copy with different spacing hashes the same."""
    return " ".join(text.split())


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
