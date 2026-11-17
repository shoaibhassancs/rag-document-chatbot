"""Shared test fixtures.

- Network guard: any attempt to connect to a non-local address fails the test, so a mistake
  can never reach Groq or Voyage.
- Database tests run against a throwaway database called `ragdb_test` on the Compose Postgres
  and are skipped (with a clear reason) when it is not reachable. The real `ragdb` is never touched.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import psycopg
import pytest

from app.config import Settings, get_settings
from tests.fakes import FakeEmbedder

TEST_DB_NAME = "ragdb_test"
TEST_DIMENSIONS = 8


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise AssertionError(f"Test tried to connect to a non-local address: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def _admin_and_test_urls() -> tuple[str, str]:
    base = get_settings().database_url
    parts = urlsplit(base)
    admin = parts._replace(path="/postgres").geturl()
    test = parts._replace(path=f"/{TEST_DB_NAME}").geturl()
    return admin, test


@pytest.fixture
def db_url():
    """A freshly emptied test database (no tables). Skips if Postgres is not reachable."""
    admin_url, test_url = _admin_and_test_urls()
    assert test_url.rsplit("/", 1)[-1].endswith("_test")  # never wipe a real database
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", [TEST_DB_NAME]
            ).fetchone()
            if not exists:
                admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"Postgres is not reachable ({type(exc).__name__}); run `docker compose up -d db`"
        )
    with psycopg.connect(test_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")  # also fine after an interrupted run
        conn.execute("CREATE SCHEMA public")
    return test_url


@pytest.fixture
def settings(db_url) -> Settings:
    return Settings(
        _env_file=None,
        database_url=db_url,
        app_api_key="test-app-key",
        voyage_api_key="fake-voyage-key",
        groq_api_key="fake-groq-key",
        embedding_model="fake-model",
        embedding_dimensions=TEST_DIMENSIONS,
        voyage_max_batch_tokens=400,
        clarify_enabled=False,  # tests that exercise the clarify step switch it on themselves
    )


@pytest.fixture
def embedder(settings) -> FakeEmbedder:
    return FakeEmbedder(dimensions=settings.embedding_dimensions, max_batch_tokens=400)
