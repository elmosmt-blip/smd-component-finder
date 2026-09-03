#!/usr/bin/env python3
"""Обогащение карточек данными от дистрибьютора (element14).

    python3 tools/rag/enrich_cards.py --attrs attrs.jsonl --cards data/cards/cards.db
    python3 tools/rag/enrich_cards.py --attrs attrs.jsonl --dry-run

`attrs.jsonl` пишет `element14.py --attributes-out`: там по парт-номеру лежат
уже разобранные атрибуты — «Operating Temperature Max = 150 °C»,
«Transistor Case Style = SOT-23», «DC Collector Current = 200 mA». Это те
самые поля, которые парсер вытаскивает из PDF хуже всего: производитель
заполнен у 32 % карточек, габариты — у 5 %.

Почему это отдельный шаг, а не часть парсера: **источник у этих данных другой,
и подмешивать его в результаты разбора нельзя.** Иначе непонятно, что
действительно написано в даташите, а что пришло из каталога — а карточка
должна этим отличаться. Поэтому три правила:

1. **Пустые поля заполняются.** Производитель, корпус, число выводов — но
   только там, где своего значения нет. Вытащенное из PDF не перезаписывается
   никогда.
2. **Всё остальное идёт в отдельный список `extra_specs`** с пометкой
   источника и показывается на сайте отдельным блоком «From the distributor».
   Ни одна цифра не притворяется вытащенной из даташита.
3. **`confidence` не растёт.** Полнота карточки выросла, а доказательство в
   PDF — нет. Завышать уверенность здесь значило бы врать в той же строке,
   где мы обещали честность.

После обогащения индекс надо пересобрать:

    python3 tools/rag/pipeline.py --from-cards data/cards/cards.db --rebuild
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import card_store, cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data"))
DEFAULT_CARDS = DATA_DIR / "cards" / "cards.db"

SOURCE = "element14"

# Подписи атрибутов, из которых можно взять число выводов. Всё остальное
# заполнять автоматически нельзя: «Voltage Rating» без контекста семейства не
# сказать, что именно это за напряжение.
PIN_LABELS = ("no. of pins", "no of pins", "number of pins", "pins",
              "no. of contacts", "number of contacts")

# Столько атрибутов на карточку имеет смысл показывать: у Farnell их бывает
# по сорок, и половина — про упаковку и сертификаты.
MAX_EXTRA = 40

_INT = re.compile(r"-?\d+")


def load_attributes(path: Path) -> Dict[str, dict]:
    """JSONL от element14 -> {PART_KEY: строка}."""
    out: Dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        part = str(row.get("part") or "").strip()
        if not part:
            continue
        out[part.upper()] = row
    return out


def _pin_count(attributes: Dict[str, str]) -> Optional[int]:
    for label, value in attributes.items():
        if label.strip().lower() in PIN_LABELS:
            match = _INT.search(str(value))
            if match:
                try:
                    return int(match.group(0))
                except ValueError:
                    return None
    return None


def extra_specs_of(attributes: Dict[str, str], limit: int = MAX_EXTRA
                   ) -> List[Dict[str, str]]:
    """Атрибуты -> список для отдельного блока на сайте."""
    out: List[Dict[str, str]] = []
    seen = set()
    for label, value in attributes.items():
        label = (label or "").strip()
        value = (value or "").strip()
        if not label or not value or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"label": label, "value": value, "source": SOURCE})
        if len(out) >= limit:
            break
    return out


def merge(card: Dict[str, Any], row: dict) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Карточка + строка дистрибьютора -> (новая карточка, что изменилось)."""
    card = dict(card)
    changed: Dict[str, int] = {}
    attributes = row.get("attributes") or {}

    if not (card.get("manufacturer") or "").strip() and (row.get("manufacturer") or "").strip():
        card["manufacturer"] = row["manufacturer"].strip()
        changed["manufacturer"] = 1
    if not (card.get("package") or "").strip() and (row.get("package") or "").strip():
        card["package"] = row["package"].strip()
        changed["package"] = 1

    pins = _pin_count(attributes)
    if pins and not card.get("pin_count"):
        card["pin_count"] = pins
        changed["pin_count"] = 1

    extra = extra_specs_of(attributes)
    if extra:
        card["extra_specs"] = extra
        changed["extra_specs"] = len(extra)

    if changed:
        sources = list(card.get("sources") or [])
        if SOURCE not in sources:
            sources.append(SOURCE)
        card["sources"] = sources
    return card, changed


def enrich(cards_db: Path, attrs_path: Path, limit: int = 0,
           dry_run: bool = False, verbose: bool = False) -> dict:
    cards_db = Path(cards_db)
    attrs_path = Path(attrs_path)
    if not cards_db.exists():
        print("Нет базы карточек: %s" % cards_db)
        return {}
    if not attrs_path.exists():
        print("Нет файла атрибутов: %s" % attrs_path)
        print("Он пишется так: element14.py --parts parts.txt "
              "--attributes-out attrs.jsonl")
        return {}

    attributes = load_attributes(attrs_path)
    print("Атрибутов загружено: %d" % len(attributes))

    store = card_store.CardStore(cards_db)
    cards = store.iter_cards()
    total = len(cards)
    matched = updated = 0
    filled: Dict[str, int] = {}
    for card in cards:
        if limit and updated >= limit:
            break
        part = str(card.get("part") or "").strip().upper()
        row = attributes.get(part)
        if not row:
            continue
        matched += 1
        merged, changed = merge(card, row)
        if not changed:
            continue
        updated += 1
        for key, value in changed.items():
            filled[key] = filled.get(key, 0) + value
        if not dry_run:
            store.upsert(merged, replace_if_richer=False)
        if verbose:
            print("  %-24s %s" % (part, ", ".join(sorted(changed))))

    print("\nКарточек в базе:      %d" % total)
    print("Совпало по номеру:    %d" % matched)
    print("Обновлено:            %d%s"
          % (updated, " (пробный прогон, ничего не записано)" if dry_run else ""))
    for key in ("manufacturer", "package", "pin_count", "extra_specs"):
        if key in filled:
            print("  %-20s %d" % ({"manufacturer": "производитель",
                                  "package": "корпус",
                                  "pin_count": "число выводов",
                                  "extra_specs": "атрибутов добавлено"}[key],
                                 filled[key]))
    if matched:
        print("Средняя полнота до/после — см. audit_cards.py:")
        print("  python3 tools/rag/audit_cards.py --db %s" % cards_db)
    if updated and not dry_run:
        print("\nИндекс надо пересобрать:")
        print("  python3 tools/rag/pipeline.py --from-cards %s --rebuild" % cards_db)
    store.close()
    return {"cards": total, "matched": matched, "updated": updated,
            "filled": filled}


def main(argv: Optional[List[str]] = None) -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(
        description="Добавить в карточки данные дистрибьютора, не притворяясь, "
                    "что они вытащены из PDF")
    ap.add_argument("--attrs", type=Path, required=True,
                    help="attrs.jsonl от element14.py --attributes-out")
    ap.add_argument("--cards", type=Path, default=DEFAULT_CARDS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что изменилось бы, и ничего не писать")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    enrich(args.cards, args.attrs, limit=args.limit, dry_run=args.dry_run,
           verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
