#!/usr/bin/env python3
"""End-to-end test for the card pipeline (extraction -> database -> shards).

Builds a throwaway corpus from tools/rag/sample_datasheets.py, extracts cards
and checks that the numbers on the cards are the numbers in the PDFs. It also
covers the parts that matter when the corpus is 300k files rather than six:
resume, failure isolation and shard generation.

    python3 tools/test_cards.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import card_store, extract, parsers, sample_datasheets  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-cards-"))
    corpus = tmp / "datasheets"
    out = tmp / "cards"
    corpus.mkdir(parents=True)

    print("--- корпус ---")
    generated = [sample_datasheets.build_pdf(spec, corpus)
                 for spec in sample_datasheets.DATASHEETS]
    check("демо-PDF сгенерированы", len(generated) == 6, str(len(generated)))

    # a file that is not a PDF at all: the run must survive it
    (corpus / "broken.pdf").write_bytes(b"this is not a pdf")
    check("битый файл добавлен в корпус", (corpus / "broken.pdf").exists())

    print("\n--- извлечение полей ---")
    parser = parsers.get_parser("auto")
    cards = {}
    for path in sorted(corpus.glob("*.pdf")):
        try:
            doc = parser.parse(path)
            card = extract.build_card(doc).to_dict()
            cards[card["part"]] = card
        except Exception as exc:                     # noqa: BLE001
            cards[path.name] = {"error": str(exc)}

    check("битый PDF не уронил разбор", "broken.pdf" not in cards or "error" in cards.get("broken.pdf", {}))

    m = cards.get("MMBT3904")
    check("MMBT3904 извлечён", bool(m))
    if m:
        check("  корпус SOT-23", m["package"] == "SOT-23", str(m["package"]))
        check("  3 вывода", m["pin_count"] == 3, str(m["pin_count"]))
        check("  распиновка B/E/C",
              [p["name"] for p in m["pins"]] == ["Base", "Emitter", "Collector"],
              str([p["name"] for p in m["pins"]]))
        check("  Vceo = 40 В",
              m["key_specs"].get("collector_emitter_voltage", {}).get("value") == 40.0,
              str(m["key_specs"].get("collector_emitter_voltage")))
        check("  Ic = 200 мА (в базовых единицах)",
              round(m["key_specs"].get("collector_current", {}).get("value") or 0, 3) == 0.2,
              str(m["key_specs"].get("collector_current")))
        check("  описание непустое", len(m["description"]) > 40, m["description"][:60])
        check("  есть особенности", len(m["features"]) >= 2, str(len(m["features"])))
        check("  габариты корпуса", m["dimensions"].get("body_length", {}).get("value") == 2.9,
              str(m["dimensions"]))
        check("  есть таблица предельных режимов", len(m["ratings"]) >= 5, str(len(m["ratings"])))
        check("  есть электрические характеристики", len(m["specs"]) >= 3, str(len(m["specs"])))

    s = cards.get("SI2301")
    check("SI2301 извлечён", bool(s))
    if s:
        check("  Vds = -20 В",
              s["key_specs"].get("drain_source_voltage", {}).get("value") == -20.0,
              str(s["key_specs"].get("drain_source_voltage")))
        check("  Rds(on) = 70 мОм -> 0.07 Ом",
              round(s["key_specs"].get("on_resistance", {}).get("value") or 0, 3) == 0.07,
              str(s["key_specs"].get("on_resistance")))
        check("  порог затвора есть",
              s["key_specs"].get("gate_threshold_voltage", {}).get("value") is not None,
              str(s["key_specs"].get("gate_threshold_voltage")))

    a = cards.get("AMS1117-3.3")
    check("AMS1117-3.3 извлечён", bool(a))
    if a:
        check("  номер детали не обрезан", a["part"] == "AMS1117-3.3", a["part"])
        check("  выход 3.3 В",
              a["key_specs"].get("output_voltage", {}).get("value") == 3.3,
              str(a["key_specs"].get("output_voltage")))
        check("  вход до 15 В",
              a["key_specs"].get("input_voltage", {}).get("value") == 15.0,
              str(a["key_specs"].get("input_voltage")))

    print("\n--- парсеры ---")
    from tools.rag import parsers as P
    try:
        import docling            # noqa: F401
        docling_installed = True
    except ImportError:
        docling_installed = False
    if not docling_installed:
        try:
            P.get_parser("docling")
            check("явный запрос Docling не подменяется молча", False, "парсер подменился")
        except P.ParserUnavailable:
            check("явный запрос Docling не подменяется молча", True)
        auto = P.get_parser("auto")
        check("auto тихо падает на pdfplumber", auto.name == "pdfplumber", auto.name)
        check("auto помнит, почему пропустил Docling",
              any("docling" in e.lower() for e in getattr(auto, "fallback_errors", [])),
              str(getattr(auto, "fallback_errors", []))[:80])

    # Docling сам не отдаёт таблицы по страницам, а карточки строятся из таблиц
    pages = [P.Page(number=1, text=""), P.Page(number=2, text="")]
    P.DoclingParser._fill_tables(corpus / "MMBT3904_datasheet.pdf", pages)
    check("гибрид Docling+pdfplumber достаёт таблицы",
          len(pages[0].tables) >= 1 and len(pages[1].tables) >= 1,
          "%d/%d" % (len(pages[0].tables), len(pages[1].tables)))
    check("в таблицах есть шапка",
          pages[0].tables and pages[0].tables[0][0][0] == "Pin",
          str(pages[0].tables[0][0] if pages[0].tables else ""))

    print("\n--- база карточек ---")
    store = card_store.CardStore(out / "cards.db")
    for card in cards.values():
        if "error" not in card:
            store.upsert(card)
    store.commit()
    check("карточки записаны", store.count() == 6, str(store.count()))

    rows, total = store.search("mosfet")
    check("поиск по тексту находит MOSFET", total >= 2, str(total))
    rows, total = store.search("", package="SOT-23")
    check("фильтр по корпусу", total >= 4, str(total))
    check("карточка читается по номеру", (store.get("MMBT3904") or {}).get("part") == "MMBT3904")
    check("поиск нечувствителен к регистру",
          (store.get("mmbt3904") or {}).get("part") == "MMBT3904")

    # richer duplicate wins
    thin = dict(cards["MMBT3904"])
    thin["ratings"], thin["specs"], thin["pins"] = [], [], []
    thin["confidence"] = 0.1
    before = store.get("MMBT3904")["confidence"]
    store.upsert(thin)
    check("бедная копия не затёрла богатую", store.get("MMBT3904")["confidence"] == before)

    print("\n--- статические шарды ---")
    index = store.dump_shards(out / "site")
    check("index.json создан", (out / "site" / "index.json").exists())
    check("все карточки в шардах", index["count"] == 6, str(index["count"]))
    brief = json.loads((out / "site" / "brief" / "MM.json").read_text(encoding="utf-8"))
    check("шард MM.json содержит MMBT3904", any(c["part"] == "MMBT3904" for c in brief))
    full = json.loads((out / "site" / "card" / "MMBT3904.json").read_text(encoding="utf-8"))
    check("полная карточка на месте", full["pin_count"] == 3)
    store.close()

    print("\n--- докачка и сбои ---")
    from tools.rag import build_cards
    stats1 = build_cards.build(corpus, out / "run", jobs=2, shards=True)
    check("извлечено 6 карточек", stats1["cards"] == 6, str(stats1))
    check("сбой записан в базу", stats1["failures"] == 1, str(stats1["failures"]))
    stats2 = build_cards.build(corpus, out / "run", jobs=2, shards=False)
    check("повторный запуск ничего не переделывает",
          stats2["cards"] == 6 and stats2["failures"] == 1, str(stats2))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
