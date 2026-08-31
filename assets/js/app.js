/* ============================================================================
 * app.js — интерфейс SMD Component Finder
 * ----------------------------------------------------------------------------
 * Принципы, заложенные в UI:
 *   • Ничего не отправляется на сервер: поиск, OCR и данные — локально.
 *   • Результат — это ВСЕГДА список кандидатов с оценкой и объяснением,
 *     а не "единственный правильный ответ". Маркировка SMD неоднозначна
 *     по своей природе, и UI обязан это показывать, а не скрывать.
 *   • Каждый кандидат ведёт к проверке: datasheet, распиновка, примечание
 *     о подделках/клонах.
 * ========================================================================= */
(function (global) {
  'use strict';

  var EXAMPLES = ['BAT54', 'BC847', '2N7002', 'SI2301', '78M05', '1007'];
  var QUICK_PKGS = ['SOT-23', 'SOT-23-5', 'SOD-323', 'SOD-123', 'SOIC-8', 'QFN', 'DFN'];
  var LS_KEY = 'smd-finder-extra-parts';
  var LS_HISTORY = 'smd-finder-history';

  var el = {};
  var state = {
    query: '',
    pkg: 'all',
    last: null,
    selected: null,
    ocrBusy: false,
    cropMode: 'auto',
    preset: 'wide',
    cropRect: null,      // в координатах отображения
    stillCanvas: null,   // захваченный кадр
    history: []
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    el = {
      q: $('q'),
      btnSearch: $('btnSearch'),
      btnCamera: $('btnCamera'),
      pkgSelect: $('pkgSelect'),
      pkgChips: $('pkgChips'),
      pkgMeta: $('pkgMeta'),
      examples: $('examples'),
      results: $('results'),
      resultsCount: $('resultsCount'),
      empty: $('empty'),
      didYouMean: $('didYouMean'),
      detail: $('detail'),
      detailBody: $('detailBody'),
      strategy: $('strategy'),
      optOnline: $('optOnline'),
      optAutoPdf: $('optAutoPdf'),
      ocrPanel: $('ocrPanel'),
      stage: $('stage'),
      video: $('video'),
      cropBox: $('cropBox'),
      btnStartCam: $('btnStartCam'),
      btnCapture: $('btnCapture'),
      btnAnalyze: $('btnAnalyze'),
      btnSwitch: $('btnSwitch'),
      btnUpload: $('btnUpload'),
      fileInput: $('fileInput'),
      ocrStatus: $('ocrStatus'),
      ocrResults: $('ocrResults'),
      cropModes: document.getElementsByName('cropMode'),
      presets: document.querySelectorAll('[data-preset]'),
      dbInfo: $('dbInfo'),
      history: $('history'),
      btnImport: $('btnImport'),
      importInput: $('importInput'),
      toast: $('toast')
    };

    loadExtraParts();
    buildPackageFilter();
    buildExamples();
    bindEvents();
    initCropBox();
    renderDbInfo();
    renderHistory();
    readUrlState();
    if (state.query) {
      el.q.value = state.query;
      doSearch();
    } else {
      renderEmpty('');
    }
  }

  /* ------------------------------------------------------- данные: импорт */

  function loadExtraParts() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (!raw) return;
      var extra = JSON.parse(raw);
      if (Array.isArray(extra) && extra.length) {
        global.SMD_DB.parts = global.SMD_DB.parts.concat(extra);
        global.SMD_DB.meta.count = global.SMD_DB.parts.length;
      }
    } catch (e) { /* повреждённые данные в localStorage игнорируем */ }
  }

  function saveExtraParts(parts) {
    try {
      var cur = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
      localStorage.setItem(LS_KEY, JSON.stringify(cur.concat(parts)));
    } catch (e) {
      toast('Не удалось сохранить в localStorage: ' + e.message);
    }
  }

  /* Парсер CSV: part,mfr,pkg,type,desc,v,i,pins,markings(через ;),conf,note */
  function parseCSV(text) {
    var lines = text.replace(/\r/g, '').split('\n').filter(function (l) { return l.trim(); });
    if (!lines.length) return [];
    var delim = (lines[0].split(';').length > lines[0].split(',').length) ? ';' : ',';
    function split(line) {
      var out = [], cur = '', inQ = false;
      for (var i = 0; i < line.length; i++) {
        var ch = line[i];
        if (ch === '"') { inQ = !inQ; continue; }
        if (ch === delim && !inQ) { out.push(cur); cur = ''; continue; }
        cur += ch;
      }
      out.push(cur);
      return out.map(function (s) { return s.trim(); });
    }
    var header = split(lines[0]).map(function (h) { return h.toLowerCase(); });
    var idx = function (name) { return header.indexOf(name); };
    var out = [];
    for (var i = 1; i < lines.length; i++) {
      var cols = split(lines[i]);
      if (!cols.length || !cols[0]) continue;
      var mk = [];
      var mki = idx('markings');
      if (mki >= 0 && cols[mki]) mk = cols[mki].split('|').map(function (s) { return s.trim(); }).filter(Boolean);
      out.push({
        part: cols[idx('part')] || cols[0],
        mfr: cols[idx('mfr')] || cols[idx('manufacturer')] || '',
        pkg: cols[idx('pkg')] || cols[idx('package')] || '',
        type: cols[idx('type')] || '',
        desc: cols[idx('desc')] || cols[idx('description')] || '',
        v: cols[idx('v')] || cols[idx('voltage')] || '',
        i: cols[idx('i')] || cols[idx('current')] || '',
        pins: cols[idx('pins')] || cols[idx('pinout')] || '',
        markings: mk,
        conf: cols[idx('conf')] || 'med',
        note: cols[idx('note')] || ''
      });
    }
    return out;
  }

  function handleImport(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result);
      var parts = null;
      try {
        if (/\.json$/i.test(file.name)) {
          var parsed = JSON.parse(text);
          parts = Array.isArray(parsed) ? parsed : (parsed.parts || null);
        } else {
          parts = parseCSV(text);
        }
      } catch (e) {
        toast('Ошибка разбора: ' + e.message);
        return;
      }
      if (!parts || !parts.length) { toast('Файл не содержит записей'); return; }
      global.SMD_DB.parts = global.SMD_DB.parts.concat(parts);
      global.SMD_DB.meta.count = global.SMD_DB.parts.length;
      saveExtraParts(parts);
      renderDbInfo();
      toast('Добавлено записей: ' + parts.length + ' (всего ' + global.SMD_DB.meta.count + ')');
      if (state.query) doSearch();
    };
    reader.readAsText(file);
  }

  /* ------------------------------------------------------------- фильтры UI */

  function buildPackageFilter() {
    var pkgs = (global.SMD_PACKAGES || []).map(function (p) { return p.name; });
    var uniq = [];
    pkgs.forEach(function (p) { if (uniq.indexOf(p) === -1) uniq.push(p); });
    uniq.sort();
    el.pkgSelect.innerHTML = '<option value="all">All Packages</option>' +
      uniq.map(function (p) { return '<option value="' + esc(p) + '">' + esc(p) + '</option>'; }).join('');

    el.pkgChips.innerHTML = QUICK_PKGS.map(function (p) {
      return '<button class="chip" data-pkg="' + esc(p) + '">' + esc(p) + '</button>';
    }).join('') + '<button class="chip chip-ghost" data-pkg="all">Clear package</button>';

    el.pkgChips.addEventListener('click', function (e) {
      var b = e.target.closest('[data-pkg]');
      if (!b) return;
      setPackage(b.dataset.pkg);
    });
  }

  function setPackage(pkg) {
    state.pkg = pkg;
    el.pkgSelect.value = pkg;
    Array.prototype.forEach.call(el.pkgChips.querySelectorAll('[data-pkg]'), function (b) {
      b.classList.toggle('chip-on', b.dataset.pkg === pkg);
    });
    renderPkgMeta(pkg);
    if (state.query) doSearch();
    syncUrl();
  }

  /** Подсказка по выбранному корпусу: выводы, размер, заметка по пайке. */
  function renderPkgMeta(pkg) {
    var meta = pkgMeta(pkg);
    el.pkgMeta.innerHTML = !meta ? ''
      : (meta.pins ? meta.pins + ' выв. · ' : '') + (meta.body ? esc(meta.body) + ' мм · ' : '') + esc(meta.note);
  }

  function buildExamples() {
    el.examples.innerHTML = EXAMPLES.map(function (e) {
      return '<button class="chip" data-ex="' + esc(e) + '">' + esc(e) + '</button>';
    }).join('');
    el.examples.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ex]');
      if (!b) return;
      el.q.value = b.dataset.ex;
      doSearch();
    });
  }

  function renderDbInfo() {
    var m = global.SMD_DB.meta;
    el.dbInfo.innerHTML = 'База: <b>' + m.count + '</b> записей · версия ' + esc(m.version) +
      ' · обновлено ' + esc(m.updated) + '<br><span class="muted">' + esc(m.disclaimer) + '</span>';
  }

  /* ---------------------------------------------------------------- события */

  function bindEvents() {
    var t = null;
    el.q.addEventListener('input', function () {
      clearTimeout(t);
      t = setTimeout(function () { doSearch(); }, 180);
    });
    el.q.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { clearTimeout(t); doSearch(); }
    });
    el.btnSearch.addEventListener('click', function () { doSearch(); });
    el.pkgSelect.addEventListener('change', function () { setPackage(el.pkgSelect.value); });
    el.btnCamera.addEventListener('click', toggleOcrPanel);

    el.results.addEventListener('click', function (e) {
      var card = e.target.closest('[data-idx]');
      if (!card) return;
      selectResult(parseInt(card.dataset.idx, 10));
    });

    el.detail.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn || !state.selected) return;
      var part = state.selected;
      var act = btn.dataset.act;
      if (act === 'pdf') openPdf(part);
      if (act === 'search') openBrowserSearch(part);
      if (act === 'share') share(part);
      if (act === 'copy') copyText(part.part, 'Номер детали скопирован');
    });

    el.didYouMean.addEventListener('click', function (e) {
      var b = e.target.closest('[data-sug]');
      if (!b) return;
      el.q.value = b.dataset.sug;
      doSearch();
    });

    el.btnStartCam.addEventListener('click', startCamera);
    el.btnCapture.addEventListener('click', captureFrame);
    el.btnAnalyze.addEventListener('click', analyzeFrame);
    el.btnSwitch.addEventListener('click', switchCamera);
    el.btnUpload.addEventListener('click', function () { el.fileInput.click(); });
    el.fileInput.addEventListener('change', function () {
      if (el.fileInput.files && el.fileInput.files[0]) loadImageFile(el.fileInput.files[0]);
    });

    Array.prototype.forEach.call(el.cropModes, function (r) {
      r.addEventListener('change', function () {
        if (!r.checked) return;
        state.cropMode = r.value;
        el.cropBox.style.display = (r.value === 'full') ? 'none' : 'block';
      });
    });

    Array.prototype.forEach.call(el.presets, function (b) {
      b.addEventListener('click', function () {
        state.preset = b.dataset.preset;
        Array.prototype.forEach.call(el.presets, function (x) {
          x.classList.toggle('chip-on', x === b);
        });
        resetCropBox();
      });
    });

    el.ocrResults.addEventListener('click', function (e) {
      var b = e.target.closest('[data-use]');
      if (!b) return;
      el.q.value = b.dataset.use;
      doSearch();
      el.ocrPanel.classList.remove('open');
    });

    el.btnImport.addEventListener('click', function () { el.importInput.click(); });
    el.importInput.addEventListener('change', function () {
      if (el.importInput.files && el.importInput.files[0]) handleImport(el.importInput.files[0]);
      el.importInput.value = '';
    });
  }

  /* ----------------------------------------------------------------- поиск */

  function doSearch() {
    state.query = el.q.value.trim();
    syncUrl();
    if (!state.query) {
      state.last = null;
      el.results.innerHTML = '';
      el.resultsCount.textContent = '';
      el.strategy.innerHTML = '';
      renderEmpty('');
      renderDetail(null);
      return;
    }
    state.last = global.SMDMatcher.search(state.query, { pkg: state.pkg, limit: 40 });
    renderResults();
    pushHistory(state.query);
  }

  function renderResults() {
    var res = state.last;
    if (!res) return;
    el.resultsCount.textContent = res.results.length
      ? ('Найдено: ' + res.results.length)
      : 'Ничего не найдено';

    if (!res.results.length) {
      el.results.innerHTML = '';
      renderEmpty(res);
      renderDetail(null);
      return;
    }
    el.empty.classList.add('hidden');
    el.results.innerHTML = res.results.map(function (r, i) {
      var p = r.part;
      var cls = r.score >= 85 ? 'high' : r.score >= 60 ? 'mid' : 'low';
      return '' +
        '<div class="card' + (i === 0 ? ' card-first' : '') + '" data-idx="' + i + '">' +
          '<div class="card-top">' +
            '<span class="part">' + esc(p.part) + '</span>' +
            '<span class="conf conf-' + cls + '">' + r.score + '%</span>' +
          '</div>' +
          '<div class="card-meta">' +
            '<span class="tag">' + esc(p.pkg) + '</span>' +
            '<span class="tag tag-mfr">' + esc(p.mfr || '—') + '</span>' +
            '<span class="tag tag-type">' + esc(p.type || '') + '</span>' +
          '</div>' +
          '<div class="card-desc">' + esc(p.desc || '') + '</div>' +
          '<div class="card-code">код: <b>' + esc(r.matchedCode) + '</b>' +
            (r.via && r.via !== 'прямой поиск' ? ' <span class="via">(' + esc(r.via) + ')</span>' : '') +
          '</div>' +
          '<div class="bar"><i class="bar-' + cls + '" style="width:' + r.score + '%"></i></div>' +
        '</div>';
    }).join('');

    renderStrategy(res);
    selectResult(0);
  }

  function renderStrategy(res) {
    var s = res.strategy;
    var parts = [];
    if (s.usedVariant) parts.push('подбор похожих символов: «' + esc(res.normalized) + '» → «' + esc(s.usedVariant) + '»');
    if (s.usedTruncation) parts.push('отсечён код партии/даты: «' + esc(s.usedTruncation.cut) + '»');
    s.attempts.forEach(function (a) {
      parts.push(esc(a.label) + ': ' + a.hits + ' совп.');
    });
    el.strategy.innerHTML = parts.length
      ? '<span class="muted">Стратегия поиска:</span> ' + parts.join(' · ')
      : '';
  }

  function renderEmpty(res) {
    el.empty.classList.remove('hidden');
    var sug = (res && res.suggestions && res.suggestions.length) ? res.suggestions : [];
    el.didYouMean.innerHTML = sug.length
      ? '<div class="dym-title">Возможно, вы имели в виду:</div>' +
        sug.map(function (s) {
          return '<button class="chip" data-sug="' + esc(s.code) + '">' + esc(s.code) +
                 '<span class="chip-sub">' + esc(s.part) + '</span></button>';
        }).join('')
      : '';
  }

  function selectResult(idx) {
    if (!state.last || !state.last.results[idx]) return;
    state.selected = state.last.results[idx].part;
    var r = state.last.results[idx];
    Array.prototype.forEach.call(el.results.querySelectorAll('.card'), function (c) {
      c.classList.toggle('card-sel', parseInt(c.dataset.idx, 10) === idx);
    });
    renderDetail(state.selected, r);
    if (el.optAutoPdf.checked) openPdf(state.selected);
  }

  /* ------------------------------------------------------------- карточка */

  function pkgMeta(name) {
    var list = global.SMD_PACKAGES || [];
    for (var i = 0; i < list.length; i++) if (list[i].name === name) return list[i];
    return null;
  }

  /**
   * Коды маркировки с пометкой надёжности. Код может храниться строкой
   * или объектом { c, conf } — если у детали один код общепринят, а другой
   * принадлежит конкретному заводу, это показывается явно.
   */
  function renderMarkings(p) {
    var list = p.markings || [];
    if (!list.length) {
      return '<span class="muted">нет кода — маркируется цветом/полосой или полным номером</span>';
    }
    return list.map(function (m) {
      var code = (m && typeof m === 'object') ? m.c : m;
      var conf = (m && typeof m === 'object') ? (m.conf || p.conf) : p.conf;
      var cls = conf === 'high' ? 'code-high' : conf === 'low' ? 'code-low' : 'code-med';
      var title = conf === 'high' ? 'код устойчив между производителями'
                : conf === 'low' ? 'код конкретного производителя, есть конфликты' : 'широко известный код';
      return '<code class="' + cls + '" title="' + esc(title) + '">' + esc(code) + '</code>';
    }).join(' ');
  }

  function renderDetail(p, r) {
    if (!p) {
      el.detailBody.innerHTML = '<div class="ph">Выберите кандидата слева</div>';
      return;
    }
    var meta = pkgMeta(p.pkg);
    var confCls = r.score >= 85 ? 'high' : r.score >= 60 ? 'mid' : 'low';
    var confLabel = r.score >= 85 ? 'высокая' : r.score >= 60 ? 'средняя — проверьте' : 'низкая — почти наверняка нужен datasheet';

    el.detailBody.innerHTML = '' +
      '<div class="d-head">' +
        '<div class="d-part">' + esc(p.part) + '</div>' +
        '<div class="d-sub">' + esc(p.type || '') + ' · ' + esc(p.mfr || 'производитель не указан') + '</div>' +
      '</div>' +
      '<div class="d-grid">' +
        row('Package', esc(p.pkg) + (meta && meta.body ? ' <span class="muted">(' + esc(meta.body) + ' мм' +
            (meta.pins ? ', ' + meta.pins + ' выв.' : '') + ')</span>' : '')) +
        row('Manufacturer', esc(p.mfr || '—')) +
        row('Type / Function', esc(p.type || '—')) +
        row('Description', esc(p.desc || '—')) +
        row('Voltage', esc(p.v || '—')) +
        row('Current', esc(p.i || '—')) +
        row('Pinout', esc(p.pins || '—')) +
        row('Коды маркировки', renderMarkings(p)) +
      '</div>' +
      (meta && meta.note ? '<div class="d-note"><b>Корпус:</b> ' + esc(meta.note) + '</div>' : '') +
      (p.note ? '<div class="d-note d-warn"><b>Важно:</b> ' + esc(p.note) + '</div>' : '') +
      '<div class="d-conf">Confidence: <b class="conf-' + confCls + '">' + (r ? r.score : 0) + '%</b> ' +
        '<span class="muted">(' + confLabel + ')</span></div>' +
      '<div class="d-reasons"><b>Match reasons:</b><ul>' +
        (r ? r.reasons.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') : '<li>—</li>') +
      '</ul></div>' +
      '<div class="d-actions">' +
        '<button class="btn btn-primary" data-act="pdf">Datasheet (PDF)</button>' +
        '<button class="btn" data-act="search">Поиск в сети</button>' +
        '<button class="btn" data-act="share">Share</button>' +
        '<button class="btn" data-act="copy">Copy Part Number</button>' +
      '</div>' +
      '<div class="d-verify">Перед заменой проверьте: распиновку мультиметром, допустимое ' +
      'напряжение и ток, цоколёвку конкретного производителя.</div>';

    function row(k, v) {
      return '<div class="d-row"><span class="d-k">' + k + '</span><span class="d-v">' + v + '</span></div>';
    }
  }

  /* -------------------------------------------------------------- действия */

  function openPdf(p) {
    var url = 'https://www.google.com/search?q=' + encodeURIComponent(p.part + ' datasheet filetype:pdf');
    if (el.optOnline.checked === false) {
      // переключатель "Online Search" выключен — всё-таки открываем, но предупреждаем
      toast('Онлайн-поиск выключен, но datasheet доступен только в сети');
    }
    window.open(url, '_blank', 'noopener');
  }

  function openBrowserSearch(p) {
    var q = p.part + ' ' + (p.pkg || '') + ' SMD marking';
    window.open('https://duckduckgo.com/?q=' + encodeURIComponent(q), '_blank', 'noopener');
  }

  function share(p) {
    var url = location.origin + location.pathname + '?q=' + encodeURIComponent(p.part);
    var data = { title: p.part, text: p.part + ' — ' + (p.desc || ''), url: url };
    if (navigator.share) {
      navigator.share(data).catch(function () {});
    } else {
      copyText(url, 'Ссылка скопирована в буфер');
    }
  }

  function copyText(text, msg) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast(msg); },
        function () { toast('Не удалось скопировать'); });
    } else {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast(msg); } catch (e) { toast('Не удалось скопировать'); }
      document.body.removeChild(ta);
    }
  }

  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.toast.classList.remove('show'); }, 2600);
  }

  /* -------------------------------------------------------------- история */

  function pushHistory(q) {
    if (!q) return;
    state.history = state.history.filter(function (x) { return x.toLowerCase() !== q.toLowerCase(); });
    state.history.unshift(q);
    state.history = state.history.slice(0, 12);
    try { localStorage.setItem(LS_HISTORY, JSON.stringify(state.history)); } catch (e) {}
    renderHistory();
  }

  function renderHistory() {
    try {
      var h = JSON.parse(localStorage.getItem(LS_HISTORY) || '[]');
      if (h.length) state.history = h;
    } catch (e) {}
    if (!state.history.length) { el.history.innerHTML = ''; return; }
    el.history.innerHTML = '<span class="muted">История:</span> ' +
      state.history.map(function (x) {
        return '<button class="chip chip-mini" data-ex="' + esc(x) + '">' + esc(x) + '</button>';
      }).join('');
    el.history.onclick = function (e) {
      var b = e.target.closest('[data-ex]');
      if (!b) return;
      el.q.value = b.dataset.ex;
      doSearch();
    };
  }

  /* ------------------------------------------------------------------ URL */

  function syncUrl() {
    var params = new URLSearchParams();
    if (state.query) params.set('q', state.query);
    if (state.pkg && state.pkg !== 'all') params.set('pkg', state.pkg);
    var qs = params.toString();
    history.replaceState(null, '', qs ? ('?' + qs) : location.pathname);
  }

  function readUrlState() {
    var params = new URLSearchParams(location.search);
    state.query = params.get('q') || '';
    state.pkg = params.get('pkg') || 'all';
    if (state.pkg !== 'all') {
      el.pkgSelect.value = state.pkg;
      Array.prototype.forEach.call(el.pkgChips.querySelectorAll('[data-pkg]'), function (b) {
        b.classList.toggle('chip-on', b.dataset.pkg === state.pkg);
      });
    }
  }

  /* -------------------------------------------------------------- OCR: UI */

  function toggleOcrPanel() {
    var open = el.ocrPanel.classList.toggle('open');
    el.btnCamera.classList.toggle('btn-on', open);
    if (open) {
      // панель только что стала видимой — теперь у сцены есть размеры
      requestAnimationFrame(resetCropBox);
    } else {
      global.SMDOCR.camera.stop();
    }
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('Камера недоступна: нужен HTTPS или localhost. Используйте «Upload File».');
      return;
    }
    setStatus('Запускаю камеру…');
    global.SMDOCR.camera.start(el.video).then(function () {
      el.video.style.display = 'block';
      if (state.stillCanvas) state.stillCanvas.style.display = 'none';
      el.cropBox.style.display = state.cropMode === 'full' ? 'none' : 'block';
      el.btnAnalyze.disabled = false;
      setStatus('Камера работает. Наведите на корпус и нажмите «Capture Image».');
      resetCropBox();
    }).catch(function (err) {
      setStatus('Ошибка камеры: ' + err.message + ' — можно загрузить фото через Upload File.');
    });
  }

  function switchCamera() {
    var f = global.SMDOCR.camera.switchFacing();
    if (global.SMDOCR.camera.active()) {
      startCamera();
      setStatus('Камера: ' + (f === 'environment' ? 'основная' : 'фронтальная'));
    }
  }

  function currentSource() {
    if (state.stillCanvas && state.stillCanvas.style.display !== 'none') return state.stillCanvas;
    if (global.SMDOCR.camera.active()) return el.video;
    return null;
  }

  function captureFrame() {
    var src = currentSource();
    if (!src) { setStatus('Сначала запустите камеру или загрузите изображение.'); return; }
    var sw = src.videoWidth || src.width;
    var sh = src.videoHeight || src.height;
    var c = document.createElement('canvas');
    c.width = sw; c.height = sh;
    c.getContext('2d').drawImage(src, 0, 0, sw, sh);
    c.className = 'stage-img';
    if (state.stillCanvas && state.stillCanvas.parentNode) {
      state.stillCanvas.parentNode.replaceChild(c, state.stillCanvas);
    } else {
      el.stage.appendChild(c);
    }
    state.stillCanvas = c;
    el.video.style.display = 'none';
    global.SMDOCR.camera.stop();
    resetCropBox();
    setStatus('Кадр захвачен. Подгоните рамку и нажмите «Analyze Current Crop».');
  }

  function loadImageFile(file) {
    var img = new Image();
    var url = URL.createObjectURL(file);
    img.onload = function () {
      var c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext('2d').drawImage(img, 0, 0);
      c.className = 'stage-img';
      if (state.stillCanvas && state.stillCanvas.parentNode) {
        state.stillCanvas.parentNode.replaceChild(c, state.stillCanvas);
      } else {
        el.stage.appendChild(c);
      }
      state.stillCanvas = c;
      el.video.style.display = 'none';
      global.SMDOCR.camera.stop();
      resetCropBox();
      setStatus('Изображение загружено (' + c.width + '×' + c.height + '). Нажмите «Analyze Current Crop».');
      URL.revokeObjectURL(url);
    };
    img.onerror = function () { setStatus('Не удалось открыть изображение'); URL.revokeObjectURL(url); };
    img.src = url;
  }

  function setStatus(msg) { el.ocrStatus.textContent = msg; }

  function analyzeFrame() {
    var src = currentSource();
    if (!src) { setStatus('Нет источника: запустите камеру или загрузите файл.'); return; }
    if (state.ocrBusy) return;
    state.ocrBusy = true;
    el.btnAnalyze.disabled = true;
    setStatus('Готовлю изображение…');

    // рамка, которую двигал пользователь, важнее режима кадрирования:
    // иначе «Auto Crop» молча игнорировал бы ручную настройку
    var manual = !!(state.cropRect && state.cropRect.manual);
    var mode = manual ? 'manual' : state.cropMode;

    var rect = null;
    if (mode !== 'full' && state.cropRect) {
      var srcW = src.videoWidth || src.width;
      var dispW = src.clientWidth || src.width;
      var k = srcW / (dispW || srcW);
      rect = {
        x: state.cropRect.x * k,
        y: state.cropRect.y * k,
        w: state.cropRect.w * k,
        h: state.cropRect.h * k
      };
    }

    global.SMDOCR.analyze(src, {
      crop: mode,
      preset: state.preset,
      rect: rect
    }, setStatus).then(function (out) {
      renderOcrResults(out);
      el.btnAnalyze.disabled = false;
      state.ocrBusy = false;
    }).catch(function (err) {
      setStatus('Ошибка OCR: ' + err.message);
      el.btnAnalyze.disabled = false;
      state.ocrBusy = false;
    });
  }

  function renderOcrResults(out) {
    var items = out.results.filter(function (r) { return r.cleaned; });
    if (!items.length) {
      setStatus('Ничего не распознано. Приблизьте камеру, добавьте боковую подсветку и повторите.');
      el.ocrResults.innerHTML = '';
      return;
    }
    // отсеиваем дубли, сортируем по уверенности OCR, затем по совпадению с базой
    var seen = {};
    items = items.filter(function (r) {
      if (seen[r.cleaned]) return false;
      seen[r.cleaned] = true;
      return true;
    });
    items.forEach(function (r) {
      var m = global.SMDMatcher.search(r.cleaned, { pkg: state.pkg, limit: 1 });
      r.match = m.results[0] || null;
      r.combined = Math.round((r.conf * 0.35) + ((r.match ? r.match.score : 0) * 0.65));
    });
    items.sort(function (a, b) { return b.combined - a.combined; });

    setStatus('Распознано вариантов: ' + items.length + '. Выберите подходящий или исправьте вручную.');
    el.ocrResults.innerHTML = '<div class="ocr-title">Результаты OCR — нажмите, чтобы искать по коду</div>' +
      items.map(function (r) {
        return '<button class="ocr-item" data-use="' + esc(r.cleaned) + '">' +
          '<code>' + esc(r.cleaned) + '</code>' +
          '<span class="ocr-conf">OCR ' + Math.round(r.conf) + '%</span>' +
          (r.match
            ? '<span class="ocr-match">→ ' + esc(r.match.part.part) + ' (' + r.match.score + '%)</span>'
            : '<span class="ocr-nomatch">в базе нет</span>') +
          '<span class="ocr-label">' + esc(r.label) + '</span>' +
        '</button>';
      }).join('');

    // автоматически подставляем лучший вариант в строку поиска, но не ищем
    // молча: пользователь должен видеть, что именно распозналось
    if (items[0]) el.q.value = items[0].cleaned;
  }

  /* -------------------------------------------------------- рамка кадрирования */

  function initCropBox() {
    var box = el.cropBox;
    var drag = null;

    function point(e) { return e.touches ? e.touches[0] : e; }

    function onDown(e) {
      if (state.cropMode === 'full') return;
      // рамка могла ещё ни разу не позиционироваться — берём текущую геометрию
      if (!state.cropRect) {
        state.cropRect = { x: box.offsetLeft, y: box.offsetTop, w: box.offsetWidth, h: box.offsetHeight };
      }
      var r = state.cropRect;
      var p = point(e);
      drag = {
        sx: p.clientX, sy: p.clientY,
        ox: r.x, oy: r.y, ow: r.w, oh: r.h,
        mode: (e.target && e.target.dataset && e.target.dataset.handle) ? 'resize' : 'move'
      };
      e.preventDefault();
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      document.addEventListener('touchmove', onMove, { passive: false });
      document.addEventListener('touchend', onUp);
    }

    function onMove(e) {
      if (!drag) return;
      var p = point(e);
      var r = state.cropRect;
      if (drag.mode === 'move') {
        r.x = drag.ox + (p.clientX - drag.sx);
        r.y = drag.oy + (p.clientY - drag.sy);
      } else {
        r.w = Math.max(30, drag.ow + (p.clientX - drag.sx));
        r.h = Math.max(20, drag.oh + (p.clientY - drag.sy));
      }
      // как только рамку тронули руками, она приоритетнее автокадрирования
      r.manual = true;
      applyCropRect();
      e.preventDefault();
    }

    function onUp() {
      drag = null;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.removeEventListener('touchmove', onMove);
      document.removeEventListener('touchend', onUp);
    }

    box.addEventListener('mousedown', onDown);
    box.addEventListener('touchstart', onDown, { passive: false });
  }

  function applyCropRect() {
    var r = state.cropRect;
    if (!r) return;
    var stage = el.stage;
    var maxW = stage.clientWidth, maxH = stage.clientHeight;
    // панель ещё скрыта — размеров нет, не портим прямоугольник нулями
    if (!maxW || !maxH) return;
    r.w = Math.min(r.w, maxW - 4);
    r.h = Math.min(r.h, maxH - 4);
    r.x = Math.max(0, Math.min(r.x, maxW - r.w));
    r.y = Math.max(0, Math.min(r.y, maxH - r.h));
    el.cropBox.style.left = r.x + 'px';
    el.cropBox.style.top = r.y + 'px';
    el.cropBox.style.width = r.w + 'px';
    el.cropBox.style.height = r.h + 'px';
  }

  function resetCropBox() {
    var stage = el.stage;
    var fracW = state.preset === 'small' ? 0.40 : state.preset === 'wide' ? 0.70 : 0.90;
    var fracH = state.preset === 'small' ? 0.22 : state.preset === 'wide' ? 0.34 : 0.55;
    var w = Math.max(40, stage.clientWidth * fracW);
    var h = Math.max(24, stage.clientHeight * fracH);
    state.cropRect = { x: (stage.clientWidth - w) / 2, y: (stage.clientHeight - h) / 2, w: w, h: h };
    applyCropRect();
  }

  window.addEventListener('resize', function () {
    if (state.cropRect) applyCropRect();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
