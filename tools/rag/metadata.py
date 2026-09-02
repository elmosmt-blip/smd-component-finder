"""Metadata enrichment: pull Part Number, Manufacturer and Package out of a
datasheet before it is chunked.

Why this matters at scale: with thousands of datasheets in one index, lexical
search alone answers "which document mentions 40 V?" but not "what is the
absolute maximum rating of *this* chip?". A structured part number on every
chunk turns vague retrieval into hard filtering:

    search("collector current", part="MMBT3904")

The extraction is deliberately regex-first (fast, deterministic, runs offline)
with an optional LLM hook for the long tail of odd part numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

# --------------------------------------------------------------------------- #
# Manufacturers
# --------------------------------------------------------------------------- #

MANUFACTURERS = {
    "STMicroelectronics": ["stmicroelectronics", "st microelectronics"],
    "Texas Instruments": ["texas instruments", r"\bti\b"],
    "Microchip": ["microchip technology", "microchip"],
    "Atmel": ["atmel"],
    "Infineon": ["infineon technologies", "infineon"],
    "NXP": [r"\bnxp\b", "nxp semiconductors"],
    "onsemi": ["onsemi", "on semiconductor", "semiconductor components industries"],
    "Diodes Incorporated": ["diodes incorporated", "diodes inc"],
    "Vishay": ["vishay"],
    "Rohm": ["rohm semiconductor", r"\brohm\b"],
    "Toshiba": ["toshiba"],
    "Analog Devices": ["analog devices", r"\badi\b"],
    "Maxim": ["maxim integrated"],
    "Nexperia": ["nexperia"],
    "Torex": ["torex semiconductor"],
    "Winbond": ["winbond"],
    "Alpha & Omega": ["alpha & omega", "alpha and omega", "aosmd"],
    "Holtek": ["holtek"],
    "WCH": ["wch", "nanjing qinheng"],
    "FTDI": ["future technology devices", r"\bftdi\b"],
    "Advanced Monolithic Systems": ["advanced monolithic", r"\bams\b"],
    "Top Power": ["top power"],
    "Monolithic Power Systems": ["monolithic power systems", r"\bmps\b"],
    "Shenzhen": ["shenzhen"],
    "Everlight": ["everlight"],
    "Lite-On": ["lite-on", "liteon"],
    "Torex": ["torex semiconductor"],
}

# --------------------------------------------------------------------------- #
# Packages — keep in sync with SMD_PACKAGES in assets/js/data.js
# --------------------------------------------------------------------------- #

PACKAGES = [
    "SOT-23-5", "SOT-23-6", "SOT-23-3", "SOT-23", "SOT-25", "SOT-26",
    "SOT-323", "SOT-353", "SOT-363", "SOT-343", "SOT-143", "SOT-89",
    "SOT-223", "SOT-416", "SOT-523", "SOT-553", "SOT-346",
    "SC-59", "SC-70-5", "SC-70-6", "SC-70", "SC-82", "SC-88",
    "SOD-123FL", "SOD-123F", "SOD-123", "SOD-323F", "SOD-323FL", "SOD-323",
    "SOD-523", "SOD-80",
    "DO-214AC", "DO-214AA", "DO-214AB", "DO-215AA", "DO-215AB",
    "SMA", "SMB", "SMC", "TO-252", "TO-263", "DPAK", "D2PAK",
    "SOIC-8", "SOIC-14", "SOIC-16", "SOP-4", "SOP-8", "SOP-16",
    "SSOP-5", "SSOP-28", "TSSOP-8", "MSOP-8", "TSSOP",
    "QFN", "DFN", "WDFN", "UFN", "SON", "USPN", "USP", "HSNT", "SNT",
    "WLCSP", "WLP", "LGA", "BGA",
    "LQFP-48", "LQFP", "TQFP-32", "TQFP", "QFP", "DIP-6", "DIP-8", "PDIP",
]

# Tokens that look like part numbers but are not.
_NOISE = {
    "PAGE", "DATE", "REV", "NOTE", "NOTES", "TABLE", "FIGURE", "ISO", "IEC",
    "JEDEC", "ROHS", "HTTP", "WWW", "PDF", "HTML", "TITLE", "INDEX", "COPY",
    "RIGHT", "RESERVED", "PRELIMINARY", "CONFIDENTIAL", "DOCUMENT", "SHEET",
    "TOTAL", "VERSION", "CHAPTER", "SECTION", "EXAMPLE", "WARNING", "CAUTION",
}

# Part numbers are written in every possible casing ("ATmega328P", "STM32F103C8T6",
# "dsPIC33EPXXGS50X"), so the pattern is case-insensitive and everything is
# normalised to upper case before scoring.
_PART_RE = re.compile(r"\b([A-Za-z]{2,8}\d{1,6}[A-Za-z0-9\-\.]{0,12})\b")

# Document numbers, not part numbers: DS70005127D, SWRS192, SLAU123B...
_DOCNO_RE = re.compile(r"^[A-Z]{2,4}\d{5,}[A-Z0-9]*$")

_MONTHS = {"JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
           "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
           "JAN", "FEB", "MAR", "APR", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}

# Words that survive the "letters + digits" test but are never part numbers.
_STOPWORDS = {
    "AND", "THE", "FOR", "WITH", "NOTE", "PAGE", "FIGURE", "TABLE", "VCC", "GND",
    "VDD", "VSS", "NC", "TYP", "MAX", "MIN", "UNIT", "VALUE", "CONDITION",
    "PARAMETER", "SYMBOL", "TEST", "LEVEL", "VERSION", "DATE", "REVISION",
}


@dataclass
class DocMeta:
    part: Optional[str] = None
    manufacturer: Optional[str] = None
    package: Optional[str] = None
    family: Optional[str] = None
    confidence: float = 0.0
    evidence: dict = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = self.evidence or {}
        return d


_BAD_PREFIXES = ("FIGURE", "TABLE", "PAGE", "SECTION", "CHAPTER", "NOTE",
                 "STEP", "EXAMPLE", "APPENDIX", "EQUATION", "GRAPH",
                 "PACKAGE", "PIN", "SIGNAL", "PORT", "BLOCK", "ORDER")


def _valid_token(token: str) -> bool:
    t = token.upper()
    if t in _NOISE or t in _STOPWORDS or t in _MONTHS:
        return False
    if t.startswith(_BAD_PREFIXES):        # FIGURE3-1, TABLE2, PAGE12 …
        return False
    if not (4 <= len(t) <= 24):
        return False
    if not (any(c.isalpha() for c in t) and any(c.isdigit() for c in t)):
        return False
    for month in _MONTHS:
        if month in t:                    # "SWRS192-JULY2018"
            return False
    return True


def _part_from_filename(filename: str) -> Optional[str]:
    """Filenames are the most reliable part-number source: ATmega328P_pins.pdf,
    ucc27212a-q1_pins.pdf, PIC32MM_GPM_pins.pdf. Take the first chunk that has
    both letters and digits; else the longest chunk that has digits."""
    stem = Path(filename).stem
    stem = re.sub(r"\s*\([^)]*\)", "", stem)
    chunks = [c for c in re.split(r"[_\s]+", stem) if c]
    chunks = [c for c in chunks if c.lower() not in
              {"pins", "pin", "datasheet", "ds", "spec", "manual", "rev", "en", "preview"}]

    with_digits = []
    for chunk in chunks:
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.]*", chunk):
            if any(c.isdigit() for c in tok):
                with_digits.append(tok)
    if not with_digits:
        return None
    for tok in with_digits:                      # prefer a letters+digits mix
        if any(c.isalpha() for c in tok) and len(tok) >= 4:
            return tok.upper()
    return max(with_digits, key=len).upper()


def _score_candidate(token: str, occurrences: int, first_page: int,
                     filename_part: Optional[str] = None) -> float:
    """Heuristic score for 'is this token the part number of the datasheet?'"""
    if not _valid_token(token):
        return 0.0
    t = token.upper()

    score = 0.0
    score += 2.0 * min(occurrences, 10) / 10.0
    score += 3.0 if first_page <= 2 else 0.0            # appears on the title page
    score += 1.5 if 5 <= len(t) <= 16 else 0.0
    score += 1.0 if "-" in t else 0.0                   # STM32F103C8T6 / ATmega328P-AU
    score += 0.5 if re.search(r"\d[A-Z]$", t) else 0.0

    if _DOCNO_RE.match(t):                              # DS70005127D and friends
        score -= 4.0

    if filename_part:
        flat_tok = re.sub(r"[^A-Z0-9]", "", t)
        flat_fn = re.sub(r"[^A-Z0-9]", "", filename_part.upper())
        if flat_tok and flat_fn:
            if flat_tok == flat_fn:
                score += 8.0
            elif flat_tok in flat_fn or flat_fn in flat_tok:
                score += 4.0
    return max(score, 0.0)


def extract_part_number(parsed) -> tuple[Optional[str], float, dict]:
    """Scan the first two pages (title page + ordering info) for a part number.

    `parsed` is a ParsedDoc from parsers.py.
    """
    filename_part = _part_from_filename(parsed.filename)
    text = parsed.first_pages_text(2)
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    counts: dict[str, dict] = {}

    def note(token: str) -> None:
        token = token.strip("-").strip(".").upper()
        if not token:
            return
        info = counts.setdefault(token, {"count": 0, "first_page": 99})
        info["count"] += 1
        for page in parsed.pages[:2]:
            if token in page.text.replace("\u2013", "-").replace("\u2014", "-").upper():
                info["first_page"] = min(info["first_page"], page.number)
                break

    for match in _PART_RE.finditer(text):
        note(match.group(1))
    # the filename is a candidate even when the body text never spells it out
    if filename_part:
        note(filename_part)

    scored = []
    for token, info in counts.items():
        score = _score_candidate(token, info["count"], info["first_page"], filename_part)
        if score > 0:
            scored.append((score, token, info))
    if not scored:
        return (filename_part, 0.4, {"source": "filename"}) if filename_part else (None, 0.0, {})

    scored.sort(key=lambda x: (-x[0], -x[2]["count"], x[1]))
    best_score, best_token, _best_info = scored[0]

    confidence = min(1.0, best_score / 9.0)
    return best_token, round(confidence, 2), {
        "filename_guess": filename_part,
        "candidates": [t for _s, t, _i in scored[:5]],
    }


def extract_manufacturer(parsed) -> Optional[str]:
    text = parsed.first_pages_text(2).lower()
    best, hits = None, 0
    for name, patterns in MANUFACTURERS.items():
        n = sum(len(re.findall(p, text)) for p in patterns)
        if n > hits:
            best, hits = name, n
    return best


def extract_package(parsed) -> Optional[str]:
    text = parsed.first_pages_text(6).upper()
    found = {}
    for pkg in PACKAGES:
        pattern = r"\b%s\b" % pkg.upper().replace("-", "[- ]?")
        n = len(re.findall(pattern, text))
        if n:
            found[pkg] = n
    if not found:
        return None
    # longest, most frequent name wins ("SOT-23-5" beats "SOT-23")
    return max(found.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def guess_family(part: Optional[str]) -> Optional[str]:
    if not part:
        return None
    p = part.upper()
    for fam in ("STM32", "ATMEGA", "ATTINY", "PIC32", "DSPIC", "MSP430", "GD32", "NRF52", "ESP32"):
        if p.startswith(fam):
            return fam
    if re.match(r"^(MMBT|BC8|2N|BCX)", p):
        return "BJT"
    if re.match(r"^(AO|SI|BSS|IRLM|NTR|DMG)", p):
        return "MOSFET"
    if re.match(r"^(BAV|BAT|MMBD|M7|SS1|SS3|1N)", p):
        return "DIODE"
    if re.match(r"^(AMS|LD|XC|HT7|78M|78L|TL4)", p):
        return "REGULATOR"
    return None


def enrich(parsed) -> DocMeta:
    part, conf, evidence = extract_part_number(parsed)
    meta = DocMeta(
        part=part,
        manufacturer=extract_manufacturer(parsed),
        package=extract_package(parsed),
        family=guess_family(part),
        confidence=conf,
        evidence=evidence,
    )
    return meta
