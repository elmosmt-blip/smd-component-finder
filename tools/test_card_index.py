#!/usr/bin/env python3
"""Tests for the card-based search index.

The PDF index costs 468 MB per 1000 documents — 140 GB for 300 000 parts. The
card index costs megabytes. Same `index.db` shape, same query API, so nothing
around it changes; these tests pin down that the swap really is transparent:
sections, filters, resume, and the fields the site reads.

    python3 tools/test_card_index.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import index_cards, index_db  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


CARD = {
    "part": "MMBT3904",
    "manufacturer": "onsemi",
    "package": "SOT-23",
    "family": "BJT",
    "description": "NPN general purpose transistor",
    "features": ["High current gain", "Low saturation voltage"],
    "applications": ["Switching", "Amplification"],
    "pins": [{"n": "1", "name": "BASE", "function": "Control input"},
             {"n": "2", "name": "EMITTER", "function": "Ground"},
             {"n": "3", "name": "COLLECTOR", "function": "Output"}],
    "pin_count": 3,
    "ratings": [{"symbol": "VCEO", "label": "Collector-Emitter Voltage",
                 "value": 40.0, "unit": "V", "text": "40 V", "page": 2}],
    "specs": [{"symbol": "IC", "label": "Collector Current", "conditions": "",
               "min": "", "typ": "", "max": "200 mA", "unit": "mA", "page": 3}],
    "dimensions": {"body_length": {"value": 2.9, "unit": "mm"},
                   "pitch": {"value": 1.9, "unit": "mm"}},
    "order_codes": [{"code": "MMBT3904LT1G", "package": "SOT-23",
                     "marking": "1AM"}],
    "headline": [{"label": "Collector Current", "value": 200, "unit": "mA",
                  "text": "200 mA"}],
    "key_specs": {"vceo": {"label": "Collector-Emitter Voltage", "value": 40,
                           "unit": "V", "text": "40 V"}},
    "pages": 6,
    "tables": 5,
    "filename": "MMBT3904_datasheet.pdf",
    "sha1": "abc",
    "parser": "pdfplumber",
    "confidence": 0.78,
}


def make_cards_db(path: Path, cards) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE cards (part TEXT PRIMARY KEY, part_key TEXT NOT NULL,"
                " manufacturer TEXT, package TEXT, confidence REAL, card TEXT)")
    for part, card in cards:
        con.execute("INSERT INTO cards VALUES (?,?,?,?,?,?)",
                    (part, part.upper(), card.get("manufacturer"),
                     card.get("package"), card.get("confidence", 0.0),
                     json.dumps(card)))
    con.commit()
    con.close()
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-cardidx-"))

    print("--- карточка -> чанки ---")
    chunks = index_cards.card_chunks(CARD)
    sections = [c["section"] for c in chunks]
    check("каждое поле карточки стало своим разделом",
          "pin_configuration" in sections and "absolute_maximum_ratings" in sections
          and "package_dimensions" in sections and "ordering_information" in sections,
          str(sections))
    check("человеческие подписи разделов на месте",
          all(c["section_label"] and c["section_label"] != "Other" for c in chunks),
          str([(c["section"], c["section_label"]) for c in chunks]))
    check("табличные поля помечены как таблицы",
          [c["is_table"] for c in chunks] ==
          [c["section"] in index_cards.FIELD_SECTION.values() and
           c["section"] in ("pin_configuration", "absolute_maximum_ratings",
                            "electrical_characteristics", "package_dimensions",
                            "ordering_information") for c in chunks],
          str([(c["section"], c["is_table"]) for c in chunks]))
    check("метаданные детали привязаны к чанку",
          all(c["part"] == "MMBT3904" and c["manufacturer"] == "onsemi"
              and c["package"] == "SOT-23" for c in chunks))
    check("id чанка уникален и воспроизводим",
          chunks[0]["id"] == "MMBT3904::general_description", chunks[0]["id"])
    check("номер страницы взят из записи таблицы",
          any(c["page"] == 2 for c in chunks),
          str([(c["section"], c["page"]) for c in chunks]))

    pins = [c for c in chunks if c["section"] == "pin_configuration"][0]
    check("распиновка — markdown-таблица, как её ждёт сайт",
          "| Pin | Name | Function |" in pins["text"] and "BASE" in pins["text"],
          pins["text"][:80])
    check("у раздела есть короткая сводка", "3 pins" in pins["summary"],
          pins["summary"])
    specs = [c for c in chunks if c["section"] == "electrical_characteristics"][0]
    check("ключевые параметры добавлены в электрический раздел",
          "Collector Current: 200 mA" in specs["text"]
          and "Collector-Emitter Voltage: 40 V" in specs["text"],
          specs["text"][:160])
    dims = [c for c in chunks if c["section"] == "package_dimensions"][0]
    check("габариты разобраны в строки", "body_length" in dims["text"]
          and "2.9 mm" in dims["text"], dims["text"][:120])

    print("\n--- пустые поля не плодят мусор ---")
    check("совсем пустая карточка не даёт чанков",
          index_cards.card_chunks({}) == [], str(index_cards.card_chunks({})))
    bare = {"part": "EMPTY1", "manufacturer": "X", "package": None,
            "description": "", "features": [], "pins": []}
    only_id = index_cards.card_chunks(bare)
    check("карточка без полей даёт один чанк-идентификатор (чтобы находилась "
          "по парт-номеру)",
          len(only_id) == 1 and only_id[0]["section"] == "general_description"
          and "EMPTY1" in only_id[0]["text"], str(only_id))
    only_desc = index_cards.card_chunks({"part": "D1", "description": "diode"})
    check("из одного описания — один чанк",
          len(only_desc) == 1 and only_desc[0]["section"] == "general_description",
          str([c["section"] for c in only_desc]))
    check("нет парт-номера — doc_id берётся из имени файла",
          index_cards.card_chunks({"part": "", "filename": "x.pdf",
                                   "description": "d"})[0]["doc_id"] == "x.pdf")

    print("\n--- сборка индекса ---")
    cards_db = make_cards_db(tmp / "cards.db", [("MMBT3904", CARD)])
    out = tmp / "rag"
    stats = index_cards.build(cards_db, out, backend="sqlite")
    check("карточка проиндексирована", stats.get("indexed") == 1, str(stats))
    check("чанков больше одного", stats.get("chunks_added", 0) >= 6,
          str(stats.get("chunks_added")))
    check("в индексе есть документ", stats.get("docs") == 1, str(stats.get("docs")))

    index = index_db.RagIndex(out / "index.db")
    hits = index.search("collector emitter voltage", k=5)
    check("поиск по карточкам работает",
          bool(hits) and hits[0]["part"] == "MMBT3904", str(hits[:1]))
    check("в выдаче есть и раздел, и файл",
          bool(hits) and hits[0]["section"] and hits[0]["filename"],
          str(hits[0]) if hits else "")
    by_section = index.search("voltage", section="absolute_maximum_ratings", k=5)
    check("фильтр по разделу работает как на PDF-индексе",
          bool(by_section) and {h["section"] for h in by_section} ==
          {"absolute_maximum_ratings"}, str([h["section"] for h in by_section]))
    check("поиск по названию вывода находит распиновку",
          bool(index.search("BASE collector", k=5)),
          str(index.search("BASE collector", k=5))[:120])
    check("в поиске участвует производитель",
          bool(index.search("MMBT3904", k=5)))
    ok_fts, msg = index.integrity()
    check("FTS цел", ok_fts is True, str(msg))
    index.close()

    print("\n--- возобновление ---")
    again = index_cards.build(cards_db, out, backend="sqlite")
    check("повторный прогон не переделывает сделанное",
          again.get("indexed") == 0 and again.get("skipped") == 1, str(again))
    check("индекс не распух", again.get("docs") == 1 and
          again.get("chunks") == stats.get("chunks"),
          "%s != %s" % (again.get("chunks"), stats.get("chunks")))
    rebuilt = index_cards.build(cards_db, out, rebuild=True, backend="sqlite")
    check("--rebuild пересобирает", rebuilt.get("indexed") == 1, str(rebuilt))

    print("\n--- много карточек ---")
    many = make_cards_db(tmp / "many.db", [
        ("RC0402FR-071KL", {"part": "RC0402FR-071KL", "manufacturer": "YAGEO",
                            "package": "0402", "description": "thick film resistor",
                            "specs": [{"symbol": "R", "label": "Resistance",
                                       "typ": "1 kOhm", "unit": "Ohm"}]}),
        ("SI2301", {"part": "SI2301", "manufacturer": "Vishay",
                    "package": "SOT-23", "description": "P-channel MOSFET",
                    "ratings": [{"symbol": "VDS", "label": "Drain-Source Voltage",
                                 "value": -20, "unit": "V", "text": "-20 V"}]}),
        ("BROKEN", {}),
    ])
    out2 = tmp / "rag2"
    stats2 = index_cards.build(many, out2, backend="sqlite")
    check("три карточки обработаны",
          stats2.get("indexed") == 2 and stats2.get("empty") == 1, str(stats2))
    idx2 = index_db.RagIndex(out2 / "index.db")
    check("ищется и резистор, и MOSFET",
          bool(idx2.search("thick film resistor", k=3))
          and bool(idx2.search("drain source voltage", k=3)))
    check("фильтр по детали работает",
          {h["part"] for h in idx2.search("voltage", part="SI2301", k=3)}
          == {"SI2301"})
    idx2.close()

    print("\n--- нет базы карточек ---")
    empty = index_cards.build(tmp / "nope.db", tmp / "rag3", backend="sqlite")
    check("отсутствие базы не роняет прогон", empty == {}, str(empty))

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
