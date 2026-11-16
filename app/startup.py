"""Startup preparation: run migrations, then make sure the embedding config and the database
agree. A mismatch (different model or vector size, or an unfinished re-embed) stops the app,
because searching vectors from one model with a query from another returns nonsense silently."""

import logging

import psycopg

from app.config import Settings
from app.migrations import migrate

log = logging.getLogger(__name__)

META_MODEL = "embedding_model"
META_DIMENSIONS = "embedding_dimensions"
META_REEMBED_TARGET = "reembed_target"  # "model|dimensions" while a re-embed is unfinished


class EmbeddingConfigMismatch(RuntimeError):
    pass


def get_meta(conn: psycopg.Connection) -> dict[str, str]:
    return {k: v for k, v in conn.execute("SELECT key, value FROM app_meta")}


def set_meta(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        [key, value],
    )


def delete_meta(conn: psycopg.Connection, key: str) -> None:
    conn.execute("DELETE FROM app_meta WHERE key = %s", [key])


def column_dimensions(conn: psycopg.Connection) -> int | None:
    """Size of chunks.embedding as the database really has it (for pgvector, atttypmod = size)."""
    row = conn.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding' AND NOT attisdropped"
    ).fetchone()
    return row[0] if row and row[0] > 0 else None


def check_embedding_config(conn: psycopg.Connection, model: str, dimensions: int) -> None:
    meta = get_meta(conn)
    target = meta.get(META_REEMBED_TARGET)
    if target:
        raise EmbeddingConfigMismatch(
            f"A re-embed to '{target}' was started and not finished. "
            "Run `python -m scripts.reembed` to finish it."
        )
    problems: list[str] = []
    column = column_dimensions(conn)
    if column != dimensions:
        problems.append(f"the database column is vector({column}) but config says {dimensions}")
    if META_MODEL in meta and meta[META_MODEL] != model:
        problems.append(
            f"the database was built with '{meta[META_MODEL]}' but config says '{model}'"
        )
    if META_DIMENSIONS in meta and meta[META_DIMENSIONS] != str(dimensions):
        problems.append(
            f"the database has {meta[META_DIMENSIONS]} dimensions but config says {dimensions}"
        )
    stored = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT embedding_model FROM chunks WHERE embedding IS NOT NULL"
        )
    }
    other = sorted(m for m in stored if m != model)
    if other:
        problems.append(f"chunks are embedded with {other} but config says '{model}'")
    if problems:
        raise EmbeddingConfigMismatch(
            "Embedding config and database disagree: "
            + "; ".join(problems)
            + ". Either restore the old EMBEDDING_MODEL / EMBEDDING_DIMENSIONS, "
            "or run `python -m scripts.reembed` to rebuild every vector with the new settings."
        )
    if META_MODEL not in meta:
        set_meta(conn, META_MODEL, model)
        set_meta(conn, META_DIMENSIONS, str(dimensions))
        conn.commit()


def prepare_database(settings: Settings, *, check: bool = True) -> list[str]:
    """Run migrations; unless check=False (the re-embed command), verify embedding settings."""
    applied = migrate(settings.database_url, settings.embedding_dimensions)
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))
    if check:
        with psycopg.connect(settings.database_url) as conn:
            check_embedding_config(conn, settings.embedding_model, settings.embedding_dimensions)
    return applied
