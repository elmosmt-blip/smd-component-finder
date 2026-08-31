#!/usr/bin/env node
/* ============================================================================
 * test-ui.js — smoke-тест интерфейса в jsdom
 *   npm install
 *   node tools/test-ui.js
 *
 * Проверяет то, что нельзя проверить юнит-тестом ядра: что страница
 * инициализируется без ошибок, фильтры и кнопки реально связаны с разметкой,
 * а карточки и панель кандидата отрисовываются.
 * ========================================================================= */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const root = path.join(__dirname, '..');
const errors = [];
let pass = 0, fail = 0;

function check(name, cond, extra) {
  if (cond) { pass++; console.log('[  ok  ] ' + name); }
  else { fail++; console.log('[ FAIL ] ' + name + (extra ? ' — ' + extra : '')); }
}

const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + e.message));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
  .replace(/<script src="[^"]*"><\/script>/g, ''); // скрипты подключаем вручную, без загрузчика ресурсов

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost:8000/',
  virtualConsole: vc,
  pretendToBeVisual: true
});
const win = dom.window;
const doc = win.document;

// Заглушка HTTP API: jsdom не реализует fetch, а панель даташитов — сетевая.
// Форма ответов повторяет tools/rag/serve.py один в один.
const API = {
  '/api/health': { ok: true, indexed: true, stats: { docs: 13, chunks: 258, tables: 63,
    chunks_with_part: 258, sections: { absolute_maximum_ratings: 36 }, parts: [] } },
  '/api/stats': { docs: 13, chunks: 258, tables: 63, chunks_with_part: 258,
    sections: {}, parts: [], built_at: '2026-08-31T00:00:00' },
  '/api/cards/stats': { cards: 2, with_pins: 2, with_package: 2, with_manufacturer: 2,
    with_ratings: 2, avg_confidence: 0.9, failures: 0,
    facets: { packages: [{ name: 'SOT-23', count: 2 }],
              manufacturers: [{ name: 'Vishay', count: 1 },
                              { name: 'Diodes Incorporated', count: 1 }] } },
  '/api/cards': { query: '', count: 2, total: 2, offset: 0, results: [
    { part: 'SI2301', manufacturer: 'Vishay', package: 'SOT-23', family: 'MOSFET',
      pin_count: 3, confidence: 1.0, pages: 2,
      description: 'P-channel MOSFET, -20 V, -2.3 A, SOT-23.',
      headline: [{ label: 'Drain-source voltage', value: -20, unit: 'V', text: '-20 V',
                   key: 'drain_source_voltage', page: 1 }],
      filename: 'SI2301_datasheet.pdf' },
    { part: '2N7002', manufacturer: 'Diodes Incorporated', package: 'SOT-23', family: 'BJT',
      pin_count: 3, confidence: 0.9, pages: 2, description: 'N-channel MOSFET, 60 V.',
      headline: [{ label: 'Drain-source voltage', value: 60, unit: 'V', text: '60 V',
                   key: 'drain_source_voltage', page: 1 }],
      filename: '2N7002_datasheet.pdf' }] },
  '/api/card': { part: 'SI2301', manufacturer: 'Vishay', package: 'SOT-23', family: 'MOSFET',
    description: 'P-channel MOSFET in SOT-23.', confidence: 1.0, pages: 2, tables: 6,
    parser: 'pdfplumber', filename: 'SI2301_datasheet.pdf',
    pins: [{ n: '1', name: 'Gate', function: 'Control input' },
           { n: '2', name: 'Source', function: '' },
           { n: '3', name: 'Drain', function: '' }],
    ratings: [{ symbol: 'VDS', param: 'Drain-source voltage', value: -20, unit: 'V',
                text: '-20 V', key: 'drain_source_voltage', page: 1 }],
    specs: [{ symbol: 'RDS(on)', param: 'On-resistance', conditions: 'VGS = -4.5 V',
              min: '-', typ: '70', max: '90', unit: 'mΩ', text: '70 mΩ',
              key: 'on_resistance', page: 2 }],
    features: ['Low on-resistance', 'Logic level gate'],
    dimensions: { body_length: { value: 2.9, unit: 'mm' } },
    order_codes: [{ code: 'SI2301BDS', package: 'SOT-23', marking: 'A1' }],
    headline: [{ label: 'Drain-source voltage', value: -20, unit: 'V', text: '-20 V',
                 key: 'drain_source_voltage', page: 1 }] },
  '/api/parts': { parts: [
    { part: 'MMBT3904', manufacturer: 'Diodes Inc.', package: 'SOT-23', n: 18 },
    { part: 'SI2301', manufacturer: 'Vishay', package: 'SOT-23', n: 12 }] }
};
win.fetch = function (url) {
  const path = String(url).split('?')[0];
  if (path === '/api/search') {
    const query = new win.URLSearchParams(String(url).split('?')[1] || '');
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
      query: query.get('q'), mode: 'bm25', count: 1, results: [{
        part: String(query.get('part') || 'MMBT3904'), manufacturer: 'Diodes Inc.',
        package: 'SOT-23', section: 'absolute_maximum_ratings',
        section_label: 'Absolute Maximum Ratings', page: 2,
        filename: 'MMBT3904_datasheet.pdf', is_table: true, score: 4.2,
        snippet: ' … VCEO; <<Collector>>-emitter voltage; 40; V …',
        summary: 'Absolute maximum ratings table', text: 'VCEO 40 V' }] }) });
  }
  const body = API[path] || {};
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};

// скрипты выполняем сразу, но app.js ждёт DOMContentLoaded, поэтому все
// проверки делаем после полной готовности документа
for (const f of ['assets/js/data.js', 'assets/js/matcher.js', 'assets/js/ocr.js', 'assets/js/app.js']) {
  try {
    win.eval(fs.readFileSync(path.join(root, f), 'utf8'));
  } catch (e) {
    errors.push(`${f}: ${e.message}`);
  }
}

// промисы в jsdom разрешаются на макрозадаче — даём им прокрутиться
function tick() { return new Promise(res => setTimeout(res, 20)); }

function domReady() {
  return new Promise(res => {
    if (doc.readyState === 'complete') return res();
    win.addEventListener('load', () => res(), { once: true });
  });
}

main().then(() => {
  console.log(`\n${pass} пройдено, ${fail} провалено`);
  process.exit(fail ? 1 : 0);
});

async function main() {
await domReady();

console.log('--- инициализация ---');
check(' скрипты выполнились без исключений', errors.length === 0, errors.join(' | '));
check(' список корпусов заполнен', doc.getElementById('pkgSelect').options.length > 50,
  String(doc.getElementById('pkgSelect').options.length));
check(' чипы примеров отрисованы', doc.getElementById('examples').children.length === 6);
check(' быстрые фильтры корпусов отрисованы', doc.getElementById('pkgChips').children.length === 8);
check(' база сообщает о себе', /parts/.test(doc.getElementById('dbInfo').textContent));
check(' стартовое состояние — заглушка детали', /Select a candidate/.test(doc.getElementById('detailBody').textContent));

console.log('\n--- поиск ---');
function search(q) {
  doc.getElementById('q').value = q;
  doc.getElementById('btnSearch').click();
}
function cards() { return [...doc.querySelectorAll('#results .card')]; }

search('A7');
check(' по «A7» есть кандидаты', cards().length > 0, String(cards().length));
check(' первый кандидат — BAV99', /BAV99/.test(cards()[0].textContent), cards()[0].textContent.slice(0, 60));
check(' панель кандидата заполнилась', /BAV99/.test(doc.getElementById('detailBody').textContent));
check(' панель содержит распиновку', /Pinout/.test(doc.getElementById('detailBody').textContent));
check(' коды маркировки видны', /BAV99|KJD/.test(doc.getElementById('detailBody').textContent));
check(' счётчик результатов заполнен', /Found/.test(doc.getElementById('resultsCount').textContent));

search('7O2');
check(' OCR-ошибка «7O2» находится как 2N7002', /2N7002/.test(cards()[0].textContent),
  cards()[0].textContent.slice(0, 60));
check(' стратегия поиска объясняет исправление', /corrected/.test(doc.getElementById('strategy').textContent),
  doc.getElementById('strategy').textContent);

// фильтр корпуса
doc.getElementById('q').value = 'A7';
doc.getElementById('btnSearch').click();
const chip = [...doc.querySelectorAll('#pkgChips [data-pkg]')].find(b => b.dataset.pkg === 'SOD-123');
chip.click();
check(' фильтр SOD-123 поднимает 1N4148W', /1N4148W/.test(cards()[0].textContent),
  cards()[0].textContent.slice(0, 60));
check(' подсказка по выбранному корпусу заполняется',
  /pins/.test(doc.getElementById('pkgMeta').textContent),
  doc.getElementById('pkgMeta').textContent);
check(' чип активного корпуса подсвечен',
  doc.querySelectorAll('#pkgChips .chip-on[data-pkg="SOD-123"]').length === 1);

// выбор кандидата
const second = cards()[1];
if (second) {
  second.click();
  check(' клик по карточке меняет панель кандидата',
    doc.querySelectorAll('#results .card-sel').length === 1);
}

console.log('\n--- пустое состояние ---');
search('ZZZQQQ99');
check(' пустой результат показывает подсказки', !doc.getElementById('empty').classList.contains('hidden'));
check(' карточек нет', cards().length === 0);

search('');
check(' очистка запроса возвращает заглушку', /Select a candidate/.test(doc.getElementById('detailBody').textContent));

console.log('\n--- OCR-панель ---');
const ocr = doc.getElementById('ocrPanel');
doc.getElementById('btnCamera').click();
check(' панель камеры открывается', ocr.classList.contains('open'));
doc.getElementById('btnCamera').click();
check(' панель камеры закрывается', !ocr.classList.contains('open'));

console.log('\n--- RAG-панель (HTTP API под заглушкой) ---');
search('SI2301');
await tick();
check(' статус индекса показан', /258/.test(doc.getElementById('ragStatus').textContent),
  doc.getElementById('ragStatus').textContent);
check(' список деталей подгружен в фильтр',
  doc.getElementById('ragPart').options.length === 3,
  String(doc.getElementById('ragPart').options.length));

// кнопка «Search datasheet» переносит деталь в поиск по даташитам
doc.querySelector('#detailBody [data-act="rag"]').click();
await tick();
check(' кнопка «Search datasheet» ставит жёсткий фильтр по детали',
  doc.getElementById('ragPart').value === 'SI2301', doc.getElementById('ragPart').value);
check(' поиск по даташитам вернул passage',
  /SI2301/.test(doc.getElementById('ragResults').textContent),
  doc.getElementById('ragResults').textContent.slice(0, 80));
check(' совпадение подсвечено <mark>', doc.querySelectorAll('#ragResults mark').length > 0);
check(' у passage есть раздел и файл',
  /Absolute Maximum Ratings/.test(doc.getElementById('ragResults').textContent) &&
  /MMBT3904_datasheet\.pdf/.test(doc.getElementById('ragResults').innerHTML));

// фильтр по детали уходит в запрос
doc.getElementById('ragQ').value = 'gate threshold voltage';
doc.getElementById('ragPart').value = 'SI2301';
doc.getElementById('ragPart').dispatchEvent(new win.Event('change'));
await tick();
check(' фильтр по детали подставляется в выборку',
  /SI2301/.test(doc.getElementById('ragResults').textContent));

// фильтр по разделу
const secBtn = [...doc.querySelectorAll('#ragSections [data-section]')]
  .find(b => b.dataset.section === 'package_dimensions');
secBtn.click();
await tick();
check(' чип раздела становится активным', secBtn.classList.contains('chip-on'));

console.log('\n--- карточки компонентов ---');
await tick();
check(' статус карточек показан', /Cards:/.test(doc.getElementById('cardStatus').textContent),
  doc.getElementById('cardStatus').textContent);
check(' фильтр корпусов заполнен из фасетов',
  doc.getElementById('cardPkg').options.length === 2,
  String(doc.getElementById('cardPkg').options.length));
check(' сетка карточек отрисована', doc.querySelectorAll('#cardGrid .pcard').length === 2,
  String(doc.querySelectorAll('#cardGrid .pcard').length));
check(' в карточке есть ключевой параметр',
  /-20 V/.test(doc.getElementById('cardGrid').textContent));

// полная карточка в модальном окне
doc.querySelector('#cardGrid .pcard').click();
await tick();
check(' модальное окно открылось', !doc.getElementById('cardModal').classList.contains('hidden'));
const modal = doc.getElementById('cardModalBody');
check(' в модальном окне есть распиновка', /Gate/.test(modal.textContent) && /Drain/.test(modal.textContent));
check(' есть таблица предельных режимов', /VDS/.test(modal.textContent) && /-20 V/.test(modal.textContent));
check(' есть таблица характеристик', /RDS\(on\)/.test(modal.textContent) &&
  /70/.test(modal.textContent) && /mΩ/.test(modal.innerHTML),
  (modal.textContent.match(/RDS.{0,60}/) || [''])[0]);
check(' есть габариты и коды заказа', /2\.9 mm/.test(modal.textContent) && /A1/.test(modal.textContent));
check(' есть ссылка на исходный PDF', /SI2301_datasheet\.pdf/.test(modal.innerHTML));

// закрытие по Escape
doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check(' Escape закрывает модальное окно',
  doc.getElementById('cardModal').classList.contains('hidden'));

console.log('\n--- итог ---');
check(' ошибок в консоли не было', errors.length === 0, errors.join(' | '));
}
