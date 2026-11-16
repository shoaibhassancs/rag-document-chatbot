"""Chunking: split each loaded segment into overlapping chunks and attach metadata.

A PDF chunk never crosses a page, so its page number is exact. Markdown/HTML/TXT sections that
are very small are packed together up to the chunk size (the chunk keeps the first heading).
Sizes are token estimates (characters / 4).
"""

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.ingest.loaders import LoadedDocument, Segment
from app.ingest.textutil import CHARS_PER_TOKEN, estimate_tokens, sha256_hex


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    page: int | None
    section: str | None
    chunk_hash: str  # sha256 of the text; identical text reuses an existing vector
    token_estimate: int


def pack_small_segments(segments: list[Segment], min_chars: int, max_chars: int) -> list[Segment]:
    """Join neighbouring sections of a Markdown/HTML/TXT document while the running piece is still
    smaller than `min_chars` and the result fits `max_chars`. The piece keeps the FIRST section's
    heading as its `section`. PDF pages are never merged, so a chunk always has one exact page."""
    packed: list[Segment] = []
    for segment in segments:
        previous = packed[-1] if packed else None
        if (
            previous is not None
            and segment.page is None
            and previous.page is None
            and len(previous.text) < min_chars
            and len(previous.text) + len(segment.text) + 2 <= max_chars
        ):
            packed[-1] = Segment(previous.text + "\n\n" + segment.text, None, previous.section)
        else:
            packed.append(segment)
    return packed


def chunk_document(doc: LoadedDocument, size_tokens: int, overlap_tokens: int) -> list[Chunk]:
    if size_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= size_tokens:
        raise ValueError("chunk overlap must be smaller than chunk size, and size must be positive")
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size_tokens * CHARS_PER_TOKEN,
        chunk_overlap=overlap_tokens * CHARS_PER_TOKEN,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    max_chars = size_tokens * CHARS_PER_TOKEN
    segments = pack_small_segments(doc.segments, min_chars=max_chars // 4, max_chars=max_chars)
    chunks: list[Chunk] = []
    for segment in segments:
        for piece in splitter.split_text(segment.text):
            text = piece.strip()
            if not text:
                continue
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=text,
                    page=segment.page,
                    section=segment.section,
                    chunk_hash=sha256_hex(text),
                    token_estimate=estimate_tokens(text),
                )
            )
    return chunks
