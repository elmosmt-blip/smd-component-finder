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
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
    """IBM Docling — layout analysis and TableFormer instead of text soup.

    Why it is worth the trouble for datasheets: `pdfplumber` guesses cell
    boundaries from ruling lines and character positions, so a pinout table
    without vertical rules comes out as one column of "1 Base Control input".
    Docling runs a layout model and then TableFormer on every table region,
    which is what keeps Pitch and Lead Width in their own columns.

    Two things decide whether this is usable on 300k files, and both are
    handled here:

      * **Models must be loaded once per worker, not once per PDF.** The
        converter is cached on the module, so a worker process pays for the
        weights on its first file and never again.
      * **Full Docling on every page is far too slow for a whole library.**
        `pages="tables"` (the default) makes a cheap pdfplumber pass first and
        sends Docling only the pages that actually contain tables — usually
        10–20 % of a datasheet.

    Environment:
        SMD_DOCLING_PAGES       tables | all          (default: tables)
        SMD_DOCLING_TABLE_MODE  fast | accurate       (default: fast)
        SMD_DOCLING_DEVICE      auto | cpu | cuda     (default: auto)
        SMD_DOCLING_THREADS     threads per worker    (default: 2)
        SMD_DOCLING_OCR         off | on | auto       (default: auto)
        DOCLING_ARTIFACTS_PATH  where the weights live (Docling's own variable)

    OCR is the whole point of Docling on a scanned datasheet, and `auto` is
    the default because it is free everywhere else: OCR is switched on only
    for files where pdfplumber found no text layer, so a native PDF never
    pays for it and a scan still gets one. Docling needs an OCR engine
    installed (tesseract or easyocr); if it is missing the conversion raises
    and the caller falls back to pdfplumber, which is where the scan was
    before — no worse, and the reason is printed.
    """

    name = "docling"

    # One converter per process per configuration: loading the models is the
    # single most expensive step and must not repeat 300 000 times.
    _CONVERTERS: dict = {}
    _CONVERTER_LOCK = threading.Lock()

    def __init__(self, pages: Optional[str] = None, table_mode: Optional[str] = None,
                 device: Optional[str] = None, threads: int = 0,
                 ocr: Optional[str] = None):
        self.pages = (pages or os.environ.get("SMD_DOCLING_PAGES") or "tables").lower()
        self.table_mode = (table_mode or os.environ.get("SMD_DOCLING_TABLE_MODE")
                           or "fast").lower()
        self.device = device or os.environ.get("SMD_DOCLING_DEVICE") or "auto"
        self.threads = int(threads or os.environ.get("SMD_DOCLING_THREADS") or 2)
        self.ocr = (ocr or os.environ.get("SMD_DOCLING_OCR") or "auto").lower()
        if self.ocr not in ("off", "on", "auto"):
            self.ocr = "auto"

    # ------------------------------------------------------------- availability

    def is_available(self) -> bool:
        try:
            import docling  # noqa: F401
        except ImportError as exc:
            raise ParserUnavailable(
                "Docling is not installed. Run: pip install docling "
                "(heavy: torch + transformers). Original error: %s" % exc
            ) from exc
        return True

    # --------------------------------------------------------------- docling io

    def _options(self, do_ocr: bool = False):
        from docling.datamodel.pipeline_options import (
            AcceleratorOptions, PdfPipelineOptions, TableFormerMode)

        opts = PdfPipelineOptions()
        # Tables are the whole point; page images are not — they cost time and
        # gigabytes on a 300k run and we never look at them.
        opts.do_table_structure = True
        opts.do_ocr = bool(do_ocr)
        opts.generate_page_images = False
        opts.generate_picture_images = False
        try:
            opts.table_structure_options.mode = (
                TableFormerMode.ACCURATE if self.table_mode.startswith("acc")
                else TableFormerMode.FAST)
            opts.table_structure_options.do_cell_matching = True
        except Exception:                        # noqa: BLE001 - version drift
            pass
        try:
            # We parallelise with processes, so Docling must not spawn 32
            # threads per worker on top of that.
            opts.accelerator_options = AcceleratorOptions(
                device=self.device, num_threads=max(1, self.threads))
        except Exception:                        # noqa: BLE001
            pass
        return opts

    def _converter(self, do_ocr: bool = False):
        # The OCR pipeline is a different engine instance, so it is a
        # different cache key: a native PDF and a scan each get one warm
        # converter per worker, not one that is rebuilt on every file.
        key = (self.pages, self.table_mode, self.device, self.threads, bool(do_ocr))
        with self._CONVERTER_LOCK:
            conv = self._CONVERTERS.get(key)
            if conv is None:
                quiet_docling()
                from docling.datamodel.base_models import InputFormat
                from docling.document_converter import DocumentConverter, PdfFormatOption
                conv = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=self._options(do_ocr))
                })
                self._CONVERTERS[key] = conv
            return conv

    def _convert(self, path: Path, page_range=None, do_ocr: bool = False):
        kwargs = {"page_range": page_range} if page_range else {}
        return self._converter(do_ocr).convert(str(path), **kwargs)

    @staticmethod
    def _table_rows(item, doc) -> List[List[str]]:
        """One Docling TableItem -> rows of strings, header row included."""
        try:
            frame = item.export_to_dataframe(doc=doc)
        except Exception:                        # noqa: BLE001
            try:
                frame = item.export_to_dataframe()
            except Exception:                    # noqa: BLE001
                return []
        try:
            header = [("" if c is None else str(c)).strip() for c in frame.columns]
            rows = [["" if v is None else str(v).strip() for v in rec]
                    for rec in frame.values.tolist()]
        except Exception:                        # noqa: BLE001
            return []
        out = [header] if any(header) else []
        out += [r for r in rows if any(r)]
        return out

    def _docling_tables(self, doc, page_offset: int = 0) -> Dict[int, List[List[List[str]]]]:
        """Tables grouped by page number, straight from Docling.

        `page_offset` fixes a subtlety: when converting `page_range=(7, 9)`,
        Docling numbers the pages it was given, so page 1 of the result is
        document page 7.
        """
        tables: Dict[int, List[List[List[str]]]] = {}
        try:
            items = list(doc.iterate_items())
        except Exception:                        # noqa: BLE001
            return tables
        for item, _level in items:
            # Duck-typed, not isinstance(TableItem): the tests run without
            # docling installed, and this must stay importable without it.
            if not self._looks_like_table(item):
                continue
            prov = getattr(item, "prov", None) or []
            page_no = getattr(prov[0], "page_no", 1) if prov else 1
            rows = self._table_rows(item, doc)
            if rows:
                tables.setdefault(int(page_no) + page_offset, []).append(rows)
        return tables

    @staticmethod
    def _looks_like_table(item) -> bool:
        if not hasattr(item, "export_to_dataframe"):
            return False
        if type(item).__name__ == "TableItem":
            return True
        return str(getattr(item, "label", "")).lower().endswith("table")

    def _page_markdown(self, doc, number: int) -> str:
        try:
            return doc.export_to_markdown(page_no=number)
        except Exception:                        # noqa: BLE001 - older docling
            return ""

    # -------------------------------------------------------------------- parse

    # Set when a scan was handed to Docling + OCR and it did not work. The
    # caller (build_cards) prints it, because "Docling made 8 cards out of
    # 854" is otherwise unexplainable. Class-level and bounded: a worker
    # process cannot send its own list back, so this is a best effort that at
    # least covers --jobs 1, and the `parser` column covers the rest.
    ISSUES: List[str] = []
    last_error: Optional[str] = None

    @classmethod
    def note_issue(cls, message: str) -> None:
        cls.last_error = message
        cls.ISSUES.append(message)
        if len(cls.ISSUES) > 20:
            del cls.ISSUES[:-20]

    def parse(self, path: Path) -> ParsedDoc:
        self.is_available()
        sha1 = _sha1(path)
        if self.pages == "all":
            return self._parse_whole(path, sha1, do_ocr=(self.ocr == "on"))
        return self._parse_table_pages(path, sha1)

    def _ocr_decision(self, base: Optional["ParsedDoc"]) -> bool:
        """Should Docling run OCR on this file?

        `auto` (the default) answers from the cheap pdfplumber pass that
        `pages="tables"` already had to make: if it pulled almost no text out
        of the document, there is no text layer and this is a scan. Nothing
        is opened twice.
        """
        if self.ocr == "on":
            return True
        if self.ocr == "off":
            return False
        if base is None or not base.pages:
            return False
        chars = sum(len(pg.text or "") for pg in base.pages)
        return chars < 20 * len(base.pages)

    def _parse_whole(self, path: Path, sha1: str, do_ocr: bool = False) -> ParsedDoc:
        try:
            result = self._convert(path, do_ocr=do_ocr)
        except Exception as exc:                 # noqa: BLE001
            raise ParserUnavailable(
                "Docling could not convert %s (%s: %s). The usual cause is "
                "missing model weights: Docling downloads them from "
                "HuggingFace on first use (see DOCLING_ARTIFACTS_PATH)."
                % (path.name, type(exc).__name__, exc)
            ) from exc
        doc = result.document
        try:
            markdown = doc.export_to_markdown()
        except Exception:                        # noqa: BLE001
            markdown = ""

        pages: List[Page] = []
        tables = self._docling_tables(doc)
        try:
            numbers = sorted(getattr(doc, "pages", {}) or {})
        except Exception:                        # noqa: BLE001
            numbers = []
        if not numbers:
            numbers = sorted(set(list(tables) + [1])) or [1]
        for number in numbers:
            text = self._page_markdown(doc, number) if numbers else markdown
            pages.append(Page(number=int(number), text=text or "",
                              tables=tables.get(int(number), [])))
        if any(p.tables for p in pages):
            pass                                  # Docling's tables are enough
        else:
            # Older versions, or a build without table structure.
            self._fill_tables(path, pages)
        return ParsedDoc(doc_id=_doc_id(path), filename=path.name, sha1=sha1,
                         pages=pages, markdown=markdown, parser=self.name)

    def _parse_table_pages(self, path: Path, sha1: str) -> ParsedDoc:
        """Cheap pass first, Docling only where tables actually are."""
        base = PdfPlumberParser().parse(path)
        candidates = [p.number for p in base.pages if p.tables]
        do_ocr = self._ocr_decision(base)
        if not candidates:
            # The trap this used to fall into: on a scan pdfplumber finds no
            # tables *because there is no text at all*, so "no tables ->
            # nothing for TableFormer to improve" returned the empty pdfplumber
            # result and Docling was never called. That is the whole reason a
            # Docling run over 854 scans came back with 8 real Docling cards.
            # No text layer means: convert the whole file, and OCR it.
            if not do_ocr:
                return base
            try:
                scanned = self._parse_whole(path, sha1, do_ocr=True)
            except Exception as exc:               # noqa: BLE001 - a scan stays a scan
                self.note_issue("OCR не сработал для %s (%s: %s)"
                                % (path.name, type(exc).__name__, str(exc)[:120]))
                return base
            if any((pg.text or "").strip() for pg in scanned.pages):
                return scanned
            self.note_issue("OCR не дал текста для %s — установлен ли tesseract "
                            "или easyocr для Docling?" % path.name)
            return base

        upgraded = 0
        for start, end in _ranges(candidates):
            try:
                result = self._convert(path, page_range=(start, end), do_ocr=do_ocr)
            except Exception:                    # noqa: BLE001 - keep the cheap pass
                continue
            doc = result.document
            tables = self._docling_tables(doc)
            if tables and min(tables) == 1 and start > 1:
                # Docling renumbered the pages inside the range.
                tables = {k + (start - 1): v for k, v in tables.items()}
            for number in range(start, end + 1):
                if not (1 <= number <= len(base.pages)):
                    continue
                page = base.pages[number - 1]
                # Inside a page_range Docling numbers pages from 1 again, so
                # page `start` of the document is page 1 of this result.
                text = self._page_markdown(doc, number - start + 1)
                if text:
                    page.text = text
                got = tables.get(number)
                if got:
                    page.tables = got
                    upgraded += 1

        markdown = "\n\n".join(p.text for p in base.pages if p.text)
        parser_name = self.name if upgraded else "pdfplumber"
        return ParsedDoc(doc_id=base.doc_id, filename=path.name, sha1=sha1,
                         pages=base.pages, markdown=markdown, parser=parser_name)

    @staticmethod
    def _fill_tables(path: Path, pages: List["Page"]) -> None:
        """Attach pdfplumber tables to pages, by page number.

        Only a fallback for builds where Docling gives no table objects: the
        card extractor works from tables, and without them a Docling run would
        produce beautiful chunks and empty cards.
        """
        try:
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                for page in pages:
                    if page.tables:
                        continue
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
        except Exception:                        # noqa: BLE001 - tables are a bonus
            return


def _ranges(numbers: List[int]) -> List[tuple]:
    """[1, 2, 3, 7] -> [(1, 3), (7, 7)] — Docling converts one page range."""
    out: List[tuple] = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


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


def doc_id_for(path) -> str:
    """The doc id without opening the PDF.

    The pipeline names its cache files after this, and it has to know the name
    before it decides whether the file still needs parsing.
    """
    return _doc_id(Path(path))


def quiet_docling() -> None:
    """Shut Docling's loggers up before they shut the pipeline down.

    A 3 519-file run printed hundreds of thousands of
    `MatchingPostProcessor WARNING Orphan pdf_cell` lines. On Windows the
    console cannot keep up, the writer blocks, and the run stops making
    progress while looking perfectly alive. The warnings are noise: an orphan
    cell only means the layout model found something we do not use.

    Called from every worker, before the first conversion.
    """
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    for name in ("docling", "docling_core", "docling_parse", "docling_ibm_models",
                 "PIL", "matplotlib", "urllib3", "fsspec"):
        try:
            logging.getLogger(name).setLevel(logging.ERROR)
        except Exception:                        # noqa: BLE001 - logging is optional
            pass


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


def get_parser(preferred: str = "auto", **docling_opts) -> BaseParser:
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
        parser: BaseParser = (DoclingParser(**docling_opts) if name == "docling"
                              else PdfPlumberParser())
        try:
            parser.is_available()
            parser.fallback_errors = errors          # why the others were skipped
            return parser
        except ParserUnavailable as exc:
            errors.append("%s: %s" % (name, exc))
    raise ParserUnavailable("No PDF parser available. " + " | ".join(errors))
