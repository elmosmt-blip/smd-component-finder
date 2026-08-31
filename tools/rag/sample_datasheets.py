#!/usr/bin/env python3
"""Generate a demo corpus of realistic SMT datasheets (PDF).

Why this exists: to test the RAG pipeline you need documents whose *correct
answers you know*. Real datasheets are perfect content but you cannot assert
"the collector current of MMBT3904 is 200 mA" without reading them first.

These generated PDFs follow the section structure every vendor uses:

    Features -> Pin Configuration -> Absolute Maximum Ratings ->
    Electrical Characteristics -> Package Dimensions -> Ordering Information

so the structural chunker, the section classifier, the part-number extractor
and the hard part filter can all be tested end to end. Numbers match the seed
database in assets/js/data.js.

    python3 tools/rag/sample_datasheets.py [--out data/datasheets]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "data" / "datasheets"

STYLES = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=STYLES["Heading1"], fontSize=16, spaceAfter=8, alignment=TA_CENTER)
H2 = ParagraphStyle("H2", parent=STYLES["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4,
                    textColor=colors.HexColor("#0b3d5c"))
BODY = ParagraphStyle("BODY", parent=STYLES["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("SMALL", parent=STYLES["BodyText"], fontSize=7.5, leading=9,
                       textColor=colors.HexColor("#444444"))

TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce9f2")),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])


def _table(rows):
    t = Table(rows, hAlign="LEFT")
    t.setStyle(TABLE_STYLE)
    return t


def _para(text):
    return Paragraph(text, BODY)


# --------------------------------------------------------------------------- #
# Datasheet definitions (numbers mirror assets/js/data.js)
# --------------------------------------------------------------------------- #

DATASHEETS = [
    {
        "part": "MMBT3904",
        "title": "MMBT3904 — NPN General Purpose Transistor",
        "mfr": "onsemi",
        "package": "SOT-23",
        "description": (
            "The MMBT3904 is a general-purpose NPN bipolar junction transistor in a "
            "SOT-23 surface mount package. It is intended for low-power switching and "
            "amplification up to 200 mA collector current."
        ),
        "features": [
            "Collector-emitter voltage VCEO = 40 V",
            "Continuous collector current IC = 200 mA",
            "High current gain: hFE 100 to 300 at IC = 10 mA",
            "SOT-23 (SC-59) package, 2.9 x 1.6 mm body",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "Base", "Control input"],
                   ["2", "Emitter", "Emitter, usually grounded"],
                   ["3", "Collector", "Switched output"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VCEO", "Collector-emitter voltage", "40", "V"],
                        ["VCBO", "Collector-base voltage", "60", "V"],
                        ["VEBO", "Emitter-base voltage", "6.0", "V"],
                        ["IC", "Collector current, continuous", "200", "mA"],
                        ["Ptot", "Total power dissipation", "350", "mW"],
                        ["Tj", "Junction temperature", "150", "degC"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VCE(sat)", "Collector-emitter saturation", "IC = 50 mA, IB = 5 mA",
                        "-", "0.15", "0.30", "V"],
                       ["hFE", "DC current gain", "VCE = 1 V, IC = 10 mA", "100", "-", "300", "-"],
                       ["fT", "Transition frequency", "VCE = 20 V, IC = 10 mA", "250", "300", "-", "MHz"],
                       ["Cobo", "Output capacitance", "VCB = 5 V, f = 1 MHz", "-", "4.0", "-", "pF"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "2.80", "2.90", "3.04", "mm"],
                         ["B (body width)", "1.20", "1.30", "1.45", "mm"],
                         ["C (height)", "0.89", "1.00", "1.12", "mm"],
                         ["e (lead pitch)", "-", "1.90", "-", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["MMBT3904", "SOT-23", "1A", "Reel, 3000 pcs"],
                     ["MMBT3904LT1G", "SOT-23", "1A", "Reel, 3000 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "357", "K/W"],
                    ["RthJL", "Thermal resistance junction-lead", "83", "K/W"]],
    },
    {
        "part": "2N7002",
        "title": "2N7002 — N-Channel Enhancement Mode MOSFET",
        "mfr": "Diodes Incorporated",
        "package": "SOT-23",
        "description": (
            "The 2N7002 is an N-channel enhancement mode MOSFET with a 60 V drain-source "
            "rating in a SOT-23 package. Suitable for low-power switching, level shifting "
            "and relay driving."
        ),
        "features": [
            "Drain-source voltage VDS = 60 V",
            "Continuous drain current ID = 300 mA",
            "Low on-resistance RDS(on) = 5 ohm at VGS = 10 V",
            "Gate threshold voltage 1.0 to 2.5 V",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "Gate", "Control input"],
                   ["2", "Source", "Source, usually grounded"],
                   ["3", "Drain", "Switched output"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VDS", "Drain-source voltage", "60", "V"],
                        ["VGS", "Gate-source voltage", "±20", "V"],
                        ["ID", "Continuous drain current", "300", "mA"],
                        ["IDM", "Pulsed drain current", "800", "mA"],
                        ["Ptot", "Total power dissipation", "300", "mW"],
                        ["Tj", "Junction temperature", "150", "degC"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VGS(th)", "Gate threshold voltage", "VDS = VGS, ID = 250 uA",
                        "1.0", "1.8", "2.5", "V"],
                       ["RDS(on)", "Static drain-source on-resistance", "VGS = 10 V, ID = 500 mA",
                        "-", "3.0", "5.0", "ohm"],
                       ["IDSS", "Zero gate voltage drain current", "VDS = 60 V", "-", "-", "1.0", "uA"],
                       ["Qg", "Total gate charge", "VDS = 10 V, VGS = 10 V", "-", "0.6", "1.5", "nC"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "2.80", "2.90", "3.04", "mm"],
                         ["B (body width)", "1.20", "1.30", "1.45", "mm"],
                         ["C (height)", "0.89", "1.00", "1.12", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["2N7002", "SOT-23", "702", "Reel, 3000 pcs"],
                     ["2N7002K", "SOT-23", "72K", "Reel, 3000 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "417", "K/W"]],
    },
    {
        "part": "BAV99",
        "title": "BAV99 — High-Speed Dual Switching Diode",
        "mfr": "Nexperia",
        "package": "SOT-23",
        "description": (
            "The BAV99 is a dual high-speed switching diode with a common cathode, "
            "housed in a SOT-23 package. It is used for high-speed switching, "
            "signal clamping and reverse polarity protection."
        ),
        "features": [
            "Repetitive reverse voltage VRRM = 70 V",
            "Forward current IF = 215 mA",
            "Reverse recovery time trr = 4 ns",
            "Common cathode configuration (pins 1 and 3 = anodes, pin 2 = cathode)",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "A1", "Anode of diode 1"],
                   ["2", "K", "Common cathode"],
                   ["3", "A2", "Anode of diode 2"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VRRM", "Repetitive reverse voltage", "70", "V"],
                        ["VR", "Continuous reverse voltage", "70", "V"],
                        ["IF", "Forward current", "215", "mA"],
                        ["IFSM", "Non-repetitive peak forward current", "4.0", "A"],
                        ["Ptot", "Total power dissipation", "250", "mW"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VF", "Forward voltage", "IF = 1 mA", "-", "0.65", "0.80", "V"],
                       ["VF", "Forward voltage", "IF = 50 mA", "-", "0.85", "1.10", "V"],
                       ["IR", "Reverse leakage current", "VR = 70 V", "-", "-", "2.5", "uA"],
                       ["trr", "Reverse recovery time", "IF = 10 mA", "-", "4", "6", "ns"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "2.80", "2.90", "3.04", "mm"],
                         ["B (body width)", "1.20", "1.30", "1.45", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["BAV99", "SOT-23", "A7", "Reel, 3000 pcs"],
                     ["BAV99W", "SOT-323", "A7", "Reel, 3000 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "500", "K/W"]],
    },
    {
        "part": "AMS1117-3.3",
        "title": "AMS1117-3.3 — 1 A Low Dropout Voltage Regulator",
        "mfr": "Advanced Monolithic Systems",
        "package": "SOT-223",
        "description": (
            "The AMS1117-3.3 is a fixed 3.3 V low dropout linear regulator capable of "
            "supplying 1 A of output current. Dropout is 1.3 V at full load."
        ),
        "features": [
            "Fixed output voltage 3.3 V",
            "Output current up to 1 A",
            "Low dropout voltage 1.3 V at 1 A",
            "Input voltage up to 15 V",
            "Thermal shutdown and current limit",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "GND", "Ground"],
                   ["2", "VOUT", "Regulated 3.3 V output"],
                   ["3", "VIN", "Supply input"],
                   ["Tab", "VOUT", "Tab is connected to VOUT"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VIN", "Maximum input voltage", "15", "V"],
                        ["IOUT", "Output current", "1.0", "A"],
                        ["Ptot", "Power dissipation", "Internally limited", "-"],
                        ["Tj", "Operating junction temperature", "125", "degC"],
                        ["Tstg", "Storage temperature", "-65 to 150", "degC"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VOUT", "Output voltage", "VIN = 5 V, IOUT = 10 mA", "3.267", "3.300", "3.333", "V"],
                       ["Vdrop", "Dropout voltage", "IOUT = 1 A", "-", "1.1", "1.3", "V"],
                       ["Iq", "Quiescent current", "VIN = 5 V", "-", "5", "10", "mA"],
                       ["PSRR", "Ripple rejection", "f = 120 Hz", "60", "72", "-", "dB"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "6.30", "6.50", "6.70", "mm"],
                         ["B (body width)", "3.30", "3.50", "3.70", "mm"],
                         ["C (height)", "1.50", "1.60", "1.80", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["AMS1117-3.3", "SOT-223", "AMS1117 3.3", "Reel, 1000 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "90", "K/W"],
                    ["RthJC", "Thermal resistance junction-case", "15", "K/W"]],
    },
    {
        "part": "TP4056",
        "title": "TP4056 — 1 A Li-Ion Battery Charger",
        "mfr": "Top Power",
        "package": "SOP-8",
        "description": (
            "The TP4056 is a complete constant-current / constant-voltage linear charger "
            "for single-cell lithium-ion batteries, with thermal regulation and a "
            "programmable charge current up to 1 A."
        ),
        "features": [
            "Programmable charge current up to 1 A",
            "Input voltage range 4.5 V to 8 V",
            "Constant-current / constant-voltage operation",
            "Thermal regulation, automatic recharge",
            "Charge status outputs CHRG and STDBY",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "TEMP", "Battery temperature sense input"],
                   ["2", "PROG", "Charge current program resistor"],
                   ["3", "GND", "Ground"],
                   ["4", "VCC", "Supply input 4.5 to 8 V"],
                   ["5", "BAT", "Battery connection"],
                   ["6", "STDBY", "Charge complete indicator, open drain"],
                   ["7", "CHRG", "Charging indicator, open drain"],
                   ["8", "CE", "Chip enable, active high"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VCC", "Input supply voltage", "8.0", "V"],
                        ["VBAT", "Battery pin voltage", "7.0", "V"],
                        ["IBAT", "Charge current", "1.0", "A"],
                        ["Tj", "Operating junction temperature", "125", "degC"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VCC", "Input supply voltage", "-", "4.5", "5.0", "8.0", "V"],
                       ["VFLOAT", "Regulated output float voltage", "-", "4.158", "4.200", "4.242", "V"],
                       ["IBAT", "Charge current", "RPROG = 1.2 kohm", "900", "1000", "1100", "mA"],
                       ["VTRIM", "Trickle charge threshold", "-", "2.8", "2.9", "3.0", "V"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "4.80", "4.90", "5.00", "mm"],
                         ["B (body width)", "3.80", "3.90", "4.00", "mm"],
                         ["e (lead pitch)", "-", "1.27", "-", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["TP4056", "SOP-8", "TP4056", "Reel, 2500 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "120", "K/W"]],
    },
    {
        "part": "SI2301",
        "title": "Si2301 — P-Channel 20 V MOSFET",
        "mfr": "Vishay",
        "package": "SOT-23",
        "description": (
            "The Si2301 is a P-channel enhancement mode MOSFET with a 20 V drain-source "
            "rating and 2.3 A continuous drain current in a SOT-23 package. It is used "
            "as a load switch in battery powered equipment."
        ),
        "features": [
            "Drain-source voltage VDS = -20 V",
            "Continuous drain current ID = -2.3 A",
            "Low on-resistance RDS(on) = 90 milliohm at VGS = -4.5 V",
            "Logic level gate drive",
        ],
        "pinout": [["Pin", "Name", "Function"],
                   ["1", "Gate", "Control input, active low"],
                   ["2", "Source", "Source, connect to supply"],
                   ["3", "Drain", "Switched output"]],
        "max_ratings": [["Symbol", "Parameter", "Value", "Unit"],
                        ["VDS", "Drain-source voltage", "-20", "V"],
                        ["VGS", "Gate-source voltage", "±8", "V"],
                        ["ID", "Continuous drain current", "-2.3", "A"],
                        ["IDM", "Pulsed drain current", "-10", "A"],
                        ["Ptot", "Total power dissipation", "750", "mW"]],
        "electrical": [["Symbol", "Parameter", "Test condition", "Min", "Typ", "Max", "Unit"],
                       ["VGS(th)", "Gate threshold voltage", "VDS = VGS, ID = -250 uA",
                        "-0.45", "-0.75", "-1.2", "V"],
                       ["RDS(on)", "Static drain-source on-resistance", "VGS = -4.5 V, ID = -2.3 A",
                        "-", "70", "90", "milliohm"],
                       ["IDSS", "Zero gate voltage drain current", "VDS = -16 V", "-", "-", "-1", "uA"],
                       ["Qg", "Total gate charge", "VDS = -10 V, VGS = -4.5 V", "-", "5.5", "9.0", "nC"]],
        "package_dims": [["Dimension", "Min", "Nom", "Max", "Unit"],
                         ["A (body length)", "2.80", "2.90", "3.04", "mm"],
                         ["B (body width)", "1.20", "1.30", "1.45", "mm"]],
        "ordering": [["Order code", "Package", "Marking", "Packing"],
                     ["SI2301BDS", "SOT-23", "A1", "Reel, 3000 pcs"]],
        "thermal": [["Symbol", "Parameter", "Value", "Unit"],
                    ["RthJA", "Thermal resistance junction-ambient", "166", "K/W"]],
    },
]


def build_pdf(spec: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s_datasheet.pdf" % spec["part"])
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=spec["title"], author=spec["mfr"],
    )

    story = [
        Paragraph(spec["part"], H1),
        Paragraph(spec["title"], ParagraphStyle(
            "sub", parent=BODY, alignment=TA_CENTER, fontSize=10,
            textColor=colors.HexColor("#333333"))),
        Paragraph("Manufacturer: %s &nbsp;&nbsp; Package: %s" % (spec["mfr"], spec["package"]),
                  ParagraphStyle("mfr", parent=SMALL, alignment=TA_CENTER, fontSize=9)),
        Spacer(1, 6 * mm),

        Paragraph("General Description", H2),
        _para(spec["description"]),

        Paragraph("Features", H2),
    ]
    story += [_para("&bull; " + f) for f in spec["features"]]

    story += [
        Paragraph("Pin Configuration", H2),
        _para("Pin assignment for the %s package (top view)." % spec["package"]),
        Spacer(1, 2 * mm),
        _table(spec["pinout"]),

        Paragraph("Absolute Maximum Ratings", H2),
        _para("Stresses beyond these values may cause permanent damage to the device."),
        Spacer(1, 2 * mm),
        KeepTogether(_table(spec["max_ratings"])),
    ]

    story += [
        PageBreak(),
        Paragraph("Electrical Characteristics", H2),
        _para("Typical values at TA = 25 degC unless otherwise noted."),
        Spacer(1, 2 * mm),
        _table(spec["electrical"]),

        Paragraph("Thermal Characteristics", H2),
        _table(spec["thermal"]),

        Paragraph("Package Dimensions", H2),
        _para("Outline drawing for the %s package. All dimensions in millimetres."
              % spec["package"]),
        Spacer(1, 2 * mm),
        _table(spec["package_dims"]),

        Paragraph("Ordering Information", H2),
        _table(spec["ordering"]),
        Spacer(1, 4 * mm),
        Paragraph("Document revision 1.0. Values are typical at TA = 25 degC. "
                  "Verify against the latest datasheet from %s before design release."
                  % spec["mfr"], SMALL),
    ]

    doc.build(story)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    for spec in DATASHEETS:
        path = build_pdf(spec, args.out)
        print("  %-46s %6.1f KB" % (path.name, path.stat().st_size / 1024))
    print("\n%d datasheets in %s" % (len(DATASHEETS), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
