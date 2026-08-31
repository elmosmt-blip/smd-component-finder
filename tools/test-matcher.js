#!/usr/bin/env node
/* ============================================================================
 * test-matcher.js — проверка ядра поиска без браузера
 *   node tools/test-matcher.js
 * Гоняет набор запросов через matcher и печатает топ-3 кандидата, чтобы
 * быстро увидеть регрессии при правках базы или алгоритма.
 * ========================================================================= */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const sandbox = { window: {}, console };
sandbox.global = sandbox;
vm.createContext(sandbox);

for (const f of ['assets/js/data.js', 'assets/js/matcher.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, f), 'utf8'), sandbox, { filename: f });
}

const M = sandbox.window.SMDMatcher;
const DB = sandbox.window.SMD_DB;

const CASES = [
  // что вводим              чего ожидаем в топе
  ['1A', 'MMBT3904', null],
  ['1AM', 'MMBT3904', null],                 // код партии в хвосте
  ['A7', 'BAV99', null],                     // конфликт: BAV99 vs 1N4148W
  ['A7', '1N4148W', 'SOD-123'],              // фильтр корпуса снимает конфликт
  ['7O2', '2N7002', null],                   // OCR: O вместо 0
  ['M7', 'M7', null],
  ['BAV99', 'BAV99', null],
  ['mmbt3904', 'MMBT3904', null],
  ['78M05', '78M05', null],
  ['662K', 'XC6206P332MR', null],
  ['2N7002', '2N7002', null],
  ['SI2301', 'SI2301', null],
  ['BAT54', 'BAT54', null],
  ['KL3', 'BAT54', null],
  ['8205A', '8205A', null],
  ['TP4056', 'TP4056', null],
  ['LM393', 'LM393', null],
  ['1OO7', null, null],                      // мусор: не должно падать
  ['1007', null, null],                      // код из примеров эталонного тула
  ['1', null, null]
];

let pass = 0, fail = 0;

console.log('База: ' + DB.parts.length + ' записей, версия ' + DB.meta.version);
console.log('Пакетов в фильтре: ' + sandbox.window.SMD_PACKAGES.length + '\n');

for (const [query, expect, pkg] of CASES) {
  const r = M.search(query, { pkg: pkg || 'all', limit: 3 });
  const top = r.results.map(x => `${x.part.part} (${x.score}%)`);
  const got = r.results[0] ? r.results[0].part.part : null;
  const ok = expect === null ? true : got === expect;
  if (ok) pass++; else fail++;

  const tag = ok ? '  ok  ' : ' FAIL ';
  console.log(`[${tag}] "${query}"${pkg ? ' [' + pkg + ']' : ''}`);
  console.log(`         топ: ${top.length ? top.join(', ') : '— пусто —'}`);
  if (r.strategy.usedVariant) console.log(`         вариант: ${r.normalized} → ${r.strategy.usedVariant}`);
  if (r.strategy.usedTruncation) console.log(`         отсечено: "${r.strategy.usedTruncation.cut}" (${r.strategy.usedTruncation.side})`);
  if (!r.results.length && r.suggestions.length) {
    console.log(`         подсказки: ${r.suggestions.slice(0, 4).map(s => s.code + '→' + s.part).join(', ')}`);
  }
  if (!ok) console.log(`         ОЖИДАЛИ: ${expect}`);
}

/* --- проверки свойств алгоритма --- */
console.log('\n--- свойства алгоритма ---');
function check(name, cond, extra) {
  if (cond) { pass++; console.log('[  ok  ] ' + name); }
  else { fail++; console.log('[ FAIL ] ' + name + (extra ? ' — ' + extra : '')); }
}

// расстояние с учётом похожих символов должно быть меньше обычного
check(' confusionDistance("7O2","702") < levenshtein("7O2","702")',
  M.confusionDistance('7O2', '702') < M.levenshtein('7O2', '702'),
  `${M.confusionDistance('7O2', '702')} vs ${M.levenshtein('7O2', '702')}`);

// похожие символы дороже обычных, но дешевле единицы
check(' похожая замена стоит 0.2',
  Math.abs(M.confusionDistance('S', '5') - 0.2) < 1e-9,
  String(M.confusionDistance('S', '5')));

check(' непохожая замена стоит 1.0',
  Math.abs(M.confusionDistance('S', 'X') - 1) < 1e-9,
  String(M.confusionDistance('S', 'X')));

// приоритет точного совпадения над нечётким
const rExact = M.search('BAV99', { pkg: 'all' });
check(' точное совпадение идёт первым и получает 100%',
  rExact.results[0].part.part === 'BAV99' && rExact.results[0].score === 100,
  JSON.stringify(rExact.results[0] && { p: rExact.results[0].part.part, s: rExact.results[0].score }));

// короткие коды наказываются
const rShort = M.search('B', { pkg: 'all' });
check(' короткий код не получает завышенную оценку',
  !rShort.results.length || rShort.results[0].score < 95,
  String(rShort.results[0] && rShort.results[0].score));

// фильтр корпуса влияет на порядок
const noFilter = M.search('A7', { pkg: 'all' });
const withSod = M.search('A7', { pkg: 'SOD-123' });
check(' фильтр корпуса поднимает нужного кандидата',
  noFilter.results[0].part.part === 'BAV99' && withSod.results[0].part.part === '1N4148W',
  `${noFilter.results[0].part.part} / ${withSod.results[0].part.part}`);

// целостность базы
check(' у всех записей есть part и pkg',
  DB.parts.every(p => p.part && p.pkg));
check(' conf принимает только high/med/low',
  DB.parts.every(p => ['high', 'med', 'low'].includes(p.conf)));
check(' нет дублей part|pkg',
  new Set(DB.parts.map(p => p.part + '|' + p.pkg)).size === DB.parts.length,
  String(DB.parts.length - new Set(DB.parts.map(p => p.part + '|' + p.pkg)).size) + ' дублей');
check(' все корпуса компонентов есть в списке пакетов',
  (() => {
    const names = new Set(sandbox.window.SMD_PACKAGES.map(p => p.name));
    return DB.parts.every(p => names.has(p.pkg));
  })(),
  DB.parts.filter(p => !sandbox.window.SMD_PACKAGES.some(x => x.name === p.pkg)).map(p => p.pkg).join(', '));

console.log(`\nИтого: ${pass} пройдено, ${fail} провалено`);
process.exit(fail ? 1 : 0);
