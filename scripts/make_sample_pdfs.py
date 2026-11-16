"""Render the sample PDFs from Markdown sources. Development tool: reportlab is a dev-only
dependency and is not in the Docker image. The generated PDFs are committed, so a clean clone
does not need to run this.

  uv run python -m scripts.make_sample_pdfs

  data/pdf_sources/*.md   ->  data/sample_docs/*.pdf        (the demo knowledge base)
  (built in code)         ->  tests/fixtures/table_heavy.pdf (a table-heavy PDF, for tests only)

Every page carries a running header and footer ("Page N of M"), which is what the loader's
header/footer cleaner is tested against.
"""

import re
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "data" / "pdf_sources"
OUT_DIR = ROOT / "data" / "sample_docs"
FIXTURES = ROOT / "tests" / "fixtures"
FOOTER = "BrightSmile Dental | 214 Maple Avenue, Suite 3, Riverton, OH 43001"

_styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_styles["BodyText"], fontSize=10.5, leading=15, spaceAfter=7)
H1 = ParagraphStyle("h1", parent=_styles["Heading1"], fontSize=20, leading=24, spaceAfter=10)
H2 = ParagraphStyle(
    "h2", parent=_styles["Heading2"], fontSize=14, leading=18, spaceBefore=10, spaceAfter=6
)
H3 = ParagraphStyle(
    "h3", parent=_styles["Heading3"], fontSize=11.5, leading=15, spaceBefore=6, spaceAfter=4
)


def inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


def markdown_to_flowables(source: str) -> list:
    flowables: list = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        if all(line.startswith("- ") for line in block):
            items = [ListItem(Paragraph(inline(ln[2:]), BODY), leftIndent=14) for ln in block]
            flowables.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))
        else:
            flowables.append(Paragraph("<br/>".join(inline(ln) for ln in block), BODY))
        block.clear()

    for line in source.split("\n"):
        stripped = line.rstrip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush()
            style = {1: H1, 2: H2, 3: H3}[len(heading.group(1))]
            flowables.append(Paragraph(inline(heading.group(2)), style))
        elif not stripped:
            flush()
        else:
            if block and (block[-1].startswith("- ") != stripped.startswith("- ")):
                flush()
            block.append(stripped)
    flush()
    return flowables


def build_pdf(path: Path, title: str, make_flowables: Callable[[], list]) -> int:
    """Two passes: the first counts pages, the second prints 'Page N of M' on every page."""
    total = {"pages": 0}

    def decorate(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.9 * inch, LETTER[1] - 0.55 * inch, f"BrightSmile Dental | {title}")
        canvas.drawString(
            0.9 * inch, 0.5 * inch, f"{FOOTER} | Page {doc.page} of {total['pages'] or '?'}"
        )
        canvas.restoreState()

    for _ in range(2):
        doc = SimpleDocTemplate(
            str(path), pagesize=LETTER, title=title, author="BrightSmile Dental (fictional)",
            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
            topMargin=0.95 * inch, bottomMargin=0.9 * inch,
        )  # fmt: skip
        # A build consumes its flowables, so each pass gets freshly made ones.
        doc.build(make_flowables(), onFirstPage=decorate, onLaterPages=decorate)
        total["pages"] = doc.page
    return total["pages"]


def title_of(source: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", source, re.M)
    return match.group(1).strip() if match else fallback


def make_fixture_table_pdf() -> int:
    """A PDF whose content is mostly a table. Text extraction flattens tables badly, which is why
    it lives in tests/fixtures only and is not part of the demo knowledge base."""
    rows = [["Service", "Code", "Fee", "Typical insurance share"]] + [
        [f"Procedure {n}", f"D{1000 + n}", f"${n * 35 + 60}", f"{50 + (n % 4) * 10}%"]
        for n in range(1, 41)
    ]

    def make() -> list:
        table = Table(rows, repeatRows=1)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey)]))
        return [Paragraph("Fee Schedule (table-heavy test fixture)", H1), Spacer(1, 8), table]

    FIXTURES.mkdir(parents=True, exist_ok=True)
    return build_pdf(FIXTURES / "table_heavy.pdf", "Fee Schedule", make)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(SOURCES.glob("*.md")):
        source = source_path.read_text(encoding="utf-8")
        pages = build_pdf(
            OUT_DIR / f"{source_path.stem}.pdf",
            title_of(source, source_path.stem),
            lambda source=source: markdown_to_flowables(source),
        )
        print(f"{source_path.stem}.pdf: {pages} pages")
    print(f"table_heavy.pdf (fixture): {make_fixture_table_pdf()} pages")


if __name__ == "__main__":
    main()
