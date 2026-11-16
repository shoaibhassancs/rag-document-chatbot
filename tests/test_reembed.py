import psycopg
import pytest

from app.ingest.embedder import EmbeddingAccountError
from app.services.ingestion import embed_pending
from app.services.reembed import ReembedRefused, plan, reembed
from app.startup import (
    META_DIMENSIONS,
    META_MODEL,
    META_REEMBED_TARGET,
    EmbeddingConfigMismatch,
    check_embedding_config,
    column_dimensions,
    get_meta,
    prepare_database,
)
from tests.fakes import FakeEmbedder
from tests.helpers import md, register, scalar, section


@pytest.fixture
def populated(settings, embedder):
    """Six chunks embedded with 'fake-model' at 8 dimensions."""
    prepare_database(settings)
    register(settings, md("a.md", "".join(section(f"S{i}", f"word{i}") for i in range(6))))
    embed_pending(settings.database_url, embedder)
    return settings


def check(settings, model, dims):
    with psycopg.connect(settings.database_url) as conn:
        check_embedding_config(conn, model, dims)


def test_plan_reports_what_would_change(populated):
    with psycopg.connect(populated.database_url) as conn:
        same = plan(conn, "fake-model", 8)
        new_model = plan(conn, "model-b", 8)
        new_size = plan(conn, "model-b", 16)
    assert (same["to_embed"], same["resizes_column"]) == (0, False)
    assert (new_model["to_embed"], new_model["resizes_column"]) == (6, False)
    assert (new_size["to_embed"], new_size["resizes_column"]) == (6, True)


def test_changing_the_model_rebuilds_every_vector_and_updates_the_metadata(populated):
    other = populated.model_copy(update={"embedding_model": "model-b"})
    with pytest.raises(EmbeddingConfigMismatch):
        prepare_database(other)  # the API would refuse to start
    new_embedder = FakeEmbedder(dimensions=8, model="model-b")
    summary = reembed(other.database_url, new_embedder)
    assert summary.chunks_embedded == 6 and new_embedder.requests == 3
    assert scalar(populated, "SELECT count(DISTINCT embedding_model) FROM chunks") == 1
    assert scalar(populated, "SELECT embedding_model FROM chunks LIMIT 1") == "model-b"
    assert scalar(populated, "SELECT status FROM documents") == "ready"
    prepare_database(other)  # now it starts
    with psycopg.connect(populated.database_url) as conn:
        meta = get_meta(conn)
    assert meta[META_MODEL] == "model-b" and META_REEMBED_TARGET not in meta


def test_changing_the_size_needs_confirmation_and_changes_nothing_without_it(populated):
    wide = FakeEmbedder(dimensions=16)
    with pytest.raises(ReembedRefused, match="--yes"):
        reembed(populated.database_url, wide)
    with psycopg.connect(populated.database_url) as conn:
        assert column_dimensions(conn) == 8 and META_REEMBED_TARGET not in get_meta(conn)
    assert scalar(populated, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 6


def test_changing_the_size_resizes_the_column_rebuilds_the_index_and_vectors(populated):
    wide = FakeEmbedder(dimensions=16)
    reembed(populated.database_url, wide, allow_resize=True)
    with psycopg.connect(populated.database_url) as conn:
        assert column_dimensions(conn) == 16
        assert conn.execute("SELECT DISTINCT vector_dims(embedding) FROM chunks").fetchall() == [
            (16,)
        ]
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_hnsw'"
        ).fetchone()
        assert index and "vector_cosine_ops" in index[0]
        meta = get_meta(conn)
    assert meta[META_DIMENSIONS] == "16"
    check(populated, "fake-model", 16)


def test_an_interrupted_reembed_blocks_startup_and_resumes_without_repeating_work(populated):
    flaky = FakeEmbedder(dimensions=8, model="model-b", fail_after_requests=1)
    with pytest.raises(EmbeddingAccountError):
        reembed(populated.database_url, flaky)
    other = populated.model_copy(update={"embedding_model": "model-b"})
    with pytest.raises(EmbeddingConfigMismatch, match="not finished"):
        prepare_database(other)
    done = scalar(populated, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    assert 0 < done < 6

    healthy = FakeEmbedder(dimensions=8, model="model-b")
    reembed(populated.database_url, healthy)
    assert len(healthy.texts_embedded) == 6 - done  # only what was still missing
    prepare_database(other)


def test_a_different_target_while_one_is_unfinished_is_refused(populated):
    flaky = FakeEmbedder(dimensions=8, model="model-b", fail_after_requests=1)
    with pytest.raises(EmbeddingAccountError):
        reembed(populated.database_url, flaky)
    with pytest.raises(ReembedRefused, match="unfinished"):
        reembed(populated.database_url, FakeEmbedder(dimensions=8, model="model-c"))


def test_the_dimension_is_validated_before_anything_changes(populated):
    with pytest.raises(ValueError):
        reembed(populated.database_url, FakeEmbedder(dimensions=5000), allow_resize=True)
    assert scalar(populated, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 6
