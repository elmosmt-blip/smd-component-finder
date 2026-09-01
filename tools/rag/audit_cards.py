#!/usr/bin/env python3
"""Why do the cards look so uneven? Read it out of the database.

Extraction is rule-based, so a card is as rich as the PDF it came from: a
proper vendor datasheet gives package, pinout, ratings and dimensions, while a
scan gives nothing but a file name. That is honest, but on a grid of cards it
looks like the parser is broken at random.

This tool turns "the cards are all different" into numbers:

    python3 tools/rag/audit_cards.py
    python3 tools/rag/audit_cards.py --corpus data/datasheets --top 20
    python3 tools/rag/audit_cards.py --scan-check --scan-limit 300
    python3 tools/rag/audit_cards.py --csv audit.csv

  * coverage per field  — how many cards actually have a package, a pinout…
  * tiers               — full / partial / sparse / empty
  * reasons             — *why* cards are thin, counted across the corpus
  * the worst cards     — so you can open three files and see for yourself
  * --scan-check        — opens the PDFs and measures the text layer, which is
                          the one thing the database cannot tell you afterwards

Nothing here writes to the database.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import card_store, cli, quality  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data")) / "cards" / "cards.db"

FIELD_RU = {
    "part": "парт-номер",
    "manufacturer": "производитель",
    "package": "корпус",
    "description": "описание",
    "features": "особенности",
    "pins": "распиновка",
    "ratings": "максимумы",
    "specs": "электрика",
    "dimensions": "размеры",
}

TIER_RU = {
    "full": "полные (8–9 полей)",
    "partial": "средние (5–7)",
    "sparse": "бедные (2–4)",
    "empty": "пустые (только имя)",
}

REASON_RU = {
    "scan": "нет текстового слоя (скан)",
    "low_text": "текста почти нет",
    "no_tables": "в PDF нет таблиц",
    "no_package": "корпус в документе не назван",
    "no_pins": "нет таблицы распиновки",
    "no_manufacturer": "производитель не указан",
    "no_description": "нет абзаца-описания",
    "part_from_filename": "парт-номер взят из имени файла",
}


def load_cards(db: Path) -> List[dict]:
    store = card_store.open_store(db)
    try:
        return store.iter_cards()
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# scan check — the one thing the database cannot answer later
# --------------------------------------------------------------------------- #

def scan_check(cards: List[dict], corpus: Path, limit: int) -> Dict[str, Any]:
    """Open the PDFs behind the thinnest cards and count characters.

    A PDF with no text layer is a scan: pdfplumber returns an empty string and
    no amount of clever parsing helps — it needs OCR. That is a property of the
    *file*, and the database only keeps the file name, so this has to go back
    to disk. Page 1 is enough to tell.
    """
    try:
        import pdfplumber
    except ImportError:
        return {"available": False, "checked": 0, "scans": [], "reason": "нет pdfplumber"}

    targets: List[Tuple[str, Path]] = []
    seen = set()
    for card in sorted(cards, key=lambda c: float(c.get("confidence") or 0.0)):
        name = os.path.basename((card.get("filename") or "").replace("\\", "/"))
        if not name:
            continue
        path = (corpus / name) if (corpus / name).exists() else None
        if path is None:
            match = sorted(corpus.rglob(name)) if corpus.exists() else []
            path = match[0] if match else None
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        targets.append((card.get("part") or name, path))
        if len(targets) >= max(0, limit):
            break

    scans: List[dict] = []
    for part, path in targets:
        try:
            with pdfplumber.open(str(path)) as pdf:
                n_pages = len(pdf.pages)
                text = pdf.pages[0].extract_text() or "" if n_pages else ""
        except Exception as exc:                  # noqa: BLE001 - a broken PDF is data
            scans.append({"part": part, "file": path.name, "pages": 0,
                          "chars": 0, "error": type(exc).__name__})
            continue
        if len(text.strip()) < quality.CHARS_PER_PAGE_LOW:
            try:
                size_kb = round(path.stat().st_size / 1024.0, 1)
            except OSError:
                size_kb = 0.0
            scans.append({"part": part, "file": path.name, "pages": n_pages,
                          "chars": len(text.strip()), "kb": size_kb})

    return {"available": True, "checked": len(targets), "scans": scans,
            "corpus": str(corpus)}


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def _bar(n: int, total: int, width: int = 24) -> str:
    filled = int(round(width * n / total)) if total else 0
    return "#" * filled + "." * (width - filled)


def report(summary: Dict[str, Any], db: Path, top: int) -> List[str]:
    lines: List[str] = []
    total = summary["total"]
    if not total:
        return ["Карточек в базе нет: %s" % db]

    lines.append("Заполненность полей (%d карточек)" % total)
    for field in quality.CARD_FIELDS:
        n = summary["per_field"].get(field, 0)
        lines.append("  %-16s %6d  %5.1f%%  %s"
                     % (FIELD_RU.get(field, field), n, quality.pct(n, total),
                        _bar(n, total)))

    lines.append("")
    lines.append("Группы")
    for tier in quality.TIER_ORDER:
        n = summary["tiers"].get(tier, 0)
        lines.append("  %-24s %6d  %5.1f%%" % (TIER_RU.get(tier, tier), n,
                                               quality.pct(n, total)))
    lines.append("  средняя полнота: %.0f%%" % (summary["avg_confidence"] * 100))

    if summary["reasons"]:
        lines.append("")
        lines.append("Почему карточки пустые (одна карточка может попасть в несколько строк)")
        for code, n in sorted(summary["reasons"].items(), key=lambda kv: -kv[1]):
            lines.append("  %-34s %6d  %5.1f%%  %s"
                         % (REASON_RU.get(code, code), n, quality.pct(n, total),
                            _bar(n, total)))

    if summary["worst"]:
        lines.append("")
        lines.append("Худшие карточки (%d) — откройте эти PDF глазами" % len(summary["worst"]))
        for i, item in enumerate(summary["worst"][:top], 1):
            head = "%2d. %-22s полнота %3.0f%%" % (
                i, item["part"] or "(без имени)", (item["confidence"] or 0) * 100)
            lines.append(head)
            lines.append("      файл:     %s" % (item["filename"] or "?"))
            if item["reasons"]:
                lines.append("      причина:  %s" % ", ".join(
                    REASON_RU.get(c, c) for c in item["reasons"]))
            if item["missing"]:
                lines.append("      пусто:    %s" % ", ".join(
                    FIELD_RU.get(f, f) for f in item["missing"]))
    return lines


def advice(summary: Dict[str, Any], scan: Optional[Dict[str, Any]]) -> List[str]:
    """What to actually do about it."""
    out: List[str] = []
    total = summary["total"] or 1
    reasons = summary["reasons"]
    scans = reasons.get("scan", 0)
    no_tables = reasons.get("no_tables", 0)

    if scans:
        out.append(
            "* скан без текстового слоя: %d карточек (%.0f%%). Им нужен Docling с "
            "OCR — обычный парсер тут бессилен. Отберите их: "
            "python3 tools/rag/audit_cards.py --csv thin.csv, затем прогоните "
            "только эту папку." % (scans, quality.pct(scans, total)))
    if no_tables:
        out.append(
            "* таблиц не найдено: %d карточек (%.0f%%). Распиновка и параметры "
            "живут в таблицах, значит pdfplumber их не увидел. Проверьте на "
            "одном файле Docling: python3 tools/rag/build_cards.py --corpus "
            "<папка> --parser docling --limit 20 --rebuild." % (
                no_tables, quality.pct(no_tables, total)))
    if not out:
        out.append("* явных причин не видно — пришлите вывод этого скрипта целиком.")
    out.append(
        "* перепарсить только плохие файлы: положите их в одну папку и "
        "python3 tools/rag/build_cards.py --corpus <папка> --parser docling "
        "(карточка перезапишется, если станет богаче).")
    return out


def write_csv(path: Path, cards: List[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["part", "filename", "confidence", "tier", "package",
              "manufacturer", "pin_count", "pages", "tables",
              "missing", "reasons", "flags"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(fields)
        for card in cards:
            missing = [f for f, ok in quality.filled_fields(card).items() if not ok]
            writer.writerow([
                card.get("part") or "", card.get("filename") or "",
                card.get("confidence") or 0, quality.tier(card),
                card.get("package") or "", card.get("manufacturer") or "",
                card.get("pin_count") or "", card.get("pages") or "",
                card.get("tables") or "",
                " ".join(missing), " ".join(quality.reason_codes(card)),
                " ".join(card.get("flags") or []),
            ])
    return len(cards)


def main(argv: Optional[List[str]] = None) -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(
        description="Почему карточки получаются разными: отчёт по базе карточек")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="путь к cards.db")
    ap.add_argument("--corpus", type=Path, default=None,
                    help="папка с PDF (нужна только для --scan-check)")
    ap.add_argument("--top", type=int, default=10, help="сколько худших карточек показать")
    ap.add_argument("--csv", type=Path, default=None, help="выгрузить все карточки в CSV")
    ap.add_argument("--scan-check", action="store_true",
                    help="открыть PDF худших карточек и измерить текстовый слой")
    ap.add_argument("--scan-limit", type=int, default=200,
                    help="сколько файлов открыть в --scan-check")
    ap.add_argument("--quiet", action="store_true", help="только цифры, без советов")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print("Базы нет: %s" % args.db)
        print("Сначала: python3 tools/rag/build_cards.py --corpus data/datasheets --jobs 8")
        return 1

    cards = load_cards(args.db)
    summary = quality.summarise(cards, worst=args.top)

    print("=" * 72)
    print("Аудит карточек — %s" % args.db)
    print("=" * 72)
    for line in report(summary, args.db, args.top):
        print(line)

    if args.scan_check:
        corpus = args.corpus or (args.db.parent.parent / "datasheets")
        print("")
        print("Проверка текстового слоя (папка %s)" % corpus)
        if not corpus.exists():
            print("  [--] папки нет — укажите --corpus")
        else:
            scan = scan_check(cards, corpus, args.scan_limit)
            if not scan["available"]:
                print("  [--] %s" % scan["reason"])
            else:
                print("  открыто файлов: %d" % scan["checked"])
                if scan["scans"]:
                    print("  похожи на скан (меньше %d символов на первой странице): %d"
                          % (quality.CHARS_PER_PAGE_LOW, len(scan["scans"])))
                    for item in scan["scans"][:args.top]:
                        extra = (" %s КБ, %d стр." % (item.get("kb"), item["pages"])
                                 if "kb" in item else
                                 (" %s" % item.get("error", "") if item.get("error") else ""))
                        print("    %-22s %-34s символов: %d%s"
                              % (item["part"], item["file"], item["chars"], extra))
                else:
                    print("  сканов не найдено — текстовый слой есть везде")

    if not args.quiet:
        print("")
        print("Что делать")
        for line in advice(summary, None):
            print(line)

    if args.csv:
        n = write_csv(args.csv, cards)
        print("")
        print("Все карточки выгружены: %s (%d строк)" % (args.csv, n))

    print("")
    print("Этот вывод можно целиком вставить в чат.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
