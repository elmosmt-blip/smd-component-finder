#!/usr/bin/env python3
"""Tests for card quality: why one card is full and the next one is empty.

Covers three layers, because the answer has to survive all of them:

  * quality.py       — flags decided at parse time, tiers, reason codes
  * card_store       — `flags` survive the round trip, including a database
                       built before the column existed
  * audit_cards.py   — the report a human pastes into a chat

    python3 tools/test_quality.py
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import audit_cards, card_store, quality  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


def rich_card(**over) -> dict:
    card = {
        "part": "MMBT3904",
        "manufacturer": "onsemi",
        "package": "SOT-23",
        "description": "NPN general purpose transistor",
        "features": ["Low voltage"],
        "pins": [{"n": 1, "name": "Base"}, {"n": 2, "name": "Emitter"}],
        "pin_count": 3,
        "ratings": [{"symbol": "Vceo", "text": "40 V"}],
        "specs": [{"symbol": "hFE", "text": "300"}],
        "dimensions": {"body_length": {"value": 2.9, "unit": "mm"}},
        "tables": 4,
        "confidence": 0.9,
        "filename": "MMBT3904.pdf",
        "flags": [],
    }
    card.update(over)
    return card


def empty_card(**over) -> dict:
    card = {
        "part": "SCAN123",
        "manufacturer": None,
        "package": None,
        "description": "",
        "features": [],
        "pins": [],
        "pin_count": None,
        "ratings": [],
        "specs": [],
        "dimensions": {},
        "tables": 0,
        "confidence": 0.11,
        "filename": "C:\\library\\SCAN123.pdf",
        "flags": ["scan", "no_tables"],
    }
    card.update(over)
    return card


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-quality-"))

    print("--- флаги на этапе парсинга ---")
    check("настоящий даташит без флагов",
          quality.parse_flags(4200, 3, 5, True) == [])
    check("скан: текста почти нет",
          quality.parse_flags(12, 4, 0, True) == ["no_tables", "scan"])
    check("мало текста на страницу",
          quality.parse_flags(500, 4, 2, True) == ["low_text"])
    check("нет таблиц",
          quality.parse_flags(9000, 4, 0, True) == ["no_tables"])
    check("парт-номер из имени файла",
          quality.parse_flags(9000, 4, 3, False) == ["part_from_filename"])
    check("порядок флагов стабилен",
          quality.parse_flags(12, 4, 0, False)
          == ["no_tables", "scan", "part_from_filename"])

    print("\n--- поля, полнота, группа ---")
    filled = quality.filled_fields(rich_card())
    check("богатая карточка: все 9 полей", sum(filled.values()) == 9, str(filled))
    check("пустая карточка: только парт-номер",
          quality.filled_count(empty_card()) == 1,
          str(quality.filled_fields(empty_card())))
    check("распиновка считается по pin_count тоже",
          quality.filled_fields(rich_card(pins=[]))["pins"] is True)
    check("группа: полная", quality.tier(rich_card()) == "full")
    check("группа: средняя",
          quality.tier(rich_card(ratings=[], specs=[], dimensions={})) == "partial")
    check("группа: пустая", quality.tier(empty_card()) == "empty")

    print("\n--- причины ---")
    codes = quality.reason_codes(empty_card())
    check("скан назван сканом", "scan" in codes, str(codes))
    check("нет таблиц названо", "no_tables" in codes, str(codes))
    check("нет корпуса названо", "no_package" in codes, str(codes))
    check("нет дублей", len(codes) == len(set(codes)), str(codes))
    check("у богатой карточки причин нет",
          quality.reason_codes(rich_card()) == [], str(quality.reason_codes(rich_card())))
    modern = rich_card(part="MMBT3904", filename="MMBT3904.pdf")
    modern.pop("flags")            # база без флагов: остаётся только догадка
    check("нормальное имя файла не считается дефектом",
          "part_from_filename" not in quality.reason_codes(modern),
          str(quality.reason_codes(modern)))

    old = empty_card()
    old.pop("flags")                       # карточка из старой базы
    check("старая база без флагов: пустота названа сканом",
          "scan" in quality.reason_codes(old), str(quality.reason_codes(old)))
    old_rich = rich_card()
    old_rich.pop("flags")
    check("старая база без флагов: полная карточка не названа сканом",
          "scan" not in quality.reason_codes(old_rich), str(quality.reason_codes(old_rich)))
    def without_flags(card):
        card = dict(card)
        card.pop("flags", None)     # старая база: флагов нет, только поля
        return card

    check("пустая карточка с Windows-путём: парт-номер из имени файла",
          "part_from_filename" in quality.reason_codes(
              without_flags(empty_card(filename="C:\\lib\\SCAN123.pdf"))),
          str(quality.reason_codes(
              without_flags(empty_card(filename="C:\\lib\\SCAN123.pdf")))))
    check("имя файла с точками внутри не ломает сравнение",
          "part_from_filename" not in quality.reason_codes(
              without_flags(empty_card(part="MMBT3904",
                                       filename="onsemi.MMBT3904.rev2.pdf"))),
          str(quality.reason_codes(without_flags(empty_card(
              part="MMBT3904", filename="onsemi.MMBT3904.rev2.pdf")))))
    check("у каждой причины есть текст для сайта",
          all(quality.reason_text(c) for c in quality.REASON_TEXT))

    print("\n--- сводка по корпусу ---")
    cards = [rich_card(), rich_card(part="BC847"), empty_card(),
             rich_card(part="1KSMB10A", package=None, pins=[], pin_count=None,
                       ratings=[], specs=[], dimensions={}, confidence=0.4)]
    summary = quality.summarise(cards, worst=2)
    check("всего карточек", summary["total"] == 4, str(summary["total"]))
    check("корпус есть у двух из четырёх", summary["per_field"]["package"] == 2,
          str(summary["per_field"]))
    check("группы посчитаны",
          summary["tiers"]["full"] == 2 and summary["tiers"]["empty"] == 1,
          str(summary["tiers"]))
    check("худшие отсортированы от пустых к полным",
          [w["part"] for w in summary["worst"]] == ["SCAN123", "1KSMB10A"],
          str([w["part"] for w in summary["worst"]]))
    check("у худшей карточки названы пропущенные поля",
          "package" in summary["worst"][0]["missing"],
          str(summary["worst"][0]["missing"]))
    check("проценты считаются", quality.pct(1, 4) == 25.0)
    check("деление на ноль не роняет", quality.pct(0, 0) == 0.0)

    print("\n--- база карточек ---")
    db = tmp / "cards" / "cards.db"
    store = card_store.open_store(db)
    store.upsert(rich_card())
    store.upsert(empty_card(filename="SCAN123.pdf"))
    store.commit()
    check("карточки записаны", store.count() == 2)
    rows, total = store.search(limit=10)
    check("флаги доехали до выдачи API",
          rows and rows[0].get("flags") is not None, str(rows[:1]))
    by_part = {r["part"]: r for r in rows}
    check("у скана флаг scan в выдаче",
          by_part["SCAN123"]["flags"] == ["scan", "no_tables"],
          str(by_part["SCAN123"].get("flags")))

    # база, созданная до появления колонки flags
    old_db = tmp / "old" / "cards.db"
    old_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(old_db))
    con.executescript(card_store.SCHEMA.replace("    flags         TEXT,\n", ""))
    con.execute(
        "INSERT INTO cards (part, part_key, manufacturer, package, pin_count,"
        " confidence, pages, tables, filename, description, features, headline,"
        " card, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("OLDPART", "OLDPART", "Vishay", "SOD-123", 2, 0.7, 2, 1, "OLDPART.pdf",
         "old row", "[]", "[]", json.dumps({"part": "OLDPART"}), 1.0))
    con.commit()
    con.close()
    store2 = card_store.open_store(old_db)
    store2.upsert(rich_card(part="NEWPART"))
    store2.commit()
    check("старая база: колонка flags добавлена на лету",
          store2.count() == 2, str(store2.count()))
    old_rows, _ = store2.search(limit=10)
    old_row = {r["part"]: r for r in old_rows}.get("OLDPART")
    check("старая строка читается, флаги пустые",
          old_row is not None and old_row["flags"] == [], str(old_rows[:1]))
    store.close()
    store2.close()

    print("\n--- отчёт аудита ---")
    lines = audit_cards.report(summary, db, top=2)
    text = "\n".join(lines)
    check("в отчёте есть заполненность полей", "Заполненность полей" in text)
    check("в отчёте есть группы", "Группы" in text)
    check("в отчёте есть худшие карточки", "SCAN123" in text)
    check("в отчёте нет «None» вместо значений", " None" not in text, text[:200])
    check("пустая база не роняет отчёт",
          any("нет" in l for l in audit_cards.report(
              quality.summarise([], worst=3), db, top=3)))

    csv_path = tmp / "audit.csv"
    n = audit_cards.write_csv(csv_path, cards)
    body = csv_path.read_text(encoding="utf-8-sig")
    check("CSV записан", csv_path.exists() and n == 4)
    check("в CSV есть заголовок", body.splitlines()[0].startswith("part;filename"))
    check("в CSV есть строка про скан", "SCAN123" in body)

    print("\n--- запуск как скрипт ---")
    buf = io.StringIO()
    stdout = sys.stdout
    sys.stdout = buf
    try:
        code = audit_cards.main(["--db", str(db), "--top", "3", "--quiet",
                                 "--csv", str(tmp / "out.csv")])
    finally:
        sys.stdout = stdout
    out_text = buf.getvalue()
    check("скрипт завершился без ошибки", code == 0, str(code))
    check("скрипт напечатал отчёт", "Аудит карточек" in out_text)
    check("реальный CSV на диске", (tmp / "out.csv").exists())
    check("нет базы — понятное сообщение, не traceback",
          audit_cards.main(["--db", str(tmp / "nope.db")]) == 1)

    print("\n--- проверка текстового слоя ---")
    try:
        import reportlab  # noqa: F401
        from tools.rag import sample_datasheets
        corpus = tmp / "corpus"
        corpus.mkdir()
        sample_datasheets.build_pdf(sample_datasheets.DATASHEETS[0], corpus)
        (corpus / "MMBT3904.pdf").write_bytes(b"%PDF-1.4\n% not really a pdf\n")
        scan = audit_cards.scan_check([empty_card(filename="MMBT3904.pdf")],
                                      corpus, limit=5)
        check("проверка выполняется", scan["available"] is True, str(scan)[:120])
        check("битый/пустой файл попал в список подозрительных",
              any(s["file"] == "MMBT3904.pdf" for s in scan["scans"]), str(scan))
    except ImportError:
        check("reportlab нужен для генерации PDF — пропущено", True)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
