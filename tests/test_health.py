import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import DbStatus
from app.main import app, get_db_checker


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GROQ_API_KEY", "VOYAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    app.dependency_overrides.clear()


def call_health(db: DbStatus, **settings_kwargs):
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **settings_kwargs)
    app.dependency_overrides[get_db_checker] = lambda: lambda url: db
    return TestClient(app).get("/health")


def test_ok_when_db_pgvector_and_keys_present():
    r = call_health(
        DbStatus(ok=True, pgvector_version="0.8.7"), groq_api_key="a", voyage_api_key="b"
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["keys_configured"] == {"groq": True, "voyage": True}


def test_degraded_but_200_when_a_key_is_missing():
    r = call_health(DbStatus(ok=True, pgvector_version="0.8.7"), groq_api_key="a")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["keys_configured"]["voyage"] is False


def test_503_when_database_is_down():
    r = call_health(DbStatus(ok=False, error="OperationalError"))
    assert r.status_code == 503
    assert r.json()["status"] == "down"
    assert r.json()["database"]["error"] == "OperationalError"


def test_503_when_pgvector_extension_is_missing():
    r = call_health(DbStatus(ok=True, pgvector_version=None), groq_api_key="a", voyage_api_key="b")
    assert r.status_code == 503


def test_response_never_contains_key_values_or_connection_string():
    r = call_health(
        DbStatus(ok=True, pgvector_version="0.8.7"),
        groq_api_key="fake-groq-secret-1",
        voyage_api_key="fake-voyage-secret-2",
        database_url="postgresql://u:pw-secret-3@h/db",
    )
    assert "secret" not in r.text
