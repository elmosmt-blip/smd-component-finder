"""Structured extraction: one parsed datasheet -> one "card".

The chunk index (index_db.py) answers "where in my PDFs is this said?". A card
answers the different question the site actually needs: "what IS this part?".

    rating tables        -> key_ratings  (Vceo 40 V, Ic 200 mA …)
    electrical tables    -> key_specs    (hFE 300, VCE(sat) 0.30 V …)
    pin tables           -> pins         (1 Base, 2 Emitter, 3 Collector)
    dimension tables     -> dimensions   (2.90 × 1.30 × 1.00 mm, pitch 1.90)
    ordering tables      -> order codes and markings
    text sections        -> description, features

Everything is rule based and offline: no LLM call, so 300k documents are a
matter of CPU hours, not API budget. Every field keeps the page it came from,
so the UI can show the evidence instead of asking the user to trust a number.

    from tools.rag import extract, parsers
    card = extract.build_card(parsers.get_parser("auto").parse(path))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from tools.rag import metadata, quality


# ---------------------------------------------------------------------------
# Canonical parameter names.
#
# Datasheet symbols are written a dozen different ways ("V(BR)CEO", "VCEO",
# "V CE", "Vceo"). Everything is normalised to lowercase without spaces and
# punctuation, then looked up here. The canonical name is what the UI shows and
# what the filters use.
# ---------------------------------------------------------------------------

SYMBOLS: Dict[str, Tuple[str, str]] = {
    # transistors
    "vceo": ("collector_emitter_voltage", "V"),
    "vces": ("collector_emitter_saturation_voltage", "V"),
    "vcbo": ("collector_base_voltage", "V"),
    "vebo": ("emitter_base_voltage", "V"),
    "vbeo": ("emitter_base_voltage", "V"),
    "vbvceo": ("collector_emitter_voltage", "V"),
    "vbrceo": ("collector_emitter_voltage", "V"),
    "ic": ("collector_current", "A"),
    "icmax": ("collector_current", "A"),
    "iccont": ("collector_current", "A"),
    "ib": ("base_current", "A"),
    "hfe": ("dc_current_gain", ""),
    "h21e": ("dc_current_gain", ""),
    "ft": ("transition_frequency", "MHz"),
    "vcesat": ("saturation_voltage", "V"),
    "vbesat": ("base_saturation_voltage", "V"),
    "cobo": ("output_capacitance", "pF"),
    "cibo": ("input_capacitance", "pF"),
    # mosfets
    "vds": ("drain_source_voltage", "V"),
    "vdss": ("drain_source_voltage", "V"),
    "vgs": ("gate_source_voltage", "V"),
    "vgss": ("gate_source_voltage", "V"),
    "id": ("drain_current", "A"),
    "idcont": ("drain_current", "A"),
    "idm": ("pulsed_drain_current", "A"),
    "vgsth": ("gate_threshold_voltage", "V"),
    "rdson": ("on_resistance", "ohm"),
    "rds": ("on_resistance", "ohm"),
    "idss": ("zero_gate_drain_current", "A"),
    "igss": ("gate_leakage_current", "A"),
    "qg": ("gate_charge", "nC"),
    "qgs": ("gate_source_charge", "nC"),
    "qgd": ("gate_drain_charge", "nC"),
    "ciss": ("input_capacitance", "pF"),
    "coss": ("output_capacitance", "pF"),
    "crss": ("reverse_transfer_capacitance", "pF"),
    # diodes
    "vr": ("reverse_voltage", "V"),
    "vrrm": ("repetitive_reverse_voltage", "V"),
    "vrm": ("reverse_voltage", "V"),
    "vf": ("forward_voltage", "V"),
    "vfm": ("forward_voltage", "V"),
    "if": ("forward_current", "A"),
    "ifav": ("forward_current", "A"),
    "ifsm": ("surge_current", "A"),
    "itsm": ("surge_current", "A"),
    "ir": ("reverse_current", "A"),
    "irm": ("reverse_current", "A"),
    "trr": ("reverse_recovery_time", "ns"),
    "vz": ("zener_voltage", "V"),
    "vzt": ("zener_voltage", "V"),
    "izt": ("zener_test_current", "A"),
    "zz": ("zener_impedance", "ohm"),
    # regulators / power / chargers
    "vcc": ("input_voltage", "V"),
    "vdd": ("supply_voltage", "V"),
    "vss": ("supply_voltage", "V"),
    "vbat": ("battery_voltage", "V"),
    "ibat": ("charge_current", "A"),
    "ichg": ("charge_current", "A"),
    "icharge": ("charge_current", "A"),
    "vfloat": ("float_voltage", "V"),
    "vreg": ("output_voltage", "V"),
    "vtrim": ("trickle_charge_threshold", "V"),
    "vtrickle": ("trickle_charge_threshold", "V"),
    "vprog": ("program_voltage", "V"),
    "rprog": ("program_resistance", "ohm"),
    "iz": ("zener_current", "A"),
    "vin": ("input_voltage", "V"),
    "vout": ("output_voltage", "V"),
    "iout": ("output_current", "A"),
    "iload": ("output_current", "A"),
    "iq": ("quiescent_current", "A"),
    "icc": ("supply_current", "A"),
    "vdrop": ("dropout_voltage", "V"),
    "vdo": ("dropout_voltage", "V"),
    "psrr": ("psrr", "dB"),
    "iline": ("line_regulation", "%"),
    "iload_reg": ("load_regulation", "%"),
    "eff": ("efficiency", "%"),
    "fsw": ("switching_frequency", "kHz"),
    "vref": ("reference_voltage", "V"),
    # thermal / common
    "ptot": ("power_dissipation", "W"),
    "pd": ("power_dissipation", "W"),
    "pdiss": ("power_dissipation", "W"),
    "tj": ("junction_temperature", "°C"),
    "tstg": ("storage_temperature", "°C"),
    "tamb": ("ambient_temperature", "°C"),
    "ta": ("ambient_temperature", "°C"),
    "topr": ("operating_temperature", "°C"),
    "rthja": ("thermal_resistance_junction_ambient", "°C/W"),
    "rthjapcb": ("thermal_resistance_junction_ambient", "°C/W"),
    "rthjc": ("thermal_resistance_junction_case", "°C/W"),
    "rthjl": ("thermal_resistance_junction_lead", "°C/W"),
    "vesd": ("esd_rating", "V"),
    "hbm": ("esd_rating", "V"),
}

# Human labels for the card. Falls back to the parameter text from the table.
LABELS: Dict[str, str] = {
    "collector_emitter_voltage": "Collector-emitter voltage",
    "collector_base_voltage": "Collector-base voltage",
    "emitter_base_voltage": "Emitter-base voltage",
    "collector_current": "Collector current",
    "base_current": "Base current",
    "dc_current_gain": "DC current gain (hFE)",
    "transition_frequency": "Transition frequency",
    "saturation_voltage": "Saturation voltage",
    "drain_source_voltage": "Drain-source voltage",
    "gate_source_voltage": "Gate-source voltage",
    "drain_current": "Drain current",
    "pulsed_drain_current": "Pulsed drain current",
    "gate_threshold_voltage": "Gate threshold voltage",
    "on_resistance": "On-resistance Rds(on)",
    "gate_charge": "Gate charge",
    "input_capacitance": "Input capacitance",
    "output_capacitance": "Output capacitance",
    "reverse_voltage": "Reverse voltage",
    "repetitive_reverse_voltage": "Repetitive reverse voltage",
    "forward_voltage": "Forward voltage",
    "forward_current": "Forward current",
    "surge_current": "Surge current",
    "reverse_current": "Reverse current",
    "reverse_recovery_time": "Reverse recovery time",
    "zener_voltage": "Zener voltage",
    "zener_impedance": "Zener impedance",
    "input_voltage": "Input voltage",
    "output_voltage": "Output voltage",
    "output_current": "Output current",
    "quiescent_current": "Quiescent current",
    "supply_current": "Supply current",
    "dropout_voltage": "Dropout voltage",
    "reference_voltage": "Reference voltage",
    "power_dissipation": "Power dissipation",
    "junction_temperature": "Junction temperature",
    "storage_temperature": "Storage temperature",
    "operating_temperature": "Operating temperature",
    "ambient_temperature": "Ambient temperature",
    "thermal_resistance_junction_ambient": "Thermal resistance RthJA",
    "thermal_resistance_junction_case": "Thermal resistance RthJC",
    "thermal_resistance_junction_lead": "Thermal resistance RthJL",
    "psrr": "PSRR",
    "switching_frequency": "Switching frequency",
    "efficiency": "Efficiency",
    "esd_rating": "ESD rating",
    "battery_voltage": "Battery voltage",
    "charge_current": "Charge current",
    "float_voltage": "Float voltage",
    "supply_voltage": "Supply voltage",
    "trickle_charge_threshold": "Trickle charge threshold",
    "program_voltage": "Program voltage",
    "program_resistance": "Program resistor",
    "zener_current": "Zener current",
    "reverse_transfer_capacitance": "Reverse transfer capacitance",
    "gate_leakage_current": "Gate leakage",
    "zero_gate_drain_current": "Zero-gate drain current",
    "base_saturation_voltage": "Base saturation voltage",
    "collector_emitter_saturation_voltage": "Collector-emitter saturation",
    "zener_test_current": "Zener test current",
    "gate_source_charge": "Gate-source charge",
    "gate_drain_charge": "Gate-drain charge",
    "line_regulation": "Line regulation",
    "load_regulation": "Load regulation",
}

# Unit multipliers to the "base" unit of the canonical parameter above.
UNIT_SCALE = {
    "v": 1.0, "mv": 1e-3, "kv": 1e3,
    "a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6, "na": 1e-9,
    "w": 1.0, "mw": 1e-3, "kw": 1e3,
    "ohm": 1.0, "ω": 1.0, "milliohm": 1e-3, "mohm": 1e-3, "mω": 1e-3, "kohm": 1e3, "kω": 1e3,
    "f": 1.0, "pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "µf": 1e-6,
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
    "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "ps": 1e-12,
    "c": 1.0, "w": 1.0,
    "nc": 1e-9, "pc": 1e-12,
    "db": 1.0, "%": 1.0, "°c": 1.0, "c/w": 1.0, "k/w": 1.0, "°c/w": 1.0,
}

# Which canonical parameters are worth showing as chips at the top of a card.
HEADLINE = [
    "collector_emitter_voltage", "collector_current", "dc_current_gain",
    "drain_source_voltage", "drain_current", "gate_threshold_voltage", "on_resistance",
    "reverse_voltage", "forward_voltage", "forward_current", "zener_voltage",
    "input_voltage", "output_voltage", "output_current", "dropout_voltage",
    "power_dissipation", "junction_temperature", "quiescent_current",
]

# Table classification: (kind, must-have header words)
TABLE_KINDS = [
    ("pins",        ("pin",), ("name", "function", "description", "symbol", "type")),
    # electrical before ratings: its header is more specific (min/typ/max/conditions),
    # while a ratings table also matches the looser "symbol + value/unit" rule.
    ("electrical",  ("symbol", "parameter"), ("min", "typ", "max", "test", "condition")),
    ("ratings",     ("symbol", "parameter"), ("value", "max", "rating", "unit", "limit")),
    ("dimensions",  ("dim", "dimension", "symbol"), ("mm", "inch", "min", "max", "nom", "typ")),
    ("ordering",    ("order", "part", "type"), ("package", "marking", "packing", "code")),
]

SECTION_PATTERNS = {
    "description": (r"general description|description|overview|general",),
    "features": (r"features|characteristics(?!( elect))|benefits",),
    "applications": (r"applications|uses|typical appl",),
    "pin": (r"pin configuration|pin description|pin assignment|pinout|pin function",),
    "maxratings": (r"absolute maximum|maximum ratings|limiting values|stress",),
    "electrical": (r"electrical characteristics|electrical specs|static characteristics",),
    "dimensions": (r"package dimensions|mechanical data|outline drawing|dimensions",),
    "ordering": (r"ordering information|order code|part numbering|marking information",),
}

_BULLET = re.compile(r"^[\s\-–—•·*\u25cf\u25aa\u2043]*")
_WS = re.compile(r"\s+")


@dataclass
class Card:
    """One part, extracted from one PDF. Rendered as a card on the site."""

    part: str
    manufacturer: Optional[str] = None
    package: Optional[str] = None
    family: Optional[str] = None
    description: str = ""
    features: List[str] = field(default_factory=list)
    applications: List[str] = field(default_factory=list)
    pins: List[dict] = field(default_factory=list)
    pin_count: Optional[int] = None
    ratings: List[dict] = field(default_factory=list)
    specs: List[dict] = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)
    order_codes: List[dict] = field(default_factory=list)
    headline: List[dict] = field(default_factory=list)
    key_specs: dict = field(default_factory=dict)
    pages: int = 0
    tables: int = 0
    filename: str = ""
    sha1: str = ""
    parser: str = ""
    confidence: float = 0.0
    sources: dict = field(default_factory=dict)
    extracted_at: str = ""
    text_chars: int = 0
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\u00a0", " ")).strip()


def norm_symbol(sym: str) -> str:
    """'V(BR)CEO', 'V CE', 'Vceo' -> 'vbrceo' / 'vce' / 'vceo'."""
    s = (sym or "").lower()
    s = re.sub(r"[\s\u00a0]+", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def _norm_header(cell: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (cell or "").lower()).strip()


def merge_header(table: List[List[str]]) -> Tuple[List[str], int]:
    """Join a two-row header into one, and say how many rows it ate.

    Vendors love stacked headers:

        ['PIN', '',       'TYPE', 'DESCRIPTION']
        ['NO.', 'NAME',   '',     '']

    Reading only the first row makes DESCRIPTION look like the name column,
    and the pin name ends up being a paragraph of prose. Merging gives
    ['PIN NO.', 'NAME', 'TYPE', 'DESCRIPTION'] and everything falls into place.
    """
    if not table:
        return [], 0
    width = max(len(r) for r in table[:2])
    merged: List[str] = []
    for i in range(width):
        parts = []
        for row in table[:2]:
            cell = _norm_header(row[i]) if i < len(row) else ""
            if cell:
                parts.append(cell)
        merged.append(" ".join(parts))

    rows_used = 1
    second = table[1] if len(table) > 1 else None
    if second is not None and not any(re.search(r"\d", c or "") for c in second):
        # a row without a single digit is a continuation of the header, not data
        rows_used = 2
    return merged, rows_used


def classify_table(table: List[List[str]]) -> Optional[Tuple[str, Dict[str, int], int]]:
    """Return (kind, {column_name: index}, header_rows) if the header is recognised."""
    if not table or len(table) < 2:
        return None
    candidates = []
    merged, rows_used = merge_header(table)
    if sum(1 for c in merged if c) >= 2:
        candidates.append((merged, rows_used))
    candidates.append(([_norm_header(c) for c in table[0]], 1))

    for cells, rows_used in candidates:
        if sum(1 for c in cells if c) < 2:
            continue
        joined = " ".join(cells)
        for kind, must, also in TABLE_KINDS:
            if not any(m in joined for m in must):
                continue
            if not any(a in joined for a in also):
                continue
            cols: Dict[str, int] = {}
            for i, c in enumerate(cells):
                if not c:
                    continue
                # keep the first occurrence of each logical column name
                for name, keys in {
                    "symbol": ("symbol", "dim", "dimension", "param", "order"),
                    "param": ("parameter", "description", "function", "name", "package"),
                    "value": ("value", "rating", "limit", "typ", "nom"),
                    "unit": ("unit",),
                    "min": ("min",),
                    "max": ("max",),
                    "conditions": ("condition", "test"),
                    "pin": ("pin",),
                    "marking": ("marking",),
                    "package": ("package",),
                    "order": ("order", "part"),
                    "packing": ("packing",),
                }.items():
                    if name in cols:
                        continue
                    if any(c == k or c.startswith(k) for k in keys):
                        cols[name] = i
                        break
            if kind == "pins" and "pin" not in cols:
                continue
            if kind in ("ratings", "electrical") and "symbol" not in cols:
                continue
            return kind, cols, rows_used
    return None


_NUM_RE = re.compile(r"[-+]?\d[\d, ]*\.?\d*")


def parse_value(raw: str) -> Tuple[Optional[float], str]:
    """'200' -> (200.0, ''); '±8 V' -> (8.0, 'V'); '-55 to +150 °C' -> (-55, '°C')."""
    if raw is None:
        return None, ""
    s = _clean(raw).replace("−", "-").replace("–", "-")
    if not s or s in {"-", "--", "—", "–"}:
        return None, ""
    m = _NUM_RE.search(s)
    if not m:
        return None, ""
    num = m.group(0).replace(" ", "").replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None, ""
    rest = s[m.end():].strip().lower()
    unit = ""
    if rest:
        m2 = re.match(
            r"(m?ohm|k?ohm|milliohm|ω|mω|kω|m?v|k?v|n?a|µa|ua|m?a|m?w|k?w|"
            r"p?f|n?f|µf|uf|m?hz|k?hz|g?hz|n?s|m?s|µs|us|p?s|°c|c|k/w|°c/w|%|db|v|nc|pc)",
            rest,
        )
        if m2:
            unit = m2.group(1)
    return value, unit


def canonical(symbol: str) -> Optional[Tuple[str, str]]:
    s = norm_symbol(symbol)
    if s in SYMBOLS:
        return SYMBOLS[s]
    # V(BR)CEO -> vceo if the longer form is unknown
    s2 = re.sub(r"^(vbr|vbv|bv)", "v", s)
    if s2 in SYMBOLS:
        return SYMBOLS[s2]
    s3 = s.replace("sat", "sat")
    if s3 in SYMBOLS:
        return SYMBOLS[s3]
    return None


_RANGE_HINT = re.compile(r"(±|\bto\b|~|…|/|\bmin\b|\bmax\b)", re.I)


def _normalise_unit(unit: str) -> str:
    u = (unit or "").strip()
    if not u or re.fullmatch(r"[-–—~.]+", u):
        return ""
    u = re.sub(r"^deg\s*", "°", u, flags=re.I)
    u = u.replace("degrees", "°C").replace("degC", "°C").replace("°c", "°C")
    u = {"c": "°C", "milliohm": "mΩ", "mohm": "mΩ", "ohm": "Ω", "kohm": "kΩ",
         "ua": "µA", "uf": "µF", "us": "µs"}.get(u, u)
    return u


def display_value(raw: str, value: Optional[float], unit: str,
                  minimum: str = "", maximum: str = "") -> str:
    """Keep what the datasheet says when a single number would be a lie.

    '±8 V', '-65 to +150 °C' and 'Internally limited' all carry information
    that a float does not, so the raw text wins whenever it is not a plain
    number. Otherwise fall back to min…max when the typical column is empty.
    """
    raw = _clean(raw)
    if raw and re.fullmatch(r"[-–—~. ]+", raw):   # "-" placeholder, not a value
        raw = ""
    if raw and (value is None or _RANGE_HINT.search(raw)) and len(raw) <= 40:
        norm = _normalise_unit(unit)
        # keep the unit next to the range: "-65 to +150" + "°C" -> "-65 to +150 °C"
        # append the unit unless the raw cell already ends with one
        # ("±8 V", "Internally limited"); word "to" inside a range is not one.
        tail = raw.split()[-1] if raw.split() else ""
        has_unit = bool(re.fullmatch(r"[a-zA-ZΩµ%°]+", tail)) or bool(re.search(r"[Ωµ°]", raw))
        if norm and not has_unit:
            return f"{raw} {norm}"
        return raw
    if value is None:
        mn, mx = _clean(minimum), _clean(maximum)
        if mn and mx and mn != mx:
            return f"{mn}…{mx} {_normalise_unit(unit)}".strip()
        if mn or mx:
            return f"{mn or mx} {_normalise_unit(unit)}".strip()
        return raw
    return _fmt_value(value, _normalise_unit(unit))


def _fmt_value(value: Optional[float], unit: str) -> str:
    if value is None:
        return ""
    if abs(value) >= 1000:
        txt = f"{value:,.0f}".replace(",", " ")
    elif abs(value) >= 1:
        txt = f"{value:g}"
    else:
        txt = f"{value:g}"
    return f"{txt} {unit}".strip()


def _to_base(value: Optional[float], unit: str, canonical_unit: str) -> Optional[float]:
    """Convert to the canonical unit so filters and sorting behave."""
    if value is None:
        return None
    u = unit.lower().replace("ω", "ohm")
    scale = UNIT_SCALE.get(u)
    if scale is None:
        return value
    target = canonical_unit.lower()
    if target in ("v",) and u in ("mv", "v", "kv"):
        return value * scale
    if target in ("a",) and u in ("a", "ma", "ua", "µa", "na"):
        return value * scale
    if target in ("w",) and u in ("w", "mw", "kw"):
        return value * scale
    if target in ("ohm",) and u in ("ohm", "milliohm", "mohm", "kohm"):
        return value * scale
    if target in ("mhz",) and u in ("hz", "khz", "mhz", "ghz"):
        return value * scale / 1e6
    if target in ("pf",) and u in ("f", "pf", "nf", "uf", "µf"):
        return value * scale / 1e-12 if u != "f" else value * 1e12
    if target in ("ns",) and u in ("s", "ms", "us", "µs", "ns", "ps"):
        return value * scale * 1e9
    if target in ("nc",) and u in ("nc", "pc"):
        return value * scale * 1e9
    return value * scale


# ---------------------------------------------------------------------------
# section text
# ---------------------------------------------------------------------------

def section_lines(pages: List[Any], key: str, max_lines: int = 60) -> List[Tuple[int, str]]:
    """Collect (page, line) belonging to a section, by heading keywords."""
    patterns = SECTION_PATTERNS.get(key, ())
    out: List[Tuple[int, str]] = []
    active = False
    for page in pages:
        for raw in (page.text or "").splitlines():
            line = _clean(raw)
            if not line:
                continue
            is_heading = _looks_heading(line)
            if is_heading:
                matched = any(re.search(p, line, re.I) for p in patterns)
                if matched:
                    active = True
                    continue
                if active:
                    active = False
                    break
            elif active:
                out.append((page.number, line))
            if len(out) >= max_lines:
                return out
    return out


def _looks_heading(line: str) -> bool:
    from tools.rag.parsers import _looks_like_heading  # local import: parsers owns the rule
    return _looks_like_heading(line)


_JUNK_START = re.compile(
    r"^(note|figure|fig\.|table|tab\.|see|refer|www\.|http|copyright|\d+\.|"
    r"[a-e]\.|\(|\[|pin |package |ordering |revision )", re.I)


def _looks_like_prose(line: str) -> bool:
    """Reject the fragments that a real datasheet page is full of.

    A description is a sentence about the part. Pin-tables squeezed by the PDF
    extractor ("OnlytheGPIOfunctionisshownonGPIOterminals"), footnotes
    ("Note 1: UART1 has assigned pins") and continuation lines ("...even if
    the clock is not running") are not.
    """
    if len(line) < 60 or len(line) > 600:
        return False
    words = line.split()
    if len(words) < 9:
        return False
    if _JUNK_START.match(line):
        return False
    if re.match(r"^[A-Z]{2,}\d*/", line):          # "PGED3/RP11/RB5" — a pin mux label
        return False
    if re.search(r"\b(Legend|Shaded|Note\s*\d+)\b", line, re.I):   # figure captions
        return False
    if not line[0].isupper():
        return False
    letters = sum(c.isalpha() for c in line)
    if letters < len(line) * 0.55:        # mostly digits/symbols — a table row
        return False
    longest = max(len(w) for w in words)
    if longest > 28:                      # words glued together by the extractor
        return False
    # The text layer wraps paragraphs mid-sentence, so a line rarely ends on a
    # period: requiring one *inside* the line is enough to call it prose.
    if not re.search(r"[.!?](\s|$)", line):
        return False
    return True


def _paragraphs(pages: List[Any], max_pages: int = 2) -> Iterator[Tuple[int, str]]:
    """Rebuild paragraphs: the text layer wraps mid-sentence, so a paragraph
    only makes sense once its lines are glued back together."""
    buf: List[str] = []
    start = 1
    for page in pages[:max_pages]:
        for raw in (page.text or "").splitlines():
            line = _clean(raw)
            if not line or _looks_heading(line) or len(line) < 25:
                if buf:
                    yield start, " ".join(buf)
                    buf = []
                continue
            if not buf:
                start = page.number
            buf.append(line)
        if buf:
            yield start, " ".join(buf)
            buf = []


def extract_description(pages: List[Any], fallback_text: str) -> Tuple[str, int]:
    """Prefer the section that says it is the description; else the first real
    paragraph on the first pages; else nothing at all."""
    section = section_lines(pages, "description")
    if section:
        text = _clean(" ".join(l for _, l in section))
        if _looks_like_prose(text[:600]):
            sentences = re.split(r"(?<=[.!?])\s+", text)
            return _clean(" ".join(sentences[:2]))[:400], section[0][0]

    for page_no, para in _paragraphs(pages):
        if _looks_like_prose(para[:600]):
            sentences = re.split(r"(?<=[.!?])\s+", para)
            return _clean(" ".join(sentences[:2]))[:400], page_no

    # A wrong description is worse than an empty one: the card would then look
    # authoritative about something it merely guessed.
    return "", 1


def extract_bullets(pages: List[Any], key: str, limit: int = 10) -> Tuple[List[str], int]:
    lines = section_lines(pages, key)
    out: List[str] = []
    page = lines[0][0] if lines else 1
    for pg, line in (lines or [(1, p) for _, p in _paragraphs(pages, 1)]):
        text = _BULLET.sub("", line).strip(" :-")
        if len(text) < 4 or len(text) > 160:
            continue
        if text.lower().startswith(("note", "figure", "table", "www.", "http")):
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out, page


# ---------------------------------------------------------------------------
# tables -> fields
# ---------------------------------------------------------------------------

def extract_pins(pages: List[Any]) -> Tuple[List[dict], Optional[int]]:
    best: List[dict] = []
    best_page: Optional[int] = None
    for page in pages:
        for table in page.tables:
            kind = classify_table(table)
            if not kind or kind[0] != "pins":
                continue
            _, cols, header_rows = kind
            pins: List[dict] = []
            for row in table[header_rows:]:
                if not row or not any(c for c in row):
                    continue
                num = row[cols["pin"]] if "pin" in cols and cols["pin"] < len(row) else ""
                if not re.search(r"\d", num or ""):
                    continue
                name = _clean(row[cols["param"]]) if "param" in cols and cols["param"] < len(row) else ""
                if len(name) > 60:
                    # a pin NAME is "HB", not a paragraph: this column is prose
                    name = ""
                func = ""
                for key in ("conditions", "value", "unit"):
                    if key in cols and cols[key] < len(row):
                        func = row[cols[key]] or ""
                        break
                pins.append({
                    "n": _clean(num).split()[0].strip("."),
                    "name": name,
                    "function": _clean(func)[:80],
                })
            named = sum(1 for x in pins if x["name"])
            # A table where almost nothing has a name is a misread, not a pinout.
            if pins and named >= max(2, len(pins) // 2) and len(pins) > len(best):
                best, best_page = pins, page.number
    return best, best_page


def _rows_from(pages: List[Any], kind_wanted: str) -> List[Tuple[int, dict, List[str]]]:
    for page in pages:
        for table in page.tables:
            kind = classify_table(table)
            if not kind or kind[0] != kind_wanted:
                continue
            _, cols, header_rows = kind
            for row in table[header_rows:]:
                if not row or not any(c for c in row):
                    continue
                yield page.number, cols, row


def extract_ratings(pages: List[Any]) -> Tuple[List[dict], Dict[str, dict]]:
    ratings: List[dict] = []
    key: Dict[str, dict] = {}
    for page_no, cols, row in _rows_from(pages, "ratings"):
        sym = _clean(row[cols["symbol"]]) if "symbol" in cols and cols["symbol"] < len(row) else ""
        if not sym or len(sym) > 24:
            continue
        param = _clean(row[cols["param"]]) if "param" in cols and cols["param"] < len(row) else ""
        raw_value = row[cols["value"]] if "value" in cols and cols["value"] < len(row) else ""
        if not raw_value and "max" in cols and cols["max"] < len(row):
            raw_value = row[cols["max"]]
        unit = _clean(row[cols["unit"]]) if "unit" in cols and cols["unit"] < len(row) else ""
        value, parsed_unit = parse_value(raw_value)
        unit = unit or parsed_unit
        can = canonical(sym)
        entry = {
            "symbol": sym,
            "param": param,
            "label": LABELS.get(can[0], param or sym) if can else (param or sym),
            "value": value,
            "unit": _normalise_unit(unit),
            "text": display_value(raw_value, value, unit),
            "key": can[0] if can else None,
            "page": page_no,
        }
        ratings.append(entry)
        if can and can[0] not in key:
            base = _to_base(value, unit, can[1])
            key[can[0]] = {
                "label": LABELS.get(can[0], param or sym),
                "value": base if base is not None else value,
                "unit": can[1],
                "text": entry["text"],
                "key": can[0],
                "page": page_no,
            }
    return ratings, key


def extract_specs(pages: List[Any]) -> Tuple[List[dict], Dict[str, dict]]:
    specs: List[dict] = []
    key: Dict[str, dict] = {}
    for page_no, cols, row in _rows_from(pages, "electrical"):
        sym = _clean(row[cols["symbol"]]) if "symbol" in cols and cols["symbol"] < len(row) else ""
        if not sym or len(sym) > 24:
            continue
        param = _clean(row[cols["param"]]) if "param" in cols and cols["param"] < len(row) else ""
        cond = _clean(row[cols["conditions"]]) if "conditions" in cols and cols["conditions"] < len(row) else ""
        unit = _clean(row[cols["unit"]]) if "unit" in cols and cols["unit"] < len(row) else ""
        mn = row[cols["min"]] if "min" in cols and cols["min"] < len(row) else ""
        typ = row[cols["value"]] if "value" in cols and cols["value"] < len(row) else ""
        mx = row[cols["max"]] if "max" in cols and cols["max"] < len(row) else ""
        t_val, t_unit = parse_value(typ)
        unit = unit or t_unit
        can = canonical(sym)
        entry = {
            "symbol": sym,
            "param": param,
            "label": LABELS.get(can[0], param or sym) if can else (param or sym),
            "conditions": cond[:90],
            "min": _clean(mn),
            "typ": _clean(typ),
            "max": _clean(mx),
            "unit": _normalise_unit(unit),
            "key": can[0] if can else None,
            "page": page_no,
        }
        entry["text"] = display_value(typ, t_val, entry["unit"], mn, mx) or display_value(mx, None, entry["unit"]) or display_value(mn, None, entry["unit"])
        specs.append(entry)
        if can and can[0] not in key:
            base = _to_base(t_val, t_unit or unit, can[1])
            key[can[0]] = {
                "label": LABELS.get(can[0], param or sym),
                "value": base if base is not None else t_val,
                "unit": can[1],
                "text": entry["text"],
                "key": can[0],
                "page": page_no,
            }
    return specs, key


_DIM_KEYS = {
    "a": "body_length", "d": "body_length", "l": "body_length",
    "b": "body_width", "e": "body_width", "w": "body_width",
    "c": "height", "h": "height", "a1": "standoff",
    "e1": "pitch", "p": "pitch", "b1": "lead_width", "l1": "lead_length",
}


def extract_dimensions(pages: List[Any]) -> Tuple[dict, Optional[int]]:
    dims: dict = {}
    page_no: Optional[int] = None
    for pg, cols, row in _rows_from(pages, "dimensions"):
        sym = _clean(row[cols["symbol"]]) if "symbol" in cols and cols["symbol"] < len(row) else ""
        if not sym:
            continue
        letter = re.match(r"([A-Za-z][0-9]?)", sym)
        if not letter:
            continue
        name = _DIM_KEYS.get(letter.group(1).lower())
        if not name or name in dims:
            continue
        # "A (body length)" — the parenthesis is a better name than the letter
        paren = re.search(r"\(([^)]+)\)", sym)
        if paren:
            hint = paren.group(1).lower()
            for kw, target in (("length", "body_length"), ("width", "body_width"),
                               ("height", "height"), ("pitch", "pitch")):
                if kw in hint:
                    name = target
                    break
        if name in dims:
            continue
        raw = row[cols["value"]] if "value" in cols and cols["value"] < len(row) else ""
        if not raw and "max" in cols and cols["max"] < len(row):
            raw = row[cols["max"]]
        value, unit = parse_value(raw)
        if value is None:
            continue
        dims[name] = {"value": value, "unit": unit or "mm"}
        page_no = pg
    return dims, page_no


def extract_ordering(pages: List[Any]) -> Tuple[List[dict], Optional[int]]:
    out: List[dict] = []
    page_no: Optional[int] = None
    for pg, cols, row in _rows_from(pages, "ordering"):
        idx = cols.get("order", cols.get("symbol"))
        code = _clean(row[idx]) if idx is not None and idx < len(row) else ""
        if not code:
            continue
        pkg = _clean(row[cols["package"]]) if "package" in cols and cols["package"] < len(row) else ""
        mark = _clean(row[cols["marking"]]) if "marking" in cols and cols["marking"] < len(row) else ""
        out.append({"code": code, "package": pkg or None, "marking": mark or None})
        page_no = pg
        if len(out) >= 12:
            break
    return out, page_no


# ---------------------------------------------------------------------------
# card assembly
# ---------------------------------------------------------------------------

def build_card(parsed, meta=None) -> Card:
    """Extract a card from a ParsedDoc."""
    from datetime import datetime, timezone

    meta = meta or metadata.enrich(parsed)
    pages = parsed.pages

    description, desc_page = extract_description(pages, parsed.first_pages_text(2))
    features, feat_page = extract_bullets(pages, "features")
    applications, app_page = extract_bullets(pages, "applications")
    pins, pin_page = extract_pins(pages)
    ratings, rating_keys = extract_ratings(pages)
    specs, spec_keys = extract_specs(pages)
    dims, dim_page = extract_dimensions(pages)
    ordering, ord_page = extract_ordering(pages)

    key_specs = dict(rating_keys)
    for k, v in spec_keys.items():
        key_specs.setdefault(k, v)

    headline = [key_specs[k] for k in HEADLINE if k in key_specs][:6]

    # a package table is the most reliable source for the package name
    package = meta.package
    if not package and ordering:
        package = next((o["package"] for o in ordering if o.get("package")), None)

    pin_count = len(pins) or None
    if pin_count is None:
        m = re.search(r"(\d+)\s*(?:-|\s)?pin", " ".join(
            (p.text or "") for p in pages[:2]), re.I)
        if m:
            pin_count = int(m.group(1))

    if not meta.family:
        meta.family = metadata.guess_family(meta.part)

    # Why is this card thin? Measured, not guessed: `flags` are what the PDF
    # actually gave us, and quality.py turns them into something a human reads.
    text_chars = sum(len(p.text or "") for p in pages)
    n_tables = sum(len(p.tables) for p in pages)
    flags = quality.parse_flags(text_chars, parsed.n_pages, n_tables,
                                part_from_meta=bool(meta.part))

    sources = {
        "description": desc_page, "features": feat_page if features else None,
        "applications": app_page if applications else None,
        "pins": pin_page if pins else None,
        "ratings": ratings[0]["page"] if ratings else None,
        "specs": specs[0]["page"] if specs else None,
        "dimensions": dim_page if dims else None,
        "ordering": ord_page if ordering else None,
    }

    # How much of a card did we actually get? Drives the UI badge.
    have = [
        bool(meta.part), bool(meta.manufacturer), bool(package),
        bool(description), bool(features), bool(pins),
        bool(ratings), bool(specs), bool(dims),
    ]
    confidence = round(sum(have) / len(have), 2)

    return Card(
        part=meta.part or Path(parsed.filename).stem,
        manufacturer=meta.manufacturer,
        package=package,
        family=meta.family,
        description=description,
        features=features,
        applications=applications,
        pins=pins,
        pin_count=pin_count,
        ratings=ratings[:40],
        specs=specs[:40],
        dimensions=dims,
        order_codes=ordering,
        headline=headline,
        key_specs=key_specs,
        pages=parsed.n_pages,
        tables=sum(len(p.tables) for p in pages),
        filename=parsed.filename,
        sha1=parsed.sha1,
        parser=parsed.parser,
        confidence=confidence,
        sources={k: v for k, v in sources.items() if v},
        extracted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        text_chars=text_chars,
        flags=flags,
    )
