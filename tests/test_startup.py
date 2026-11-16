import psycopg
import pytest

from app.migrations import migrate, render
from app.services.ingestion import embed_pending
from app.startup import (
    META_DIMENSIONS,
    META_MODEL,
    META_REEMBED_TARGET,
    EmbeddingConfigMismatch,
    check_embedding_config,
    column_dimensions,
    get_meta,
    prepare_database,
    set_meta,
)
from tests.helpers import md, register, scalar, section

# ----------------------------------------------------------------------------- render (no DB)


def test_render_substitutes_the_dimension():
    assert render("vector({{EMBEDDING_DIMENSIONS}})", 1024) == "vector(1024)"
    assert render("vector({{ EMBEDDING_DIMENSIONS }})", 2000) == "vector(2000)"


@pytest.mark.parametrize("bad", [0, -1, 2001, "1024", "8); DROP TABLE x; --", None, 1.5, True])
def test_render_rejects_anything_but_an_integer_within_the_index_limit(bad):
    with pytest.raises(ValueError):
        render("vector({{EMBEDDING_DIMENSIONS}})", bad)


# ----------------------------------------------------------------------------- migrations


def test_migrate_creates_the_schema_with_the_configured_size_and_is_repeatable(settings):
    assert migrate(settings.database_url, 8) == ["001_init.sql", "002_memory_and_logs.sql"]
    assert migrate(settings.database_url, 8) == []  # nothing to do the second time
    with psycopg.connect(settings.database_url) as conn:
        assert column_dimensions(conn) == 8
        tables = {
            r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        assert {"documents", "chunks", "app_meta", "schema_migrations"} <= tables
        assert {"chat_sessions", "chat_turns", "chat_logs"} <= tables
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_hnsw'"
        ).fetchone()
        assert "hnsw" in index[0] and "vector_cosine_ops" in index[0]


def test_the_vector_size_comes_only_from_the_setting(settings):
    migrate(settings.database_url, 16)
    with psycopg.connect(settings.database_url) as conn:
        assert column_dimensions(conn) == 16


def test_the_database_enforces_the_vector_size(settings):
    migrate(settings.database_url, 8)
    register(settings, md("a.md", "Some text for the chunk."))
    with (
        psycopg.connect(settings.database_url) as conn,
        pytest.raises(psycopg.errors.DataException),
    ):
        conn.execute("UPDATE chunks SET embedding = '[1,2,3]'::vector")


# ----------------------------------------------------------------------------- startup check


def test_fresh_database_records_the_embedding_config(settings):
    prepare_database(settings)
    with psycopg.connect(settings.database_url) as conn:
        meta = get_meta(conn)
    assert meta[META_MODEL] == "fake-model" and meta[META_DIMENSIONS] == "8"


def test_matching_config_passes_repeatedly(settings, embedder):
    prepare_database(settings)
    register(settings, md("a.md", section("One", "alpha")))
    embed_pending(settings.database_url, embedder)
    prepare_database(settings)  # does not raise


def test_model_mismatch_with_recorded_metadata_fails_clearly(settings):
    prepare_database(settings)
    other = settings.model_copy(update={"embedding_model": "voyage-9"})
    with pytest.raises(EmbeddingConfigMismatch) as err:
        prepare_database(other)
    assert "fake-model" in str(err.value) and "voyage-9" in str(err.value)
    assert "scripts.reembed" in str(err.value)


def test_dimension_mismatch_fails_clearly(settings):
    prepare_database(settings)
    other = settings.model_copy(update={"embedding_dimensions": 16})
    with pytest.raises(EmbeddingConfigMismatch) as err:
        prepare_database(other)
    assert "vector(8)" in str(err.value) and "16" in str(err.value)


def test_chunks_embedded_with_another_model_fail_the_check_even_without_metadata(
    settings, embedder
):
    prepare_database(settings)
    register(settings, md("a.md", section("One", "alpha")))
    embed_pending(settings.database_url, embedder)
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("DELETE FROM app_meta")  # simulate a database from before metadata existed
        conn.commit()
        conn.execute("UPDATE chunks SET embedding_model = 'old-model'")
        conn.commit()
        with pytest.raises(EmbeddingConfigMismatch) as err:
            check_embedding_config(conn, "fake-model", 8)
    assert "old-model" in str(err.value)


def test_an_unfinished_reembed_blocks_startup(settings):
    prepare_database(settings)
    with psycopg.connect(settings.database_url) as conn:
        set_meta(conn, META_REEMBED_TARGET, "voyage-4|1024")
        conn.commit()
    with pytest.raises(EmbeddingConfigMismatch) as err:
        prepare_database(settings)
    assert "not finished" in str(err.value) and "scripts.reembed" in str(err.value)


def test_unembedded_chunks_do_not_count_as_a_mismatch(settings):
    prepare_database(settings)
    register(settings, md("a.md", section("One", "alpha")))
    assert scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 1
    prepare_database(settings.model_copy(update={"embedding_model": "fake-model"}))
