"""Helpers shared by the database tests."""

import psycopg

from app.config import Settings
from app.ingest.loaders import LoadedDocument, load_bytes
from app.services.ingestion import ItemResult, register_document


def md(name: str, text: str) -> LoadedDocument:
    return load_bytes(name, text.encode("utf-8"))


def section(title: str, seed: str, chars: int = 700) -> str:
    """A Markdown section long enough (over the packing threshold) to become its own chunk."""
    body = (f"{seed} " * (chars // (len(seed) + 1) + 1))[:chars]
    return f"## {title}\n{body}\n\n"


def register(settings: Settings, doc: LoadedDocument) -> ItemResult:
    with psycopg.connect(settings.database_url) as conn:
        result = register_document(
            conn,
            doc,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            embedding_model=settings.embedding_model,
        )
        conn.commit()
    return result


def scalar(settings: Settings, sql: str, params=None):
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(sql, params or []).fetchone()[0]


def rows(settings: Settings, sql: str, params=None) -> list[tuple]:
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(sql, params or []).fetchall()
