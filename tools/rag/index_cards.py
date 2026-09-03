#!/usr/bin/env python3
"""Поисковый индекс по карточкам, а не по PDF.

    python3 tools/rag/index_cards.py --cards data/cards/cards.db --out data/rag
    python3 tools/rag/pipeline.py --from-cards data/cards/cards.db
    python3 tools/rag/pipeline.py --query "collector current 200 mA"

Зачем отдельный путь, если есть `pipeline.py` по PDF:

* **место.** Индекс по PDF стоит 468 МБ на 1000 файлов — 300 000 деталей это
  ~140 ГБ. Индекс по карточкам: 14 459 карточек дают единицы мегабайт, 300 000
  — примерно полтора гигабайта. Разница в два порядка, и она не в точности, а
  в мусоре: в PDF-индексе лежат оглавления, ревизии, юридический текст и
  повторы одной таблицы на пяти страницах;
* **точность.** В карточке уже есть структура: распиновка отдельно от
  предельных режимов, габариты отдельно от электрики. Запрос
  «section=absolute_maximum_ratings» на карточках отвечает ровно тем, чем надо,
  а не случайным абзацем, где эти слова встретились;
* **скорость.** Никакого Docling, никакого OCR: карточки уже собраны.
  300 000 карточек индексируются за минуты, а не за ночь.

Формат индекса ровно тот же (`index_db.RagIndex`), поэтому ничего вокруг не
меняется: `serve.py`, `/api/search`, `--query`, `--stats`, `--check`,
`--repair` работают как раньше. Один документ — одна карточка, разделы
карточки — отдельные чанки с теми же каноническими именами секций
(`pin_configuration`, `absolute_maximum_ratings`, …), так что фильтр по
`--section` продолжает работать.

И то и другое можно держать в одном индексе одновременно: у чанков из PDF
`doc_id` — имя файла, у чанков из карточек — парт-номер.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import cli, index_db, opensearch_index  # noqa: E402
from tools.rag.chunking import SECTION_LABELS  # noqa: E402
from tools.rag.embeddings import get_backend  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data"))
DEFAULT_CARDS = DATA_DIR / "cards" / "cards.db"
DEFAULT_OUT = DATA_DIR / "rag"

# Поле карточки -> каноническая секция. Те же имена, что в chunking.py,
# иначе фильтр --section перестанет находить то, что должен.
FIELD_SECTION = {
    "description": "general_description",
    "features": "features",
    "applications": "applications",
    "pins": "pin_configuration",
    "ratings": "absolute_maximum_ratings",
    "specs": "electrical_characteristics",
    "dimensions": "package_dimensions",
    "order_codes": "ordering_information",
}

TABLE_FIELDS = frozenset({"pins", "ratings", "specs", "dimensions", "order_codes"})


# --------------------------------------------------------------------------- #
# карточка -> чанки
# --------------------------------------------------------------------------- #

def _cell(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _table(header: List[str], rows: List[List[str]]) -> str:
    """Markdown-таблица: её же видит сайт в выдаче поиска."""
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        cells = [(row[i] if i < len(row) else "") for i in range(len(header))]
        out.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(out)


def _one_line(items: List[str], limit: int = 160) -> str:
    text = ", ".join(item for item in items if item)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _render(field: str, value: Any) -> Tuple[str, str]:
    """Поле карточки -> (текст чанка, короткая сводка)."""
    if field == "description":
        text = _cell(value)
        return text, text[:180]
    if field in ("features", "applications"):
        items = [_cell(v) for v in (value or []) if _cell(v)]
        return "\n".join("- " + item for item in items), _one_line(items)
    if field == "pins":
        rows = [[_cell(p.get("n")), _cell(p.get("name")), _cell(p.get("function"))]
                for p in (value or [])]
        summary = "%d pins: %s" % (
            len(rows), _one_line([": ".join(x for x in (r[0], r[1]) if x)
                                  for r in rows[:8]]))
        return _table(["Pin", "Name", "Function"], rows), summary
    if field == "ratings":
        rows = [[_cell(r.get("symbol")), _cell(r.get("label") or r.get("param")),
                 _cell(r.get("text") or r.get("value")), _cell(r.get("unit"))]
                for r in (value or [])]
        summary = "%d ratings: %s" % (
            len(rows), _one_line([": ".join(x for x in (r[0], r[2]) if x)
                                  for r in rows[:8]]))
        return _table(["Symbol", "Parameter", "Value", "Unit"], rows), summary
    if field == "specs":
        rows = [[_cell(s.get("symbol")), _cell(s.get("label") or s.get("param")),
                 _cell(s.get("conditions")), _cell(s.get("min")),
                 _cell(s.get("typ")), _cell(s.get("max")), _cell(s.get("unit"))]
                for s in (value or [])]
        summary = "%d specs: %s" % (
            len(rows), _one_line([": ".join(x for x in (r[0], r[4] or r[3] or r[5])
                                            if x) for r in rows[:8]]))
        return _table(["Symbol", "Parameter", "Conditions", "Min", "Typ", "Max",
                       "Unit"], rows), summary
    if field == "dimensions":
        rows = []
        for name, entry in (value or {}).items():
            if isinstance(entry, dict):
                number, unit = entry.get("value"), entry.get("unit") or ""
            else:
                number, unit = entry, ""
            if number in (None, ""):
                continue
            rows.append([_cell(name), "%s %s" % (_cell(number), _cell(unit))])
        summary = "dimensions: %s" % _one_line(
            ["%s %s" % (r[0], r[1]) for r in rows])
        return _table(["Dimension", "Value"], rows), summary
    if field == "order_codes":
        rows = [[_cell(o.get("code")), _cell(o.get("package")),
                 _cell(o.get("marking"))] for o in (value or [])]
        summary = "order codes: %s" % _one_line([r[0] for r in rows])
        return _table(["Order code", "Package", "Marking"], rows), summary
    return _cell(value), _cell(value)[:180]


def _headline_text(card: Dict[str, Any]) -> str:
    """Ключевые параметры и «шапка» — в текст электрического раздела."""
    lines: List[str] = []
    for source in ("headline",):
        for item in card.get(source) or []:
            if not isinstance(item, dict):
                continue
            label = _cell(item.get("label") or item.get("symbol"))
            text = _cell(item.get("text") or item.get("value"))
            unit = _cell(item.get("unit"))
            if label or text:
                lines.append(("%s: %s %s" % (label, text, unit)).strip())
    for key, entry in (card.get("key_specs") or {}).items():
        if not isinstance(entry, dict):
            continue
        label = _cell(entry.get("label") or key)
        text = _cell(entry.get("text") or entry.get("value"))
        unit = _cell(entry.get("unit"))
        if label or text:
            lines.append(("%s: %s %s" % (label, text, unit)).strip())
    return "\n".join(lines)


def card_chunks(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Одна карточка -> чанки в том же виде, что пишет pipeline.py."""
    part = _cell(card.get("part"))
    doc_id = part.upper() or _cell(card.get("filename"))
    manufacturer = _cell(card.get("manufacturer"))
    package = _cell(card.get("package"))
    filename = _cell(card.get("filename"))

    # Поиск по парт-номеру — это половина всех запросов, а колонка `part` в
    # FTS5 помечена UNINDEXED (она для фильтра, не для поиска). Поэтому
    # парт-номер, производитель и корпус живут в тексте первого же чанка:
    # иначе «MMBT3904» в поиске просто ничего не находит.
    identity = " ".join(x for x in (part, manufacturer, package) if x).strip()

    chunks: List[Dict[str, Any]] = []
    order = 0
    for field, section in FIELD_SECTION.items():
        value = card.get(field)
        if value in (None, "", [], {}):
            continue
        text, summary = _render(field, value)
        if field == "specs":
            extra = _headline_text(card)
            if extra:
                text = (extra + "\n\n" + text).strip()
        if not text.strip():
            continue
        order += 1
        page = 0
        for item in (value if isinstance(value, list) else []):
            if isinstance(item, dict) and item.get("page"):
                page = int(item["page"])
                break
        if section == "general_description" and identity:
            text = identity + "\n\n" + text
        chunks.append({
            "id": "%s::%s" % (doc_id, section),
            "doc_id": doc_id,
            "part": part,
            "manufacturer": manufacturer,
            "package": package,
            "section": section,
            "section_label": SECTION_LABELS.get(section, "Other"),
            "header": SECTION_LABELS.get(section, field),
            "page": page,
            "is_table": field in TABLE_FIELDS,
            "text": text,
            "summary": summary,
            "order": order,
            "filename": filename,
        })

    if not any(c["section"] == "general_description" for c in chunks) and identity:
        # Карточка без описания всё равно должна находиться по парт-номеру.
        chunks.insert(0, {
            "id": "%s::general_description" % doc_id,
            "doc_id": doc_id,
            "part": part,
            "manufacturer": manufacturer,
            "package": package,
            "section": "general_description",
            "section_label": SECTION_LABELS.get("general_description", "Description"),
            "header": part,
            "page": 0,
            "is_table": False,
            "text": identity,
            "summary": identity,
            "order": 0,
            "filename": filename,
        })
    return chunks


def card_doc(card: Dict[str, Any]) -> Dict[str, Any]:
    """Строка таблицы `docs` для карточки."""
    part = _cell(card.get("part"))
    return {
        "doc_id": part.upper() or _cell(card.get("filename")),
        "filename": _cell(card.get("filename")),
        "sha1": _cell(card.get("sha1")),
        "n_pages": int(card.get("pages") or 0),
        "parser": _cell(card.get("parser")) or "cards",
        "part": part,
        "manufacturer": _cell(card.get("manufacturer")),
        "package": _cell(card.get("package")),
        "family": _cell(card.get("family")),
        "conf": float(card.get("confidence") or 0.0),
    }


# --------------------------------------------------------------------------- #
# сборка
# --------------------------------------------------------------------------- #

def build(cards_db: Path, out: Path, rebuild: bool = False, limit: int = 0,
          embed: str = "none", backend: str = "auto", verbose: bool = False) -> dict:
    cards_db = Path(cards_db)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if not cards_db.exists():
        print("Нет базы карточек: %s" % cards_db)
        print("Сначала: python3 tools/rag/build_cards.py --corpus <папка с PDF>")
        return {}

    embedder = get_backend(embed)
    index = opensearch_index.open_index(out / "index.db",
                                        embedding_backend=embedder,
                                        prefer=backend, verbose=verbose)
    if rebuild:
        index.reset()

    done = getattr(index, "doc_ids", lambda: set())()
    started = time.time()
    indexed = skipped = empty = 0
    total_chunks = 0

    con = sqlite3.connect(str(cards_db))
    con.row_factory = sqlite3.Row
    try:
        query = "SELECT part_key, card FROM cards"
        if limit:
            query += " LIMIT %d" % int(limit)
        for row in con.execute(query):
            if not rebuild and row["part_key"] in done:
                skipped += 1
                continue
            try:
                card = json.loads(row["card"])
            except ValueError:
                skipped += 1
                continue
            chunks = card_chunks(card)
            if not chunks:
                empty += 1
                continue
            index.add_document(card_doc(card), chunks)
            total_chunks += len(chunks)
            indexed += 1
            if verbose or indexed % 500 == 0:
                print("  %6d карточек, %7d чанков  %.0f карт/с"
                      % (indexed, total_chunks,
                         indexed / max(1e-9, time.time() - started)), flush=True)
    finally:
        con.close()

    built = index.build_vectors() if embedder.name != "none" else 0
    index.set_meta("source", "cards")
    index.set_meta("cards_db", str(cards_db))
    index.set_meta("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))

    stats = index.stats()
    stats.update({"indexed": indexed, "skipped": skipped, "empty": empty,
                  "chunks_added": total_chunks, "vectors": built})
    print("\nГотово за %.1f с: карточек %d (пропущено %d, пустых %d), "
          "чанков %d, векторов %d"
          % (time.time() - started, indexed, skipped, empty, total_chunks, built))
    verdict = ""
    try:
        from tools.rag import pipeline
        verdict = pipeline.check_index(index, repair=True)
        print("Индекс FTS: %s" % verdict)
    except Exception:                              # noqa: BLE001 - отчёт, не логика
        pass
    index.close()
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(
        description="Индекс поиска по карточкам (быстрее и в 100 раз меньше, "
                    "чем по PDF)")
    ap.add_argument("--cards", type=Path, default=DEFAULT_CARDS,
                    help="база карточек (data/cards/cards.db)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="куда писать index.db (data/rag)")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересобрать индекс с нуля")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "opensearch", "sqlite"])
    ap.add_argument("--embed", default="none",
                    choices=["none", "auto", "sentence-transformers", "st", "openai"])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    stats = build(args.cards, args.out, rebuild=args.rebuild, limit=args.limit,
                  embed=args.embed, backend=args.backend, verbose=args.verbose)
    if stats:
        print("Индекс: %s" % (args.out / "index.db"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
