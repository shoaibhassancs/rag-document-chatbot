"""Re-embed: rebuild every vector from the stored chunk text with the current embedding config.

Needed when EMBEDDING_MODEL or EMBEDDING_DIMENSIONS changes. Order of work:
  1. record the target in app_meta (so a crash is detectable and the API refuses to start);
  2. clear old vectors (changing the column size also drops and recreates the index);
  3. embed every chunk again, throttled and resumable (this is the normal embed_pending);
  4. recreate the index, store the new model/dimension, clear the marker.
Running it again after an interruption simply continues from step 3.
"""

from collections.abc import Callable

import psycopg

from app.db import connect
from app.ingest.embedder import Embedder
from app.migrations import render
from app.services.ingestion import EmbedSummary, Progress, embed_pending
from app.startup import (
    META_DIMENSIONS,
    META_MODEL,
    META_REEMBED_TARGET,
    column_dimensions,
    delete_meta,
    get_meta,
    set_meta,
)

_INDEX_NAME = "chunks_embedding_hnsw"
_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} ON chunks USING hnsw (embedding vector_cosine_ops)"
)


class ReembedRefused(RuntimeError):
    pass


def plan(conn: psycopg.Connection, model: str, dimensions: int) -> dict:
    """What a re-embed would do, without changing anything."""
    meta = get_meta(conn)
    column = column_dimensions(conn)
    total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    stale = conn.execute(
        "SELECT count(*) FROM chunks WHERE embedding_model IS DISTINCT FROM %s", [model]
    ).fetchone()[0]
    return {
        "chunks": total,
        "to_embed": total if column != dimensions else stale,
        "column_dimensions": column,
        "resizes_column": column != dimensions,
        "in_progress_target": meta.get(META_REEMBED_TARGET),
        "built_with": meta.get(META_MODEL),
    }


def reembed(
    database_url: str,
    embedder: Embedder,
    *,
    allow_resize: bool = False,
    progress: Callable[[Progress], None] | None = None,
) -> EmbedSummary:
    model, dimensions = embedder.model, embedder.dimensions
    target = f"{model}|{dimensions}"
    render("", dimensions)  # validates the dimension (integer, within the index limit)
    with connect(database_url) as conn:
        meta = get_meta(conn)
        in_progress = meta.get(META_REEMBED_TARGET)
        if in_progress and in_progress != target:
            raise ReembedRefused(
                f"An unfinished re-embed to '{in_progress}' exists but config now says "
                f"'{target}'. Restore the earlier config and finish it first."
            )
        if not in_progress:
            resize = column_dimensions(conn) != dimensions
            if resize and not allow_resize:
                raise ReembedRefused(
                    f"This changes the vector size to {dimensions} and clears every stored "
                    "vector first. Run again with --yes to confirm."
                )
            set_meta(conn, META_REEMBED_TARGET, target)
            if resize:
                conn.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
                conn.execute("UPDATE chunks SET embedding = NULL, embedding_model = NULL")
                conn.execute(
                    "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector("
                    + str(int(dimensions))
                    + ") USING NULL"
                )
            else:
                conn.execute(
                    "UPDATE chunks SET embedding = NULL, embedding_model = NULL "
                    "WHERE embedding_model IS DISTINCT FROM %s",
                    [model],
                )
            conn.execute("UPDATE documents SET status = 'ingesting', error = NULL")
            conn.commit()

    summary = embed_pending(database_url, embedder, progress=progress)

    with connect(database_url) as conn:
        pending = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        if pending:
            raise ReembedRefused(f"{pending} chunks still have no vector; run the command again.")
        conn.execute(_INDEX_SQL)
        set_meta(conn, META_MODEL, model)
        set_meta(conn, META_DIMENSIONS, str(dimensions))
        delete_meta(conn, META_REEMBED_TARGET)
        conn.commit()
    return summary
