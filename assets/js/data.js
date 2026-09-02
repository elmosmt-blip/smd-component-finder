/* ============================================================================
 * data.js  —  data layer of SMD Component Finder
 * ----------------------------------------------------------------------------
 * IMPORTANT: this is a SEED dataset (demo data). SMD marking is NOT
 * standardised: the same code means different parts at different vendors, and
 * one part may carry several codes (depending on fab, year and package). So
 * every record carries a `conf` field — how reliable the "code -> part" link is
 * according to our data:
 *
 *   high — the code is accepted by most vendors (usually the full part number
 *          or nearly so: M7, SS14, 78M05, PC817...);
 *   med  — the code is widely known but has variants or exceptions
 *          (1A = MMBT3904, A7 = BAV99, T4 = 1N4148W);
 *   low  — the code comes from one vendor's tables and overlaps with others
 *          (typical for BZX84 zeners and small logic).
 *
 * ALWAYS confirm a result against the datasheet before replacing a part. The
 * "Datasheet" button in the UI runs a PDF search, it does not open one specific
 * document.
 *
 * To swap in a real database see data/README.md and tools/import_dataset.mjs
 * (CSV/JSON normalisation -> components.js).
 * ========================================================================= */

/* ---------------------------------------------------------------------------
 * PACKAGES
 * The names mirror the filter of the reference tool (smtinsider.com/finder),
 * plus metadata: family, pin count, body size, a soldering note. Metadata is
 * filled in for the common packages; the rest still work as a plain filter with
 * no hint attached.
 * Format: [ name, family, pins, body size mm, note ]
 * ------------------------------------------------------------------------ */
window.SMD_PACKAGES = [
  ['SOT-23-3', 'transistor', 3, '2.9 × 1.6', 'Same as SOT-23 (pin count spelled out)'],
  ['SOP-4', 'ic', 4, '4.4 × 2.6', 'Optocoupler package (LSOP-4): 4 leads, 2.54 mm pitch'],
  ['DIP-6', 'ic', 6, '9.5 × 6.4', 'SMD version of DIP with gull-wing leads'],
  ['SOIC-14', 'ic', 14, '8.65 × 3.9', 'Logic and op amps, 1.27 mm pitch'],
  ['SOIC-16', 'ic', 16, '9.9 × 3.9', 'Interface ICs, 1.27 mm pitch'],
  ['SOP-16', 'ic', 16, '10.0 × 3.9', 'USB bridges and controllers, 1.27 mm pitch'],
  ['SSOP-28', 'ic', 28, '10.2 × 5.3', '0.65 mm pitch: needs accurate paste and bridge inspection'],
  ['LQFP-48', 'ic', 48, '7.0 × 7.0', 'Microcontrollers, 0.5 mm pitch'],
  ['TQFP-32', 'ic', 32, '7.0 × 7.0', 'Microcontrollers, 0.8 mm pitch'],
  ['SOT-23', 'transistor', 3, '2.9 × 1.6', 'The most common package for transistors and diodes; solder with hot air or a fine-tip iron'],
  ['SOT-23-3L', 'transistor', 3, '2.9 × 1.6', 'Same as SOT-23 (name spells out the pin count)'],
  ['SOT-23-5', 'ic', 5, '2.9 × 1.6', '5-pin: LDOs, op amps, comparators'],
  ['SOT-23-5L', 'ic', 5, '2.9 × 1.6', 'Alternative name for SOT-23-5'],
  ['SOT-23-6', 'ic', 6, '2.9 × 1.6', '6-pin: dual MOSFETs, protection ICs'],
  ['SOT-23-6L', 'ic', 6, '2.9 × 1.6', 'Alternative name for SOT-23-6'],
  ['SOT-25', 'ic', 5, '2.9 × 1.6', 'Name used by some Asian vendors for SOT-23-5'],
  ['SOT-26', 'ic', 6, '2.9 × 1.6', 'Name used by some Asian vendors for SOT-23-6'],
  ['SOT-323', 'transistor', 3, '2.0 × 1.25', 'SC-70: a shrunken SOT-23, easy to overheat when soldering'],
  ['SOT-353', 'ic', 5, '2.0 × 1.25', 'SC-70-5 / TSSOP-5'],
  ['SOT-363', 'ic', 6, '2.0 × 1.25', 'SC-70-6'],
  ['SOT-343', 'transistor', 4, '2.0 × 1.25', 'SC-82 / SC-70-4'],
  ['SOT-343R', 'transistor', 4, '2.0 × 1.25', 'SC-82 with a reversed pinout'],
  ['SOT-143', 'transistor', 4, '2.9 × 1.6', 'Often dual-gate MOSFETs and RF transistors'],
  ['SOT-143R', 'transistor', 4, '2.9 × 1.6', 'SOT-143 with a reversed pinout'],
  ['SOT-89', 'transistor', 3, '4.5 × 2.5', 'Heatsinking through the centre lead and the pad'],
  ['SOT-89-5', 'ic', 5, '4.5 × 2.5', '5-pin, centre tab = heatsink/ground'],
  ['SOT-223', 'ic', 4, '6.5 × 3.5', 'LDOs and regulators up to ~1 A, heat leaves through the tab'],
  ['SOT-223-5', 'ic', 5, '6.5 × 3.5', '5-pin variant'],
  ['TO-252', 'ic', 3, '6.6 × 6.1', 'DPAK: power package, heat leaves through the pad'],
  ['TO-252-5', 'ic', 5, '6.6 × 6.1', 'DPAK-5 / TO-252-5'],
  ['TO-263', 'ic', 3, '10.2 × 9.1', 'D2PAK: large power package'],
  ['DPAK', 'ic', 3, '6.6 × 6.1', 'Same as TO-252'],
  ['SMA-F', 'diode', 2, '4.3 × 2.6', 'DO-214AC: 1 A rectifier diodes'],
  ['DO-214AC', 'diode', 2, '4.3 × 2.6', 'SMA: 1 A rectifier diodes'],
  ['DO-214AA', 'diode', 2, '5.3 × 3.6', 'SMB: 1.5–2 A diodes'],
  ['DO-214AB', 'diode', 2, '7.1 × 6.2', 'SMC: 3 A diodes'],
  ['SMB', 'diode', 2, '5.3 × 3.6', 'DO-214AA'],
  ['SMC', 'diode', 2, '7.1 × 6.2', 'DO-214AB'],
  ['DO-215AA', 'diode', 2, '5.3 × 3.6', 'SMB with flat leads'],
  ['DO-215AB', 'diode', 2, '7.1 × 6.2', 'SMC with flat leads'],
  ['SOD-123', 'diode', 2, '2.7 × 1.6', 'The common package for signal diodes'],
  ['SOD-123F', 'diode', 2, '2.7 × 1.6', 'SOD-123 with a flat lead'],
  ['SOD-123FL', 'diode', 2, '2.9 × 1.9', 'SOD-123FL: higher current, better heatsinking'],
  ['SOD-323', 'diode', 2, '1.7 × 1.25', 'SC-76: a tiny diode, easy to flick away when soldering'],
  ['SOD-323F', 'diode', 2, '1.7 × 1.25', 'SOD-323 with a flat lead'],
  ['SOD-323FL', 'diode', 2, '1.7 × 1.25', 'SOD-323FL'],
  ['SOD-523', 'diode', 2, '1.2 × 0.8', 'Very small: needs an accurate reflow profile'],
  ['SOD-80', 'diode', 2, '3.5 × 1.5', 'MiniMELF: cylindrical, marked with colour rings'],
  ['SC-59', 'transistor', 3, '2.9 × 1.6', 'Japanese name for SOT-23 (Toshiba/Rohm)'],
  ['SC-70', 'transistor', 3, '2.0 × 1.25', 'SOT-323'],
  ['SC-70-5', 'ic', 5, '2.0 × 1.25', 'SOT-353'],
  ['SC-70-6', 'ic', 6, '2.0 × 1.25', 'SOT-363'],
  ['SC-82', 'transistor', 4, '2.0 × 1.25', 'SOT-343'],
  ['SC-82-4L', 'transistor', 4, '2.0 × 1.25', 'SOT-343 (name spells out the pin count)'],
  ['SC-82A', 'transistor', 4, '2.0 × 1.25', 'SC-82 variant'],
  ['SC-82AB', 'transistor', 4, '2.0 × 1.25', 'SC-82 variant with a different pinout'],
  ['SC-82S', 'transistor', 4, '2.0 × 1.25', 'SC-82 variant'],
  ['SC-82SA', 'transistor', 4, '2.0 × 1.25', 'SC-82 variant'],
  ['SC-88A', 'transistor', 5, '2.0 × 1.25', 'SC-70-5 / SOT-353'],
  ['SOT-416', 'transistor', 3, '1.6 × 0.8', 'SC-75 / SOT-416: ultra small'],
  ['SOT-523', 'transistor', 3, '1.6 × 0.8', 'SC-75 / SOT-523'],
  ['SOT-553', 'transistor', 5, '1.6 × 1.2', '5-pin ultra small'],
  ['SOT-346', 'transistor', 3, '2.9 × 1.6', 'SC-59 / SOT-23'],
  ['SOT-23-3L', 'transistor', 3, '2.9 × 1.6', 'SOT-23 (pin count spelled out)'],
  ['SOT-23-5L', 'ic', 5, '2.9 × 1.6', 'SOT-23-5 (pin count spelled out)'],
  ['SOT-23-6L', 'ic', 6, '2.9 × 1.6', 'SOT-23-6 (pin count spelled out)'],
  ['SOT-26W', 'ic', 6, '2.9 × 1.6', 'SOT-26 with a wider body'],
  ['SSOT-24', 'ic', 6, '2.9 × 1.6', 'Shrink SOT: small 6-pin'],
  ['TSOT-23', 'transistor', 5, '2.9 × 1.6', 'Thin SOT-23'],
  ['TSOT-23-5', 'ic', 5, '2.9 × 1.6', 'Thin SOT-23-5'],
  ['TSOT-23-6', 'ic', 6, '2.9 × 1.6', 'Thin SOT-23-6'],
  ['MSOP-8', 'ic', 8, '3.0 × 3.0', 'MicroSOP: 0.65 mm pitch, needs paste control'],
  ['MSOP-8A', 'ic', 8, '3.0 × 3.0', 'MSOP-8 variant A'],
  ['SOIC-8', 'ic', 8, '4.9 × 3.9', 'SOP-8, 1.27 mm pitch: the easiest to rework'],
  ['SOP-8', 'ic', 8, '4.9 × 3.9', 'SOIC-8'],
  ['SOP-8FD', 'ic', 8, '4.9 × 3.9', 'SOP-8 with a heatsink tab'],
  ['SOP-6', 'ic', 6, '4.4 × 2.6', '6-pin SOP'],
  ['SSOP-3', 'transistor', 3, '2.9 × 1.6', '3-pin SSOP'],
  ['SSOP-5', 'ic', 5, '3.0 × 1.7', '5-pin SSOP (optocouplers, drivers)'],
  ['TSSOP-8', 'ic', 8, '3.0 × 3.0', '0.65 mm pitch'],
  ['HSOP-6J', 'ic', 6, '4.9 × 3.9', 'SOP-6 with a heatsink pad'],
  ['DSP-8', 'ic', 8, '5.0 × 4.0', 'SOIC-8 with an exposed pad'],
  ['DFN', 'ic', null, null, 'Leadless package with bottom pads'],
  ['DFN-6', 'ic', 6, '2.0 × 2.0', 'DFN-6'],
  ['DFN-6L', 'ic', 6, '2.0 × 2.0', 'DFN-6 (size spelled out)'],
  ['DFN-8', 'ic', 8, '3.0 × 3.0', 'DFN-8'],
  ['DFN1010-4', 'ic', 4, '1.0 × 1.0', 'Ultra small: risk of voids under the body'],
  ['DFN1212-6', 'ic', 6, '1.2 × 1.2', 'DFN 1.2 × 1.2 mm'],
  ['DFN1216-8', 'ic', 8, '1.2 × 1.6', 'DFN 1.2 × 1.6 mm'],
  ['DFN1616-6', 'ic', 6, '1.6 × 1.6', 'DFN 1.6 × 1.6 mm'],
  ['DFN1820-6', 'ic', 6, '1.8 × 2.0', 'DFN 1.8 × 2.0 mm'],
  ['DFN2020-6', 'ic', 6, '2.0 × 2.0', 'DFN 2.0 × 2.0 mm'],
  ['DFN-8L-2x2', 'ic', 8, '2.0 × 2.0', 'DFN-8 2 × 2 mm'],
  ['WDFN-8L-2x2', 'ic', 8, '2.0 × 2.0', 'DFN-8 2 × 2 mm with wider pads'],
  ['WDFN-8L-3x3', 'ic', 8, '3.0 × 3.0', 'DFN-8 3 × 3 mm'],
  ['WDFN-6L-1.6x1.6', 'ic', 6, '1.6 × 1.6', 'DFN-6 1.6 × 1.6 mm'],
  ['QFN', 'ic', null, null, 'Leadless square: solder paste under the body needs inspection'],
  ['MLP33-10', 'ic', 10, '3.0 × 3.0', 'QFN-10 3 × 3 mm'],
  ['LLP-6', 'ic', 6, '2.9 × 2.9', 'QFN-6 (TI LLP)'],
  ['SON-4', 'ic', 4, '1.4 × 1.0', 'Small leadless'],
  ['SON-6', 'ic', 6, '2.0 × 2.0', 'SON-6'],
  ['SON-8', 'ic', 8, '3.0 × 3.0', 'SON-8'],
  ['SON1612-6', 'ic', 6, '1.6 × 1.2', 'SON 1.6 × 1.2 mm'],
  ['HSON-6', 'ic', 6, '3.3 × 3.3', 'HSON-6 with a heatsink pad'],
  ['USPN-4', 'ic', 4, '1.2 × 1.2', 'Ultra small leadless'],
  ['USPN-6', 'ic', 6, '1.6 × 1.6', 'Ultra small leadless'],
  ['USP-3', 'ic', 3, '1.2 × 1.2', 'Ultra small'],
  ['USP-4', 'ic', 4, '1.4 × 1.4', 'Ultra small'],
  ['USP-6B', 'ic', 6, '1.6 × 1.6', 'Ultra small 6-pin'],
  ['USP-6C', 'ic', 6, '2.0 × 1.8', 'Ultra small 6-pin'],
  ['USP-6EL', 'ic', 6, '1.6 × 1.6', 'Ultra small'],
  ['USP-10B', 'ic', 10, '2.5 × 2.5', 'Ultra small 10-pin'],
  ['USP-10B03', 'ic', 10, '2.5 × 2.5', 'Ultra small'],
  ['USPQ-4B03', 'ic', 4, '0.9 × 0.9', 'Ultra small'],
  ['USPQ-4B05', 'ic', 4, '1.0 × 1.0', 'Ultra small'],
  ['UFN-6', 'ic', 6, '1.6 × 1.6', 'Leadless'],
  ['SNT-4A', 'ic', 4, '1.2 × 1.2', 'Ultra small'],
  ['SNT-6A', 'ic', 6, '1.6 × 1.6', 'Ultra small'],
  ['HSNT-4', 'ic', 4, '1.0 × 1.0', 'Ultra small with a heatsink pad'],
  ['WLP-5-03', 'ic', 5, null, 'Wafer-level package: X-ray or replacement only'],
  ['WLP-6-01', 'ic', 6, null, 'Wafer-level package'],
  ['LGA-8B01', 'ic', 8, '1.6 × 1.6', 'LGA: needs X-ray inspection'],
  ['TSM', 'transistor', 3, '2.9 × 1.6', 'SOT-23 variant'],
  ['CL-2025', 'other', null, null, 'Large package (crystal / filter)']
].map(function (p) {
  return { name: p[0], family: p[1], pins: p[2], body: p[3], note: p[4] || '' };
});

/* ---------------------------------------------------------------------------
 * COMPONENTS
 * ------------------------------------------------------------------------ */
window.SMD_DB = {
  meta: {
    version: '0.1.0-seed',
    updated: '2026-08-31',
    count: 0,
    disclaimer: 'Demo dataset. SMD marking is vendor specific — confirm against the datasheet before replacing a part.'
  },
  parts: [

    /* -------------------------------------------------------- BIPOLAR TRANSISTORS */
    {
      part: 'MMBT3904', mfr: 'onsemi / Diodes Inc.', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'General-purpose small-signal NPN, replacement for 2N3904',
      v: '40 V', i: '200 mA', pins: '1=B, 2=E, 3=C', markings: ['1A', '3904'], conf: 'med',
      note: 'Code 1A is used by ON Semiconductor and Fairchild; other vendors may use 1A for a different part.'
    },
    {
      part: 'MMBT3906', mfr: 'onsemi / Diodes Inc.', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'General-purpose small-signal PNP, replacement for 2N3906',
      v: '40 V', i: '200 mA', pins: '1=B, 2=E, 3=C', markings: ['2A', '3906'], conf: 'med',
      note: 'Complement to MMBT3904.'
    },
    {
      part: 'MMBT2222A', mfr: 'onsemi', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN, higher current, replacement for 2N2222A',
      v: '40 V', i: '600 mA', pins: '1=B, 2=E, 3=C', markings: ['1P', '2222A'], conf: 'med',
      note: 'Commonly used in discrete switches.'
    },
    {
      part: 'MMBT2907A', mfr: 'onsemi', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP, higher current, replacement for 2N2907A',
      v: '60 V', i: '600 mA', pins: '1=B, 2=E, 3=C', markings: ['2F', '2907A'], conf: 'med',
      note: 'Complement to MMBT2222A.'
    },
    {
      part: 'BC847B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN general purpose, gain group B (hFE 200–450)',
      v: '45 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['1F'], conf: 'med',
      note: 'NXP series: BC847A = 1E, BC847B = 1F, BC847C = 1G. Infineon marks the same dies differently.'
    },
    {
      part: 'BC847C', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN general purpose, gain group C (hFE 420–800)',
      v: '45 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['1G'], conf: 'med',
      note: 'Analogue of BC547C.'
    },
    {
      part: 'BC847A', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN general purpose, gain group A (hFE 110–220)',
      v: '45 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['1E'], conf: 'med',
      note: 'Analogue of BC547A/B.'
    },
    {
      part: 'BC857B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP general purpose, gain group B',
      v: '45 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['3F'], conf: 'med',
      note: 'PNP version of BC847B. Series: BC857A = 3E, BC857B = 3F, BC857C = 3G.'
    },
    {
      part: 'BC857C', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP general purpose, gain group C',
      v: '45 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['3G'], conf: 'med', note: ''
    },
    {
      part: 'BC846B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN, 65 V, gain group B',
      v: '65 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['1B'], conf: 'low',
      note: 'Code 1B conflicts with other parts — check the package and the circuit.'
    },
    {
      part: 'BC856B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP, 65 V, gain group B',
      v: '65 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['3B'], conf: 'low', note: ''
    },
    {
      part: 'BC848B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN, 30 V, gain group B',
      v: '30 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['1K'], conf: 'low', note: ''
    },
    {
      part: 'BC858B', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP, 30 V, gain group B',
      v: '30 V', i: '100 mA', pins: '1=B, 2=E, 3=C', markings: ['3K'], conf: 'low', note: ''
    },
    {
      part: 'BC817-16', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN 45 V / 500 mA, hFE 100–250',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['6A'], conf: 'med',
      note: 'Series: -16 = 6A, -25 = 6B, -40 = 6C. BC817 analogue in SOT-23.'
    },
    {
      part: 'BC817-25', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN 45 V / 500 mA, hFE 160–400',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['6B'], conf: 'med', note: ''
    },
    {
      part: 'BC817-40', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN 45 V / 500 mA, hFE 250–600',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['6C'], conf: 'med', note: ''
    },
    {
      part: 'BC807-16', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP 45 V / 500 mA, hFE 100–250',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['5A'], conf: 'med',
      note: 'Complementary series to BC817: -16 = 5A, -25 = 5B, -40 = 5C.'
    },
    {
      part: 'BC807-25', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP 45 V / 500 mA, hFE 160–400',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['5B'], conf: 'med', note: ''
    },
    {
      part: 'BC807-40', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP 45 V / 500 mA, hFE 250–600',
      v: '45 V', i: '500 mA', pins: '1=B, 2=E, 3=C', markings: ['5C'], conf: 'med', note: ''
    },
    {
      part: 'S8050', mfr: 'Various (CN)', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'Commodity NPN 25 V / 700 mA from Chinese vendors',
      v: '25 V', i: '700 mA', pins: '1=B, 2=E, 3=C', markings: ['J3Y', 'Y1', '8050'], conf: 'med',
      note: 'Code J3Y is the most common marking for S8050. Other marking variants exist.'
    },
    {
      part: 'S8550', mfr: 'Various (CN)', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'Commodity PNP 25 V / 700 mA, complement to S8050',
      v: '25 V', i: '700 mA', pins: '1=B, 2=E, 3=C', markings: ['2TY', 'Y2', '8550'], conf: 'med',
      note: 'Code 2TY is the most common marking for S8550.'
    },
    {
      part: 'MMBTA42', mfr: 'onsemi', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'High-voltage NPN (300 V) for driver stages',
      v: '300 V', i: '200 mA', pins: '1=B, 2=E, 3=C', markings: ['1D'], conf: 'low',
      note: 'Short high-voltage transistor codes often overlap — always verify.'
    },
    {
      part: 'MMBTA92', mfr: 'onsemi', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'High-voltage PNP (300 V)',
      v: '300 V', i: '200 mA', pins: '1=B, 2=E, 3=C', markings: ['2D'], conf: 'low', note: ''
    },
    {
      part: 'MMBT4401', mfr: 'onsemi', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN switching transistor, 40 V / 600 mA',
      v: '40 V', i: '600 mA', pins: '1=B, 2=E, 3=C', markings: ['2W'], conf: 'low', note: ''
    },
    {
      part: 'MMBT4403', mfr: 'onsemi', pkg: 'SOT-23', type: 'PNP BJT',
      desc: 'PNP switching transistor, 40 V / 600 mA',
      v: '40 V', i: '600 mA', pins: '1=B, 2=E, 3=C', markings: ['2T'], conf: 'low', note: ''
    },
    {
      part: 'FMMT617', mfr: 'Diodes Inc.', pkg: 'SOT-23', type: 'NPN BJT',
      desc: 'NPN with low saturation, 15 V / 3 A',
      v: '15 V', i: '3 A', pins: '1=B, 2=E, 3=C', markings: ['617'], conf: 'med', note: ''
    },
    {
      part: 'BCX56', mfr: 'Nexperia', pkg: 'SOT-89', type: 'NPN BJT',
      desc: 'NPN medium power, 80 V / 1 A',
      v: '80 V', i: '1 A', pins: '1=B, 2=C, 3=E', markings: ['BCX56'], conf: 'med',
      note: 'In SOT-89 the middle lead is tied to the heatsink — usually the collector.'
    },
    {
      part: 'BCX53', mfr: 'Nexperia', pkg: 'SOT-89', type: 'PNP BJT',
      desc: 'PNP medium power, 80 V / 1 A',
      v: '80 V', i: '1 A', pins: '1=B, 2=C, 3=E', markings: ['BCX53'], conf: 'med', note: ''
    },

    /* ---------------------------------------------------------------- MOSFET */
    {
      part: '2N7002', mfr: 'onsemi / Diodes Inc.', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'Small-signal N-channel MOSFET, logic-level gate',
      v: '60 V', i: '115–300 mA', pins: '1=G, 2=S, 3=D', markings: ['702', '7002'], conf: 'med',
      note: 'One of the most frequently faked parts: check Rds(on) and the gate threshold.'
    },
    {
      part: '2N7002K', mfr: 'Diodes Inc.', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'N-channel MOSFET 60 V, gate ESD protected',
      v: '60 V', i: '300 mA', pins: '1=G, 2=S, 3=D', markings: ['72K'], conf: 'med', note: ''
    },
    {
      part: 'BSS138', mfr: 'onsemi / Diodes Inc.', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'N-channel MOSFET 50 V, classic I2C level shifter',
      v: '50 V', i: '200 mA', pins: '1=G, 2=S, 3=D', markings: ['138', 'S138'], conf: 'med',
      note: 'Both code variants are seen from different fabs.'
    },
    {
      part: 'BSS84', mfr: 'onsemi / Diodes Inc.', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 50 V, complement to BSS138',
      v: '50 V', i: '130 mA', pins: '1=G, 2=S, 3=D', markings: ['S84'], conf: 'low', note: ''
    },
    {
      part: 'SI2301', mfr: 'Vishay', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 20 V, typical load switch',
      v: '20 V', i: '2.3 A', pins: '1=G, 2=S, 3=D', markings: ['A1', '2301'], conf: 'med',
      note: 'The short A1/A2 codes at Vishay are the common ones; clones usually carry the full number.'
    },
    {
      part: 'SI2302', mfr: 'Vishay', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'N-channel MOSFET 20 V, typical load switch',
      v: '20 V', i: '2.1 A', pins: '1=G, 2=S, 3=D', markings: ['A2', '2302'], conf: 'med', note: ''
    },
    {
      part: 'AO3400', mfr: 'Alpha & Omega', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'N-channel MOSFET 30 V / 5.7 A, low Rds(on)',
      v: '30 V', i: '5.7 A', pins: '1=G, 2=S, 3=D', markings: ['A09T', '3400'], conf: 'med',
      note: 'A19T is the complementary P-channel AO3401. Both are cloned very often.'
    },
    {
      part: 'AO3401', mfr: 'Alpha & Omega', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 30 V / 4 A',
      v: '30 V', i: '4 A', pins: '1=G, 2=S, 3=D', markings: ['A19T', '3401'], conf: 'med', note: ''
    },
    {
      part: 'AO3407', mfr: 'Alpha & Omega', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 30 V / 4 A, low gate threshold',
      v: '30 V', i: '4 A', pins: '1=G, 2=S, 3=D', markings: ['A29T'], conf: 'low', note: ''
    },
    {
      part: 'SI2305', mfr: 'Vishay', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 8 V / 3 A for logic rails',
      v: '8 V', i: '3 A', pins: '1=G, 2=S, 3=D', markings: ['A5'], conf: 'low', note: ''
    },
    {
      part: 'IRLML6401', mfr: 'Infineon / IR', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 12 V, logic level',
      v: '12 V', i: '4.3 A', pins: '1=G, 2=S, 3=D', markings: ['I01'], conf: 'low',
      note: 'Infineon Micro3 codes are short and overlap — check the datasheet.'
    },
    {
      part: 'IRLML2502', mfr: 'Infineon / IR', pkg: 'SOT-23', type: 'N-MOSFET',
      desc: 'N-channel MOSFET 20 V / 3.4 A, logic level',
      v: '20 V', i: '3.4 A', pins: '1=G, 2=S, 3=D', markings: ['I02'], conf: 'low', note: ''
    },
    {
      part: 'DMG2305UX', mfr: 'Diodes Inc.', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 20 V / 4.2 A',
      v: '20 V', i: '4.2 A', pins: '1=G, 2=S, 3=D', markings: ['2305'], conf: 'low', note: ''
    },
    {
      part: 'NTR4101PT1G', mfr: 'onsemi', pkg: 'SOT-23', type: 'P-MOSFET',
      desc: 'P-channel MOSFET 20 V / 3.1 A',
      v: '20 V', i: '3.1 A', pins: '1=G, 2=S, 3=D', markings: ['T1'], conf: 'low', note: ''
    },
    {
      part: '8205A', mfr: 'Various (CN)', pkg: 'SOT-23-6', type: 'Dual N-MOSFET',
      desc: 'Dual N-channel MOSFET, Li-Ion battery protection switch',
      v: '20 V', i: '6 A', pins: '1=S1, 2=G1, 3=S2, 4=G2, 5=D2, 6=D1', markings: ['8205A', '8205'], conf: 'med',
      note: 'Almost always paired with the DW01 protection controller.'
    },
    {
      part: 'FS8205A', mfr: 'Fortune', pkg: 'SOT-23-6', type: 'Dual N-MOSFET',
      desc: 'Dual N-channel MOSFET for Li-Ion protection',
      v: '20 V', i: '6 A', pins: '1=S1, 2=G1, 3=S2, 4=G2, 5=D2, 6=D1', markings: ['8205A', 'FS8205'], conf: 'med', note: ''
    },

    /* --------------------------------------------------------------------- DIODES */
    {
      part: '1N4148W', mfr: 'Diodes Inc. / onsemi', pkg: 'SOD-123', type: 'Signal diode',
      desc: 'Fast switching diode 100 V / 150 mA',
      v: '100 V', i: '150 mA', pins: '1=K, 2=A',
      markings: [{ c: 'T4', conf: 'med' }, { c: 'A7', conf: 'low' }], conf: 'med',
      note: 'Code T4 is Diodes Inc./Fairchild. A7 means BAV99 at some fabs, so A7 is flagged unreliable here. Cathode = stripe.'
    },
    {
      part: 'LL4148', mfr: 'Vishay / Diodes Inc.', pkg: 'SOD-80', type: 'Signal diode',
      desc: 'Switching diode in a glass MiniMELF, 100 V',
      v: '100 V', i: '200 mA', pins: 'cathode — stripe', markings: [], conf: 'med',
      note: 'MiniMELF is marked with COLOUR RINGS, not a code: black ring = cathode. OCR cannot help here.'
    },
    {
      part: 'MMBD4148', mfr: 'onsemi', pkg: 'SOT-23', type: 'Signal diode',
      desc: 'Switching diode 100 V in SOT-23 (usually only 2 pins used)',
      v: '100 V', i: '200 mA', pins: '1=K, 2=A (depends on variant)', markings: ['5H', 'T4'], conf: 'low',
      note: 'In SOT-23 a diode may be single or dual — always check with a multimeter.'
    },
    {
      part: 'BAV99', mfr: 'Nexperia / Diodes Inc.', pkg: 'SOT-23', type: 'Dual diode',
      desc: 'Dual diode with common cathode, 70 V / 215 mA',
      v: '70 V', i: '215 mA', pins: '1=A1, 2=K(common), 3=A2', markings: ['A7', 'KJD'], conf: 'med',
      note: 'Code A7 is NXP/Nexperia. IMPORTANT: at some Chinese fabs A7 means 1N4148W — check the package.'
    },
    {
      part: 'BAV70', mfr: 'Nexperia', pkg: 'SOT-23', type: 'Dual diode',
      desc: 'Dual diode with common cathode, 70 V (fast)',
      v: '70 V', i: '215 mA', pins: '1=A1, 2=K(common), 3=A2', markings: ['A4'], conf: 'low', note: ''
    },
    {
      part: 'BAW56', mfr: 'Nexperia', pkg: 'SOT-23', type: 'Dual diode',
      desc: 'Dual diode with common cathode, 70 V (ultra-fast)',
      v: '70 V', i: '215 mA', pins: '1=A1, 2=K(common), 3=A2', markings: ['A1'], conf: 'low',
      note: 'Caution: A1 at Vishay is the SI2301 MOSFET. Same package, different part.'
    },
    {
      part: 'BAT54', mfr: 'Diodes Inc. / Nexperia', pkg: 'SOT-23', type: 'Schottky diode',
      desc: 'Single Schottky diode 30 V / 200 mA',
      v: '30 V', i: '200 mA', pins: '1=A, 2=n.c., 3=K', markings: ['KL3'], conf: 'low',
      note: 'Nexperia family: BAT54 = KL3, BAT54A = KL1, BAT54C = KL2, BAT54S = KL4. Diodes Inc. uses different codes.'
    },
    {
      part: 'BAT54C', mfr: 'Nexperia', pkg: 'SOT-23', type: 'Dual Schottky diode',
      desc: 'Schottky diode with common cathode, 30 V / 200 mA',
      v: '30 V', i: '200 mA', pins: '1=A1, 2=K(common), 3=A2', markings: ['KL2'], conf: 'low', note: ''
    },
    {
      part: 'BAT54S', mfr: 'Nexperia', pkg: 'SOT-23', type: 'Dual Schottky diode',
      desc: 'Two Schottky diodes in series, 30 V / 200 mA',
      v: '30 V', i: '200 mA', pins: '1=A1, 2=K1/A2, 3=K2', markings: ['KL4'], conf: 'low', note: ''
    },
    {
      part: 'BAT54A', mfr: 'Nexperia', pkg: 'SOT-23', type: 'Dual Schottky diode',
      desc: 'Schottky diode with common anode, 30 V / 200 mA',
      v: '30 V', i: '200 mA', pins: '1=K1, 2=A(common), 3=K2', markings: ['KL1'], conf: 'low', note: ''
    },
    {
      part: 'B5819W', mfr: 'Diodes Inc.', pkg: 'SOD-123', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 1 A',
      v: '40 V', i: '1 A', pins: '1=K, 2=A', markings: ['SL'], conf: 'med', note: ''
    },
    {
      part: 'SS14', mfr: 'Diodes Inc. / Vishay', pkg: 'DO-214AC', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 1 A (SMA)',
      v: '40 V', i: '1 A', pins: '1=K, 2=A', markings: ['SS14'], conf: 'high', note: ''
    },
    {
      part: 'SS24', mfr: 'Diodes Inc.', pkg: 'DO-214AA', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 2 A (SMB)',
      v: '40 V', i: '2 A', pins: '1=K, 2=A', markings: ['SS24'], conf: 'high', note: ''
    },
    {
      part: 'SS34', mfr: 'Diodes Inc. / Vishay', pkg: 'DO-214AB', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 3 A (SMC)',
      v: '40 V', i: '3 A', pins: '1=K, 2=A', markings: ['SS34'], conf: 'high', note: ''
    },
    {
      part: 'SS54', mfr: 'Diodes Inc.', pkg: 'DO-214AB', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 5 A (SMC)',
      v: '40 V', i: '5 A', pins: '1=K, 2=A', markings: ['SS54'], conf: 'high', note: ''
    },
    {
      part: 'M1', mfr: 'Various', pkg: 'DO-214AC', type: 'Rectifier diode',
      desc: 'SMD version of 1N4001, 50 V / 1 A',
      v: '50 V', i: '1 A', pins: '1=K, 2=A', markings: ['M1'], conf: 'high',
      note: 'Family M1..M7 = 1N4001..1N4007: M1=50 V, M2=100 V, M3=200 V, M4=400 V, M5=600 V, M6=800 V, M7=1000 V.'
    },
    {
      part: 'M4', mfr: 'Various', pkg: 'DO-214AC', type: 'Rectifier diode',
      desc: 'SMD version of 1N4004, 400 V / 1 A',
      v: '400 V', i: '1 A', pins: '1=K, 2=A', markings: ['M4'], conf: 'high', note: ''
    },
    {
      part: 'M7', mfr: 'Various', pkg: 'DO-214AC', type: 'Rectifier diode',
      desc: 'SMD version of 1N4007, 1000 V / 1 A',
      v: '1000 V', i: '1 A', pins: '1=K, 2=A', markings: ['M7'], conf: 'high',
      note: 'One of the most common SMD diodes. Cathode = white stripe.'
    },
    {
      part: 'SD103AW', mfr: 'Diodes Inc.', pkg: 'SOD-123', type: 'Schottky diode',
      desc: 'Low-capacitance Schottky diode 40 V / 350 mA',
      v: '40 V', i: '350 mA', pins: '1=K, 2=A', markings: ['S4'], conf: 'low', note: ''
    },
    {
      part: 'RB751S-40', mfr: 'Diodes Inc.', pkg: 'SOD-523', type: 'Schottky diode',
      desc: 'Schottky diode 40 V / 30 mA in an ultra-small package',
      v: '40 V', i: '30 mA', pins: '1=K, 2=A', markings: ['B'], conf: 'low',
      note: 'Single-letter codes are practically unidentifiable from the marking alone — only from the circuit.'
    },

    /* --------------------------------------------------------- BZX84 ZENER DIODES */
    {
      part: 'BZX84C3V3', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 3.3 V, 250 mW',
      v: '3.3 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z14'], conf: 'low',
      note: 'NXP table: Z11=2V4, Z13=3V0, Z14=3V3, Z15=3V6, Z16=3V9. Other vendors use different codes (W4, Y..).'
    },
    {
      part: 'BZX84C5V1', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 5.1 V, 250 mW',
      v: '5.1 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z19'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C5V6', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 5.6 V, 250 mW',
      v: '5.6 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z20'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C6V2', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 6.2 V, 250 mW',
      v: '6.2 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z21'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C8V2', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 8.2 V, 250 mW',
      v: '8.2 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z24'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C10', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 10 V, 250 mW',
      v: '10 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z26'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C12', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 12 V, 250 mW',
      v: '12 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z28'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C15', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 15 V, 250 mW',
      v: '15 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z30'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C18', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 18 V, 250 mW',
      v: '18 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z32'], conf: 'low', note: ''
    },
    {
      part: 'BZX84C24', mfr: 'Nexperia / NXP', pkg: 'SOT-23', type: 'Zener diode',
      desc: 'Zener diode 24 V, 250 mW',
      v: '24 V', i: '250 mW', pins: '1=A, 2=n.c., 3=K', markings: ['Z35'], conf: 'low', note: ''
    },

    /* ----------------------------------------------------------- REGULATORS / LDO */
    {
      part: 'AMS1117-3.3', mfr: 'Advanced Monolithic', pkg: 'SOT-223', type: 'LDO',
      desc: 'Linear regulator 3.3 V / 1 A, fixed output',
      v: 'input up to 15 V, output 3.3 V', i: '1 A', pins: '1=GND, 2=VOUT, 3=VIN (tab = VOUT)',
      markings: ['AMS1117 3.3', '1117-3.3', 'AMS1117-3.3'], conf: 'high',
      note: 'Enormous number of clones. IMPORTANT: the AMS1117 pinout differs from LM1117 — check it.'
    },
    {
      part: 'AMS1117-5.0', mfr: 'Advanced Monolithic', pkg: 'SOT-223', type: 'LDO',
      desc: 'Linear regulator 5.0 V / 1 A',
      v: 'input up to 15 V, output 5.0 V', i: '1 A', pins: '1=GND, 2=VOUT, 3=VIN (tab = VOUT)',
      markings: ['AMS1117 5.0', '1117-5.0', 'AMS1117-5.0'], conf: 'high', note: ''
    },
    {
      part: 'AMS1117-ADJ', mfr: 'Advanced Monolithic', pkg: 'SOT-223', type: 'LDO',
      desc: 'Adjustable regulator, Vref 1.25 V, 1 A',
      v: 'input up to 15 V', i: '1 A', pins: '1=ADJ, 2=VOUT, 3=VIN',
      markings: ['AMS1117 ADJ', '1117-ADJ'], conf: 'high', note: ''
    },
    {
      part: '78M05', mfr: 'Various', pkg: 'TO-252', type: 'Linear regulator',
      desc: '+5 V regulator, 500 mA',
      v: 'input up to 35 V, output 5 V', i: '500 mA', pins: '1=VIN, 2=GND, 3=VOUT',
      markings: ['78M05'], conf: 'high',
      note: 'Also found in TO-252 (DPAK), SOT-223 and D2PAK. The pinout depends on the package.'
    },
    {
      part: '78L05', mfr: 'Various', pkg: 'SOT-89', type: 'Linear regulator',
      desc: '+5 V regulator, 100 mA',
      v: 'input up to 30 V, output 5 V', i: '100 mA', pins: '1=VOUT, 2=GND, 3=VIN',
      markings: ['78L05'], conf: 'high', note: ''
    },
    {
      part: 'LD1117S33TR', mfr: 'STMicroelectronics', pkg: 'SOT-223', type: 'LDO',
      desc: 'LDO 3.3 V / 800 mA',
      v: 'input up to 15 V, output 3.3 V', i: '800 mA', pins: '1=GND, 2=VOUT, 3=VIN',
      markings: ['LD1117S33', '1117S33'], conf: 'high',
      note: 'The ST pinout differs from AMS1117 — always check the datasheet.'
    },
    {
      part: 'XC6206P332MR', mfr: 'Torex', pkg: 'SOT-23-3', type: 'LDO',
      desc: 'Micropower LDO 3.3 V / 200 mA (low dropout)',
      v: 'input up to 6 V, output 3.3 V', i: '200 mA', pins: '1=VSS, 2=VOUT, 3=VIN',
      markings: ['662K', '662'], conf: 'med',
      note: 'Code 662K is cloned very often; the clones have noticeably worse parameters.'
    },
    {
      part: 'TL431', mfr: 'Various', pkg: 'SOT-23-3', type: 'Voltage reference',
      desc: 'Adjustable precision 2.5 V voltage reference',
      v: 'Vref 2.5 V, cathode up to 36 V', i: '100 mA', pins: '1=REF, 2=A, 3=K',
      markings: ['431', 'TL431', 'A431'], conf: 'med',
      note: 'In SOT-23 the pinout varies (431, A431, N431, TL431) — verify the pinout.'
    },
    {
      part: 'HT7333-1', mfr: 'Holtek', pkg: 'SOT-89', type: 'LDO',
      desc: 'Micropower LDO 3.3 V / 250 mA',
      v: 'input up to 12 V, output 3.3 V', i: '250 mA', pins: '1=VSS, 2=VIN, 3=VOUT',
      markings: ['7333', 'HT7333'], conf: 'med', note: ''
    },
    {
      part: 'LM2596S-5.0', mfr: 'Texas Instruments', pkg: 'TO-263', type: 'DC-DC (buck)',
      desc: 'Step-down converter 5 V / 3 A, 150 kHz',
      v: 'input up to 40 V, output 5 V', i: '3 A', pins: '1=VIN, 2=VOUT, 3=GND, tab=GND',
      markings: ['LM2596S-5.0', 'LM2596S'], conf: 'high',
      note: 'One of the most counterfeited DC-DC parts: verify switching frequency and efficiency.'
    },
    {
      part: 'TP4056', mfr: 'Top Power', pkg: 'SOP-8', type: 'Li-Ion charger',
      desc: '1 A linear Li-Ion charge controller with thermal regulation',
      v: 'input 4.5–8 V', i: '1 A', pins: '1=TEMP, 2=PROG, 3=GND, 4=VCC, 5=BAT, 6=STDBY, 7=CHRG, 8=CE',
      markings: ['TP4056'], conf: 'high', note: 'The marking is the full part number — the easiest case to identify.'
    },
    {
      part: 'DW01', mfr: 'Various (CN)', pkg: 'SOT-23-6', type: 'Li-Ion protection',
      desc: 'Battery protection controller: over-charge / over-discharge / short',
      v: 'up to 10 V', i: '—', pins: '1=OD, 2=CS, 3=OC, 4=TD, 5=VCC, 6=GND',
      markings: ['DW01', 'DW01A'], conf: 'high', note: 'Almost always paired with 8205A.'
    },

    /* --------------------------------------------------------------- OPTOCOUPLERS */
    {
      part: 'PC817', mfr: 'Sharp', pkg: 'SOP-4', type: 'Optocoupler',
      desc: 'Transistor optocoupler, 5 kV isolation',
      v: 'Vceo 35 V, isolation 5000 V', i: '50 mA', pins: '1=A, 2=K, 3=E, 4=C',
      markings: ['817', 'PC817'], conf: 'high',
      note: 'Many clones (EL817, CT817, LTV817) share the same 817 marking.'
    },
    {
      part: 'EL817', mfr: 'Everlight', pkg: 'SOP-4', type: 'Optocoupler',
      desc: 'Transistor optocoupler, analogue of PC817',
      v: 'Vceo 35 V, isolation 5000 V', i: '50 mA', pins: '1=A, 2=K, 3=E, 4=C',
      markings: ['817', 'EL817'], conf: 'high', note: ''
    },
    {
      part: 'TLP281', mfr: 'Toshiba', pkg: 'SOP-4', type: 'Optocoupler',
      desc: 'Transistor optocoupler, 2.5 kV isolation',
      v: 'Vceo 80 V, isolation 2500 V', i: '50 mA', pins: '1=A, 2=K, 3=E, 4=C',
      markings: ['TLP281'], conf: 'high', note: ''
    },
    {
      part: 'MOC3021', mfr: 'Lite-On / onsemi', pkg: 'DIP-6', type: 'Optotriac',
      desc: 'Triac driver with zero-crossing (400 V)',
      v: '400 V', i: '100 mA', pins: '1=A, 2=K, 4=MT1, 6=MT2',
      markings: ['MOC3021'], conf: 'high', note: 'MOC302x series: 3021 has no zero-cross detector, 3041/3063 do.'
    },

    /* --------------------------------------------------- ANALOGUE AND DIGITAL ICs */
    {
      part: 'LM358', mfr: 'Various', pkg: 'SOIC-8', type: 'Operational amplifier',
      desc: 'Dual op amp, single-supply operation',
      v: '3–32 V', i: '20 mA output', pins: '1=OUT1, 2=IN1-, 3=IN1+, 4=GND, 5=IN2+, 6=IN2-, 7=OUT2, 8=VCC',
      markings: ['LM358', '358'], conf: 'high', note: ''
    },
    {
      part: 'LM393', mfr: 'Various', pkg: 'SOIC-8', type: 'Comparator',
      desc: 'Dual comparator with open collector',
      v: '2–36 V', i: '16 mA', pins: '1=OUT1, 2=IN1-, 3=IN1+, 4=GND, 5=IN2+, 6=IN2-, 7=OUT2, 8=VCC',
      markings: ['LM393', '393'], conf: 'high', note: 'Identical to LM358 in outline and pinout — do not mix them up.'
    },
    {
      part: 'NE555', mfr: 'Various', pkg: 'SOIC-8', type: 'Timer',
      desc: '555 timer, up to 100 kHz',
      v: '4.5–16 V', i: '200 mA', pins: '1=GND, 2=TRIG, 3=OUT, 4=RESET, 5=CTRL, 6=THR, 7=DIS, 8=VCC',
      markings: ['NE555', '555'], conf: 'high', note: ''
    },
    {
      part: 'AT24C02', mfr: 'Atmel / Microchip', pkg: 'SOP-8', type: 'EEPROM',
      desc: '2 kbit I2C EEPROM',
      v: '1.8–5.5 V', i: '5 mA', pins: '1=A0, 2=A1, 3=A2, 4=GND, 5=SDA, 6=SCL, 7=WP, 8=VCC',
      markings: ['AT24C02', '24C02'], conf: 'high',
      note: 'Marking is usually the full number with a lot/date code on the line below.'
    },
    {
      part: '74HC04', mfr: 'Various', pkg: 'SOIC-14', type: 'Logic',
      desc: 'Six inverters',
      v: '2–6 V', i: '25 mA/output', pins: 'standard 14-pin logic',
      markings: ['74HC04', 'HC04'], conf: 'high', note: ''
    },
    {
      part: 'MAX3232', mfr: 'Maxim / Analog', pkg: 'SOIC-16', type: 'RS-232',
      desc: 'RS-232 transceiver from 3.3/5 V',
      v: '3–5.5 V', i: '—', pins: 'standard 16-pin',
      markings: ['MAX3232'], conf: 'high', note: 'Widely counterfeited — prefer verified lots.'
    },
    {
      part: 'CH340C', mfr: 'WCH', pkg: 'SOP-16', type: 'USB-UART',
      desc: 'USB-UART bridge with built-in 12 MHz oscillator',
      v: '3.3/5 V', i: '—', pins: 'standard 16-pin',
      markings: ['CH340C'], conf: 'high', note: 'CH340G needs an external 12 MHz crystal; CH340C does not.'
    },
    {
      part: 'FT232RL', mfr: 'FTDI', pkg: 'SSOP-28', type: 'USB-UART',
      desc: 'USB-UART bridge with configuration EEPROM',
      v: '3.3/5 V', i: '—', pins: 'SSOP-28',
      markings: ['FT232RL'], conf: 'high', note: 'Frequently counterfeited: check the driver and the VID/PID.'
    },
    {
      part: 'STM32F103C8T6', mfr: 'STMicroelectronics', pkg: 'LQFP-48', type: 'MCU',
      desc: 'Cortex-M3, 72 MHz, 64 kB Flash, 20 kB RAM',
      v: '2.0–3.6 V', i: '—', pins: 'LQFP-48',
      markings: ['STM32F103C8T6'], conf: 'high',
      note: 'Multi-line marking: part number + lot code + week/year. OCR reads the first line.'
    },
    {
      part: 'ATmega328P-AU', mfr: 'Microchip', pkg: 'TQFP-32', type: 'MCU',
      desc: 'AVR 8-bit, 20 MHz, 32 kB Flash (Arduino Uno)',
      v: '1.8–5.5 V', i: '—', pins: 'TQFP-32',
      markings: ['ATMEGA328P-AU', 'ATMEGA328P'], conf: 'high', note: ''
    },
    {
      part: 'W25Q64', mfr: 'Winbond', pkg: 'SOP-8', type: 'Flash',
      desc: 'SPI Flash 64 Mbit',
      v: '2.7–3.6 V', i: '—', pins: '1=CS, 2=DO, 3=WP, 4=GND, 5=DI, 6=CLK, 7=HOLD, 8=VCC',
      markings: ['25Q64', 'W25Q64'], conf: 'high', note: 'Vendor marking is often abbreviated (25Qxx).'
    },

    /* ----------------------------------------------------------- PROTECTION / TVS */
    {
      part: 'SMAJ5.0A', mfr: 'Various', pkg: 'DO-214AC', type: 'TVS diode',
      desc: 'Overvoltage clamp 5 V, 400 W',
      v: 'Vrwm 5 V, Vbr 6.4 V', i: '43 A (pulse)', pins: '1=K, 2=A',
      markings: ['AE', 'SMAJ5.0A'], conf: 'low',
      note: 'TVS diodes use 2-character codes (e.g. AE) — they cannot be decoded without a vendor table.'
    },
    {
      part: 'PESD5V0S1UL', mfr: 'Nexperia', pkg: 'SOD-323', type: 'ESD protection',
      desc: 'ESD protection for a 5 V line, 15 kV',
      v: '5 V', i: '—', pins: '1,2 = line/ground',
      markings: ['Z1'], conf: 'low', note: ''
    },
    {
      part: 'SRV05-4', mfr: 'Various', pkg: 'SOT-23-6', type: 'ESD array',
      desc: 'ESD protection array for 4 lines (USB)',
      v: '5 V', i: '—', pins: '1..4 = lines, 5=VCC, 6=GND',
      markings: ['SRV05-4', 'R05'], conf: 'low', note: ''
    }
  ]
};

window.SMD_DB.meta.count = window.SMD_DB.parts.length;
