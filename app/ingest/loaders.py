"""Loaders: turn a file's bytes (or one web page) into text segments with page/section metadata.

Page means the 1-based PDF page number; it is None for Markdown, TXT and HTML, where the
nearest section heading is stored instead. Scanned PDFs are out of scope.
"""

import ipaddress
import math
import re
import socket
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.ingest.textutil import clean_text, normalize_for_hash


class LoadError(Exception):
    """A file or URL could not be turned into text. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Segment:
    text: str
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class LoadedDocument:
    name: str
    source_type: str  # pdf | markdown | text | html | web
    segments: list[Segment]
    source_url: str | None = None
    page_count: int | None = None

    @property
    def normalized_text(self) -> str:
        return normalize_for_hash("\n\n".join(s.text for s in self.segments))


_EXTENSIONS = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
}
SUPPORTED_EXTENSIONS = tuple(sorted(_EXTENSIONS))


def source_type_for(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in _EXTENSIONS:
        raise LoadError(
            "unsupported_type",
            f"Unsupported file type '{suffix or name}'. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}",
        )
    return _EXTENSIONS[suffix]


def load_bytes(name: str, data: bytes) -> LoadedDocument:
    """Load one uploaded or on-disk file. Raises LoadError with a clear reason on any problem."""
    source_type = source_type_for(name)
    if not data:
        raise LoadError("empty", "The file is empty.")
    if source_type == "pdf":
        segments, pages = _load_pdf(data)
        return LoadedDocument(name, "pdf", segments, page_count=pages)
    text = _decode(data)
    if source_type == "markdown":
        segments = _markdown_segments(text)
    elif source_type == "html":
        segments = _html_segments(text)
    else:
        segments = [Segment(clean_text(text))]
    segments = [s for s in segments if s.text.strip()]
    if not segments:
        raise LoadError("empty", "The file contains no text.")
    return LoadedDocument(name, source_type, segments)


# ----------------------------------------------------------------------------- text decoding


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LoadError("not_text", "The file is not valid UTF-8 text.") from exc


# ----------------------------------------------------------------------------- PDF


def _load_pdf(data: bytes) -> tuple[list[Segment], int]:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise LoadError("encrypted_pdf", "The PDF is password protected.")
        raw_pages = [page.extract_text() or "" for page in reader.pages]
    except LoadError:
        raise
    except Exception as exc:  # pypdf raises many types on malformed files
        raise LoadError(
            "unreadable_pdf", f"The PDF could not be read ({type(exc).__name__})."
        ) from exc
    pages = [clean_text(t) for t in strip_repeated_page_lines(raw_pages)]
    if not any(pages):
        raise LoadError(
            "empty", "The PDF has no extractable text (scanned PDFs are not supported)."
        )
    segments = [Segment(text, page=i + 1) for i, text in enumerate(pages) if text]
    return segments, len(raw_pages)


def _line_key(line: str) -> str:
    """Compare lines across pages with page numbers masked ('Page 2 of 5', '2 of 5', '- 3 -'),
    but leave other digits alone so body lines such as 'Step 1' and 'Step 2' stay different."""
    key = line.strip().lower()
    key = re.sub(r"\bpage\s+\d+(\s*(of|/)\s*\d+)?", "page #", key)
    key = re.sub(r"^[-\u2013\s]*\d+(\s*(of|/)\s*\d+)?[-\u2013\s]*$", "#", key)
    return key


def strip_repeated_page_lines(pages: list[str], edge_lines: int = 2) -> list[str]:
    """Remove running headers and footers: short lines at the top or bottom edge of a page that
    repeat (digits ignored) on at least 60% of the pages. Needs two or more pages."""
    if len(pages) < 2:
        return pages
    split = [[ln for ln in p.splitlines() if ln.strip()] for p in pages]
    threshold = max(2, math.ceil(0.6 * len(pages)))
    counts: Counter[str] = Counter()
    for lines in split:
        edges = lines[:edge_lines] + lines[-edge_lines:]
        counts.update({_line_key(ln) for ln in edges if len(ln.strip()) <= 120})
    repeated = {key for key, n in counts.items() if n >= threshold}
    cleaned = []
    for lines in split:
        keep = list(lines)
        for index in list(range(min(edge_lines, len(lines)))) + list(
            range(max(0, len(lines) - edge_lines), len(lines))
        ):
            if _line_key(lines[index]) in repeated and len(lines[index].strip()) <= 120:
                keep[index] = ""
        cleaned.append("\n".join(ln for ln in keep if ln))
    return cleaned


# ----------------------------------------------------------------------------- Markdown

_HEADING = re.compile(r"^(#{1,2})\s+(.+?)\s*#*\s*$")  # sections split at # and ## only
_FENCE = re.compile(r"^\s*(```|~~~)")


def _markdown_segments(text: str) -> list[Segment]:
    """Split at level 1 and 2 headings (deeper headings stay inside their section as text).
    A heading with no body of its own is carried into the next segment, so we never store a
    chunk that is only a title."""
    segments: list[Segment] = []
    section: str | None = None
    lines: list[str] = []
    pending_headings: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal lines
        body = "\n".join(lines).strip()
        if body:
            prefix = "\n".join(pending_headings)
            pending_headings.clear()
            segments.append(
                Segment(clean_text(f"{prefix}\n{body}" if prefix else body), None, section)
            )
        lines = []

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            section = match.group(2).strip()
            pending_headings.append(line.strip())
        else:
            lines.append(line)
    flush()
    if pending_headings:  # trailing headings with no body
        segments.append(Segment("\n".join(pending_headings), None, section))
    return segments


# ----------------------------------------------------------------------------- HTML

_DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe")
_BLOCK_TAGS = (
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote", "dt", "dd",
    "address", "figcaption",
)  # fmt: skip
_HEADING_TAGS = {"h1", "h2"}  # sections split at h1 and h2 only; deeper headings stay as text


def _html_segments(html: str) -> list[Segment]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    root = soup.body or soup
    blocks = [el for el in root.find_all(_BLOCK_TAGS) if el.find_parent(_BLOCK_TAGS) is None]
    if not blocks:
        text = clean_text(root.get_text("\n"))
        return [Segment(text)] if text else []

    segments: list[Segment] = []
    section: str | None = None
    lines: list[str] = []
    pending_headings: list[str] = []

    def flush() -> None:
        nonlocal lines
        if lines:
            body = "\n".join(lines)
            prefix = "\n".join(pending_headings)
            pending_headings.clear()
            segments.append(
                Segment(clean_text(f"{prefix}\n{body}" if prefix else body), None, section)
            )
        lines = []

    for el in blocks:
        if el.name in _HEADING_TAGS:
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            flush()
            section = text
            pending_headings.append(text)
        elif el.name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["th", "td"])]
            line = " | ".join(c for c in cells if c)
            if line:
                lines.append(line)
        else:
            text = el.get_text(" ", strip=True)
            if text:
                lines.append(f"- {text}" if el.name == "li" else text)
    flush()
    if pending_headings:
        segments.append(Segment("\n".join(pending_headings), None, section))
    return segments


# ----------------------------------------------------------------------------- URLs


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url_allowed(
    url: str,
    allow_private: bool,
    resolver: Callable[..., list] = socket.getaddrinfo,
) -> None:
    """Allow only http/https, and (unless allow_private) only hosts that resolve to public IPs.

    Known limit: the address is checked before the request, not pinned for it, so a hostile DNS
    server could in theory answer differently the second time. Fine for a local showcase.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise LoadError("bad_url", "Only http and https URLs are supported.")
    if not parts.hostname:
        raise LoadError("bad_url", "The URL has no host name.")
    if allow_private:
        return
    try:
        infos = resolver(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise LoadError("fetch_failed", "The host name could not be resolved.") from exc
    for info in infos:
        if _is_blocked_address(ipaddress.ip_address(info[4][0])):
            raise LoadError(
                "private_url",
                "That address is private or local. Set ALLOW_PRIVATE_URLS=true to allow it.",
            )


def load_url(
    url: str,
    *,
    allow_private: bool = False,
    timeout: float = 10,
    max_bytes: int = 2_000_000,
    max_redirects: int = 3,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[..., list] = socket.getaddrinfo,
) -> LoadedDocument:
    """Fetch ONE web page (not a crawler). HTML only, size capped, redirects re-checked."""
    url = urldefrag(url.strip())[0]
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _ in range(max_redirects + 1):
            check_url_allowed(current, allow_private, resolver)
            try:
                with client.stream("GET", current, headers={"Accept": "text/html"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise LoadError("fetch_failed", "Redirect without a Location header.")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise LoadError(
                            "fetch_failed", f"The page returned HTTP {response.status_code}."
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type:
                        raise LoadError("not_html", "The URL did not return an HTML page.")
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > max_bytes:
                            raise LoadError(
                                "too_large", f"The page is larger than {max_bytes} bytes."
                            )
            except LoadError:
                raise
            except httpx.TimeoutException as exc:
                raise LoadError("fetch_failed", "The request timed out.") from exc
            except httpx.HTTPError as exc:
                raise LoadError(
                    "fetch_failed", f"The request failed ({type(exc).__name__})."
                ) from exc
            break
        else:
            raise LoadError("fetch_failed", f"Too many redirects (more than {max_redirects}).")
    segments = [s for s in _html_segments(_decode(bytes(body))) if s.text.strip()]
    if not segments:
        raise LoadError("empty", "The page contains no text.")
    return LoadedDocument(name=url, source_type="web", segments=segments, source_url=url)
