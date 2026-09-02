#!/usr/bin/env node
/* ============================================================================
 * import_dataset.mjs — нормализация внешнего датасета маркировок
 *
 *   node tools/import_dataset.mjs markings.csv
 *   node tools/import_dataset.mjs markings.csv --out data/extra-parts.json
 *   node tools/import_dataset.mjs markings.csv --check         (только проверка)
 *
 * Зачем: встроенная база — это seed на ~100 позиций. Настоящая работа начинается
 * с загрузки полного датасета (SMD Codebook, собственная база ремонтной
 * лаборатории, выгрузка из ERP). Инструмент приводит чужие колонки к нашему
 * формату и ловит типичные ошибки до того, как они попадут в UI.
 *
 * Формат CSV (разделитель определяется автоматически: , или ;):
 *   part,mfr,pkg,type,desc,v,i,pins,markings,conf,note
 * Обязательные: part, pkg. markings — несколько кодов через «|».
 * conf: high | med | low (по умолчанию med).
 * ========================================================================= */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..');

const args = process.argv.slice(2);
if (!args.length || args[0] === '--help' || args[0] === '-h') {
  console.log('Использование: node tools/import_dataset.mjs <file.csv|file.json> [--out FILE] [--check] [--merge]');
  process.exit(0);
}

const input = args[0];
const outIdx = args.indexOf('--out');
const outPath = outIdx > -1 ? args[outIdx + 1] : path.join(root, 'data', 'extra-parts.json');
const checkOnly = args.includes('--check');
const merge = args.includes('--merge');

/* ------------------------------------------------------------------ чтение */

if (!fs.existsSync(input)) {
  console.error('Файл не найден: ' + input);
  process.exit(1);
}

let rows = [];
if (/\.json$/i.test(input)) {
  const parsed = JSON.parse(fs.readFileSync(input, 'utf8'));
  rows = Array.isArray(parsed) ? parsed : (parsed.parts || []);
} else {
  rows = parseCSV(fs.readFileSync(input, 'utf8'));
}

/* --------------------------------------------------------------- нормализация */

const CONF_OK = new Set(['high', 'med', 'low']);
const KNOWN_FIELDS = ['part', 'mfr', 'pkg', 'type', 'desc', 'v', 'i', 'pins', 'markings', 'conf', 'note'];
const ALIASES = {
  manufacturer: 'mfr', vendor: 'mfr', brand: 'mfr',
  package: 'pkg', case: 'pkg', корпус: 'pkg',
  description: 'desc',
  voltage: 'v', current: 'i',
  pinout: 'pins', pin: 'pins',
  code: 'markings', codes: 'markings', marking: 'markings', smd: 'markings',
  comment: 'note', notes: 'note'
};

const warnings = [];
const errors = [];
const seen = new Set();
const normalized = [];

rows.forEach((raw, idx) => {
  const lineNo = idx + 1;
  const row = {};
  for (const [k, v] of Object.entries(raw)) {
    const key = String(k).toLowerCase().trim();
    const mapped = ALIASES[key] || key;
    row[mapped] = v;
  }

  const part = String(row.part || '').trim();
  const pkg = String(row.pkg || '').trim();
  if (!part) { errors.push(`строка ${lineNo}: пустой part — пропущена`); return; }
  if (!pkg) { errors.push(`строка ${lineNo}: пустой pkg — пропущена`); return; }

  const markings = String(row.markings || '')
    .split('|')
    .map(s => s.trim())
    .filter(Boolean);

  let conf = String(row.conf || 'med').toLowerCase().trim();
  if (!CONF_OK.has(conf)) {
    warnings.push(`строка ${lineNo}: conf="${row.conf}" не распознан, поставлен med`);
    conf = 'med';
  }

  const key = part + '|' + pkg;
  if (seen.has(key)) { warnings.push(`строка ${lineNo}: дубль ${key} — пропущен`); return; }
  seen.add(key);

  const entry = {
    part,
    mfr: String(row.mfr || '').trim(),
    pkg,
    type: String(row.type || '').trim(),
    desc: String(row.desc || '').trim(),
    v: String(row.v || '').trim(),
    i: String(row.i || '').trim(),
    pins: String(row.pins || '').trim(),
    markings,
    conf,
    note: String(row.note || '').trim()
  };

  for (const k of Object.keys(row)) {
    if (!KNOWN_FIELDS.includes(k)) warnings.push(`строка ${lineNo}: неизвестное поле "${k}" проигнорировано`);
  }
  normalized.push(entry);
});

/* Проверка корпусов против списка в data.js */
const dataSrc = fs.readFileSync(path.join(root, 'assets', 'js', 'data.js'), 'utf8');
const knownPkgs = new Set(
  [...dataSrc.matchAll(/^\s*\['([^']+)', '(?:transistor|ic|diode|other)'/gm)].map(m => m[1])
);
const unknownPkgs = new Set(normalized.filter(p => !knownPkgs.has(p.pkg)).map(p => p.pkg));
if (unknownPkgs.size) {
  warnings.push('корпуса вне списка SMD_PACKAGES (добавьте их в data.js, иначе фильтр будет пустым): ' +
    [...unknownPkgs].join(', '));
}

/* ------------------------------------------------------------------ вывод */

console.log('Прочитано строк:   ' + rows.length);
console.log('Нормализовано:     ' + normalized.length);
console.log('Кодов маркировки:  ' + normalized.reduce((a, p) => a + p.markings.length, 0));
if (warnings.length) {
  console.log('\nПредупреждения (' + warnings.length + '):');
  warnings.slice(0, 25).forEach(w => console.log('  ! ' + w));
  if (warnings.length > 25) console.log('  … и ещё ' + (warnings.length - 25));
}
if (errors.length) {
  console.log('\nОшибки (' + errors.length + '):');
  errors.slice(0, 25).forEach(e => console.log('  x ' + e));
}

if (checkOnly) {
  console.log('\n--check: файл не записан');
  process.exit(errors.length ? 1 : 0);
}

let final = normalized;
if (merge && fs.existsSync(outPath)) {
  try {
    const prev = JSON.parse(fs.readFileSync(outPath, 'utf8'));
    const map = new Map();
    [...prev, ...normalized].forEach(p => map.set(p.part + '|' + p.pkg, p));
    final = [...map.values()];
    console.log('\nСлияние с существующим файлом: итого ' + final.length);
  } catch (e) {
    console.error('Не удалось прочитать ' + outPath + ': ' + e.message);
  }
}

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, JSON.stringify(final, null, 2), 'utf8');
console.log('\nЗаписано: ' + outPath);
console.log('Дальше: откройте index.html → Import CSV / JSON → выберите этот файл.');
process.exit(errors.length ? 1 : 0);

/* ------------------------------------------------------------------ парсер */

function parseCSV(text) {
  const lines = text.replace(/^\uFEFF/, '').replace(/\r/g, '').split('\n').filter(l => l.trim());
  if (!lines.length) return [];
  const delim = (lines[0].split(';').length > lines[0].split(',').length) ? ';' : ',';
  const split = (line) => {
    const out = [];
    let cur = '', inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (ch === '"') { inQ = !inQ; continue; }
      if (ch === delim && !inQ) { out.push(cur); cur = ''; continue; }
      cur += ch;
    }
    out.push(cur);
    return out.map(s => s.trim());
  };
  const header = split(lines[0]);
  return lines.slice(1).map(line => {
    const cols = split(line);
    const obj = {};
    header.forEach((h, i) => { obj[h] = cols[i] ?? ''; });
    return obj;
  });
}
