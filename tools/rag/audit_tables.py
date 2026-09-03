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
    python3 tools/rag/audit_tables.py --corpus C:\smd-corpus\scans --parser pdfplumber

The output is meant to be pasted into a chat: frequencies first, then a few
full examples. Nothing here writes to the database.

`--json` is rewritten after every file, on purpose. A 50-file audit takes tens
of minutes, and on Windows it can end in STATUS_STACK_OVERFLOW — a crash that
no `except` catches, so neither `finally` nor Ctrl-C handling would save the
result. Writing as we go means a crash at file 41 still leaves 41 files of
answer on disk instead of a 45-minute hole.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
          progress: bool = True, parser=None,
          sink: Optional[Callable[[Dict[str, Any]], None]] = None
          ) -> Dict[str, Any]:
    """Parse every file, classify every table, count what came out.

    `sink` is called with a snapshot of the result after every file, so the
    caller can persist it. That is what makes a crash survivable: the numbers
    from the files that did get parsed are already on disk.
    """
    parser = parser or parsers.get_parser("auto")
    kinds: Counter = Counter()
    unknown: Counter = Counter()
    unknown_examples: Dict[str, List[str]] = {}
    pin_shaped = 0
    pin_shaped_caught = 0
    docs = 0
    docs_without_tables = 0
    tables_total = 0
    failures: List[str] = []
    done = [0]

    def snapshot() -> Dict[str, Any]:
        return {
            # How far the run got, so a partial file is never mistaken for a
            # complete one — the whole point of writing as we go.
            "files_total": len(pdf_paths),
            "files_done": done[0],
            "docs": docs,
            "docs_without_tables": docs_without_tables,
            "tables": tables_total,
            "kinds": dict(kinds),
            # Recognised on purpose and then dropped: register maps and the
            # like. Not a failure: counting them as "распознано" would flatter
            # the number, counting them as "не распознано" would hide the fix.
            "ignored": sum(n for k, n in kinds.items() if k in extract.IGNORED_KINDS),
            "recognised": sum(n for k, n in kinds.items()
                              if k not in extract.IGNORED_KINDS),
            # `unknown` is truncated to `--top` for printing; the total is not.
            "unknown_total": sum(unknown.values()),
            "unknown": unknown.most_common(top),
            "unknown_examples": unknown_examples,
            "pin_shaped_caught": pin_shaped_caught,
            "pin_shaped_headers": pin_shaped,
            "failures": failures,
        }

    for i, path in enumerate(pdf_paths, 1):
        if progress and (i % 10 == 0 or i == len(pdf_paths)):
            print("  %d/%d файлов…" % (i, len(pdf_paths)), flush=True)
        try:
            # Torch's table-transformer recurses deep enough to exhaust the
            # 1 MB stack Windows gives python.exe; the process then dies with
            # 0xC00000FD and no `except` in the world can catch it. A thread
            # with a big stack is the only thing that survives.
            doc = cli.run_with_big_stack(parser.parse, path)
        except Exception as exc:                   # noqa: BLE001 - a broken PDF is data
            failures.append("%s: %s" % (path.name, type(exc).__name__))
            done[0] = i
            if sink:
                sink(snapshot())
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

        done[0] = i
        if sink:
            sink(snapshot())

    return snapshot()


def report(result: Dict[str, Any], top: int) -> List[str]:
    lines: List[str] = []
    tables = result["tables"]

    # A partial run must say so in the first line, otherwise forty minutes of
    # work read like a complete answer to a question it only half answered.
    done = result.get("files_done")
    total = result.get("files_total")
    if done is not None and total and done < total:
        lines.append("ВНИМАНИЕ: прогон прерван, обработано %d файлов из %d. "
                     "Цифры ниже — по тем, что успели." % (done, total))
        lines.append("")
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
    ap.add_argument("--json", type=Path, default=None,
                    help="выгружать всё в JSON — файл перезаписывается после "
                         "каждого PDF, чтобы падение не съело результат")
    ap.add_argument("--parser", default="auto",
                    help="auto | pdfplumber | docling. Если Docling падает "
                         "(0xC00000FD на Windows — переполнение стека в torch), "
                         "ставьте pdfplumber: классификация таблиц от парсера "
                         "не зависит")
    ap.add_argument("--flush-every", type=int, default=1,
                    help="писать JSON каждые N файлов (по умолчанию после "
                         "каждого — на медленном диске поставьте 5)")
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

    def write_json(result: Dict[str, Any]) -> None:
        if not args.json:
            return
        args.json.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.json.with_suffix(args.json.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(str(tmp), str(args.json))

    try:
        parser = parsers.get_parser(args.parser)
    except parsers.ParserUnavailable as exc:
        print("Парсер %s недоступен: %s" % (args.parser, exc))
        return 1
    print("Парсер: %s" % parser.name)

    seen = [0]

    def sink(result: Dict[str, Any]) -> None:
        seen[0] += 1
        if args.flush_every <= 1 or seen[0] % args.flush_every == 0:
            write_json(result)

    result: Dict[str, Any] = {}
    try:
        result = audit(pdfs, top=args.top, parser=parser, sink=sink)
    except KeyboardInterrupt:
        print("")
        print("Прервано (Ctrl-C) — вывожу то, что успело набраться.")
        result = {}

    print("")
    print("=" * 72)
    print("Аудит таблиц — %s" % corpus)
    print("=" * 72)
    if result:
        for line in report(result, args.top):
            print(line)
        if args.json:
            write_json(result)
            print("")
            print("Полные данные: %s" % args.json)
    elif args.json and args.json.exists():
        print("")
        print("Прогон не завершился. Данные по обработанным файлам — в %s:"
              % args.json)
        try:
            partial = json.loads(args.json.read_text(encoding="utf-8"))
            for line in report(partial, args.top):
                print(line)
        except (ValueError, KeyError) as exc:
            print("  (прочитать не удалось: %s)" % exc)
    else:
        print("Ни одного файла не обработано, данных нет.")

    print("")
    print("Этот вывод можно целиком вставить в чат — по нему дополню словарь заголовков.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
