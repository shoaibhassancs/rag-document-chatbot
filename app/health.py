"""Health report logic, kept apart from the route so other code can reuse it."""

from collections.abc import Callable

from app.config import Settings
from app.db import DbStatus


def build_health(settings: Settings, check_db: Callable[[str], DbStatus]) -> tuple[dict, int]:
    """Return (report, http_status). Reports only whether keys are set, never their values."""
    db = check_db(settings.database_url)
    keys = {
        "groq": settings.groq_api_key is not None,
        "voyage": settings.voyage_api_key is not None,
    }
    database = {"ok": db.ok, "pgvector": db.pgvector_version, "error": db.error}
    if not db.ok or db.pgvector_version is None:
        status, code = "down", 503
    elif not all(keys.values()):
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return {"status": status, "api": "ok", "database": database, "keys_configured": keys}, code
