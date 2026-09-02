"""Why is one card full and the next one almost empty?

Extraction is rule-based: a card gets exactly what the PDF actually contains.
That is deliberate — inventing a package name to make a card look pretty is
worse than showing an honest gap. But "honest gap" is only useful if you can
see *which* PDFs are thin and *why*, otherwise a corpus of 300k turns into a
grid where every third card is a bare part number and nobody knows why.

This module is the single place that turns a card dict into:

  * `filled_fields`  — which of the nine card fields actually carry data
  * `tier`           — full / partial / sparse / empty
  * `reason_codes`   — short machine codes for what is missing and, when the
                       card carries `flags` from the parser, why

Codes are language-neutral keys. The English text lives in `REASON_TEXT` (the
site is English); `tools/rag/audit_cards.py` translates them for the terminal.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The nine things a useful card has. `part` is always present: build_card falls
# back to the file name, so it is really "we know what to call this row".
CARD_FIELDS: Tuple[str, ...] = (
    "part",
    "manufacturer",
    "package",
    "description",
    "features",
    "pins",
    "ratings",
    "specs",
    "dimensions",
)

# tier name -> minimum number of filled fields
TIERS: Tuple[Tuple[str, int], ...] = (
    ("full", 8),
    ("partial", 5),
    ("sparse", 2),
    ("empty", 0),
)

TIER_ORDER: Tuple[str, ...] = tuple(name for name, _ in TIERS)

REASON_TEXT: Dict[str, str] = {
    "scan": "No text layer — this datasheet is a scan, OCR is required",
    "low_text": "Almost no text could be extracted",
    "no_tables": "No tables found — pinout and parameters live in tables",
    "no_package": "No package name in the document",
    "no_pins": "No pinout table",
    "no_manufacturer": "Manufacturer not stated in the document",
    "no_description": "No description paragraph",
    "part_from_filename": "Part number taken from the file name",
}

# A card with no flags at all (parsed by an older build) gets this instead of
# `scan` — it is a guess from the fields, not a measurement.
LIKELY_SCAN = "scan"

# What `build_card` uses to decide the two text flags. Below ~120 characters
# per page there is no text layer worth speaking of; a real datasheet page
# carries 1.5–4 kB.
CHARS_PER_PAGE_LOW = 400
CHARS_TOTAL_SCAN = 200


# --------------------------------------------------------------------------- #
# flags decided at parse time
# --------------------------------------------------------------------------- #

def parse_flags(text_chars: int, n_pages: int, n_tables: int,
                part_from_meta: bool) -> List[str]:
    """Flags for a freshly parsed document.

    Kept separate from `reason_codes` because these are *measured*: we opened
    the PDF and counted. Everything `reason_codes` derives from stored fields
    is inference and can be wrong.
    """
    flags: List[str] = []
    pages = max(1, int(n_pages or 0))
    if not n_tables:
        flags.append("no_tables")
    if int(text_chars or 0) < CHARS_TOTAL_SCAN:
        flags.append("scan")
    elif int(text_chars or 0) / pages < CHARS_PER_PAGE_LOW:
        flags.append("low_text")
    if not part_from_meta:
        flags.append("part_from_filename")
    return flags


# --------------------------------------------------------------------------- #
# per-card analysis
# --------------------------------------------------------------------------- #

def _stem(filename: Optional[str]) -> str:
    """'C:\\lib\\MMBT3904.pdf' -> 'MMBT3904' on any OS.

    pathlib would leave the whole Windows path in `stem` when it runs on
    Linux, and this file gets read on Linux from a database built on Windows.
    """
    base = os.path.basename((filename or "").replace("\\", "/"))
    return base.rsplit(".", 1)[0] if "." in base else base


def filled_fields(card: Dict[str, Any]) -> Dict[str, bool]:
    """Which of CARD_FIELDS actually carry something."""
    out: Dict[str, bool] = {}
    for name in CARD_FIELDS:
        if name == "part":
            out[name] = bool((card.get("part") or "").strip())
            continue
        if name == "pins":
            out[name] = bool(card.get("pins")) or bool(card.get("pin_count"))
            continue
        out[name] = bool(card.get(name))
    return out


def filled_count(card: Dict[str, Any]) -> int:
    return sum(1 for ok in filled_fields(card).values() if ok)


def tier(card: Dict[str, Any]) -> str:
    """full / partial / sparse / empty — by number of filled fields."""
    n = filled_count(card)
    for name, minimum in TIERS:
        if n >= minimum:
            return name
    return "empty"


def _has_any_data(card: Dict[str, Any]) -> bool:
    return any((
        bool(card.get("description")),
        bool(card.get("features")),
        bool(card.get("ratings")),
        bool(card.get("specs")),
        bool(card.get("pins")) or bool(card.get("pin_count")),
        bool(card.get("dimensions")),
        bool(card.get("tables")),
    ))


def reason_codes(card: Dict[str, Any]) -> List[str]:
    """Short codes, most explanatory first, de-duplicated, stable order."""
    codes: List[str] = []
    stored = card.get("flags")
    measured = list(stored) if isinstance(stored, list) else []

    for code in ("scan", "low_text"):
        if code in measured:
            codes.append(code)

    filled = filled_fields(card)
    if not filled.get("manufacturer"):
        codes.append("no_manufacturer")
    if not filled.get("package"):
        codes.append("no_package")
    if not filled.get("pins"):
        codes.append("no_pins")
    if not filled.get("description"):
        codes.append("no_description")
    if "no_tables" in measured or not card.get("tables"):
        codes.append("no_tables")

    if "part_from_filename" in measured:
        codes.append("part_from_filename")
    elif stored is None and tier(card) in ("sparse", "empty"):
        # Only a weak hint, and only worth reporting on a card that has
        # nothing else: most libraries name the file after the part, so
        # "part == file name" on a full card means nothing at all.
        part = (card.get("part") or "").strip().upper()
        stem = _stem(card.get("filename")).upper()
        if part and stem and part == stem:
            codes.append("part_from_filename")

    # Old databases have no `flags`. If literally nothing came out of the PDF,
    # the overwhelmingly likely cause is a scan, so say so — but only when the
    # card is genuinely empty, and never when we measured the text ourselves.
    if stored is None and not _has_any_data(card):
        codes.append(LIKELY_SCAN)

    seen, out = set(), []
    for code in codes:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def reason_text(code: str) -> str:
    return REASON_TEXT.get(code, code.replace("_", " "))


# --------------------------------------------------------------------------- #
# corpus-level summary
# --------------------------------------------------------------------------- #

def summarise(cards: Iterable[Dict[str, Any]],
              worst: int = 10) -> Dict[str, Any]:
    """Counts, coverage, tiers, reasons and the worst offenders.

    Pure: takes card dicts, returns plain data, so it is testable without a
    database and reusable by the web API later.
    """
    total = 0
    per_field: Dict[str, int] = {f: 0 for f in CARD_FIELDS}
    tiers: Dict[str, int] = {name: 0 for name in TIER_ORDER}
    reasons: Dict[str, int] = {}
    empty_cards: List[Tuple[float, str, Dict[str, Any]]] = []
    confidence_sum = 0.0

    for card in cards:
        total += 1
        filled = filled_fields(card)
        for name, ok in filled.items():
            if ok:
                per_field[name] += 1
        tiers[tier(card)] += 1
        confidence_sum += float(card.get("confidence") or 0.0)
        for code in reason_codes(card):
            reasons[code] = reasons.get(code, 0) + 1
        if tiers and tier(card) in ("sparse", "empty"):
            empty_cards.append((float(card.get("confidence") or 0.0),
                                card.get("part") or "", card))

    empty_cards.sort(key=lambda item: (item[0], item[1]))
    return {
        "total": total,
        "per_field": per_field,
        "tiers": tiers,
        "reasons": reasons,
        "avg_confidence": round(confidence_sum / total, 3) if total else 0.0,
        "worst": [
            {
                "part": card.get("part") or "",
                "filename": card.get("filename") or "",
                "confidence": conf,
                "tier": tier(card),
                "missing": [f for f, ok in filled_fields(card).items() if not ok],
                "reasons": reason_codes(card),
            }
            for conf, _part, card in empty_cards[:max(0, int(worst))]
        ],
    }


def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0
