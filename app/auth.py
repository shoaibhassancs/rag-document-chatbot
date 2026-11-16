"""X-API-Key check for the routes that change data (/ingest, DELETE /documents/{id})."""

import secrets

from fastapi import Depends, Header, HTTPException

from app.config import Settings, get_settings


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Fails closed: if the server has no APP_API_KEY, protected routes answer 503, not 200."""
    expected = settings.app_api_key.get_secret_value() if settings.app_api_key else ""
    if not expected:
        raise HTTPException(status_code=503, detail="APP_API_KEY is not configured on the server.")
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key.")
