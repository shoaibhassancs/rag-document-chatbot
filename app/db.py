"""Database access. Milestone 1 only needs a connectivity and pgvector check."""

from dataclasses import dataclass

import psycopg


@dataclass
class DbStatus:
    ok: bool
    pgvector_version: str | None = None
    error: str | None = None  # exception class name only; never the connection string


def check_database(database_url: str, timeout_seconds: int = 3) -> DbStatus:
    try:
        with psycopg.connect(database_url, connect_timeout=timeout_seconds) as conn:
            conn.execute("SELECT 1")
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
    except Exception as exc:  # report the type only; the message can contain the DSN
        return DbStatus(ok=False, error=type(exc).__name__)
    return DbStatus(ok=True, pgvector_version=row[0] if row else None)
