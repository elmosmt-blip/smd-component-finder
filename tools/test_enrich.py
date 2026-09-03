#!/usr/bin/env python3
"""Tests for enriching cards with distributor data.

The rule that makes this honest: distributor data never pretends to come from
the PDF. Empty fields get filled, everything else lands in a separate
`extra_specs` block, and `confidence` does not move. If a future change starts
overwriting parsed values or inflating confidence, these tests should fail.

    python3 tools/test_enrich.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import card_store, enrich_cards, index_cards  # noqa: E402

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
    "manufacturer": "",
    "package": "",
    "family": "BJT",
    "description": "NPN transistor",
    "features": ["high gain"],
    "pins": [{"n": "1", "name": "BASE", "function": ""}],
    "pin_count": None,
    "ratings": [{"symbol": "VCEO", "label": "Collector-Emitter Voltage",
                 "value": 40.0, "unit": "V", "text": "40 V"}],
    "specs": [],
    "dimensions": {},
    "order_codes": [],
    "headline": [],
    "key_specs": {},
    "pages": 6,
    "tables": 5,
    "filename": "MMBT3904_datasheet.pdf",
    "sha1": "abc",
    "parser": "pdfplumber",
    "confidence": 0.42,
}

ATTRS = {
    "part": "MMBT3904",
    "manufacturer": "ONSEMI",
    "package": "SOT-23",
    "store": "uk.farnell.com",
    "attributes": {
        "Transistor Case Style": "SOT-23",
        "DC Collector Current": "200 mA",
        "Operating Temperature Max": "150 °C",
        "No. of Pins": "3",
    },
}

# База ключуется по парт-номеру, поэтому у полной карточки другой номер.
FULL_CARD = dict(CARD, part="MMBT3905", manufacturer="Diodes Inc",
                 package="SOT-23", pin_count=3)

ATTRS_FULL = dict(ATTRS, part="MMBT3905")


def write_attrs(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def make_db(path: Path, cards) -> Path:
    store = card_store.CardStore(path)
    for card in cards:
        store.upsert(card)
    store.close()
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-enrich-"))

    print("--- что можно заполнять, а что нет ---")
    merged, changed = enrich_cards.merge(CARD, ATTRS)
    check("производитель заполняется, если своего нет",
          merged["manufacturer"] == "ONSEMI", str(merged["manufacturer"]))
    check("корпус заполняется, если своего нет",
          merged["package"] == "SOT-23", str(merged["package"]))
    check("число выводов берётся из «No. of Pins»",
          merged["pin_count"] == 3, str(merged["pin_count"]))
    check("атрибуты идут в отдельный список, а не в specs",
          len(merged["extra_specs"]) == 4 and merged["specs"] == [],
          str(merged.get("extra_specs")))
    check("у каждого атрибута помечен источник",
          all(x["source"] == "element14" for x in merged["extra_specs"]),
          str(merged["extra_specs"][:1]))
    check("источник записан в карточку",
          merged.get("sources") == ["element14"], str(merged.get("sources")))
    check("уверенность не выросла",
          merged["confidence"] == CARD["confidence"],
          "%s != %s" % (merged["confidence"], CARD["confidence"]))

    merged2, changed2 = enrich_cards.merge(FULL_CARD, ATTRS)
    check("свои значения не перезаписываются",
          merged2["manufacturer"] == "Diodes Inc"
          and merged2["package"] == "SOT-23" and merged2["pin_count"] == 3,
          str(merged2["manufacturer"]))
    check("но атрибуты всё равно добавляются",
          len(merged2.get("extra_specs") or []) == 4, str(changed2))
    check("распиновка из PDF не тронута",
          merged2["pins"] == FULL_CARD["pins"], str(merged2["pins"]))

    print("\n--- пустая строка дистрибьютора ---")
    merged3, changed3 = enrich_cards.merge(CARD, {"part": "MMBT3904"})
    check("без атрибутов карточка не меняется", changed3 == {}, str(changed3))
    check("extra_specs не создаётся пустым",
          "extra_specs" not in merged3, str(merged3.keys()))

    print("\n--- разбор атрибутов ---")
    check("число выводов из «Number of Pins»",
          enrich_cards._pin_count({"Number of Pins": "8"}) == 8)
    check("число выводов из «8 Pins»",
          enrich_cards._pin_count({"No. of Pins": "8 Pins"}) == 8)
    check("нет подходящей подписи — None",
          enrich_cards._pin_count({"Voltage": "40 V"}) is None)
    check("подписи атрибутов не дублируются",
          len(enrich_cards.extra_specs_of({"A": "1", "a": "2"})) == 1)
    check("пустые значения отброшены",
          enrich_cards.extra_specs_of({"A": "", "B": "2"}) ==
          [{"label": "B", "value": "2", "source": "element14"}],
          str(enrich_cards.extra_specs_of({"A": "", "B": "2"})))

    print("\n--- прогон по базе ---")
    db = make_db(tmp / "cards.db", [CARD, FULL_CARD,
                                    {"part": "NOATTR", "description": "x"}])
    attrs = write_attrs(tmp / "attrs.jsonl", [ATTRS, ATTRS_FULL])
    report = enrich_cards.enrich(db, attrs)
    check("совпало две карточки из трёх",
          report["matched"] == 2, str(report))
    check("обе обновлены", report["updated"] == 2, str(report))
    check("производитель заполнен один раз (у второй свой был)",
          report["filled"].get("manufacturer") == 1, str(report["filled"]))
    check("атрибуты добавлены обеим",
          report["filled"].get("extra_specs") == 8, str(report["filled"]))

    store = card_store.CardStore(db)
    saved = store.get("MMBT3904")
    store.close()
    check("карточка записана в базу",
          saved and saved["manufacturer"] == "ONSEMI", str(saved)[:120])
    check("атрибуты доехали до карточки",
          bool(saved) and len(saved.get("extra_specs") or []) == 4,
          str((saved or {}).get("extra_specs"))[:120])
    check("уверенность в базе не выросла",
          saved and saved["confidence"] == CARD["confidence"],
          str((saved or {}).get("confidence")))

    print("\n--- пробный прогон ничего не пишет ---")
    db2 = make_db(tmp / "cards2.db", [CARD])
    report2 = enrich_cards.enrich(db2, attrs, dry_run=True)
    store = card_store.CardStore(db2)
    untouched = store.get("MMBT3904")
    store.close()
    check("dry-run обновил бы", report2["updated"] == 1, str(report2))
    check("но база не тронута", untouched["manufacturer"] == "",
          str(untouched["manufacturer"]))

    print("\n--- нет файлов ---")
    check("нет базы карточек — не падаем",
          enrich_cards.enrich(tmp / "nope.db", attrs) == {})
    check("нет атрибутов — не падаем",
          enrich_cards.enrich(db, tmp / "nope.jsonl") == {})

    print("\n--- обогащённое попадает в поиск ---")
    chunks = index_cards.card_chunks(saved)
    sections = [c["section"] for c in chunks]
    check("extra_specs стал отдельным разделом",
          "other" in sections, str(sections))
    extra = [c for c in chunks if c["section"] == "other"][0]
    check("и он таблица", extra["is_table"] is True)
    check("атрибуты ищутся по тексту",
          "Operating Temperature Max" in extra["text"]
          and "150 °C" in extra["text"], extra["text"][:120])
    check("производитель из атрибутов тоже в тексте первого чанка",
          any("ONSEMI" in c["text"] for c in chunks),
          str([c["text"][:40] for c in chunks]))

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
