"""Ingestion service: register documents (hash, dedupe, chunk, store) and embed what is pending.

Plain functions with no FastAPI or Streamlit imports, so the API, the CLIs and later projects
all call the same code. Two steps, deliberately separate:

  register_document  fast; parses nothing new, stores chunks with NO vector yet, status 'ingesting'
  embed_pending      slow and throttled; embeds every chunk without a vector, in token-sized
                     batches, saving each batch as soon as it returns, so a crash loses nothing
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import psycopg

from app.db import connect
from app.ingest.chunker import Chunk, chunk_document
from app.ingest.embedder import Embedder, EmbeddingError, plan_batches
from app.ingest.loaders import LoadedDocument
from app.ingest.ratelimit import simulate_duration
from app.ingest.textutil import sha256_hex

log = logging.getLogger(__name__)


def vector_literal(vector: list[float]) -> str:
    """pgvector accepts '[0.1,0.2,...]' text, so no extra Python package is needed."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# ----------------------------------------------------------------------------- registering


@dataclass
class ItemResult:
    name: str
    status: str  # queued | unchanged | duplicate | rejected
    action: str | None = None  # created | updated | resumed (only when queued)
    document_id: str | None = None
    duplicate_of: str | None = None
    reason_code: str | None = None
    detail: str | None = None
    chunks: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def register_document(
    conn: psycopg.Connection,
    doc: LoadedDocument,
    *,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    embedding_model: str,
) -> ItemResult:
    """Store a document and its chunks (without vectors). The caller commits."""
    content_hash = sha256_hex(doc.normalized_text)
    existing = conn.execute(
        "SELECT id, content_hash, status FROM documents WHERE name = %s", [doc.name]
    ).fetchone()

    if existing and existing[1] == content_hash:
        doc_id, _, status = existing
        pending = conn.execute(
            "SELECT count(*) FROM chunks WHERE document_id = %s AND embedding IS NULL", [doc_id]
        ).fetchone()[0]
        if status == "ready" and pending == 0:
            return ItemResult(doc.name, "unchanged", document_id=str(doc_id))
        conn.execute(
            "UPDATE documents SET status = 'ingesting', error = NULL, updated_at = now() "
            "WHERE id = %s",
            [doc_id],
        )
        return ItemResult(doc.name, "queued", "resumed", document_id=str(doc_id))

    same_text = conn.execute(
        "SELECT name FROM documents WHERE content_hash = %s AND name <> %s AND status <> 'failed' "
        "ORDER BY created_at LIMIT 1",
        [content_hash, doc.name],
    ).fetchone()
    if same_text:
        return ItemResult(
            doc.name,
            "duplicate",
            duplicate_of=same_text[0],
            detail=f"Same text as '{same_text[0]}'; skipped.",
        )

    chunks = chunk_document(doc, chunk_size_tokens, chunk_overlap_tokens)
    if not chunks:
        return ItemResult(doc.name, "rejected", reason_code="empty", detail="No text to store.")

    carried: dict[str, str] = {}
    if existing:
        # An update: keep vectors of chunks whose text did not change (they cost nothing to keep).
        carried = {
            h: v
            for h, v in conn.execute(
                "SELECT chunk_hash, embedding::text FROM chunks "
                "WHERE document_id = %s AND embedding IS NOT NULL AND embedding_model = %s",
                [existing[0], embedding_model],
            )
        }
        conn.execute("DELETE FROM chunks WHERE document_id = %s", [existing[0]])
        doc_id = existing[0]
        conn.execute(
            "UPDATE documents SET source_type = %s, source_url = %s, content_hash = %s, "
            "status = 'ingesting', error = NULL, page_count = %s, chunk_total = %s, "
            "updated_at = now() WHERE id = %s",
            [doc.source_type, doc.source_url, content_hash, doc.page_count, len(chunks), doc_id],
        )
        action = "updated"
    else:
        doc_id = conn.execute(
            "INSERT INTO documents (name, source_type, source_url, content_hash, status, "
            "page_count, chunk_total) VALUES (%s, %s, %s, %s, 'ingesting', %s, %s) RETURNING id",
            [doc.name, doc.source_type, doc.source_url, content_hash, doc.page_count, len(chunks)],
        ).fetchone()[0]
        action = "created"

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (document_id, chunk_index, text, chunk_hash, page, section, "
            "token_estimate, embedding, embedding_model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            [_chunk_row(doc_id, c, carried, embedding_model) for c in chunks],
        )
    return ItemResult(doc.name, "queued", action, document_id=str(doc_id), chunks=len(chunks))


def _chunk_row(doc_id, chunk: Chunk, carried: dict[str, str], model: str) -> list:
    vector = carried.get(chunk.chunk_hash)
    return [
        doc_id,
        chunk.index,
        chunk.text,
        chunk.chunk_hash,
        chunk.page,
        chunk.section,
        chunk.token_estimate,
        vector,
        model if vector else None,
    ]


# ----------------------------------------------------------------------------- embedding


@dataclass
class EmbedSummary:
    chunks_embedded: int = 0  # chunk rows that got a vector from Voyage in this run
    chunks_reused: int = 0  # chunk rows that copied an identical chunk's vector (no tokens)
    requests: int = 0
    estimated_tokens: int = 0
    actual_tokens: int = 0
    batches: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class Progress:
    batch: int
    batches: int
    batch_estimated_tokens: int
    batch_chunks: int
    embedded: int  # unique texts embedded so far
    total: int  # unique texts to embed in this run
    elapsed_seconds: float


_REUSE_SQL = """
UPDATE chunks c SET embedding = s.embedding, embedding_model = s.embedding_model
FROM (
    SELECT DISTINCT ON (chunk_hash) chunk_hash, embedding, embedding_model
    FROM chunks WHERE embedding IS NOT NULL AND embedding_model = %s
    ORDER BY chunk_hash, id
) s
WHERE c.embedding IS NULL AND c.chunk_hash = s.chunk_hash
"""

_MARK_READY_SQL = """
UPDATE documents d SET status = 'ready', error = NULL, updated_at = now()
WHERE d.status = 'ingesting'
  AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.embedding IS NULL)
"""

_MARK_FAILED_SQL = """
UPDATE documents d SET status = 'failed', error = %s, updated_at = now()
WHERE d.status = 'ingesting'
  AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.embedding IS NULL)
"""


def embed_pending(
    database_url: str,
    embedder: Embedder,
    *,
    progress: Callable[[Progress], None] | None = None,
) -> EmbedSummary:
    """Embed every chunk that has no vector yet. Safe to run again after any interruption.

    Identical texts are embedded once. A failure marks the unfinished documents 'failed' with a
    short reason, keeps everything already saved, and re-raises so the caller can report it.
    """
    summary = EmbedSummary()
    started = time.monotonic()
    requests_before = getattr(embedder, "requests", 0)
    with connect(database_url) as conn:
        try:
            summary.chunks_reused = conn.execute(_REUSE_SQL, [embedder.model]).rowcount
            conn.execute(_MARK_READY_SQL)
            conn.commit()

            rows = conn.execute(
                "SELECT DISTINCT ON (chunk_hash) chunk_hash, text FROM chunks "
                "WHERE embedding IS NULL ORDER BY chunk_hash, id"
            ).fetchall()
            batches = plan_batches(
                [embedder.estimate_tokens(text) for _, text in rows], embedder.max_batch_tokens
            )
            summary.batches = len(batches)
            done = 0
            for number, indexes in enumerate(batches, start=1):
                hashes = [rows[i][0] for i in indexes]
                # A document may have been deleted since we listed the work; skip its chunks.
                still_pending = {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT chunk_hash FROM chunks "
                        "WHERE embedding IS NULL AND chunk_hash = ANY(%s)",
                        [hashes],
                    )
                }
                indexes = [i for i in indexes if rows[i][0] in still_pending]
                if not indexes:
                    continue
                texts = [rows[i][1] for i in indexes]
                estimated = sum(embedder.estimate_tokens(t) for t in texts)
                result = embedder.embed(texts, "document")
                for i, vector in zip(indexes, result.vectors, strict=True):
                    summary.chunks_embedded += conn.execute(
                        "UPDATE chunks SET embedding = %s::vector, embedding_model = %s "
                        "WHERE chunk_hash = %s AND embedding IS NULL",
                        [vector_literal(vector), embedder.model, rows[i][0]],
                    ).rowcount
                conn.execute(_MARK_READY_SQL)
                conn.commit()
                summary.estimated_tokens += estimated
                summary.actual_tokens += result.tokens
                done += len(indexes)
                if progress:
                    progress(
                        Progress(
                            number, len(batches), estimated, len(indexes), done, len(rows),
                            time.monotonic() - started,
                        )
                    )  # fmt: skip
            conn.execute(_MARK_READY_SQL)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            reason = str(exc)[:300] if isinstance(exc, EmbeddingError) else f"{type(exc).__name__}"
            conn.execute(_MARK_FAILED_SQL, [reason])
            conn.commit()
            summary.requests = getattr(embedder, "requests", 0) - requests_before
            raise
    summary.requests = getattr(embedder, "requests", 0) - requests_before
    summary.elapsed_seconds = time.monotonic() - started
    return summary


def fail_pending(database_url: str, reason: str) -> None:
    """Mark documents with unembedded chunks as failed (used when no embedder can run)."""
    with connect(database_url) as conn:
        conn.execute(_MARK_FAILED_SQL, [reason[:300]])
        conn.commit()


# ----------------------------------------------------------------------------- dry run


@dataclass
class RunEstimate:
    documents: int
    duplicates: list[tuple[str, str]]  # (name, duplicate_of)
    chunks: int
    unique_chunks: int
    estimated_tokens: int
    batches: int
    estimated_seconds: float


def estimate_run(
    docs: list[LoadedDocument],
    *,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    safety_factor: float,
    max_batch_tokens: int,
    requests_per_minute: int,
    tokens_per_minute: int,
) -> RunEstimate:
    """What would a fresh ingestion of these documents cost? No database, no network."""
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    texts: dict[str, int] = {}
    chunk_total = 0
    kept = 0
    for doc in docs:
        doc_hash = sha256_hex(doc.normalized_text)
        if doc_hash in seen:
            duplicates.append((doc.name, seen[doc_hash]))
            continue
        seen[doc_hash] = doc.name
        kept += 1
        for chunk in chunk_document(doc, chunk_size_tokens, chunk_overlap_tokens):
            chunk_total += 1
            texts.setdefault(chunk.chunk_hash, chunk.token_estimate)
    counts = [math.ceil(t * safety_factor) for t in texts.values()]
    batches = plan_batches(counts, max_batch_tokens)
    batch_tokens = [sum(counts[i] for i in b) for b in batches]
    seconds = simulate_duration(batch_tokens, requests_per_minute, tokens_per_minute)
    return RunEstimate(
        documents=kept,
        duplicates=duplicates,
        chunks=chunk_total,
        unique_chunks=len(texts),
        estimated_tokens=sum(counts),
        batches=len(batches),
        estimated_seconds=seconds,
    )
