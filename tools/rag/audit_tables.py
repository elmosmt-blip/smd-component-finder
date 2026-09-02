#!/usr/bin/env python3
"""What is in the tables we fail to recognise.

`audit_cards.py` says *how many* cards are thin. This says *why*, in the only
way that can actually be fixed: it opens a sample of the PDFs, runs the very
same classifier the pipeline uses, and prints the headers of the tables that
came back as "unknown".

    91 % of the PDFs have tables, but only 29 % of the cards have a pinout —
    so the tables are there and the classifier is not seeing them. Which
    words are missing is the whole question, and only the corpus can answer it.

    python3 tools/rag/audit_tables.py --corpus data/datasheets --limit 50
    python3 tools/rag/audit_tables.py --corpus data/datasheets --top 40 --json t.json

The output is meant to be pasted into a chat: frequencies first, then a few
full examples. Nothing here writes to the database.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import cli, extract, parsers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CORPUS = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data")) / "datasheets"

KIND_RU = {
    "registers": "регистры МК (распознаны и отброшены)",
    "pins": "распиновка",
    "ratings": "предельные режимы",
    "electrical": "электрические характеристики",
    "dimensions": "габариты",
    "ordering": "коды заказа",
    None: "НЕ РАСПОЗНАНА",
}


def _fingerprint(table: List[List[str]], width: int = 5, cell: int = 18) -> str:
    """A short, comparable signature of the header row."""
    merged, rows_used = extract.merge_header(table)
    cells = merged if sum(1 for c in merged if c) >= 2 else \
        [extract._norm_header(c) for c in (table[0] if table else [])]
    out = []
    for c in cells[:width]:
        c = (c or "?").strip()
        out.append(c[:cell])
    return " | ".join(out)


def _first_row(table: List[List[str]], cell: int = 16) -> str:
    merged, rows_used = extract.merge_header(table)
    for row in table[rows_used:]:
        cells = [(c or "").strip()[:cell] for c in row]
        if any(cells):
            return " | ".join(cells[:5])
    return ""


def audit(pdf_paths: List[Path], top: int = 25, examples: int = 3,
          progress: bool = True) -> Dict[str, Any]:
    """Parse every file, classify every table, count what came out."""
    parser = parsers.get_parser("auto")
    kinds: Counter = Counter()
    unknown: Counter = Counter()
    unknown_examples: Dict[str, List[str]] = {}
    pin_shaped = 0
    pin_shaped_caught = 0
    docs = 0
    docs_without_tables = 0
    tables_total = 0
    failures: List[str] = []

    for i, path in enumerate(pdf_paths, 1):
        if progress and (i % 10 == 0 or i == len(pdf_paths)):
            print("  %d/%d файлов…" % (i, len(pdf_paths)), flush=True)
        try:
            doc = parser.parse(path)
        except Exception as exc:                   # noqa: BLE001 - a broken PDF is data
            failures.append("%s: %s" % (path.name, type(exc).__name__))
            continue
        docs += 1
        tables_here = 0
        for page in doc.pages:
            for table in page.tables:
                if not table or len(table) < 2:
                    continue
                tables_here += 1
                tables_total += 1
                verdict = extract.classify_table(table)
                if verdict is None:
                    shape = extract._shape_is_pins(table)
                    if shape:
                        pin_shaped_caught += 1
                    fp = _fingerprint(table)
                    unknown[fp] += 1
                    unknown_examples.setdefault(fp, [])
                    if len(unknown_examples[fp]) < examples:
                        unknown_examples[fp].append("%s :: %s" % (
                            path.name, _first_row(table)))
                else:
                    kinds[verdict[0]] += 1
                    if verdict[0] == "pins" and extract._shape_is_pins(table):
                        pin_shaped += 1
        if not tables_here:
            docs_without_tables += 1

    return {
        "docs": docs,
        "docs_without_tables": docs_without_tables,
        "tables": tables_total,
        "kinds": dict(kinds),
        # Recognised on purpose and then dropped: register maps and the like.
        # They are not failures — counting them as "распознано" would flatter
        # the number, counting them as "не распознано" would hide the fix.
        "ignored": sum(n for k, n in kinds.items() if k in extract.IGNORED_KINDS),
        "recognised": sum(n for k, n in kinds.items() if k not in extract.IGNORED_KINDS),
        # `unknown` is truncated to `--top` for printing; the total is not.
        "unknown_total": sum(unknown.values()),
        "unknown": unknown.most_common(top),
        "unknown_examples": unknown_examples,
        "pin_shaped_caught": pin_shaped_caught,
        "pin_shaped_headers": pin_shaped,
        "failures": failures,
    }


def report(result: Dict[str, Any], top: int) -> List[str]:
    lines: List[str] = []
    tables = result["tables"]
    recognised = result["recognised"]
    pct = (100.0 * recognised / tables) if tables else 0.0

    lines.append("Документов разобрано: %d (без единой таблицы: %d)"
                 % (result["docs"], result["docs_without_tables"]))
    lines.append("Таблиц найдено: %d, распознано %d (%.1f%%)"
                 % (tables, recognised, pct))
    if result["kinds"]:
        lines.append("")
        lines.append("Что распозналось")
        for kind, n in sorted(result["kinds"].items(), key=lambda kv: -kv[1]):
            lines.append("  %-30s %6d  %5.1f%%"
                         % (KIND_RU.get(kind, kind), n, 100.0 * n / max(1, tables)))

    ignored = result.get("ignored", 0)
    if ignored:
        lines.append("")
        lines.append("Распознано и отброшено (в карточку не идёт): %d (%.1f%%)"
                     % (ignored, 100.0 * ignored / max(1, tables)))

    unknown_total = result.get("unknown_total") or sum(n for _fp, n in result["unknown"])
    if unknown_total:
        lines.append("")
        lines.append("НЕ распознано: %d таблиц (%.1f%%). Частые заголовки "
                     "(показаны первые %d):"
                     % (unknown_total, 100.0 * unknown_total / max(1, tables),
                        len(result["unknown"])))
        for i, (fp, n) in enumerate(result["unknown"][:top], 1):
            lines.append("  %2d. [%4d×] %s" % (i, n, fp))
        lines.append("")
        lines.append("Примеры строк из этих таблиц")
        shown = 0
        for fp, n in result["unknown"][:top]:
            for example in result["unknown_examples"].get(fp, [])[:1]:
                lines.append("  [%s]" % fp)
                lines.append("      %s" % example)
                shown += 1
                if shown >= min(12, top):
                    break
            if shown >= min(12, top):
                break

    if result["pin_shaped_caught"] or result["pin_shaped_headers"]:
        lines.append("")
        lines.append("Таблиц, похожих на распиновку по форме (1,2,3… в первом столбце): "
                     "%d распознано по заголовку, %d не распознано вообще"
                     % (result["pin_shaped_headers"], result["pin_shaped_caught"]))
    if result["failures"]:
        lines.append("")
        lines.append("Не прочитались: %d" % len(result["failures"]))
        for line in result["failures"][:5]:
            lines.append("  %s" % line[:120])
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(
        description="Какие таблицы в корпусе не распознаются и почему")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--limit", type=int, default=50,
                    help="сколько PDF разобрать (выборка случайная)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--top", type=int, default=25, help="сколько заголовков показать")
    ap.add_argument("--json", type=Path, default=None, help="выгрузить всё в JSON")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print("Нет папки: %s" % corpus)
        return 1
    pdfs = sorted(corpus.rglob("*.pdf"))
    if not pdfs:
        print("В %s нет PDF" % corpus)
        return 1
    if args.limit and len(pdfs) > args.limit:
        random.Random(args.seed).shuffle(pdfs)
        pdfs = pdfs[:args.limit]
        print("Случайная выборка: %d файлов из %s" % (len(pdfs), corpus))
    else:
        print("Разбираю %d файлов из %s" % (len(pdfs), corpus))

    result = audit(pdfs, top=args.top)
    print("")
    print("=" * 72)
    print("Аудит таблиц — %s" % corpus)
    print("=" * 72)
    for line in report(result, args.top):
        print(line)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print("")
        print("Полные данные: %s" % args.json)

    print("")
    print("Этот вывод можно целиком вставить в чат — по нему дополню словарь заголовков.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
