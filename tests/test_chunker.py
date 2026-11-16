import pytest

from app.ingest.chunker import chunk_document, pack_small_segments
from app.ingest.loaders import LoadedDocument, Segment, load_bytes
from app.ingest.textutil import estimate_tokens, sha256_hex


def doc_of(*segments: Segment, source_type="markdown") -> LoadedDocument:
    return LoadedDocument("d", source_type, list(segments))


def sentences(n: int) -> str:
    return " ".join(f"This is sentence number {i} of the long test paragraph." for i in range(n))


def test_chunk_sizes_respect_the_limit_and_indexes_are_sequential():
    chunks = chunk_document(doc_of(Segment(sentences(200))), size_tokens=100, overlap_tokens=20)
    assert len(chunks) > 3
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.token_estimate <= 100 + 2 for c in chunks)
    assert all(c.token_estimate == estimate_tokens(c.text) for c in chunks)


def test_consecutive_chunks_overlap():
    chunks = chunk_document(doc_of(Segment(sentences(120))), size_tokens=100, overlap_tokens=30)
    overlapping = sum(
        1 for a, b in zip(chunks, chunks[1:], strict=False) if a.text[-40:].split()[-1] in b.text
    )
    assert overlapping >= len(chunks) - 2


def test_pdf_pages_are_never_merged_and_keep_their_page_number():
    doc = doc_of(
        Segment("Page one text.", page=1), Segment("Page two text.", page=2), source_type="pdf"
    )
    chunks = chunk_document(doc, 600, 100)
    assert [(c.page, c.text) for c in chunks] == [(1, "Page one text."), (2, "Page two text.")]


def test_long_pdf_page_is_split_within_the_same_page():
    chunks = chunk_document(doc_of(Segment(sentences(150), page=4), source_type="pdf"), 100, 20)
    assert len(chunks) > 1 and {c.page for c in chunks} == {4}


def test_small_sections_are_packed_and_keep_the_first_heading():
    segments = [Segment("a" * 100, section="One"), Segment("b" * 100, section="Two")]
    packed = pack_small_segments(segments, min_chars=600, max_chars=2400)
    assert len(packed) == 1 and packed[0].section == "One"
    assert packed[0].text == "a" * 100 + "\n\n" + "b" * 100


def test_big_sections_are_not_packed_and_pdf_is_never_packed():
    big = [Segment("a" * 700, section="One"), Segment("b" * 100, section="Two")]
    assert len(pack_small_segments(big, 600, 2400)) == 2
    pages = [Segment("a" * 10, page=1), Segment("b" * 10, page=2)]
    assert len(pack_small_segments(pages, 600, 2400)) == 2


def test_section_is_kept_on_markdown_chunks():
    doc = load_bytes(
        "g.md", ("## Fees\n" + sentences(20) + "\n\n## Hours\n" + sentences(60)).encode()
    )
    chunks = chunk_document(doc, 150, 20)
    assert chunks[0].section == "Fees" and chunks[-1].section == "Hours"
    assert all(c.page is None for c in chunks)


def test_identical_text_gets_the_same_hash():
    a, b = (
        chunk_document(doc_of(Segment("Same text.")), 600, 100),
        chunk_document(doc_of(Segment("Same text.")), 600, 100),
    )
    assert a[0].chunk_hash == b[0].chunk_hash == sha256_hex("Same text.")


def test_blank_segments_produce_no_chunks():
    assert chunk_document(doc_of(Segment("   ")), 600, 100) == []


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, 100), (100, 150), (100, -1)])
def test_invalid_chunk_settings_are_rejected(size, overlap):
    with pytest.raises(ValueError):
        chunk_document(doc_of(Segment("x")), size, overlap)
