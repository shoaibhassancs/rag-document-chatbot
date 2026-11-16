"""The command-line tools and the seed file."""

import json
from pathlib import Path

import psycopg
import pytest

from app.config import Settings
from app.services.ingestion import embed_pending
from app.services.seed import (
    SeedError,
    build_seed,
    load_seed,
    read_seed,
    stale_report,
    write_seed,
)
from app.startup import prepare_database
from scripts import ingest_folder, reembed, seed
from scripts.ingest_folder import collect_files, format_duration
from tests.fakes import FakeEmbedder
from tests.helpers import md, register, scalar, section

ROOT = Path(__file__).resolve().parent.parent


def write(folder: Path, name: str, content: str | bytes) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


@pytest.fixture
def docs_folder(tmp_path):
    folder = tmp_path / "docs"
    write(folder, "b.md", section("B", "bbb") + section("B2", "bbb2"))
    write(folder, "a.md", section("A", "aaa"))
    write(folder, "a-copy.md", section("A", "aaa").replace("\n", "  \n"))
    write(folder, "notes/n.txt", "Some plain notes.")
    write(folder, ".hidden/secret.md", "# hidden")
    write(folder, "image.png", b"\x89PNG")
    return folder


def patch_cli(monkeypatch, module, settings, embedder):
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "build_embedder", lambda s: embedder)


# ----------------------------------------------------------------------------- ingest_folder


def test_collect_files_is_recursive_sorted_and_skips_hidden_and_unsupported(docs_folder):
    names = [str(p.relative_to(docs_folder)) for p in collect_files(docs_folder)]
    assert names == ["a.md", "a-copy.md", "b.md", "notes/n.txt"]  # "a" sorts before "a-copy"


def test_format_duration():
    assert (format_duration(5), format_duration(65), format_duration(180)) == (
        "5s",
        "1m05s",
        "3m00s",
    )


def test_dry_run_estimates_without_touching_voyage_or_the_database(monkeypatch, capsys):
    monkeypatch.setattr(ingest_folder, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(ingest_folder, "build_embedder", lambda s: pytest.fail("must not embed"))
    monkeypatch.setattr(
        ingest_folder, "prepare_database", lambda s: pytest.fail("must not use the DB")
    )
    assert ingest_folder.main([str(ROOT / "data/sample_docs"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "documents to ingest : 29" in out
    assert "faq-printed-copy.md (same text as faq.md)" in out
    assert "estimated tokens" in out and "batches (requests)" in out and "estimated time" in out


def test_the_sample_set_is_30_files_29_unique_and_all_loadable(monkeypatch):
    from app.ingest.loaders import load_bytes

    files = collect_files(ROOT / "data/sample_docs")
    assert len(files) == 30
    docs = [load_bytes(p.name, p.read_bytes()) for p in files]
    assert len({d.normalized_text for d in docs}) == 29
    assert {d.source_type for d in docs} == {"pdf", "markdown", "text", "html"}


def test_folder_ingest_end_to_end_then_a_repeat_costs_nothing(
    settings, embedder, docs_folder, monkeypatch, capsys
):
    patch_cli(monkeypatch, ingest_folder, settings, embedder)
    assert ingest_folder.main([str(docs_folder)]) == 0
    out = capsys.readouterr().out
    assert "a-copy.md: duplicate (same text as a.md)" in out
    assert "chunks embedded (new vectors) : 4" in out and "requests to Voyage" in out
    assert scalar(settings, "SELECT count(*) FROM documents WHERE status = 'ready'") == 3

    requests_before = embedder.requests
    assert ingest_folder.main([str(docs_folder)]) == 0
    assert embedder.requests == requests_before
    assert "unchanged" in capsys.readouterr().out


def test_unloadable_files_give_exit_code_1_but_good_files_are_still_ingested(
    settings, embedder, docs_folder, monkeypatch, capsys
):
    write(docs_folder, "broken.pdf", b"%PDF-1.4 nope")
    patch_cli(monkeypatch, ingest_folder, settings, embedder)
    assert ingest_folder.main([str(docs_folder)]) == 1
    assert "[FAIL] broken.pdf: unreadable_pdf" in capsys.readouterr().out
    assert scalar(settings, "SELECT count(*) FROM documents WHERE status = 'ready'") == 3


def test_a_quota_error_stops_with_exit_code_2_and_a_rerun_resumes(
    settings, docs_folder, monkeypatch, capsys
):
    flaky = FakeEmbedder(dimensions=8, fail_after_requests=1)
    patch_cli(monkeypatch, ingest_folder, settings, flaky)
    assert ingest_folder.main([str(docs_folder)]) == 2
    out = capsys.readouterr().out
    assert "STOP:" in out and "tell the owner" in out.lower()
    saved = scalar(settings, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    assert saved > 0

    healthy = FakeEmbedder(dimensions=8)
    patch_cli(monkeypatch, ingest_folder, settings, healthy)
    assert ingest_folder.main([str(docs_folder)]) == 0
    assert len(healthy.texts_embedded) == 4 - saved
    assert scalar(settings, "SELECT count(*) FROM documents WHERE status = 'ready'") == 3


def test_missing_embedding_key_stops_before_touching_anything(
    settings, docs_folder, monkeypatch, capsys
):
    monkeypatch.setattr(
        ingest_folder, "get_settings", lambda: settings.model_copy(update={"voyage_api_key": None})
    )
    assert ingest_folder.main([str(docs_folder)]) == 1
    assert "VOYAGE_API_KEY" in capsys.readouterr().out


# ----------------------------------------------------------------------------- reembed CLI


def test_reembed_cli_dry_run_refusal_and_success(settings, embedder, monkeypatch, capsys):
    prepare_database(settings)
    register(settings, md("a.md", section("A", "aaa") + section("B", "bbb")))
    embed_pending(settings.database_url, embedder)

    new_model = settings.model_copy(update={"embedding_model": "model-b"})
    monkeypatch.setattr(reembed, "get_settings", lambda: new_model)
    monkeypatch.setattr(
        reembed, "build_embedder", lambda s: FakeEmbedder(dimensions=8, model="model-b")
    )
    assert reembed.main(["--dry-run"]) == 0
    assert "to re-embed: 2" in capsys.readouterr().out
    assert (
        scalar(settings, "SELECT embedding_model FROM chunks LIMIT 1") == "fake-model"
    )  # untouched

    wide = settings.model_copy(update={"embedding_dimensions": 16})
    monkeypatch.setattr(reembed, "get_settings", lambda: wide)
    monkeypatch.setattr(reembed, "build_embedder", lambda s: FakeEmbedder(dimensions=16))
    assert reembed.main([]) == 1
    assert "REFUSED" in capsys.readouterr().out

    monkeypatch.setattr(reembed, "get_settings", lambda: new_model)
    monkeypatch.setattr(
        reembed, "build_embedder", lambda s: FakeEmbedder(dimensions=8, model="model-b")
    )
    assert reembed.main([]) == 0
    assert "re-embed complete" in capsys.readouterr().out
    assert scalar(settings, "SELECT embedding_model FROM chunks LIMIT 1") == "model-b"


def test_reembed_cli_reports_quota_stop_with_exit_code_2(settings, embedder, monkeypatch, capsys):
    prepare_database(settings)
    register(settings, md("a.md", section("A", "aaa")))
    embed_pending(settings.database_url, embedder)
    new_model = settings.model_copy(update={"embedding_model": "model-b"})
    monkeypatch.setattr(reembed, "get_settings", lambda: new_model)
    monkeypatch.setattr(
        reembed,
        "build_embedder",
        lambda s: FakeEmbedder(dimensions=8, model="model-b", fail_after_requests=0),
    )
    assert reembed.main([]) == 2
    assert "STOP" in capsys.readouterr().out


# ----------------------------------------------------------------------------- seed file


@pytest.fixture
def ingested(settings, embedder):
    prepare_database(settings)
    register(settings, md("a.md", section("A", "aaa") + section("B", "bbb")))
    register(settings, md("n.txt", "Plain notes about parking and hours."))
    embed_pending(settings.database_url, embedder)
    return settings


def reset_database(settings):
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    prepare_database(settings)


def test_seed_round_trip_restores_documents_chunks_and_vectors(ingested, tmp_path):
    settings = ingested
    with psycopg.connect(settings.database_url) as conn:
        built = build_seed(conn, settings)
    path = tmp_path / "seed.json.gz"
    size = write_seed(built, path)
    assert size > 0 and path.read_bytes()[:2] == b"\x1f\x8b"  # gzip

    original = {r[0]: r[1] for r in psycopg.connect(settings.database_url).execute(
        "SELECT chunk_hash, embedding::text FROM chunks")}  # fmt: skip
    reset_database(settings)
    with psycopg.connect(settings.database_url) as conn:
        counts = load_seed(conn, read_seed(path), settings)
        conn.commit()
    assert counts == {"loaded": 2, "updated": 0, "skipped": 0}
    assert scalar(settings, "SELECT count(*) FROM documents WHERE status = 'ready'") == 2
    restored = {r[0]: r[1] for r in psycopg.connect(settings.database_url).execute(
        "SELECT chunk_hash, embedding::text FROM chunks")}  # fmt: skip
    assert restored.keys() == original.keys()
    assert all(
        all(
            abs(a - b) < 1e-5
            for a, b in zip(json.loads(restored[h]), json.loads(original[h]), strict=True)
        )
        for h in original
    )
    prepare_database(settings)  # the startup check accepts the loaded data

    with psycopg.connect(settings.database_url) as conn:  # loading twice skips what exists
        assert load_seed(conn, read_seed(path), settings)["skipped"] == 2


def test_seed_refuses_a_different_model_or_size(ingested, tmp_path):
    with psycopg.connect(ingested.database_url) as conn:
        built = build_seed(conn, ingested)
    with psycopg.connect(ingested.database_url) as conn:
        with pytest.raises(SeedError, match="model-b"):
            load_seed(conn, built, ingested.model_copy(update={"embedding_model": "model-b"}))
        with pytest.raises(SeedError, match="dimension"):
            load_seed(conn, built, ingested.model_copy(update={"embedding_dimensions": 16}))


def test_seed_needs_finished_documents_and_a_readable_file(settings, tmp_path):
    prepare_database(settings)
    register(settings, md("a.md", section("A", "aaa")))  # still ingesting, so not exportable
    with psycopg.connect(settings.database_url) as conn, pytest.raises(SeedError, match="no ready"):
        build_seed(conn, settings)
    (tmp_path / "bad.gz").write_bytes(b"not gzip")
    with pytest.raises(SeedError):
        read_seed(tmp_path / "bad.gz")


def test_stale_report_detects_changed_new_removed_files_and_chunk_settings(ingested, tmp_path):
    with psycopg.connect(ingested.database_url) as conn:
        built = build_seed(conn, ingested)
    docs = tmp_path / "docs"
    write(docs, "a.md", section("A", "aaa") + section("B", "bbb"))
    write(docs, "a-printed.md", section("A", "aaa").replace("\n", "  \n") + section("B", "bbb"))
    write(docs, "n.txt", "Plain notes about parking and hours.")
    clean = stale_report(built, docs, ingested)
    assert not any(clean.values()), clean

    write(docs, "n.txt", "Plain notes about parking and hours. Changed!")
    write(docs, "new.md", "# A brand new document")
    changed = stale_report(built, docs, ingested)
    assert sorted(changed["missing_from_seed"]) == ["n.txt", "new.md"]
    assert changed["not_on_disk"] == ["n.txt"]

    other = ingested.model_copy(update={"chunk_size_tokens": 300})
    assert stale_report(built, tmp_path / "docs", other)["changed_chunks"]


def test_seed_cli_build_load_check(ingested, monkeypatch, tmp_path, capsys):
    docs = tmp_path / "docs"
    write(docs, "a.md", section("A", "aaa") + section("B", "bbb"))
    write(docs, "n.txt", "Plain notes about parking and hours.")
    monkeypatch.setattr(seed, "get_settings", lambda: ingested)
    file = tmp_path / "seed.json.gz"
    assert seed.main(["build", "--file", str(file)]) == 0
    assert "2 documents" in capsys.readouterr().out
    assert seed.main(["check", "--file", str(file), "--docs", str(docs)]) == 0
    write(docs, "n.txt", "Changed notes")
    assert seed.main(["check", "--file", str(file), "--docs", str(docs)]) == 1
    assert "STALE" in capsys.readouterr().out
    reset_database(ingested)
    assert seed.main(["load", "--file", str(file)]) == 0
    assert scalar(ingested, "SELECT count(*) FROM documents") == 2
    assert seed.main(["load", "--file", str(tmp_path / "missing.gz")]) == 1


def test_the_committed_seed_file_matches_the_sample_documents_if_it_exists():
    path = ROOT / "data/seed/sample_docs.seed.json.gz"
    if not path.exists():
        pytest.skip("No seed file built yet (it is built after the first real ingestion).")
    report = stale_report(read_seed(path), ROOT / "data/sample_docs", Settings(_env_file=None))
    assert not any(report.values()), (
        f"The seed is stale; rebuild it with scripts.seed build: {report}"
    )
