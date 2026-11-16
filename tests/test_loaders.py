from io import BytesIO
from pathlib import Path

import httpx
import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from app.ingest.loaders import (
    LoadError,
    check_url_allowed,
    load_bytes,
    load_url,
    strip_repeated_page_lines,
)
from app.ingest.textutil import clean_text, normalize_for_hash, sha256_hex

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small PDF: each page is a list of text lines (header and footer included by the caller)."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)
    for lines in pages:
        y = 740
        for line in lines:
            pdf.drawString(72, y, line)
            y -= 18
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# ----------------------------------------------------------------------------- text helpers


def test_clean_text_keeps_paragraphs_and_trims_spaces():
    assert clean_text("a  b \r\n\r\n\r\n\r\n c\t d \n") == "a b\n\nc d"


def test_hash_ignores_whitespace_differences():
    a = "Hello   world.\n\nSecond paragraph."
    b = "Hello world.   Second\nparagraph."
    assert sha256_hex(normalize_for_hash(a)) == sha256_hex(normalize_for_hash(b))
    assert sha256_hex(normalize_for_hash(a)) != sha256_hex(normalize_for_hash(a + "!"))


# ----------------------------------------------------------------------------- Markdown / TXT


def test_markdown_sections_and_none_page():
    doc = load_bytes(
        "g.md", b"Intro text.\n\n# Title\nBody one.\n\n## Fees\nFee text.\n\n## Hours\nHour text."
    )
    assert doc.source_type == "markdown"
    assert [s.section for s in doc.segments] == [None, "Title", "Fees", "Hours"]
    assert all(s.page is None for s in doc.segments)
    assert "## Fees" in doc.segments[2].text  # the heading line stays in the text


def test_markdown_heading_without_body_is_carried_into_next_section():
    doc = load_bytes("g.md", b"# Guide\n## Part\nReal text here.\n")
    assert len(doc.segments) == 1
    assert doc.segments[0].section == "Part"
    assert doc.segments[0].text.startswith("# Guide")


def test_markdown_deeper_headings_stay_inside_their_section():
    doc = load_bytes("g.md", b"## Q&A\n### One\nA1\n### Two\nA2\n")
    assert len(doc.segments) == 1
    assert "### Two" in doc.segments[0].text


def test_markdown_ignores_hash_lines_inside_code_fences():
    doc = load_bytes("g.md", b"## Real\n```\n# not a heading\n```\nafter\n")
    assert [s.section for s in doc.segments] == ["Real"]


def test_plain_text_is_one_segment():
    doc = load_bytes("n.txt", b"Line one\n\n\n\nLine two")
    assert doc.source_type == "text"
    assert doc.segments[0].text == "Line one\n\nLine two"


def test_utf8_bom_is_removed():
    assert load_bytes("n.txt", b"\xef\xbb\xbfHello").segments[0].text == "Hello"


def test_not_utf8_is_rejected():
    with pytest.raises(LoadError) as err:
        load_bytes("n.txt", b"\xff\xfe\x00bad")
    assert err.value.code == "not_text"


@pytest.mark.parametrize("name", ["data.csv", "photo.png", "noextension", "x.docx"])
def test_unsupported_types_are_rejected(name):
    with pytest.raises(LoadError) as err:
        load_bytes(name, b"data")
    assert err.value.code == "unsupported_type"


def test_empty_and_blank_files_are_rejected():
    for data in (b"", b"   \n\n  "):
        with pytest.raises(LoadError) as err:
            load_bytes("a.txt", data)
        assert err.value.code == "empty"


# ----------------------------------------------------------------------------- HTML

HTML = """<html><head><title>T</title><style>p{color:red}</style>
<script>var secret=1</script></head>
<body><nav><a href="/">Home</a></nav>
<h1>Clinic</h1><p>Welcome to the clinic.</p>
<h2>Hours</h2><ul><li>Mon 8-5</li><li>Sat 9-1</li></ul>
<table><tr><th>Day</th><th>Hours</th></tr><tr><td>Sun</td><td>Closed</td></tr></table>
<h3>Note</h3><p>Closed on holidays.</p></body></html>"""


def test_html_drops_scripts_and_styles_and_keeps_structure():
    doc = load_bytes("site.html", HTML.encode())
    text = "\n".join(s.text for s in doc.segments)
    assert "secret" not in text and "color:red" not in text
    assert [s.section for s in doc.segments] == ["Clinic", "Hours"]
    assert "- Mon 8-5" in text
    assert "Sun | Closed" in text
    assert "Closed on holidays." in text


# ----------------------------------------------------------------------------- PDF


def test_pdf_pages_are_one_based_and_headers_footers_removed():
    pages = [
        [
            "BrightSmile | Guide",
            f"Content of page {n} is unique {n}.",
            f"Footer line | Page {n} of 3",
        ]
        for n in (1, 2, 3)
    ]
    doc = load_bytes("g.pdf", make_pdf(pages))
    assert doc.source_type == "pdf" and doc.page_count == 3
    assert [s.page for s in doc.segments] == [1, 2, 3]
    for number, segment in enumerate(doc.segments, start=1):
        assert f"unique {number}" in segment.text
        assert "Footer line" not in segment.text and "BrightSmile |" not in segment.text
        assert segment.section is None


def test_strip_repeated_lines_needs_repetition_and_keeps_unique_edges():
    pages = ["Header\nBody A\nPage 1", "Header\nBody B\nPage 2", "Other top\nBody C\nPage 3"]
    cleaned = strip_repeated_page_lines(pages)
    assert cleaned[0] == "Body A" and cleaned[1] == "Body B"
    assert cleaned[2] == "Other top\nBody C"  # 'Header' is not on page 3; page numbers are removed
    assert strip_repeated_page_lines(["Only page"]) == ["Only page"]  # nothing to compare


def test_real_sample_pdf_loads_with_pages():
    doc = load_bytes(
        "cancellation-policy.pdf", (ROOT / "data/sample_docs/cancellation-policy.pdf").read_bytes()
    )
    assert doc.page_count == 2 and [s.page for s in doc.segments] == [1, 2]
    assert "Page 1 of 2" not in doc.segments[0].text
    assert "$50" in " ".join(s.text for s in doc.segments)


def test_table_heavy_fixture_loads_even_if_flattened():
    doc = load_bytes("table_heavy.pdf", (FIXTURES / "table_heavy.pdf").read_bytes())
    assert doc.page_count >= 1 and "Procedure 1" in " ".join(s.text for s in doc.segments)


def test_malformed_pdf_is_rejected_with_a_reason():
    with pytest.raises(LoadError) as err:
        load_bytes("bad.pdf", b"%PDF-1.4 this is not really a pdf")
    assert err.value.code == "unreadable_pdf"


def test_pdf_without_text_is_treated_as_scanned_and_rejected():
    with pytest.raises(LoadError) as err:
        load_bytes("scan.pdf", make_pdf([[], []]))
    assert err.value.code == "empty" and "scanned" in err.value.message


# ----------------------------------------------------------------------------- URLs


def resolver_for(ip: str):
    return lambda host, port: [(2, 1, 6, "", (ip, port))]


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.5",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
    ],
)
def test_private_and_local_addresses_are_blocked_by_default(ip):
    with pytest.raises(LoadError) as err:
        check_url_allowed("http://example.test/", allow_private=False, resolver=resolver_for(ip))
    assert err.value.code == "private_url"


def test_public_address_is_allowed_and_private_can_be_enabled():
    check_url_allowed("https://example.test/", False, resolver_for("93.184.216.34"))
    check_url_allowed("http://localhost:9000/", True, resolver_for("127.0.0.1"))


@pytest.mark.parametrize(
    "url", ["ftp://example.test/x", "file:///etc/passwd", "javascript:alert(1)", "http:///x"]
)
def test_bad_schemes_and_hosts_are_rejected(url):
    with pytest.raises(LoadError) as err:
        check_url_allowed(url, True)
    assert err.value.code == "bad_url"


def transport(handler):
    return httpx.MockTransport(handler)


PUBLIC = resolver_for("93.184.216.34")
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def test_load_url_returns_a_web_document():
    def handler(request):
        return httpx.Response(200, headers=HTML_HEADERS, text="<h1>Page</h1><p>Hello there.</p>")

    doc = load_url("https://example.test/p#frag", transport=transport(handler), resolver=PUBLIC)
    assert doc.source_type == "web" and doc.name == "https://example.test/p"
    assert doc.source_url == "https://example.test/p"
    assert "Hello there." in doc.segments[0].text


def test_load_url_rejects_non_html_errors_and_oversize():
    cases = [
        (
            httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"x"),
            "not_html",
        ),
        (httpx.Response(404, headers=HTML_HEADERS, text="no"), "fetch_failed"),
        (
            httpx.Response(200, headers=HTML_HEADERS, content=b"<p>" + b"x" * 5000 + b"</p>"),
            "too_large",
        ),
        (httpx.Response(200, headers=HTML_HEADERS, text="<script>only()</script>"), "empty"),
    ]
    for response, code in cases:
        with pytest.raises(LoadError) as err:
            load_url(
                "https://example.test/",
                max_bytes=1000,
                transport=transport(lambda r, resp=response: resp),
                resolver=PUBLIC,
            )
        assert err.value.code == code


def test_load_url_timeout_becomes_a_clear_error():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(LoadError) as err:
        load_url("https://example.test/", transport=transport(handler), resolver=PUBLIC)
    assert err.value.code == "fetch_failed" and "timed out" in err.value.message


def test_redirect_to_a_private_address_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal.test/admin"})

    def resolver(host, port):
        ip = "10.0.0.5" if host == "internal.test" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, port))]

    with pytest.raises(LoadError) as err:
        load_url("https://example.test/", transport=transport(handler), resolver=resolver)
    assert err.value.code == "private_url"


def test_redirect_loop_stops_after_three_redirects():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.test/again"})

    with pytest.raises(LoadError) as err:
        load_url("https://example.test/", transport=transport(handler), resolver=PUBLIC)
    assert "redirects" in err.value.message and len(calls) == 4


def test_body_lines_that_differ_only_by_a_number_are_not_treated_as_footers():
    pages = ["Step 1: rinse\nmore text\nend one", "Step 2: brush\nmore text\nend two"]
    cleaned = strip_repeated_page_lines(pages)
    assert cleaned[0].startswith("Step 1: rinse") and cleaned[1].startswith("Step 2: brush")
