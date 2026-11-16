"""Pre-embedded seed file: the sample documents' chunks with their vectors, as gzipped JSON.

A fresh clone can load it in seconds instead of waiting minutes for throttled Voyage calls.
Loading refuses a seed built with a different embedding model or size, and `stale_report`
detects when the sample documents or the chunking settings have changed since it was built.
"""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from app.config import Settings
from app.ingest.chunker import chunk_document
from app.ingest.loaders import LoadError, load_bytes
from app.ingest.textutil import sha256_hex
from app.services.ingestion import vector_literal

FORMAT_VERSION = 1
VECTOR_DECIMALS = 6  # rounding to 6 decimals does not change cosine rankings in practice


class SeedError(RuntimeError):
    pass


def build_seed(conn: psycopg.Connection, settings: Settings) -> dict:
    """Export every fully embedded document (with vectors) from the database."""
    documents = []
    rows = conn.execute(
        "SELECT id, name, source_type, source_url, content_hash, page_count FROM documents "
        "WHERE status = 'ready' ORDER BY name"
    ).fetchall()
    for doc_id, name, source_type, source_url, content_hash, page_count in rows:
        chunks = conn.execute(
            "SELECT chunk_index, text, chunk_hash, page, section, token_estimate, embedding::text "
            "FROM chunks WHERE document_id = %s ORDER BY chunk_index",
            [doc_id],
        ).fetchall()
        if any(c[6] is None for c in chunks):
            raise SeedError(f"{name} has chunks without vectors; finish ingesting first.")
        documents.append(
            {
                "name": name,
                "source_type": source_type,
                "source_url": source_url,
                "content_hash": content_hash,
                "page_count": page_count,
                "chunks": [
                    {
                        "chunk_index": c[0],
                        "text": c[1],
                        "chunk_hash": c[2],
                        "page": c[3],
                        "section": c[4],
                        "token_estimate": c[5],
                        "embedding": [round(x, VECTOR_DECIMALS) for x in json.loads(c[6])],
                    }
                    for c in chunks
                ],
            }
        )
    if not documents:
        raise SeedError("There are no ready documents to export.")
    return {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "chunk_size_tokens": settings.chunk_size_tokens,
        "chunk_overlap_tokens": settings.chunk_overlap_tokens,
        "documents": documents,
    }


def write_seed(seed: dict, path: Path) -> int:
    """Write gzipped JSON (fixed timestamp, so identical content gives identical bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(seed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(payload)
    return path.stat().st_size


def read_seed(path: Path) -> dict:
    try:
        with gzip.open(path, "rb") as gz:
            seed = json.loads(gz.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise SeedError(f"The seed file could not be read ({type(exc).__name__}).") from exc
    if seed.get("format_version") != FORMAT_VERSION:
        raise SeedError(f"Unsupported seed format {seed.get('format_version')!r}.")
    return seed


def load_seed(conn: psycopg.Connection, seed: dict, settings: Settings) -> dict[str, int]:
    """Insert the seed's documents. The caller commits. Refuses a model or size mismatch."""
    if seed["embedding_model"] != settings.embedding_model:
        raise SeedError(
            f"The seed was built with '{seed['embedding_model']}' but config says "
            f"'{settings.embedding_model}'. Rebuild the seed or ingest the documents directly."
        )
    if seed["embedding_dimensions"] != settings.embedding_dimensions:
        raise SeedError(
            f"The seed has {seed['embedding_dimensions']}-dimension vectors but config says "
            f"{settings.embedding_dimensions}."
        )
    counts = {"loaded": 0, "updated": 0, "skipped": 0}
    for doc in seed["documents"]:
        for chunk in doc["chunks"]:
            if len(chunk["embedding"]) != settings.embedding_dimensions:
                raise SeedError(f"{doc['name']} contains a vector of the wrong size.")
        existing = conn.execute(
            "SELECT id, content_hash, status FROM documents WHERE name = %s", [doc["name"]]
        ).fetchone()
        if existing and existing[1] == doc["content_hash"] and existing[2] == "ready":
            counts["skipped"] += 1
            continue
        if existing:
            conn.execute("DELETE FROM documents WHERE id = %s", [existing[0]])
        doc_id = conn.execute(
            "INSERT INTO documents (name, source_type, source_url, content_hash, status, "
            "page_count, chunk_total) VALUES (%s, %s, %s, %s, 'ready', %s, %s) RETURNING id",
            [
                doc["name"],
                doc["source_type"],
                doc["source_url"],
                doc["content_hash"],
                doc["page_count"],
                len(doc["chunks"]),
            ],
        ).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (document_id, chunk_index, text, chunk_hash, page, section, "
                "token_estimate, embedding, embedding_model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                [
                    [
                        doc_id,
                        c["chunk_index"],
                        c["text"],
                        c["chunk_hash"],
                        c["page"],
                        c["section"],
                        c["token_estimate"],
                        vector_literal(c["embedding"]),
                        seed["embedding_model"],
                    ]
                    for c in doc["chunks"]
                ],
            )
        counts["updated" if existing else "loaded"] += 1
    return counts


def stale_report(seed: dict, docs_dir: Path, settings: Settings) -> dict[str, list[str]]:
    """Compare the seed with the files in docs_dir. All lists empty means the seed is current.

    Files whose text duplicates another file are ignored (they are skipped at ingestion too).
    """
    seed_by_hash = {d["content_hash"]: d for d in seed["documents"]}
    report: dict[str, list[str]] = {
        "missing_from_seed": [],
        "changed_chunks": [],
        "not_on_disk": [],
    }
    if (seed["chunk_size_tokens"], seed["chunk_overlap_tokens"]) != (
        settings.chunk_size_tokens,
        settings.chunk_overlap_tokens,
    ):
        report["changed_chunks"].append("(chunk size or overlap settings changed)")
    present_hashes: set[str] = set()
    for path in sorted(p for p in docs_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
        try:
            doc = load_bytes(path.name, path.read_bytes())
        except LoadError:
            continue
        content_hash = sha256_hex(doc.normalized_text)
        present_hashes.add(content_hash)
        entry = seed_by_hash.get(content_hash)
        if entry is None:
            report["missing_from_seed"].append(path.name)
            continue
        if entry["name"] != path.name:
            continue  # a copy under another name: skipped at ingestion, so nothing to compare
        current = [
            c.chunk_hash
            for c in chunk_document(doc, settings.chunk_size_tokens, settings.chunk_overlap_tokens)
        ]
        if current != [c["chunk_hash"] for c in entry["chunks"]]:
            report["changed_chunks"].append(path.name)
    report["not_on_disk"] = sorted(
        d["name"] for h, d in seed_by_hash.items() if h not in present_hashes
    )
    return report
