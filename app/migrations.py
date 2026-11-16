"""Plain-SQL migrations. Files in db/migrations run once, in name order, and are recorded in
schema_migrations. `{{EMBEDDING_DIMENSIONS}}` is replaced from config (validated as an integer),
so the vector size is defined in exactly one place."""

import re
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
MAX_INDEXABLE_DIMENSIONS = 2000  # pgvector HNSW and IVFFlat limit for the `vector` type
_LOCK_KEY = 727_001  # arbitrary; stops the API and a CLI from migrating at the same moment


def render(sql: str, dimensions: int) -> str:
    if not isinstance(dimensions, int) or isinstance(dimensions, bool):
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer")
    if not 1 <= dimensions <= MAX_INDEXABLE_DIMENSIONS:
        raise ValueError(
            f"EMBEDDING_DIMENSIONS must be between 1 and {MAX_INDEXABLE_DIMENSIONS} "
            "(pgvector cannot index more with the vector type)"
        )
    return re.sub(r"\{\{\s*EMBEDDING_DIMENSIONS\s*\}\}", str(dimensions), sql)


def migrate(database_url: str, dimensions: int, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the names applied in this call."""
    files = sorted(directory.glob("*.sql"))
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", [_LOCK_KEY])
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for path in files:
                if path.name in done:
                    continue
                sql = render(path.read_text(encoding="utf-8"), dimensions)
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", [path.name])
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", [_LOCK_KEY])
    return applied_now
