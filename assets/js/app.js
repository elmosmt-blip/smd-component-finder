/* ============================================================================
 * app.js — SMD Component Finder UI
 * ----------------------------------------------------------------------------
 * Two engines, one page:
 *   1. Marking lookup   — fuzzy, offline, tolerant of OCR mistakes.
 *   2. Datasheet RAG    — asks the local /api/search endpoint built by
 *                         tools/rag/pipeline.py over your own PDF library.
 *
 * Principles:
 *   • Nothing leaves the machine: search, OCR and retrieval are local.
 *   • A result is ALWAYS a ranked list with reasons, never a single "answer".
 *     SMD marking is ambiguous by nature and the UI must show that, not hide it.
 *   • Every candidate leads to verification: datasheet, pinout, clone warning.
 * ========================================================================= */
(function (global) {
  'use strict';

  var EXAMPLES = ['BAT54', 'BC847', '2N7002', 'SI2301', '78M05', '1007'];
  var QUICK_PKGS = ['SOT-23', 'SOT-23-5', 'SOD-323', 'SOD-123', 'SOIC-8', 'QFN', 'DFN'];
  var LS_KEY = 'smd-finder-extra-parts';
  var LS_HISTORY = 'smd-finder-history';

  var RAG_SECTIONS = [
    ['', 'All sections'],
    ['absolute_maximum_ratings', 'Absolute Maximum Ratings'],
    ['pin_configuration', 'Pin Configuration'],
    ['electrical_characteristics', 'Electrical Characteristics'],
    ['package_dimensions', 'Package Dimensions'],
    ['features', 'Features'],
    ['ordering_information', 'Ordering Information']
  ];

  var el = {};
  var state = {
    query: '',
    pkg: 'all',
    last: null,
    selected: null,
    ocrBusy: false,
    cropMode: 'auto',
    preset: 'wide',
    cropRect: null,
    stillCanvas: null,
    history: [],
    rag: { ready: false, part: '', section: '', busy: false },
    cards: { q: '', pkg: '', mfr: '', offset: 0, limit: 60, total: 0, ready: false, busy: false },
    ingest: { job: null, timer: null, picked: [], busy: false }
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
      q: $('q'), btnSearch: $('btnSearch'), btnCamera: $('btnCamera'),
      pkgSelect: $('pkgSelect'), pkgChips: $('pkgChips'), pkgMeta: $('pkgMeta'),
      examples: $('examples'),
      results: $('results'), resultsCount: $('resultsCount'),
      empty: $('empty'), didYouMean: $('didYouMean'),
      detail: $('detail'), detailBody: $('detailBody'), strategy: $('strategy'),
      optOnline: $('optOnline'), optAutoPdf: $('optAutoPdf'),
      ocrPanel: $('ocrPanel'), stage: $('stage'), video: $('video'), cropBox: $('cropBox'),
      btnStartCam: $('btnStartCam'), btnCapture: $('btnCapture'), btnAnalyze: $('btnAnalyze'),
      btnSwitch: $('btnSwitch'), btnUpload: $('btnUpload'), fileInput: $('fileInput'),
      ocrStatus: $('ocrStatus'), ocrResults: $('ocrResults'),
      cropModes: document.getElementsByName('cropMode'),
      presets: document.querySelectorAll('[data-preset]'),
      ragQ: $('ragQ'), btnRagSearch: $('btnRagSearch'), ragPart: $('ragPart'),
      ragSections: $('ragSections'), ragStatus: $('ragStatus'),
      ragResults: $('ragResults'), ragHelp: $('ragHelp'),
      folderPath: $('folderPath'), btnIngestPath: $('btnIngestPath'),
      optRecursive: $('optRecursive'), folderPicker: $('folderPicker'),
      btnPickFolder: $('btnPickFolder'), pickInfo: $('pickInfo'),
      btnIngestUpload: $('btnIngestUpload'), ingestProgress: $('ingestProgress'),
      ingestBar: $('ingestBar'), ingestStatus: $('ingestStatus'),
      ingestErrors: $('ingestErrors'), btnIngestCancel: $('btnIngestCancel'),
      ingestHint: $('ingestHint'),
      cardQ: $('cardQ'), btnCardSearch: $('btnCardSearch'), cardPkg: $('cardPkg'),
      cardMfr: $('cardMfr'), cardStatus: $('cardStatus'), cardGrid: $('cardGrid'),
      btnCardMore: $('btnCardMore'), cardHelp: $('cardHelp'),
      cardModal: $('cardModal'), cardModalBody: $('cardModalBody'),
      dbInfo: $('dbInfo'), btnImport: $('btnImport'), importInput: $('importInput'),
      history: $('history'), toast: $('toast')
    };

    loadExtraParts();
    buildPackageFilter();
    buildExamples();
    bindEvents();
    initCropBox();
    renderDbInfo();
    renderHistory();
    buildRagSections();
    readUrlState();
    initRag();
    initIngest();
    initCards();

    if (state.query) {
      el.q.value = state.query;
      doSearch();
    } else {
      renderEmpty('');
    }
  }

  /* ------------------------------------------------------- data: import */

  function loadExtraParts() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (!raw) return;
      var extra = JSON.parse(raw);
      if (Array.isArray(extra) && extra.length) {
        global.SMD_DB.parts = global.SMD_DB.parts.concat(extra);
        global.SMD_DB.meta.count = global.SMD_DB.parts.length;
      }
    } catch (e) { /* corrupted localStorage is simply ignored */ }
  }

  function saveExtraParts(parts) {
    try {
      var cur = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
      localStorage.setItem(LS_KEY, JSON.stringify(cur.concat(parts)));
    } catch (e) {
      toast('Could not save to localStorage: ' + e.message);
    }
  }

  /** CSV columns: part,mfr,pkg,type,desc,v,i,pins,markings(|),conf,note */
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
      var mki = idx('markings');
      var mk = [];
      if (mki >= 0 && cols[mki]) {
        mk = cols[mki].split('|').map(function (s) { return s.trim(); }).filter(Boolean);
      }
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
        toast('Parse error: ' + e.message);
        return;
      }
      if (!parts || !parts.length) { toast('The file contains no rows'); return; }
      global.SMD_DB.parts = global.SMD_DB.parts.concat(parts);
      global.SMD_DB.meta.count = global.SMD_DB.parts.length;
      saveExtraParts(parts);
      renderDbInfo();
      toast('Imported ' + parts.length + ' rows (' + global.SMD_DB.meta.count + ' total)');
      if (state.query) doSearch();
    };
    reader.readAsText(file);
  }

  /* ------------------------------------------------------------- filters UI */

  function buildPackageFilter() {
    var names = (global.SMD_PACKAGES || []).map(function (p) { return p.name; });
    var uniq = [];
    names.forEach(function (p) { if (uniq.indexOf(p) === -1) uniq.push(p); });
    uniq.sort();
    el.pkgSelect.innerHTML = '<option value="all">All Packages</option>' +
      uniq.map(function (p) {
        return '<option value="' + esc(p) + '">' + esc(p) + '</option>';
      }).join('');

    el.pkgChips.innerHTML = QUICK_PKGS.map(function (p) {
      return '<button class="chip" data-pkg="' + esc(p) + '">' + esc(p) + '</button>';
    }).join('') + '<button class="chip chip-ghost" data-pkg="all">Clear package</button>';

    el.pkgChips.addEventListener('click', function (e) {
      var b = e.target.closest('[data-pkg]');
      if (b) setPackage(b.dataset.pkg);
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

  /** Hint for the selected package: pins, body size, soldering note. */
  function renderPkgMeta(pkg) {
    var meta = pkgMeta(pkg);
    el.pkgMeta.innerHTML = !meta ? ''
      : (meta.pins ? meta.pins + ' pins · ' : '') +
        (meta.body ? esc(meta.body) + ' mm · ' : '') + esc(meta.note);
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
    el.dbInfo.innerHTML = 'Database: <b>' + m.count + '</b> parts · version ' + esc(m.version) +
      ' · updated ' + esc(m.updated) + '<br><span class="muted">' + esc(m.disclaimer) + '</span>';
  }

  /* ---------------------------------------------------------------- events */

  function bindEvents() {
    var t = null;
    el.q.addEventListener('input', function () {
      clearTimeout(t);
      t = setTimeout(doSearch, 180);
    });
    el.q.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { clearTimeout(t); doSearch(); }
    });
    el.btnSearch.addEventListener('click', doSearch);
    el.pkgSelect.addEventListener('change', function () { setPackage(el.pkgSelect.value); });
    el.btnCamera.addEventListener('click', toggleOcrPanel);

    el.results.addEventListener('click', function (e) {
      var card = e.target.closest('[data-idx]');
      if (card) selectResult(parseInt(card.dataset.idx, 10));
    });

    el.detail.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn || !state.selected) return;
      var part = state.selected;
      var act = btn.dataset.act;
      if (act === 'pdf') openPdf(part);
      else if (act === 'search') openBrowserSearch(part);
      else if (act === 'share') share(part);
      else if (act === 'copy') copyText(part.part, 'Part number copied');
      else if (act === 'rag') openInRag(part);
      else if (act === 'card') openCard(part.part);
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

    el.btnRagSearch.addEventListener('click', function () { runRagSearch(); });
    el.ragQ.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') runRagSearch();
    });
    el.ragPart.addEventListener('change', function () {
      state.rag.part = el.ragPart.value;
      if (el.ragQ.value.trim()) runRagSearch();
    });

    el.btnIngestPath.addEventListener('click', startIngestByPath);
    el.folderPath.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') startIngestByPath();
    });
    el.btnPickFolder.addEventListener('click', function () { el.folderPicker.click(); });
    el.folderPicker.addEventListener('change', function () {
      var files = Array.prototype.filter.call(el.folderPicker.files || [], function (f) {
        return /\.pdf$/i.test(f.name);
      });
      state.ingest.picked = files;
      el.pickInfo.textContent = files.length
        ? files.length + ' PDF(s) selected'
        : 'No PDF found in that folder';
      el.btnIngestUpload.disabled = !files.length;
    });
    el.btnIngestUpload.addEventListener('click', startIngestUpload);
    el.btnIngestCancel.addEventListener('click', function () {
      if (!state.ingest.job) return;
      apiPost('/api/ingest/cancel', { id: state.ingest.job }).then(function () {
        toast('Cancelling after the current file…');
      });
    });

    el.btnCardSearch.addEventListener('click', function () { loadCards(true); });
    el.cardQ.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadCards(true);
    });
    el.cardPkg.addEventListener('change', function () { loadCards(true); });
    el.cardMfr.addEventListener('change', function () { loadCards(true); });
    el.btnCardMore.addEventListener('click', function () { loadCards(false); });
    el.cardGrid.addEventListener('click', function (e) {
      var card = e.target.closest('[data-card]');
      if (card) openCard(card.dataset.card);
    });
    el.cardModal.addEventListener('click', function (e) {
      if (e.target.closest('[data-close]')) closeCard();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !el.cardModal.classList.contains('hidden')) closeCard();
    });

    el.btnImport.addEventListener('click', function () { el.importInput.click(); });
    el.importInput.addEventListener('change', function () {
      if (el.importInput.files && el.importInput.files[0]) handleImport(el.importInput.files[0]);
      el.importInput.value = '';
    });
  }

  /* ----------------------------------------------------------------- search */

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
      ? ('Found: ' + res.results.length)
      : 'Nothing found';

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
          '<div class="card-code">code: <b>' + esc(r.matchedCode) + '</b>' +
            (r.via && r.via !== 'direct search' ? ' <span class="via">(' + esc(r.via) + ')</span>' : '') +
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
    if (s.usedVariant) {
      parts.push('confusable characters: “' + esc(res.normalized) + '” → “' + esc(s.usedVariant) + '”');
    }
    if (s.usedTruncation) {
      parts.push('stripped lot code “' + esc(s.usedTruncation.cut) + '”');
    }
    s.attempts.forEach(function (a) { parts.push(esc(a.label) + ': ' + a.hits + ' hits'); });
    el.strategy.innerHTML = parts.length
      ? '<span class="muted">Search strategy:</span> ' + parts.join(' · ')
      : '';
  }

  function renderEmpty(res) {
    el.empty.classList.remove('hidden');
    var sug = (res && res.suggestions && res.suggestions.length) ? res.suggestions : [];
    el.didYouMean.innerHTML = sug.length
      ? '<div class="dym-title">Did you mean:</div>' +
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

  /* ------------------------------------------------------------- detail */

  function pkgMeta(name) {
    var list = global.SMD_PACKAGES || [];
    for (var i = 0; i < list.length; i++) if (list[i].name === name) return list[i];
    return null;
  }

  /**
   * Marking codes with a reliability badge. A code may be stored as a string or
   * as { c: 'A7', conf: 'low' } when one code is well established and another
   * belongs to a single vendor.
   */
  function renderMarkings(p) {
    var list = p.markings || [];
    if (!list.length) {
      return '<span class="muted">no code — marked by colour band or full part number</span>';
    }
    return list.map(function (m) {
      var code = (m && typeof m === 'object') ? m.c : m;
      var conf = (m && typeof m === 'object') ? (m.conf || p.conf) : p.conf;
      var cls = conf === 'high' ? 'code-high' : conf === 'low' ? 'code-low' : 'code-med';
      var title = conf === 'high' ? 'code is stable across manufacturers'
                : conf === 'low' ? 'vendor-specific code, conflicts known'
                : 'widely used code, some exceptions';
      return '<code class="' + cls + '" title="' + esc(title) + '">' + esc(code) + '</code>';
    }).join(' ');
  }

  function renderDetail(p, r) {
    if (!p) {
      el.detailBody.innerHTML = '<div class="ph">Select a candidate on the left</div>';
      return;
    }
    var meta = pkgMeta(p.pkg);
    var confCls = r.score >= 85 ? 'high' : r.score >= 60 ? 'mid' : 'low';
    var confLabel = r.score >= 85 ? 'high'
                  : r.score >= 60 ? 'medium — verify before use'
                  : 'low — you almost certainly need the datasheet';

    el.detailBody.innerHTML = '' +
      '<div class="d-head">' +
        '<div class="d-part">' + esc(p.part) + '</div>' +
        '<div class="d-sub">' + esc(p.type || '') + ' · ' + esc(p.mfr || 'manufacturer not stated') + '</div>' +
      '</div>' +
      '<div class="d-grid">' +
        row('Package', esc(p.pkg) + (meta && meta.body
            ? ' <span class="muted">(' + esc(meta.body) + ' mm' +
              (meta.pins ? ', ' + meta.pins + ' pins' : '') + ')</span>' : '')) +
        row('Manufacturer', esc(p.mfr || '—')) +
        row('Type / Function', esc(p.type || '—')) +
        row('Description', esc(p.desc || '—')) +
        row('Voltage', esc(p.v || '—')) +
        row('Current', esc(p.i || '—')) +
        row('Pinout', esc(p.pins || '—')) +
        row('Marking codes', renderMarkings(p)) +
      '</div>' +
      (meta && meta.note ? '<div class="d-note"><b>Package:</b> ' + esc(meta.note) + '</div>' : '') +
      (p.note ? '<div class="d-note d-warn"><b>Important:</b> ' + esc(p.note) + '</div>' : '') +
      '<div class="d-conf">Confidence: <b class="conf-' + confCls + '">' + (r ? r.score : 0) +
        '%</b> <span class="muted">(' + confLabel + ')</span></div>' +
      '<div class="d-reasons"><b>Match reasons:</b><ul>' +
        (r ? r.reasons.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') : '<li>—</li>') +
      '</ul></div>' +
      '<div class="d-actions">' +
        '<button class="btn btn-primary" data-act="rag">Search datasheet</button>' +
        '<button class="btn" data-act="card">Open card</button>' +
        '<button class="btn" data-act="pdf">Datasheet (PDF)</button>' +
        '<button class="btn" data-act="search">Browser Search</button>' +
        '<button class="btn" data-act="share">Share</button>' +
        '<button class="btn" data-act="copy">Copy Part Number</button>' +
      '</div>' +
      '<div class="d-verify">Before replacing: check the pinout with a multimeter, the ' +
        'voltage and current ratings, and the pinout used by that exact manufacturer.</div>';

    function row(k, v) {
      return '<div class="d-row"><span class="d-k">' + k + '</span><span class="d-v">' + v + '</span></div>';
    }
  }

  /* -------------------------------------------------------------- actions */

  function openPdf(p) {
    if (!el.optOnline.checked) {
      toast('Online search is off, but a datasheet is only available online');
    }
    window.open('https://www.google.com/search?q=' +
      encodeURIComponent(p.part + ' datasheet filetype:pdf'), '_blank', 'noopener');
  }

  function openBrowserSearch(p) {
    var q = p.part + ' ' + (p.pkg || '') + ' SMD marking';
    window.open('https://duckduckgo.com/?q=' + encodeURIComponent(q), '_blank', 'noopener');
  }

  /** Hand the identified part over to the datasheet engine. */
  function openInRag(p) {
    // A hard part filter beats keywords: if the part is in the index we filter
    // on it, so "absolute maximum ratings" can never pull in another device.
    var opt = findPartOption(p.part);
    if (opt) {
      el.ragPart.value = p.part;
      state.rag.part = p.part;
      el.ragQ.value = 'absolute maximum ratings pin configuration';
    } else {
      el.ragPart.value = '';
      state.rag.part = '';
      el.ragQ.value = p.part + ' absolute maximum ratings';
    }
    runRagSearch();
    if (el.ragQ.scrollIntoView) {
      el.ragQ.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    if (!state.rag.ready) {
      toast('Datasheet index is not reachable — see the instructions below');
    }
  }

  function findPartOption(part) {
    if (!el.ragPart) return null;
    var want = String(part || '').toUpperCase();
    for (var i = 0; i < el.ragPart.options.length; i++) {
      if (el.ragPart.options[i].value.toUpperCase() === want) return el.ragPart.options[i];
    }
    return null;
  }

  function share(p) {
    var url = location.origin + location.pathname + '?q=' + encodeURIComponent(p.part);
    var data = { title: p.part, text: p.part + ' — ' + (p.desc || ''), url: url };
    if (navigator.share) {
      navigator.share(data).catch(function () {});
    } else {
      copyText(url, 'Link copied to clipboard');
    }
  }

  function copyText(text, msg) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { toast(msg); },
        function () { toast('Could not copy'); });
    } else {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast(msg); }
      catch (e) { toast('Could not copy'); }
      document.body.removeChild(ta);
    }
  }

  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.toast.classList.remove('show'); }, 2600);
  }

  /* -------------------------------------------------------------- history */

  function pushHistory(q) {
    if (!q) return;
    state.history = state.history.filter(function (x) {
      return x.toLowerCase() !== q.toLowerCase();
    });
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
    el.history.innerHTML = '<span class="muted">History:</span> ' +
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
      requestAnimationFrame(resetCropBox);   // stage has real dimensions now
    } else {
      global.SMDOCR.camera.stop();
    }
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('Camera unavailable: HTTPS or localhost is required. Use “Upload File”.');
      return;
    }
    setStatus('Starting camera…');
    global.SMDOCR.camera.start(el.video).then(function () {
      el.video.style.display = 'block';
      if (state.stillCanvas) state.stillCanvas.style.display = 'none';
      el.cropBox.style.display = state.cropMode === 'full' ? 'none' : 'block';
      el.btnAnalyze.disabled = false;
      setStatus('Camera is live. Frame the component and press “Capture Image”.');
      resetCropBox();
    }).catch(function (err) {
      setStatus('Camera error: ' + err.message + ' — you can upload a photo instead.');
    });
  }

  function switchCamera() {
    var f = global.SMDOCR.camera.switchFacing();
    if (global.SMDOCR.camera.active()) {
      startCamera();
      setStatus('Camera: ' + (f === 'environment' ? 'rear' : 'front'));
    }
  }

  function currentSource() {
    if (state.stillCanvas && state.stillCanvas.style.display !== 'none') return state.stillCanvas;
    if (global.SMDOCR.camera.active()) return el.video;
    return null;
  }

  function captureFrame() {
    var src = currentSource();
    if (!src) { setStatus('Start the camera or upload an image first.'); return; }
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
    setStatus('Frame captured. Adjust the box and press “Analyze Current Crop”.');
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
      setStatus('Image loaded (' + c.width + '×' + c.height + '). Press “Analyze Current Crop”.');
      URL.revokeObjectURL(url);
    };
    img.onerror = function () {
      setStatus('Could not open the image');
      URL.revokeObjectURL(url);
    };
    img.src = url;
  }

  function setStatus(msg) { el.ocrStatus.textContent = msg; }

  function analyzeFrame() {
    var src = currentSource();
    if (!src) { setStatus('No source: start the camera or upload a file.'); return; }
    if (state.ocrBusy) return;
    state.ocrBusy = true;
    el.btnAnalyze.disabled = true;
    setStatus('Preparing the image…');

    // A box the user dragged beats the crop mode, otherwise "Auto Crop" would
    // silently ignore the manual selection.
    var manual = !!(state.cropRect && state.cropRect.manual);
    var mode = manual ? 'manual' : state.cropMode;

    var rect = null;
    if (mode !== 'full' && state.cropRect) {
      var srcW = src.videoWidth || src.width;
      var dispW = src.clientWidth || src.width;
      var k = srcW / (dispW || srcW);
      rect = {
        x: state.cropRect.x * k, y: state.cropRect.y * k,
        w: state.cropRect.w * k, h: state.cropRect.h * k
      };
    }

    global.SMDOCR.analyze(src, { crop: mode, preset: state.preset, rect: rect }, setStatus)
      .then(function (out) {
        renderOcrResults(out);
        el.btnAnalyze.disabled = false;
        state.ocrBusy = false;
      })
      .catch(function (err) {
        setStatus('OCR error: ' + err.message);
        el.btnAnalyze.disabled = false;
        state.ocrBusy = false;
      });
  }

  function renderOcrResults(out) {
    var items = out.results.filter(function (r) { return r.cleaned; });
    if (!items.length) {
      setStatus('Nothing recognised. Move closer, add side lighting and retry.');
      el.ocrResults.innerHTML = '';
      return;
    }
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

    setStatus('Recognised ' + items.length + ' variant(s). Pick one or correct it by hand.');
    el.ocrResults.innerHTML =
      '<div class="ocr-title">OCR results — click to search that code</div>' +
      items.map(function (r) {
        return '<button class="ocr-item" data-use="' + esc(r.cleaned) + '">' +
          '<code>' + esc(r.cleaned) + '</code>' +
          '<span class="ocr-conf">OCR ' + Math.round(r.conf) + '%</span>' +
          (r.match
            ? '<span class="ocr-match">→ ' + esc(r.match.part.part) + ' (' + r.match.score + '%)</span>'
            : '<span class="ocr-nomatch">not in database</span>') +
          '<span class="ocr-label">' + esc(r.label) + '</span>' +
        '</button>';
      }).join('');

    // fill the search box but do not search silently: the user must see what
    // the engine actually read
    if (items[0]) el.q.value = items[0].cleaned;
  }

  /* -------------------------------------------------------------- crop box */

  function initCropBox() {
    var box = el.cropBox;
    var drag = null;

    function point(e) { return e.touches ? e.touches[0] : e; }

    function onDown(e) {
      if (state.cropMode === 'full') return;
      if (!state.cropRect) {
        state.cropRect = {
          x: box.offsetLeft, y: box.offsetTop, w: box.offsetWidth, h: box.offsetHeight
        };
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
    if (!maxW || !maxH) return;   // panel still hidden — do not zero the rect
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
    state.cropRect = {
      x: (stage.clientWidth - w) / 2, y: (stage.clientHeight - h) / 2, w: w, h: h
    };
    applyCropRect();
  }

  window.addEventListener('resize', function () {
    if (state.cropRect) applyCropRect();
  });

  /* ------------------------------------------------------------------ RAG */

  function buildRagSections() {
    el.ragSections.innerHTML = RAG_SECTIONS.map(function (s) {
      return '<button class="chip' + (s[0] === '' ? ' chip-on' : '') +
             '" data-section="' + esc(s[0]) + '">' + esc(s[1]) + '</button>';
    }).join('');
    el.ragSections.addEventListener('click', function (e) {
      var b = e.target.closest('[data-section]');
      if (!b) return;
      state.rag.section = b.dataset.section;
      Array.prototype.forEach.call(el.ragSections.querySelectorAll('[data-section]'), function (x) {
        x.classList.toggle('chip-on', x === b);
      });
      if (el.ragQ.value.trim()) runRagSearch();
    });
  }

  function initRag() {
    if (typeof fetch !== 'function') {   // no network API: degrade quietly
      ragUnavailable('This browser has no fetch() — the datasheet index needs the HTTP API');
      return;
    }
    apiGet('/api/health').then(function (data) {
      if (!data || !data.ok) throw new Error('bad response');
      state.rag.ready = data.indexed;
      renderRagStatus(data.stats);
      if (!data.indexed) {
        el.ragHelp.classList.remove('hidden');
        return;
      }
      return apiGet('/api/parts').then(function (p) {
        var parts = (p && p.parts) || [];
        el.ragPart.innerHTML = '<option value="">All parts</option>' +
          parts.map(function (x) {
            var label = x.part + (x.manufacturer ? ' — ' + x.manufacturer : '');
            return '<option value="' + esc(x.part) + '">' + esc(label) + '</option>';
          }).join('');
      });
    }).catch(function () {
      ragUnavailable('Datasheet index unavailable');
    });
  }

  function ragUnavailable(msg) {
    state.rag.ready = false;
    el.ragStatus.textContent = msg;
    el.ragHelp.classList.remove('hidden');
  }

  function renderRagStatus(stats) {
    if (!stats) return;
    var mode = stats.vectors ? 'hybrid (BM25 + vectors)' : 'BM25';
    el.ragStatus.innerHTML = 'Index: <b>' + stats.docs + '</b> datasheets · <b>' +
      stats.chunks + '</b> chunks · <b>' + stats.tables + '</b> tables · mode: ' + esc(mode) +
      (stats.built_at ? ' · built ' + esc(stats.built_at) : '');
  }

  function runRagSearch() {
    var q = el.ragQ.value.trim();
    if (!q) { toast('Enter a question about the datasheets'); return; }
    if (typeof fetch !== 'function') { ragUnavailable('No fetch() — cannot reach the index'); return; }
    if (state.rag.busy) return;
    state.rag.busy = true;
    el.btnRagSearch.disabled = true;
    el.ragResults.innerHTML = '<div class="rag-loading">Searching…</div>';

    var url = '/api/search?q=' + encodeURIComponent(q) +
      (state.rag.part ? '&part=' + encodeURIComponent(state.rag.part) : '') +
      (state.rag.section ? '&section=' + encodeURIComponent(state.rag.section) : '') +
      '&k=8';

    apiGet(url).then(function (data) {
      state.rag.busy = false;
      el.btnRagSearch.disabled = false;
      renderRagResults(data);
    }).catch(function (err) {
      state.rag.busy = false;
      el.btnRagSearch.disabled = false;
      el.ragResults.innerHTML = '';
      el.ragHelp.classList.remove('hidden');
      toast('Datasheet search failed: ' + err.message);
    });
  }

  function renderRagResults(data) {
    var results = (data && data.results) || [];
    if (!results.length) {
      el.ragResults.innerHTML =
        '<div class="rag-none">No passage found. Try other words, or clear the part filter.</div>';
      return;
    }
    el.ragHelp.classList.add('hidden');
    el.ragResults.innerHTML = '<div class="rag-count">' + results.length +
      ' passage(s) · ' + esc(data.mode || 'bm25') + '</div>' +
      results.map(function (r) {
        var body = r.snippet ? highlight(r.snippet) : esc(trimText(r.text));
        var link = r.filename
          ? ' <a class="rag-pdf" href="/pdfs/' + encodeURIComponent(r.filename) +
            (r.page ? '#page=' + r.page : '') + '" target="_blank" rel="noopener">Open PDF' +
            (r.page ? ' (p.' + r.page + ')' : '') + '</a>'
          : '';
        return '<div class="rag-item">' +
          '<div class="rag-item-head">' +
            '<span class="rag-part">' + esc(r.part || '—') + '</span>' +
            '<span class="rag-section">' + esc(r.section_label || r.section || '') + '</span>' +
            (r.manufacturer ? '<span class="rag-mfr">' + esc(r.manufacturer) + '</span>' : '') +
            (r.package ? '<span class="rag-tag">' + esc(r.package) + '</span>' : '') +
            (r.is_table ? '<span class="rag-tag rag-tag-table">table</span>' : '') +
          '</div>' +
          '<div class="rag-body">' + body + '</div>' +
          (r.summary ? '<div class="rag-summary">' + esc(trimText(r.summary, 220)) + '</div>' : '') +
          '<div class="rag-foot">' + esc(r.filename || '') + link + '</div>' +
        '</div>';
      }).join('');
  }

  /** FTS5 marks hits as <<term>>; turn them into <mark> safely. */
  function highlight(snippet) {
    return esc(snippet).replace(/&lt;&lt;/g, '<mark>').replace(/&gt;&gt;/g, '</mark>');
  }

  function trimText(text, max) {
    max = max || 320;
    var t = String(text || '').replace(/\s+/g, ' ').trim();
    return t.length > max ? t.slice(0, max) + '…' : t;
  }

  function apiPost(path, body, asJson) {
    var opts = { method: 'POST' };
    if (asJson) {
      opts.headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
      opts.body = JSON.stringify(body || {});
    } else {
      opts.body = body;                       // FormData: the browser sets the boundary
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  function apiGet(path) {
    return fetch(path, { headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }


  /* --------------------------------------------------------------- cards */

  function cardUnavailable(msg) {
    state.cards.ready = false;
    el.cardStatus.textContent = msg;
    el.cardHelp.classList.remove('hidden');
  }

  function initCards() {
    if (typeof fetch !== 'function') {
      cardUnavailable('This browser has no fetch() — cards need the HTTP API');
      return;
    }
    apiGet('/api/cards/stats').then(function (st) {
      state.cards.ready = (st.cards || 0) > 0;
      fillFacets('cardPkg', (st.facets && st.facets.packages) || [], 'All packages');
      fillFacets('cardMfr', (st.facets && st.facets.manufacturers) || [], 'All manufacturers');
      if (!state.cards.ready) {
        cardUnavailable('No cards extracted yet — ' + (st.cards || 0) + ' in the database');
        return;
      }
      el.cardHelp.classList.add('hidden');
      loadCards(true);
    }).catch(function () {
      cardUnavailable('Card database unavailable — see the instructions below');
    });
  }

  function fillFacets(id, items, allLabel) {
    var sel = el[id];
    if (!sel) return;
    sel.innerHTML = '<option value="">' + esc(allLabel) + '</option>' +
      items.map(function (f) {
        return '<option value="' + esc(f.name) + '">' + esc(f.name) +
               ' (' + f.count + ')</option>';
      }).join('');
  }

  function loadCards(reset) {
    var c = state.cards;
    if (c.busy) return;
    c.busy = true;
    if (reset) {
      c.offset = 0;
      el.cardGrid.innerHTML = '<div class="rag-loading">Loading…</div>';
    }
    c.q = el.cardQ.value.trim();
    c.pkg = el.cardPkg.value;
    c.mfr = el.cardMfr.value;

    var url = '/api/cards?limit=' + c.limit + '&offset=' + c.offset +
      (c.q ? '&q=' + encodeURIComponent(c.q) : '') +
      (c.pkg ? '&pkg=' + encodeURIComponent(c.pkg) : '') +
      (c.mfr ? '&mfr=' + encodeURIComponent(c.mfr) : '');

    apiGet(url).then(function (data) {
      c.busy = false;
      c.total = data.total || 0;
      var items = data.results || [];
      renderCards(items, reset);
      c.offset = (data.offset || 0) + items.length;
      el.btnCardMore.hidden = c.offset >= c.total;
      el.cardStatus.innerHTML = c.total
        ? 'Cards: <b>' + c.total + '</b> shown ' + Math.min(c.offset, c.total) +
          (c.q ? ' · query “' + esc(c.q) + '”' : '') +
          (c.pkg ? ' · ' + esc(c.pkg) : '') + (c.mfr ? ' · ' + esc(c.mfr) : '')
        : 'No cards match the filters';
    }).catch(function (err) {
      c.busy = false;
      el.cardGrid.innerHTML = '';
      el.cardStatus.textContent = 'Card search failed: ' + err.message;
    });
  }

  /* Every card renders the same four rows — part, facts, description,
     parameters — even when the PDF gave us nothing. A grid where some cards
     have a package chip and some have no chip at all reads as a broken
     parser; a grid where the missing chip says "package unknown" reads as
     an honest datasheet that never named its package. */
  function renderCards(items, reset) {
    if (!items.length && reset) {
      el.cardGrid.innerHTML = '<div class="rag-none">Nothing found. Try a shorter query.</div>';
      return;
    }
    var html = items.map(function (c) {
      var cls = c.confidence >= 0.8 ? 'good' : c.confidence >= 0.5 ? 'mid' : 'low';
      var specs = (c.headline || []).slice(0, 4).map(function (h) {
        return '<span class="spec-chip">' + esc(h.label) + ' <b>' + esc(h.text) + '</b></span>';
      }).join('');
      var chips = [
        c.package ? '<span class="pcard-chip">' + esc(c.package) + '</span>' : ghost('package unknown'),
        c.pin_count ? '<span class="pcard-chip">' + c.pin_count + ' pins</span>' : ghost('no pinout'),
        c.family ? '<span class="pcard-chip">' + esc(c.family) + '</span>' : '',
        c.pages ? '<span class="pcard-chip">' + c.pages + ' pages</span>' : ''
      ].filter(Boolean).join('');
      var flags = c.flags || [];
      var warn = flagChip(flags);
      return '<article class="pcard" data-card="' + esc(c.part) + '">' +
        '<div class="pcard-head">' +
          '<span class="pcard-part">' + esc(c.part) + '</span>' +
          '<span class="pcard-mfr">' + esc(c.manufacturer || 'manufacturer unknown') + '</span>' +
          '<span class="pcard-badge ' + cls + '">' +
            Math.round((c.confidence || 0) * 100) + '% extracted</span>' +
        '</div>' +
        '<div class="pcard-chips">' + chips + warn + '</div>' +
        '<div class="pcard-desc' + (c.description ? '' : ' pcard-none') + '">' +
          (c.description ? esc(c.description) : 'No description found in this datasheet') + '</div>' +
        '<div class="pcard-specs">' +
          (specs || ghost('no parameters extracted')) + '</div>' +
        '<div class="pcard-foot">' + esc(c.filename || '') + '</div>' +
      '</article>';
    }).join('');
    el.cardGrid.innerHTML = reset ? html : el.cardGrid.innerHTML + html;
  }

  function ghost(text) {
    return '<span class="pcard-chip pcard-chip-ghost">' + esc(text) + '</span>';
  }

  /* Why this card is thin. Flags are measured at parse time (no text layer,
     no tables); the wording here is what the engineer sees, so it says what
     to do about it, not just that something went wrong. */
  var FLAG_CHIP = {
    scan: 'scan — no text layer',
    low_text: 'almost no text',
    no_tables: 'no tables found'
  };
  function flagChip(flags) {
    for (var i = 0; i < flags.length; i++) {
      if (FLAG_CHIP[flags[i]]) {
        return '<span class="pcard-chip pcard-chip-warn">' + esc(FLAG_CHIP[flags[i]]) + '</span>';
      }
    }
    return '';
  }

  function openCard(part) {
    if (!part) return;
    if (typeof fetch !== 'function') { toast('Cards need the HTTP API'); return; }
    apiGet('/api/card?part=' + encodeURIComponent(part)).then(function (card) {
      el.cardModalBody.innerHTML = renderCardFull(card);
      el.cardModal.classList.remove('hidden');
    }).catch(function (err) {
      toast(err && err.message === 'HTTP 404'
        ? 'No card in the datasheet library for ' + part
        : 'Could not load the card: ' + err.message);
    });
  }

  function closeCard() {
    el.cardModal.classList.add('hidden');
  }

  function cardNotice(c) {
    var flags = c.flags || [];
    if (flags.indexOf('scan') >= 0) {
      return 'This PDF has no text layer — it is a scan, so almost nothing could be '
        + 'extracted. Re-run this file through Docling with OCR to fill the card in.';
    }
    if (flags.indexOf('low_text') >= 0) {
      return 'Only a few hundred characters of text could be read from this PDF. '
        + 'It is probably a drawing or a scan — OCR would recover the rest.';
    }
    if (flags.indexOf('no_tables') >= 0) {
      return 'No tables were recognised in this PDF. Pinout and electrical '
        + 'parameters live in tables, so this card is thin until the tables parse.';
    }
    return '';
  }

  function renderCardFull(c) {
    var out = '';
    out += '<div class="card-full-head">' +
      '<span class="card-full-part">' + esc(c.part) + '</span>' +
      '<span class="pcard-mfr">' + esc(c.manufacturer || 'manufacturer unknown') + '</span>' +
      (c.package ? '<span class="pcard-chip">' + esc(c.package) + '</span>' : '') +
      (c.family ? '<span class="pcard-chip">' + esc(c.family) + '</span>' : '') +
      '<span class="pcard-badge ' + (c.confidence >= 0.8 ? 'good' : c.confidence >= 0.5 ? 'mid' : 'low') +
        '">' + Math.round((c.confidence || 0) * 100) + '% extracted</span>' +
    '</div>';

    var notice = cardNotice(c);
    if (notice) out += '<div class="card-notice">' + esc(notice) + '</div>';

    if (c.description) out += '<div class="pcard-desc" style="font-size:14px">' + esc(c.description) + '</div>';

    if ((c.headline || []).length) {
      out += '<div class="card-section"><h4>Key parameters</h4><div class="pcard-specs">' +
        c.headline.map(function (h) {
          return '<span class="spec-chip">' + esc(h.label) + ' <b>' + esc(h.text) + '</b>' +
                 (h.page ? ' <span class="src">p.' + h.page + '</span>' : '') + '</span>';
        }).join('') + '</div></div>';
    }

    if ((c.pins || []).length) {
      out += '<div class="card-section"><h4>Pin configuration (' + c.pins.length + ')</h4>' +
        '<div class="pcard-pins">' + c.pins.map(function (p) {
          var title = [p.name, p.function].filter(Boolean).join(' — ');
          return '<span class="pin-pill" title="' + esc(title) + '"><b>' + esc(p.n) + '</b> ' +
                 esc(p.name || '') + '</span>';
        }).join('') + '</div></div>';
    }

    out += specTable('Absolute maximum ratings', c.ratings, ['Symbol', 'Parameter', 'Value', 'Page'],
      function (r) {
        return '<td class="mono">' + esc(r.symbol) + '</td><td>' + esc(r.param) +
               '</td><td class="num">' + esc(r.text) + '</td><td class="src">' + (r.page || '') + '</td>';
      });

    out += specTable('Electrical characteristics', c.specs,
      ['Symbol', 'Parameter', 'Conditions', 'Min', 'Typ', 'Max', 'Unit', 'Page'],
      function (r) {
        return '<td class="mono">' + esc(r.symbol) + '</td><td>' + esc(r.param) +
               '</td><td class="src">' + esc(r.conditions || '') +
               '</td><td class="num">' + esc(r.min || '') + '</td><td class="num">' + esc(r.typ || '') +
               '</td><td class="num">' + esc(r.max || '') + '</td>' +
               '<td class="src">' + esc(r.unit || '') + '</td>' +
               '<td class="src">' + (r.page || '') + '</td>';
      });

    if ((c.features || []).length) {
      out += '<div class="card-section"><h4>Features</h4><ul class="card-list">' +
        c.features.map(function (f) { return '<li>' + esc(f) + '</li>'; }).join('') + '</ul></div>';
    }
    if ((c.applications || []).length) {
      out += '<div class="card-section"><h4>Applications</h4><ul class="card-list">' +
        c.applications.map(function (f) { return '<li>' + esc(f) + '</li>'; }).join('') + '</ul></div>';
    }

    var dims = c.dimensions || {};
    var dimKeys = Object.keys(dims);
    if (dimKeys.length) {
      out += '<div class="card-section"><h4>Package dimensions</h4><div class="card-dims">' +
        dimKeys.map(function (k) {
          var d = dims[k];
          return '<span class="pcard-chip">' + esc(k.replace(/_/g, ' ')) + ': <b>' +
                 d.value + ' ' + esc(d.unit) + '</b></span>';
        }).join('') + '</div></div>';
    }

    if ((c.order_codes || []).length) {
      out += '<div class="card-section"><h4>Order codes and markings</h4><div class="pcard-pins">' +
        c.order_codes.map(function (o) {
          return '<span class="pin-pill"><b>' + esc(o.code) + '</b>' +
                 (o.package ? ' ' + esc(o.package) : '') +
                 (o.marking ? ' · ' + esc(o.marking) : '') + '</span>';
        }).join('') + '</div></div>';
    }

    out += '<div class="card-section"><h4>Source</h4><div class="pcard-foot">' +
      esc(c.filename || '') + ' · ' + (c.pages || 0) + ' pages · ' + (c.tables || 0) +
      ' tables · parsed by ' + esc(c.parser || '?') +
      (c.filename ? ' <a class="rag-pdf" href="/pdfs/' + encodeURIComponent(c.filename) +
        '" target="_blank" rel="noopener">Open PDF</a>' : '') +
      '</div></div>';

    return out;

    function specTable(title, rows, headers, rowFn) {
      if (!rows || !rows.length) return '';
      return '<div class="card-section"><h4>' + title + ' (' + rows.length + ')</h4>' +
        '<table class="card-table"><thead><tr>' +
        headers.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
        '</tr></thead><tbody>' +
        rows.map(function (r) { return '<tr>' + rowFn(r) + '</tr>'; }).join('') +
        '</tbody></table></div>';
    }
  }


  /* -------------------------------------------------------------- ingest */

  function initIngest() {
    if (typeof fetch !== 'function') {
      el.ingestHint.textContent =
        'This browser has no fetch() — ingestion needs the HTTP API.';
      return;
    }
    el.folderPath.value = el.folderPath.value || '';
    apiGet('/api/ingest/status').then(function (st) {
      // a reload in the middle of a run should keep showing that run
      if (st && st.id && st.state === 'running') {
        state.ingest.job = st.id;
        startPolling();
      }
    }).catch(function () { /* no API: the panel simply stays idle */ });
  }

  function startIngestByPath() {
    var folder = el.folderPath.value.trim();
    if (!folder) { toast('Enter the path to a folder of PDFs'); return; }
    el.ingestHint.textContent = '';
    apiPost('/api/ingest/path',
      { path: folder, recursive: el.optRecursive.checked }, true)
      .then(function (data) { onIngestStarted(data, 'Parsing ' + folder); })
      .catch(function (err) {
        el.ingestHint.textContent = 'Could not start: ' + err.message;
        toast('Ingestion failed to start: ' + err.message);
      });
  }

  function startIngestUpload() {
    var files = state.ingest.picked;
    if (!files.length) { toast('Choose a folder first'); return; }
    var form = new FormData();
    files.forEach(function (f) { form.append('files[]', f, f.name); });
    el.ingestHint.textContent = 'Uploading ' + files.length + ' file(s)…';
    apiPost('/api/ingest/upload', form, false)
      .then(function (data) { onIngestStarted(data, 'Parsing uploaded files'); })
      .catch(function (err) {
        el.ingestHint.textContent = 'Upload failed: ' + err.message;
        toast('Upload failed: ' + err.message);
      });
  }

  function onIngestStarted(data, message) {
    state.ingest.job = data.job_id;
    state.ingest.busy = true;
    el.btnIngestPath.disabled = true;
    el.btnIngestUpload.disabled = true;
    el.ingestProgress.classList.remove('hidden');
    el.ingestHint.textContent = message + ' — ' + data.files + ' file(s)';
    el.ingestErrors.innerHTML = '';
    startPolling();
  }

  function startPolling() {
    stopPolling();
    state.ingest.timer = setInterval(pollIngest, 700);
    pollIngest();
  }

  function stopPolling() {
    if (state.ingest.timer) {
      clearInterval(state.ingest.timer);
      state.ingest.timer = null;
    }
  }

  function pollIngest() {
    var id = state.ingest.job;
    if (!id) { stopPolling(); return; }
    apiGet('/api/ingest/status?id=' + encodeURIComponent(id)).then(function (st) {
      renderIngest(st);
      if (st.state !== 'running' && st.state !== 'pending') {
        stopPolling();
        finishIngest(st);
      }
    }).catch(function () {
      stopPolling();                      // server gone: stop hammering it
    });
  }

  function renderIngest(st) {
    var pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
    el.ingestBar.style.width = Math.max(pct, st.state === 'running' ? 2 : 0) + '%';
    var bits = [
      '<b>' + st.done + '</b> / ' + st.total,
      st.ok + ' new',
      st.unchanged ? st.unchanged + ' unchanged' : null,
      st.skipped ? st.skipped + ' already indexed' : null,
      st.failed ? st.failed + ' failed' : null
    ].filter(Boolean);
    if (st.state === 'running') {
      if (st.rate) bits.push(st.rate.toFixed(1) + ' PDF/s');
      if (st.eta) bits.push('about ' + fmtDuration(st.eta) + ' left');
      if (st.current) bits.push('current: ' + esc(st.current));
    }
    el.ingestStatus.innerHTML = bits.join(' · ') +
      (st.message ? '<br><span class="muted">' + esc(st.message) + '</span>' : '');
    if (st.errors && st.errors.length) {
      el.ingestErrors.innerHTML = st.errors.map(function (e) {
        return '<div>' + esc(e.file) + ' — ' + esc(e.error) + '</div>';
      }).join('');
    }
  }

  function finishIngest(st) {
    state.ingest.busy = false;
    el.btnIngestPath.disabled = false;
    el.btnIngestUpload.disabled = !state.ingest.picked.length;
    var label = { done: 'Done', cancelled: 'Cancelled', error: 'Failed' }[st.state] || st.state;
    toast(label + ': ' + st.ok + ' card(s), ' + st.failed + ' failed');
    // the card database changed underneath us — pull the fresh view
    if (typeof fetch === 'function') {
      apiGet('/api/cards/stats').then(function (cs) {
        fillFacets('cardPkg', (cs.facets && cs.facets.packages) || [], 'All packages');
        fillFacets('cardMfr', (cs.facets && cs.facets.manufacturers) || [], 'All manufacturers');
        state.cards.ready = (cs.cards || 0) > 0;
        if (state.cards.ready) {
          el.cardHelp.classList.add('hidden');
          loadCards(true);
        }
      }).catch(function () {});
    }
  }

  function fmtDuration(seconds) {
    var s = Math.max(0, Math.round(seconds || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h) return h + 'h ' + (m < 10 ? '0' + m : m) + 'm';
    if (m) return m + 'm ' + (s % 60) + 's';
    return s + 's';
  }

  /* ------------------------------------------------------------------ boot */

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
