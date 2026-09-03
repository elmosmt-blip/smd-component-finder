#!/usr/bin/env python3
"""Tests for table recognition: the header vocabulary and the table audit.

Card extraction lives or dies on one function — `classify_table`. A pinout is
"Pin / Name / Function" at one vendor and "Terminal No. / Symbol / I-O" at
another; the words decide, and the words only ever come from a real corpus.
So there are two things to test: that the vocabulary we already know still
works, and that we can *see* the tables we do not know (audit_tables.py).

    python3 tools/test_tables.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import threading
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import audit_tables, cli, extract, parsers, sample_datasheets  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


def kind_of(header, *rows):
    table = [list(header)] + [list(r) for r in rows]
    verdict = extract.classify_table(table)
    return verdict[0] if verdict else None


PIN_ROWS = (["1", "Base", "Control input"], ["2", "Emitter", "Ground"],
            ["3", "Collector", "Output"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-tables-"))

    print("--- сопоставление заголовков по границам слов ---")
    check("'mm' не находится внутри 'dummy'",
          extract._cell_has(["dummy", "min", "max"], "mm") is False)
    check("'pin' не находится внутри 'spindle'",
          extract._cell_has(["spindle", "type"], "pin") is False)
    check("'mm' находится в 'dimensions in mm'",
          extract._cell_has(["dimensions in mm"], "mm") is True)
    check("'pin' находится в 'pin no'",
          extract._cell_has(["pin no", "name"], "pin") is True)
    check("'dim' совпадает только с целым словом 'dim'",
          extract._cell_has(["dim", "min"], "dim") is True)
    check("'dimension' накрывает 'dimensions' по основе",
          extract._cell_has(["dimensions", "min"], "dimension") is True)
    check("короткий токен не накрывает чужое слово",
          extract._cell_has(["dimmer", "min"], "dim") is False)
    check("'mm' не совпадает с парт-номером MMBT3904",
          extract._cell_has(["order code mmbt3904", "package"], "mm") is False)
    check("пустая ячейка не ломает поиск",
          extract._cell_has(["", "min"], "min") is True)

    print("\n--- распознавание таблиц по заголовку ---")
    check("Pin / Name / Function",
          kind_of(["Pin", "Name", "Function"], *PIN_ROWS) == "pins")
    check("Terminal No. / Symbol / Description",
          kind_of(["Terminal No.", "Symbol", "Description"], *PIN_ROWS) == "pins")
    check("Pad / Signal / Type (BGA)",
          kind_of(["Pad", "Signal", "Type"], *PIN_ROWS) == "pins")
    check("Ball / Name / Description (BGA)",
          kind_of(["Ball", "Name", "Description"], *PIN_ROWS) == "pins")
    check("PIN / NO. / NAME с двухэтажным заголовком",
          kind_of(["PIN", "", "TYPE", "DESCRIPTION"],
                  ["NO.", "NAME", "", ""], *PIN_ROWS) == "pins")
    check("Symbol / Parameter / Value / Unit — максимумы",
          kind_of(["Symbol", "Parameter", "Value", "Unit"],
                  ["Vceo", "Collector-emitter voltage", "40", "V"]) == "ratings")
    check("Symbol / Parameter / Min / Typ / Max — электрика",
          kind_of(["Symbol", "Parameter", "Conditions", "Min", "Typ", "Max", "Unit"],
                  ["hFE", "DC current gain", "IC=10mA", "100", "", "300", ""]) == "electrical")
    check("Dim / Min / Nom / Max — габариты",
          kind_of(["Dim", "Min", "Nom", "Max"], ["A", "1.75", "1.90", "2.05"]) == "dimensions")
    check("Dimensions in Millimeters / Inches — габариты",
          kind_of(["Symbol", "Millimeters", "Inches"],
                  ["A", "1.75", "0.069"]) == "dimensions")
    check("Order code / Package / Marking — заказ",
          kind_of(["Order code", "Package", "Marking"],
                  ["MMBT3904", "SOT-23", "1AM"]) == "ordering")
    check("чужая таблица не распознаётся",
          kind_of(["Ref", "Qty", "Note"], ["R1", "4", "1% 0603"]) is None)
    check("'Dummy / Min / Max' — не габариты (старая ошибка подстроки)",
          kind_of(["Dummy", "Min", "Max"], ["A", "1", "2"]) is None)
    check("одна строка — не таблица",
          extract.classify_table([["Pin", "Name"]]) is None)

    print("\n--- регистры МК: распознаются, чтобы быть отброшенными ---")
    # Половина даташита на микроконтроллер — это карта регистров. В аудите
    # 50 файлов они дали 385 "нераспознанных" таблиц и забили весь вывод.
    REG_ROWS = (["bit 7", "gie", "rw-0"], ["bit 6", "peie", "rw-0"])
    check("Core Registers Tab",
          kind_of(["Core Registers Tab"], *REG_ROWS) == "registers")
    check("Name / intcon / bit 7 gie",
          kind_of(["Name", "intcon", "bit 7 gie"], *REG_ROWS) == "registers")
    check("Name / config1 / bits 13 8",
          kind_of(["Name", "config1", "bits 13 8"], *REG_ROWS) == "registers")
    check("0x0F / bit 7 / reset",
          kind_of(["0x0F", "bit 7", "reset"], *REG_ROWS) == "registers")
    check("SFR / address / reset",
          kind_of(["SFR", "address", "reset"], *REG_ROWS) == "registers")
    check("распиновка НЕ уходит в регистры",
          kind_of(["Pin", "Name", "Function"], *PIN_ROWS) == "pins")
    check("электрика НЕ уходит в регистры",
          kind_of(["Symbol", "Parameter", "Min", "Typ", "Max", "Unit"],
                  ["VCEO", "Collector-emitter voltage", " ", " ", "40", "V"])
          == "electrical")
    check("счётчик регистров молчит на обычных заголовках",
          extract._register_score(["pin", "name", "function"]) == 0,
          str(extract._register_score(["pin", "name", "function"])))
    check("одинокое слабое слово не делает таблицу регистрами",
          extract._register_score(["name", "type"]) < 2,
          str(extract._register_score(["name", "type"])))

    print("\n--- распиновка по форме, без заголовка ---")
    bare = [["1", "Base", "Control input"], ["2", "Emitter", "Ground"],
            ["3", "Collector", "Output"]]
    verdict = extract.classify_table(bare)
    check("1,2,3 в первом столбце — это распиновка",
          verdict is not None and verdict[0] == "pins", str(verdict))
    check("заголовочных строк съедено ноль", verdict and verdict[2] == 0, str(verdict))
    check("номера выводов идут с первого столбца",
          verdict and verdict[1].get("pin") == 0, str(verdict))
    check("счёт 1,2,2 — не распиновка",
          extract._shape_is_pins([["1", "a", "b"], ["2", "c", "d"],
                                  ["2", "e", "f"]]) is False)
    check("счёт не с единицы — не распиновка",
          extract._shape_is_pins([["5", "a", "b"], ["6", "c", "d"],
                                  ["7", "e", "f"]]) is False)
    check("длинный текст во втором столбце — не распиновка",
          extract._shape_is_pins([["1", "x" * 80, "b"], ["2", "c", "d"],
                                  ["3", "e", "f"]]) is False)
    check("две строки мало",
          extract._shape_is_pins([["1", "a", "b"], ["2", "c", "d"]]) is False)

    print("\n--- аудит таблиц на сгенерированном корпусе ---")
    corpus = tmp / "corpus"
    corpus.mkdir()
    base = dict(sample_datasheets.DATASHEETS[0])

    odd = dict(base)
    odd["part"] = "ODDPINS"
    odd["pinout"] = [["Terminal No.", "Symbol", "Description"],
                     ["1", "VCC", "Supply"], ["2", "GND", "Ground"],
                     ["3", "OUT", "Output"]]
    odd["package_dims"] = [["Dim", "Min", "Nom", "Max"],
                           ["A", "1.75", "1.90", "2.05"],
                           ["b", "0.30", "0.40", "0.50"]]

    bare_spec = dict(base)
    bare_spec["part"] = "BAREPINS"
    bare_spec["pinout"] = [["1", "Base", "Control input"],
                           ["2", "Emitter", "Ground"],
                           ["3", "Collector", "Output"]]

    for spec in (base, odd, bare_spec):
        sample_datasheets.build_pdf(spec, corpus)
    (corpus / "broken.pdf").write_bytes(b"not a pdf at all")

    result = audit_tables.audit(sorted(corpus.glob("*.pdf")), top=10, progress=False)
    check("битый файл не роняет аудит", "broken.pdf" in " ".join(result["failures"]),
          str(result["failures"]))
    check("три документа разобраны", result["docs"] == 3, str(result["docs"]))
    check("таблицы найдены", result["tables"] > 10, str(result["tables"]))
    check("распиновка распознана", result["kinds"].get("pins", 0) >= 3,
          str(result["kinds"]))
    check("максимумы распознаны", result["kinds"].get("ratings", 0) >= 3,
          str(result["kinds"]))
    check("габариты распознаны", result["kinds"].get("dimensions", 0) >= 2,
          str(result["kinds"]))
    check(" Terminal-таблица не ушла в неизвестные",
          not any("terminal" in fp for fp, _ in result["unknown"]),
          str([fp for fp, _ in result["unknown"]][:4]))
    check("большинство таблиц распознано",
          result["recognised"] > result["tables"] * 0.5,
          "%d из %d" % (result["recognised"], result["tables"]))

    lines = audit_tables.report(result, top=5)
    text = "\n".join(lines)
    check("в отчёте есть сводка", "Таблиц найдено" in text)
    check("в отчёте есть процент распознанного", "распознано" in text)
    if result["unknown"]:
        check("в отчёте показаны примеры", "Примеры строк" in text)
    else:
        check("неизвестных нет — примеры не нужны", True)

    print("\n--- запуск как скрипт ---")
    buf, stdout = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        code = audit_tables.main(["--corpus", str(corpus), "--limit", "3", "--top", "5"])
    finally:
        sys.stdout = stdout
    out_text = buf.getvalue()
    check("скрипт завершился без ошибки", code == 0, str(code))
    check("скрипт напечатал аудит", "Аудит таблиц" in out_text)
    check("нет папки — понятное сообщение",
          audit_tables.main(["--corpus", str(tmp / "nope")]) == 1)
    empty = tmp / "empty"
    empty.mkdir()
    check("пустая папка — понятное сообщение",
          audit_tables.main(["--corpus", str(empty)]) == 1)

    print("\n--- выживание после падения ---")
    # 0xC00000FD убивает процесс без всякого except, поэтому единственный
    # способ не терять 45 минут работы — писать результат после каждого файла.
    snapshots = []
    pdfs = sorted(corpus.rglob("*.pdf"))
    audit_tables.audit(pdfs, top=5, progress=False,
                       parser=parsers.get_parser("pdfplumber"),
                       sink=snapshots.append)
    check("snapshot пишется после каждого файла",
          len(snapshots) == len(pdfs), "%d снимков на %d файлов"
          % (len(snapshots), len(pdfs)))
    check("в каждом снимке есть счётчик готовности",
          all(s["files_done"] == i + 1 for i, s in enumerate(snapshots)),
          str([s["files_done"] for s in snapshots]))
    check("завершённый прогон помечен полным",
          snapshots[-1]["files_done"] == snapshots[-1]["files_total"],
          str(snapshots[-1]["files_done"]))
    check("снимок можно записать в JSON в любой момент",
          json.loads(json.dumps(snapshots[0], ensure_ascii=False))["tables"]
          == snapshots[0]["tables"])

    partial = dict(snapshots[1])
    partial["files_done"] = 2
    partial["files_total"] = 50
    partial_text = "\n".join(audit_tables.report(partial, 5))
    check("недоделанный аудит помечен предупреждением",
          "прогoн".replace("o", "о") in partial_text and "2" in partial_text
          and "50" in partial_text, partial_text.splitlines()[:2])

    check("run_with_big_stack возвращает результат",
          cli.run_with_big_stack(lambda a, b=0: a + b, 2, b=3) == 5)
    box = {"deep": 0}

    def boom():
        raise ValueError("как будто torch лёг")

    try:
        cli.run_with_big_stack(boom)
        check("run_with_big_stack пробрасывает исключение", False, "не упало")
    except ValueError:
        check("run_with_big_stack пробрасывает исключение", True)

    # The point is the stack, not the Python recursion limit: Windows dies in
    # torch's C++ recursion, which `sys.setrecursionlimit` does not govern.
    # So check the mechanism — a separate thread — and that deep recursion
    # survives once the Python-side limit is out of the way.
    seen_thread = {}

    def whoami():
        seen_thread["id"] = threading.get_ident()
        return 1

    cli.run_with_big_stack(whoami)
    check("код выполняется в отдельном потоке",
          seen_thread.get("id") not in (None, threading.get_ident()),
          str(seen_thread.get("id")))

    def deep(n):
        return deep(n + 1) if n < 20000 else n

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(40000)
    try:
        depth = cli.run_with_big_stack(deep, 0)
        check("20 000 кадров помещаются в большой стек", depth == 20000,
              str(depth))
    except RecursionError:
        check("20 000 кадров помещаются в большой стек", False, "RecursionError")
    finally:
        sys.setrecursionlimit(old_limit)

    print("\n--- битый файл не роняет прогон ---")
    broken = tmp / "broken"
    broken.mkdir(exist_ok=True)
    (broken / "bad.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all")
    res = audit_tables.audit([broken / "bad.pdf"] + pdfs[:2], top=5,
                             progress=False,
                             parser=parsers.get_parser("pdfplumber"))
    check("битый PDF попал в failures", len(res["failures"]) == 1,
          str(res["failures"]))
    check("остальные файлы всё равно разобраны", res["files_done"] == 3,
          str(res["files_done"]))

    print("\n--- бэкап перед --rebuild ---")
    from tools.rag import build_cards

    db_dir = tmp / "cardsdir"
    db_dir.mkdir(exist_ok=True)
    build_cards.build(corpus, db_dir, jobs=2, shards=False)
    db = db_dir / "cards.db"
    check("карточки собраны", build_cards._count_cards(db) > 0,
          str(build_cards._count_cards(db)))
    backup = build_cards._backup_before_rebuild(db)
    check("бэкап сделан", backup is not None and backup.exists(), str(backup))
    check("в бэкапе те же карточки",
          backup is not None
          and build_cards._count_cards(backup) == build_cards._count_cards(db),
          "%s против %s" % (backup and build_cards._count_cards(backup),
                            build_cards._count_cards(db)))
    check("бэкап не перезаписал саму базу",
          build_cards._count_cards(db) > 0 and not db.name.startswith("cards.db."))

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
