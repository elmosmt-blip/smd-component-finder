"""PDF -> Markdown for the datasheet RAG pipeline.

Parser #1: Docling (IBM) — best open tool for datasheets in 2026: it recovers
table structure (rows/columns) instead of flattening them into text soup, and
exports to Markdown directly.

The catch: Docling downloads its layout/tableformer models from HuggingFace on
first use. Sandboxes and air-gapped machines usually cannot reach HF, so the
pipeline ships a second parser that needs no models at all:

Parser #2: pdfplumber — pure Python, recovers tables with `extract_table()`
and keeps page numbers. Weaker on rotated/scanned pages, but it always runs.

Both produce the same ParsedDoc, so chunking and indexing do not care which
one was used.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Page:
    number: int
    text: str
    tables: List[List[List[str]]] = field(default_factory=list)


@dataclass
class ParsedDoc:
    doc_id: str
    filename: str
    sha1: str
    pages: List[Page]
    markdown: str
    parser: str

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    def first_pages_text(self, n: int = 2) -> str:
        return "\n".join(p.text for p in self.pages[:n])


class ParserUnavailable(RuntimeError):
    """Raised when a parser cannot run (missing package or missing models)."""


class BaseParser:
    name = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    def parse(self, path: Path) -> ParsedDoc:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Parser #1 — Docling
# --------------------------------------------------------------------------- #

class DoclingParser(BaseParser):
    """IBM Docling. Requires `pip install docling` and access to HF weights."""

    name = "docling"

    def is_available(self) -> bool:
        try:
            import docling  # noqa: F401
        except ImportError as exc:
            raise ParserUnavailable(
                "Docling is not installed. Run: pip install docling "
                "(heavy: torch + models). Original error: %s" % exc
            ) from exc
        return True

    @staticmethod
    def _fill_tables(path: Path, pages: List["Page"]) -> None:
        """Attach pdfplumber tables to Docling pages, by page number."""
        try:
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                for page in pages:
                    idx = page.number - 1
                    if not (0 <= idx < len(pdf.pages)):
                        continue
                    raw = pdf.pages[idx].extract_tables() or []
                    tables = []
                    for t in raw:
                        cleaned = [
                            [clean_text((cell or "")).strip() for cell in row]
                            for row in t
                            if row and any((cell or "").strip() for cell in row)
                        ]
                        if cleaned:
                            tables.append(cleaned)
                    page.tables = tables
        except Exception:                      # noqa: BLE001 - tables are a bonus
            return

    def parse(self, path: Path) -> ParsedDoc:
        self.is_available()
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:  # pragma: no cover - defensive
            raise ParserUnavailable("docling import failed: %s" % exc) from exc

        sha1 = _sha1(path)
        try:
            converter = DocumentConverter()
            result = converter.convert(str(path))
            doc = result.document
            markdown = doc.export_to_markdown()
        except Exception as exc:
            # Most common cause: no network to HuggingFace for the models.
            raise ParserUnavailable(
                "Docling could not convert %s (%s: %s). In an offline sandbox "
                "this usually means the layout/tableformer weights cannot be "
                "downloaded." % (path.name, type(exc).__name__, exc)
            ) from exc

        pages = []
        # Docling keeps page geometry; fall back to a single synthetic page when
        # the backend does not expose per-page text.
        try:
            for number, page in sorted(getattr(doc, "pages", {}).items()):
                text = getattr(page, "text", "") or ""
                pages.append(Page(number=int(number), text=text))
        except Exception:
            pages = []
        if not pages:
            pages = [Page(number=1, text=markdown)]

        # Docling's markdown is excellent for retrieval but carries no per-page
        # table objects, and the card extractor works from tables. Without this
        # a Docling run would produce beautiful chunks and empty cards, so the
        # tables are filled in with pdfplumber (offline, no model weights).
        self._fill_tables(path, pages)

        return ParsedDoc(
            doc_id=_doc_id(path),
            filename=path.name,
            sha1=sha1,
            pages=pages,
            markdown=markdown,
            parser=self.name,
        )


# --------------------------------------------------------------------------- #
# Parser #2 — pdfplumber (offline fallback)
# --------------------------------------------------------------------------- #

class PdfPlumberParser(BaseParser):
    """No models, no network. Text + tables per page."""

    name = "pdfplumber"

    def is_available(self) -> bool:
        try:
            import pdfplumber  # noqa: F401
        except ImportError as exc:
            raise ParserUnavailable(
                "pdfplumber is not installed. Run: pip install pdfplumber"
            ) from exc
        return True

    def parse(self, path: Path) -> ParsedDoc:
        self.is_available()
        import pdfplumber

        pages: List[Page] = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    text = clean_text(page.extract_text() or "")
                except Exception:
                    text = ""
                tables: List[List[List[str]]] = []
                try:
                    raw_tables = page.extract_tables() or []
                except Exception:
                    raw_tables = []
                for raw in raw_tables:
                    cleaned = [
                        [clean_text((cell or "")).strip() for cell in row]
                        for row in raw
                        if row and any((cell or "").strip() for cell in row)
                    ]
                    if cleaned:
                        tables.append(cleaned)
                pages.append(Page(number=i, text=text, tables=tables))

        return ParsedDoc(
            doc_id=_doc_id(path),
            filename=path.name,
            sha1=_sha1(path),
            pages=pages,
            markdown=_pages_to_markdown(pages, path.name),
            parser=self.name,
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def clean_text(text: str) -> str:
    """Remove PDF text-layer artefacts.

    Font glyphs without a Unicode mapping come out as "(cid:127)" — that is
    the bullet in a Features list, or a Greek letter in a formula. Useless for
    retrieval and noisy in a snippet.
    """
    if not text:
        return ""
    text = re.sub(r"\(cid:\d+\)", " ", text)
    text = re.sub(r"\s*\(cid:\d+\)\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _doc_id(path: Path) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", path.stem).strip("_").lower()
    return stem or "doc"


def _table_to_markdown(table: List[List[str]]) -> str:
    if not table:
        return ""
    width = max(len(row) for row in table)
    rows = []
    for idx, row in enumerate(table):
        cells = list(row) + [""] * (width - len(row))
        rows.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        if idx == 0:
            rows.append("|" + "|".join(["---"] * width) + "|")
    return "\n".join(rows)


# Titles that appear in almost every vendor datasheet. A PDF text layer has no
# styling, so headings are recovered by matching these (plus an ALL-CAPS rule).
# Docling returns real headings and makes this heuristic unnecessary.
KNOWN_HEADINGS = {
    "general description", "description", "overview", "introduction", "summary",
    "features", "applications", "application", "typical applications",
    "pin configuration", "pin assignment", "pin description", "pinout", "pin functions",
    "absolute maximum ratings", "maximum ratings", "limiting values",
    "recommended operating conditions", "operating conditions", "operating ratings",
    "electrical characteristics", "electrical specifications", "dc characteristics",
    "thermal characteristics", "thermal information", "thermal data",
    "package dimensions", "package outline", "mechanical data", "outline drawing",
    "ordering information", "ordering guide", "marking information",
    "block diagram", "functional description", "functional block diagram",
    "typical application circuit", "typical performance characteristics",
    "revision history", "document history", "package information",
}


def _looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 70:
        return False
    if s.lower().strip(" :") in KNOWN_HEADINGS:
        return True
    # ALL CAPS short line without a final period: "ABSOLUTE MAXIMUM RATINGS".
    # The letter-ratio guard keeps pin names out — "DIO_23", "RF_P_2_4GHZ" and
    # "X32K_Q1" are upper case too, but they are not headings.
    if (s.isupper() and len(s) <= 60 and not s.endswith(".") and "." not in s
            and 2 <= len(s.split()) <= 7):
        letters = sum(1 for c in s if c.isalpha() or c.isspace())
        if letters / float(len(s)) > 0.85:
            return True
    return False


def _text_to_markdown(text: str) -> str:
    out = []
    for line in text.splitlines():
        out.append("## %s" % line.strip() if _looks_like_heading(line) else line)
    return "\n".join(out)


def _pages_to_markdown(pages: List[Page], title: str) -> str:
    out = ["# %s" % title, ""]
    for page in pages:
        out.append("<!-- page %d -->" % page.number)
        out.append("")
        if page.text.strip():
            out.append(_text_to_markdown(page.text.strip()))
            out.append("")
        for table in page.tables:
            out.append(_table_to_markdown(table))
            out.append("")
    return "\n".join(out)


def get_parser(preferred: str = "auto") -> BaseParser:
    """Return the best parser that can actually run here.

    preferred: 'docling' | 'pdfplumber' | 'auto'
    With 'auto' we try Docling first and silently fall back, because a parser
    that cannot download its models is worse than a weaker parser that works.
    """
    # Asking for Docling and quietly getting something else is worse than an
    # error: the user would trust results produced by another engine. Only
    # 'auto' is allowed to substitute, and it reports what it did.
    order = {
        "docling": ["docling"],
        "pdfplumber": ["pdfplumber"],
        "auto": ["docling", "pdfplumber"],
    }[preferred]

    errors = []
    for name in order:
        parser: BaseParser = DoclingParser() if name == "docling" else PdfPlumberParser()
        try:
            parser.is_available()
            parser.fallback_errors = errors          # why the others were skipped
            return parser
        except ParserUnavailable as exc:
            errors.append("%s: %s" % (name, exc))
    raise ParserUnavailable("No PDF parser available. " + " | ".join(errors))
