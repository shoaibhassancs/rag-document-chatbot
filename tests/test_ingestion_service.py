import psycopg
import pytest

from app.ingest.embedder import EmbeddingAccountError
from app.services.documents import delete_document, list_documents
from app.services.ingestion import embed_pending, estimate_run, fail_pending
from app.startup import prepare_database
from tests.fakes import FakeEmbedder
from tests.helpers import md, register, rows, scalar, section


@pytest.fixture(autouse=True)
def migrated(settings):
    prepare_database(settings)


def three_sections(last_seed="gamma"):
    return md(
        "guide.md", section("One", "alpha") + section("Two", "beta") + section("Three", last_seed)
    )


# ----------------------------------------------------------------------------- registering


def test_registering_stores_chunks_without_vectors_and_marks_ingesting(settings):
    result = register(settings, three_sections())
    assert (result.status, result.action, result.chunks) == ("queued", "created", 3)
    assert scalar(settings, "SELECT status FROM documents") == "ingesting"
    assert scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 3
    assert [r[0] for r in rows(settings, "SELECT section FROM chunks ORDER BY chunk_index")] == [
        "One", "Two", "Three",
    ]  # fmt: skip
    assert scalar(settings, "SELECT chunk_total FROM documents") == 3


def test_same_name_same_text_is_unchanged_once_ready(settings, embedder):
    register(settings, three_sections())
    embed_pending(settings.database_url, embedder)
    again = register(settings, three_sections())
    assert again.status == "unchanged"
    assert scalar(settings, "SELECT count(*) FROM documents") == 1


def test_a_copy_under_another_name_is_a_duplicate_and_stores_nothing(settings):
    original = section("One", "alpha") + section("Two", "beta")
    register(settings, md("guide.md", original))
    copy = md("guide-copy.md", original.replace("\n", "  \n\n"))  # same text, different spacing
    result = register(settings, copy)
    assert (result.status, result.duplicate_of) == ("duplicate", "guide.md")
    assert scalar(settings, "SELECT count(*) FROM documents") == 1


def test_changed_text_updates_and_keeps_vectors_of_unchanged_chunks(settings, embedder):
    register(settings, three_sections())
    embed_pending(settings.database_url, embedder)
    assert len(embedder.texts_embedded) == 3

    result = register(settings, three_sections(last_seed="delta"))
    assert (result.status, result.action) == ("queued", "updated")
    # Two chunks are identical, so they kept their vectors; only the changed one is pending.
    assert scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 1
    assert scalar(settings, "SELECT count(*) FROM documents") == 1

    embed_pending(settings.database_url, embedder)
    assert len(embedder.texts_embedded) == 4  # exactly one new text was embedded
    assert scalar(settings, "SELECT status FROM documents") == "ready"


def test_a_heading_only_document_is_stored_as_one_small_chunk(settings):
    assert register(settings, md("a.md", "# Title only")).status == "queued"
    assert scalar(settings, "SELECT chunk_total FROM documents") == 1


# ----------------------------------------------------------------------------- embedding


def test_embed_pending_fills_vectors_in_token_sized_batches_and_marks_ready(settings, embedder):
    register(settings, md("a.md", "".join(section(f"S{i}", f"word{i}") for i in range(6))))
    seen = []
    summary = embed_pending(settings.database_url, embedder, progress=seen.append)
    assert summary.chunks_embedded == 6 and summary.chunks_reused == 0
    assert (
        summary.requests == embedder.requests == summary.batches == 3
    )  # ~175 tokens each, 2 fit in 400
    assert [p.batch for p in seen] == [1, 2, 3] and seen[-1].embedded == seen[-1].total == 6
    assert scalar(settings, "SELECT status FROM documents") == "ready"
    assert scalar(settings, "SELECT count(*) FROM chunks WHERE embedding_model = 'fake-model'") == 6
    assert scalar(settings, "SELECT vector_dims(embedding) FROM chunks LIMIT 1") == 8
    assert set(embedder.input_types) == {"document"}


def test_nothing_pending_means_no_requests(settings, embedder):
    summary = embed_pending(settings.database_url, embedder)
    assert summary.requests == 0 and summary.chunks_embedded == 0 and embedder.requests == 0


def test_identical_text_in_two_documents_is_embedded_once(settings, embedder):
    shared = section("Shared", "same words here")
    register(settings, md("a.md", section("A", "aaa") + shared))
    register(settings, md("b.md", section("B", "bbb") + shared))
    summary = embed_pending(settings.database_url, embedder)
    assert len(embedder.texts_embedded) == 3  # aaa, bbb, and one shared text
    assert summary.chunks_embedded == 4  # four chunk rows got a vector
    assert scalar(settings, "SELECT count(*) FROM documents WHERE status = 'ready'") == 2


def test_a_text_repeated_inside_one_document_is_embedded_once(settings, embedder):
    repeated = section("Same", "repeat me")
    register(
        settings, md("a.md", repeated.replace("Same", "Same") + repeated.replace("Same", "Same2"))
    )
    embed_pending(settings.database_url, embedder)
    assert scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 2


def test_chunks_of_an_already_ingested_text_are_reused_without_a_request(settings, embedder):
    register(settings, md("a.md", section("Only", "reuse")))
    embed_pending(settings.database_url, embedder)
    before = embedder.requests
    register(settings, md("b.md", section("Only", "reuse") + section("Extra", "other")))
    summary = embed_pending(settings.database_url, embedder)
    assert summary.chunks_reused == 1 and summary.chunks_embedded == 1
    assert embedder.requests == before + 1


def test_a_failure_marks_documents_failed_keeps_progress_and_resumes_cheaply(settings):
    doc = md("a.md", "".join(section(f"S{i}", f"word{i}") for i in range(6)))
    register(settings, doc)
    flaky = FakeEmbedder(dimensions=8, fail_after_requests=1)
    with pytest.raises(EmbeddingAccountError):
        embed_pending(settings.database_url, flaky)
    assert scalar(settings, "SELECT status FROM documents") == "failed"
    assert "quota" in scalar(settings, "SELECT error FROM documents")
    done = scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    assert 0 < done < 6  # the first batch was saved

    assert register(settings, doc).action == "resumed"  # same text, failed before: resumes
    healthy = FakeEmbedder(dimensions=8)
    embed_pending(settings.database_url, healthy)
    assert len(healthy.texts_embedded) == 6 - done  # only the missing chunks cost anything
    assert scalar(settings, "SELECT status FROM documents") == "ready"
    assert scalar(settings, "SELECT error FROM documents") is None


def test_unexpected_errors_are_recorded_by_type_only(settings):
    register(settings, three_sections())

    class Exploding(FakeEmbedder):
        def embed(self, texts, input_type):
            raise RuntimeError("secret detail postgresql://user:pw@host")

    with pytest.raises(RuntimeError):
        embed_pending(settings.database_url, Exploding(dimensions=8))
    assert scalar(settings, "SELECT error FROM documents") == "RuntimeError"


def test_documents_deleted_while_embedding_are_skipped(settings):
    register(settings, md("small.md", section("A", "aaa") + section("B", "bbb")))
    register(settings, md("big.md", "".join(section(f"S{i}", f"word{i}") for i in range(8))))

    class DeletesBig(FakeEmbedder):
        def embed(self, texts, input_type):
            if self.requests == 0:
                with psycopg.connect(settings.database_url) as conn:
                    conn.execute("DELETE FROM documents WHERE name = 'big.md'")
                    conn.commit()
            return super().embed(texts, input_type)

    embedder = DeletesBig(dimensions=8, max_batch_tokens=400)
    embed_pending(settings.database_url, embedder)
    assert len(embedder.texts_embedded) <= 4  # not all ten: big.md's remaining chunks were skipped
    assert scalar(settings, "SELECT count(*) FROM documents") == 1
    assert scalar(settings, "SELECT status FROM documents") == "ready"


def test_fail_pending_marks_only_unfinished_documents(settings, embedder):
    register(settings, md("done.md", section("A", "aaa")))
    embed_pending(settings.database_url, embedder)
    register(settings, md("todo.md", section("B", "bbb")))
    fail_pending(settings.database_url, "VOYAGE_API_KEY is not set.")
    statuses = dict(rows(settings, "SELECT name, status FROM documents"))
    assert statuses == {"done.md": "ready", "todo.md": "failed"}


# ----------------------------------------------------------------------------- list and delete


def test_list_documents_reports_progress_and_metadata(settings, embedder):
    register(settings, three_sections())
    with psycopg.connect(settings.database_url) as conn:
        before = list_documents(conn)[0]
    assert (before["status"], before["chunks"], before["chunks_done"]) == ("ingesting", 3, 0)
    embed_pending(settings.database_url, embedder)
    with psycopg.connect(settings.database_url) as conn:
        after = list_documents(conn)[0]
    assert (after["status"], after["chunks_done"], after["embedding_model"]) == (
        "ready",
        3,
        "fake-model",
    )
    assert len(after["content_hash"]) == 12 and after["source_type"] == "markdown"


def test_delete_removes_the_document_and_its_chunks_but_not_while_ingesting(settings, embedder):
    register(settings, three_sections())
    with psycopg.connect(settings.database_url) as conn:
        doc_id = list_documents(conn)[0]["id"]
        assert delete_document(conn, doc_id) == "ingesting"
    embed_pending(settings.database_url, embedder)
    with psycopg.connect(settings.database_url) as conn:
        assert delete_document(conn, doc_id) == "deleted"
        conn.commit()
        assert delete_document(conn, doc_id) == "not_found"
    assert scalar(settings, "SELECT count(*) FROM chunks") == 0


# ----------------------------------------------------------------------------- dry-run estimate


def test_estimate_run_counts_duplicates_unique_texts_and_batches():
    docs = [
        md("a.md", section("A", "aaa") + section("B", "bbb")),
        md("a-copy.md", section("A", "aaa") + section("B", "bbb")),
        md("c.md", section("C", "ccc")),
    ]
    est = estimate_run(
        docs, chunk_size_tokens=600, chunk_overlap_tokens=100, safety_factor=1.0,
        max_batch_tokens=400, requests_per_minute=3, tokens_per_minute=10_000,
    )  # fmt: skip
    assert est.documents == 2 and est.duplicates == [("a-copy.md", "a.md")]
    assert est.chunks == est.unique_chunks == 3 and est.batches == 2
    assert 3 * 170 <= est.estimated_tokens <= 3 * 180  # about 176 tokens per section chunk
    assert est.estimated_seconds == 0  # two requests fit inside the first minute
