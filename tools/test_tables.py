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
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import audit_tables, extract, sample_datasheets  # noqa: E402

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

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
