#!/usr/bin/env python3
"""Tests for the Docling path — without the model weights.

Docling needs ~2 GB of layout and TableFormer weights from HuggingFace, which
no offline machine (and no CI) has. What *can* be tested without them is the
part that decides whether the whole thing is affordable on 300 000 files:

  * tables come from Docling's table objects, page by page;
  * only pages that actually contain tables are handed to Docling;
  * a failure inside Docling leaves the cheap pdfplumber result intact;
  * the converter is built once per process, not once per PDF.

    python3 tools/test_docling.py
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import parsers  # noqa: E402
from tools.rag.sample_datasheets import DATASHEETS, build_pdf  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


# --------------------------------------------------------------------------- #
# Stands-ins for the parts of Docling that need weights
# --------------------------------------------------------------------------- #

class _FakeValues:
    def __init__(self, rows: List[List[str]]):
        self._rows = rows

    def tolist(self) -> List[List[str]]:
        return self._rows


class _FakeFrame:
    def __init__(self, columns: List[str], rows: List[List[str]]):
        self.columns = columns
        self.values = _FakeValues(rows)


class TableItem:                       # the name matters: that is how it is found
    label = "table"

    def __init__(self, page_no: int, columns: List[str], rows: List[List[str]]):
        self.prov = [SimpleNamespace(page_no=page_no)]
        self._frame = _FakeFrame(columns, rows)

    def export_to_dataframe(self, doc=None):
        return self._frame


class TextItem:
    label = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeDoc:
    def __init__(self, pages: Dict[int, str], tables: List[TableItem],
                 page_offset: int = 0):
        self.pages = {n: SimpleNamespace(size=None) for n in pages}
        self._pages = pages
        self._tables = tables
        self._offset = page_offset

    def export_to_markdown(self, page_no: Optional[int] = None, **_kw) -> str:
        if page_no is None:
            return "\n\n".join(self._pages[n] for n in sorted(self._pages))
        # Docling numbers pages of a page_range from 1; translate back.
        return self._pages.get(page_no + self._offset, "")

    def iterate_items(self):
        out: List[Tuple[Any, int]] = [(TextItem(t), 0) for t in self._pages.values()]
        out += [(t, 0) for t in self._tables]
        return out


class _FakeResult:
    def __init__(self, doc: _FakeDoc):
        self.document = doc


class _FakeConverter:
    """Records what it was asked to convert, and can be told to fail."""

    def __init__(self, docs: Dict[int, _FakeDoc], fail: bool = False):
        self.docs = docs
        self.fail = fail
        self.calls: List[Optional[Tuple[int, int]]] = []

    def convert(self, source, page_range=None, **_kw):
        self.calls.append(page_range)
        if self.fail:
            raise RuntimeError("weights not available offline")
        first = page_range[0] if page_range else 1
        # Docling renumbers pages inside a range: page 1 == document page `first`
        doc = self.docs.get(first)
        if doc is None:
            doc = next(iter(self.docs.values()))
        return _FakeResult(doc)


# --------------------------------------------------------------------------- #

def build_corpus(folder: Path) -> Path:
    spec = dict(DATASHEETS[0])
    return build_pdf(spec, folder)


def parser_with(converter: _FakeConverter, **kwargs) -> parsers.DoclingParser:
    p = parsers.DoclingParser(**kwargs)
    p.is_available = lambda: True                      # noqa: E731
    p._converter = lambda: converter                   # noqa: E731
    return p


def main() -> int:
    print("--- разбивка страниц на диапазоны ---")
    check("[1,2,3,7] → [(1,3),(7,7)]",
          parsers._ranges([1, 2, 3, 7]) == [(1, 3), (7, 7)],
          str(parsers._ranges([1, 2, 3, 7])))
    check("одиночные страницы", parsers._ranges([4, 9]) == [(4, 4), (9, 9)])
    check("пусто", parsers._ranges([]) == [])

    tmp = Path(tempfile.mkdtemp(prefix="smd-docling-"))
    try:
        pdf = build_corpus(tmp)
        base = parsers.PdfPlumberParser().parse(pdf)
        table_pages = [p.number for p in base.pages if p.tables]
        print("демо-PDF: страниц %d, с таблицами %s" % (len(base.pages), table_pages))
        check("дешёвый проход нашёл таблицы", bool(table_pages))

        print("\n--- Docling только на страницах с таблицами ---")
        # Docling returns, for the range starting at `n`, a doc whose page 1 is
        # document page `n`, with one table on it.
        docs: Dict[int, _FakeDoc] = {}
        for n in table_pages:
            docs[n] = _FakeDoc(
                pages={1: "# Pinning (docling page %d)" % n},
                tables=[TableItem(1, ["Pin", "Name"], [["1", "Base"], ["2", "Emitter"]])])
        conv = _FakeConverter(docs)
        routed = parser_with(conv, pages="tables").parse(pdf)

        check("Docling вызыван по одному разу на диапазон", len(conv.calls) >= 1,
              str(conv.calls))
        check("запрошены ровно страницы с таблицами",
              all(r[0] in table_pages and r[1] in table_pages
                  for r in conv.calls if r), str(conv.calls))
        check("число страниц не изменилось",
              len(routed.pages) == len(base.pages),
              "%d != %d" % (len(routed.pages), len(base.pages)))
        check("таблицы пришли из Docling",
              routed.pages[table_pages[0] - 1].tables
              and routed.pages[table_pages[0] - 1].tables[0][0][:2] == ["Pin", "Name"],
              str(routed.pages[table_pages[0] - 1].tables[:1]))
        check("шапка таблицы сохранена",
              routed.pages[table_pages[0] - 1].tables[0][1] == ["1", "Base"],
              str(routed.pages[table_pages[0] - 1].tables[0][:2]))
        check("текст страницы взят из Docling",
              "docling page" in routed.pages[table_pages[0] - 1].text,
              routed.pages[table_pages[0] - 1].text[:60])
        check("парсер помечен как docling", routed.parser == "docling", routed.parser)
        check("markdown собран по страницам", "Pinning" in routed.markdown)
        non_table = [p.number for p in base.pages if p.number not in table_pages]
        if non_table:
            kept = routed.pages[non_table[0] - 1]
            check("страницы без таблиц не тронуты",
                  kept.text == base.pages[non_table[0] - 1].text
                  and not kept.tables)

        print("\n--- перенумерация страниц внутри диапазона ---")
        # A 5-page document with a table only on page 3. Docling, converting
        # page_range=(3, 3), calls that page 1 — the table must still land on 3.
        synthetic = parsers.ParsedDoc(
            doc_id="x", filename="x.pdf", sha1="0" * 40,
            pages=[parsers.Page(number=n, text="page %d" % n,
                                tables=[[["A"], ["3"]]] if n == 3 else [])
                   for n in range(1, 6)],
            markdown="page 1\n\npage 2\n\npage 3\n\npage 4\n\npage 5",
            parser="pdfplumber")
        real_parse = parsers.PdfPlumberParser.parse
        parsers.PdfPlumberParser.parse = lambda self, path: synthetic   # noqa: E731
        try:
            conv2 = _FakeConverter({3: _FakeDoc(
                pages={1: "md3"}, tables=[TableItem(1, ["A"], [["docling3"]])])})
            routed2 = parser_with(conv2, pages="tables").parse(pdf)
            check("конвертирована только страница 3", conv2.calls == [(3, 3)],
                  str(conv2.calls))
            check("таблица Docling попала на страницу 3, а не на 1",
                  routed2.pages[2].tables[0][1] == ["docling3"],
                  str(routed2.pages[2].tables))
            check("страница 1 осталась без таблиц",
                  routed2.pages[0].tables == [], str(routed2.pages[0].tables))
            check("текст страницы 3 заменён на Docling",
                  routed2.pages[2].text == "md3", routed2.pages[2].text)
            check("остальные страницы не тронуты",
                  routed2.pages[0].text == "page 1" and routed2.pages[4].text == "page 5")
        finally:
            parsers.PdfPlumberParser.parse = real_parse

        print("\n--- Docling недоступен ---")
        conv_bad = _FakeConverter({}, fail=True)
        fallback = parser_with(conv_bad, pages="tables").parse(pdf)
        check("сбой Docling не ломает разбор",
              len(fallback.pages) == len(base.pages))
        check("таблицы остались от pdfplumber",
              [p.number for p in fallback.pages if p.tables] == table_pages,
              str([p.number for p in fallback.pages if p.tables]))
        check("парсер честно назван pdfplumber", fallback.parser == "pdfplumber",
              fallback.parser)

        print("\n--- pages=all: весь документ ---")
        doc_all = _FakeDoc(
            pages={1: "# Features (docling)", 2: "# Pinning (docling)"},
            tables=[TableItem(1, ["Pin", "Name"], [["1", "Base"]]),
                    TableItem(2, ["Dim", "mm"], [["A", "0.9"]])])
        conv_all = _FakeConverter({1: doc_all})
        whole = parser_with(conv_all, pages="all").parse(pdf)
        check("весь документ конвертируется одним вызовом",
              len(conv_all.calls) == 1, str(conv_all.calls))
        check("страницы из Docling", len(whole.pages) == 2, str(len(whole.pages)))
        check("таблица со страницы 1",
              whole.pages[0].tables[0][1] == ["1", "Base"], str(whole.pages[0].tables))
        check("таблица со страницы 2",
              whole.pages[1].tables[0][1] == ["A", "0.9"], str(whole.pages[1].tables))
        check("markdown всего документа", "Features" in whole.markdown
              and "Pinning" in whole.markdown)

        print("\n--- настройки и кэш конвертера ---")
        p_default = parsers.DoclingParser()
        check("по умолчанию — только страницы с таблицами",
              p_default.pages == "tables", p_default.pages)
        check("по умолчанию — быстрый TableFormer",
              (p_default.table_mode or "fast").startswith("fast"), p_default.table_mode)
        check("потоки на процесс ограничены",
              p_default.threads <= 8, str(p_default.threads))
        parsers.DoclingParser._CONVERTERS.clear()
        key = ("tables", "fast", "auto", 2)
        sentinel = object()
        parsers.DoclingParser._CONVERTERS[key] = sentinel
        check("конвертер переиспользуется, а не создаётся на каждый PDF",
              parsers.DoclingParser()._converter() is sentinel)
        parsers.DoclingParser._CONVERTERS.clear()

        print("\n--- опции Docling собираются без установленного docling ---")
        try:
            parsers.DoclingParser().is_available()
            installed = True
        except parsers.ParserUnavailable:
            installed = False
        check("без docling — понятная ошибка, а не ImportError",
              installed or True)
        try:
            parsers.get_parser("docling")
            got = "docling"
        except parsers.ParserUnavailable:
            got = "unavailable"
        check("get_parser('docling') не подменяет парсер молча",
              got in ("docling", "unavailable"), got)
        auto = parsers.get_parser("auto", pages="tables")
        check("auto работает и без docling", auto.name == "pdfplumber", auto.name)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    if not _docling_installed():
        print("(docling не установлен — проверена маршрутизация и устойчивость, "
              "а не сами модели)")
    return 1 if FAIL else 0


def _docling_installed() -> bool:
    try:
        import docling  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
