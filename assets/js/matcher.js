/* ============================================================================
 * matcher.js — the search core of SMD Component Finder
 * ----------------------------------------------------------------------------
 * The problem is not autocomplete. An SMD marking is 2–6 characters, lasered or
 * printed onto a tiny body, and OCR reads it wrong more often than not. Plain
 * "exact match" is useless here — the search has to survive typos and OCR noise.
 *
 * Three mechanisms:
 *   1. CONFUSION MATRIX — a weighted Levenshtein distance where swapping
 *      visually similar characters (O<->0, I<->1, S<->5, B<->8, Z<->2) costs
 *      0.2 instead of 1.0. This is exactly what the reference tool
 *      (smtinsider.com) lacks: it matches on substrings and shows
 *      "No results found" at the slightest mismatch.
 *   2. SUFFIX / PREFIX STRIPPING — if the full code is not found, cut the tail
 *      and the head: markings often carry a date/lot code. "1AM" -> "1A"
 *      (M = lot code), "KL3 4" -> "KL3".
 *   3. VARIANT GENERATION — enumerate substitutions of similar characters. An
 *      exact hit on a variant beats any fuzzy score, and we can then tell the
 *      user "corrected: O -> 0".
 * ========================================================================= */
(function (global) {
  'use strict';

  /* Visually similar character pairs — the key to surviving OCR output. */
  var CONFUSION = [
    ['O', '0'], ['0', 'O'],
    ['I', '1'], ['1', 'I'], ['L', '1'], ['1', 'L'], ['l', '1'],
    ['S', '5'], ['5', 'S'],
    ['Z', '2'], ['2', 'Z'],
    ['B', '8'], ['8', 'B'],
    ['G', '6'], ['6', 'G'],
    ['T', '7'], ['7', 'T'],
    ['q', '9'], ['9', 'q'], ['g', '9'],
    ['A', '4'], ['4', 'A'],
    ['D', '0'], ['0', 'D'],
    ['U', 'V'], ['V', 'U'],
    ['C', '0'], ['0', 'C'],
    ['E', 'F'], ['F', 'E'],
    ['P', 'R'], ['R', 'P'],
    ['M', 'N'], ['N', 'M'],
    ['.', ','], [',', '.'],
    ['-', '.'], ['.', '-']
  ];

  var CONFUSION_COST = 0.2; // cost of a "similar" substitution vs 1.0 for a normal one

  var CONFUSION_MAP = (function () {
    var m = {};
    CONFUSION.forEach(function (p) {
      (m[p[0]] = m[p[0]] || []).push(p[1]);
    });
    return m;
  })();

  /* ------------------------------------------------------------------ utils */

  /** Normalisation: upper case, no spaces or separators. */
  function normalize(s) {
    if (s == null) return '';
    return String(s)
      .toUpperCase()
      .replace(/[\s\-_/,]/g, '')
      .trim();
  }

  /** Plain Levenshtein distance. */
  function levenshtein(a, b) {
    var m = a.length, n = b.length;
    if (!m) return n;
    if (!n) return m;
    var prev = new Array(n + 1), cur = new Array(n + 1), i, j;
    for (j = 0; j <= n; j++) prev[j] = j;
    for (i = 1; i <= m; i++) {
      cur[0] = i;
      for (j = 1; j <= n; j++) {
        var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      }
      for (j = 0; j <= n; j++) prev[j] = cur[j];
    }
    return prev[n];
  }

  /**
   * Distance that accounts for visual similarity.
   * Swapping O for 0 costs 0.2, swapping O for X costs 1.0.
   */
  function confusionDistance(a, b) {
    var m = a.length, n = b.length;
    if (!m) return n;
    if (!n) return m;
    var prev = new Array(n + 1), cur = new Array(n + 1), i, j, k;
    for (j = 0; j <= n; j++) prev[j] = j;
    for (i = 1; i <= m; i++) {
      cur[0] = i;
      var ca = a.charAt(i - 1);
      for (j = 1; j <= n; j++) {
        var cb = b.charAt(j - 1);
        var cost;
        if (ca === cb) {
          cost = 0;
        } else {
          cost = 1;
          var alts = CONFUSION_MAP[ca];
          if (alts) {
            for (k = 0; k < alts.length; k++) {
              if (alts[k] === cb) { cost = CONFUSION_COST; break; }
            }
          }
        }
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      }
      for (j = 0; j <= n; j++) prev[j] = cur[j];
    }
    return prev[n];
  }

  /**
   * Enumerate variants of a string by substituting similar characters.
   * Capped, so that long codes cannot explode combinatorially.
   */
  function variants(str, maxResults) {
    maxResults = maxResults || 60;
    var out = [], seen = {};
    seen[str] = true;
    var queue = [str];
    while (queue.length && out.length < maxResults) {
      var cur = queue.shift();
      out.push(cur);
      for (var i = 0; i < cur.length; i++) {
        var alts = CONFUSION_MAP[cur.charAt(i)];
        if (!alts) continue;
        for (var k = 0; k < alts.length; k++) {
          var next = cur.substring(0, i) + alts[k] + cur.substring(i + 1);
          if (!seen[next]) { seen[next] = true; queue.push(next); }
        }
      }
    }
    return out;
  }

  /** "Stripped" variants: tail and head (lot/date codes). */
  function truncations(str) {
    var out = [];
    for (var len = str.length - 1; len >= 2; len--) {
      out.push({ query: str.substring(0, len), cut: str.substring(len), side: 'suffix' });
      out.push({ query: str.substring(str.length - len), cut: str.substring(0, str.length - len), side: 'prefix' });
    }
    return out;
  }

  /* ------------------------------------------------------------------ score */

  var CONF_WEIGHT = { high: 4, med: 0, low: -6 };

  /**
   * Rejects near-misses that cannot be an OCR error.
   * Without this threshold short codes produce absurd matches: KL3 suddenly
   * looks "similar" to 817, and 1007 to 74HC04.
   *
   * Rule: the difference must be at most 2.0 in absolute terms AND at most 30%
   * of the code length. For short SMD codes that is strict, but necessary — in
   * three characters any substitution changes the meaning.
   */
  function isPlausibleOcrError(q, c, cd) {
    if (cd > 2.0) return false;
    var ratio = cd / Math.max(q.length, c.length, 1);
    return ratio <= 0.30;
  }

  /**
   * Scores one candidate code (a marking or a part number) against the query.
   * Returns { score, reasons[] } or null.
   *
   * @param {string} codeConf — reliability of this very code. A marking can be
   *   more or less reliable than the part as a whole: for 1N4148W the primary
   *   code is T4, while A7 is secondary and means BAV99 at some Chinese fabs.
   *   Hence the database stores conf both per part and per individual code.
   */
  function scoreCode(query, code, codeKind, part, opts, codeConf) {
    if (!code) return null;
    var q = query, c = normalize(code);
    if (!q || !c) return null;

    var reasons = [];
    var score = 0;
    var d = levenshtein(q, c);
    var cd = confusionDistance(q, c);

    if (d === 0) {
      score = 100;
      reasons.push(codeKind === 'part' ? 'exact match on part number' : 'exact match on marking code');
    } else if (cd < d && isPlausibleOcrError(q, c, cd)) {
      // the difference is explained by similar characters — a classic OCR slip
      score = 93 - Math.round(cd * 6);
      if (score < 70) score = 70;
      reasons.push('match within confusable characters: “' + q + '” ~ “' + c + '”');
    } else if (c.indexOf(q) === 0 || q.indexOf(c) === 0) {
      var ratio = Math.min(q.length, c.length) / Math.max(q.length, c.length);
      score = Math.round(70 + 22 * ratio);
      reasons.push(codeKind === 'part' ? 'part number starts with the query' : 'marking code starts with the query');
    } else if (c.indexOf(q) !== -1 || q.indexOf(c) !== -1) {
      score = 66;
      reasons.push('contained in code: “' + q + '” ⊂ “' + c + '”');
    } else if (d === 1) {
      score = 78;
      reasons.push('differs by one character (distance 1): “' + q + '” / “' + c + '”');
    } else if (d === 2) {
      score = 58;
      reasons.push('differs by two characters (distance 2): “' + q + '” / “' + c + '”');
    } else {
      return null;
    }

    // short codes are inherently ambiguous — lower the confidence
    if (c.length <= 2) {
      score -= 8;
      reasons.push('short code (≤2 characters) — different vendors mean different parts');
    } else if (c.length === 3) {
      score -= 3;
    }

    // reliability of the record itself (the code, not the whole part)
    var w = CONF_WEIGHT[codeConf || part.conf] || 0;
    if (w > 0) { score += w; reasons.push('code is stable across manufacturers'); }
    if (w < 0) { score += w; reasons.push('vendor-specific code — verify against the datasheet'); }

    // the package matches the selected filter
    if (opts && opts.pkg && opts.pkg !== 'all') {
      if (normalize(part.pkg) === normalize(opts.pkg)) {
        score += 10;
        reasons.push('matches the selected package ' + part.pkg);
      } else if (pkgFamily(part.pkg) && pkgFamily(part.pkg) === pkgFamily(opts.pkg)) {
        score += 4;
        reasons.push('matches the package family');
      } else {
        score -= 5;
      }
    }

    if (score > 100) score = 100;
    if (score < 0) score = 0;
    return { score: score, reasons: reasons, matchedCode: code, codeKind: codeKind, dist: d, cDist: cd };
  }

  var FAMILY_HINT = {
    'SOT-23': /^SOT-23|^SOT-25|^SOT-26|^SC-59|^SOT-346|^TSOT-23|^SSOT|^TO-236/,
    'SC-70': /^SOT-323|^SOT-353|^SOT-363|^SOT-343|^SC-70|^SC-82|^SC-88/,
    'SOD': /^SOD-/,
    'DO-214': /^DO-214|^DO-215|^SMA|^SMB|^SMC/,
    'DFN': /^DFN|^WDFN|^UFN|^MLP/,
    'QFN': /^QFN|^LLP|^MLP/,
    'SON': /^SON|^HSON|^USPN|^USP-|^SNT|^HSNT/,
    'SOP': /^SOIC|^SOP-|^SSOP|^MSOP|^TSSOP|^HSOP/
  };

  function pkgFamily(pkg) {
    if (!pkg) return null;
    for (var fam in FAMILY_HINT) {
      if (FAMILY_HINT[fam].test(pkg)) return fam;
    }
    return null;
  }

  /* ------------------------------------------------------------------ search */

  /**
   * All codes of a part, normalised: markings plus the part number.
   * A marking is stored either as a string or as { c, conf }.
   */
  function codeList(part) {
    var out = (part.markings || []).map(function (m) {
      if (m && typeof m === 'object') {
        return { raw: m.c, norm: normalize(m.c), conf: m.conf || part.conf, kind: 'marking' };
      }
      return { raw: m, norm: normalize(m), conf: part.conf, kind: 'marking' };
    });
    out.push({ raw: part.part, norm: normalize(part.part), conf: part.conf, kind: 'part' });
    return out.filter(function (x) { return !!x.norm; });
  }

  /** Is there a part with exactly this code (for variants and stripping)? */
  function hasExactCode(norm) {
    var db = (global.SMD_DB && global.SMD_DB.parts) || [];
    for (var i = 0; i < db.length; i++) {
      var codes = codeList(db[i]);
      for (var j = 0; j < codes.length; j++) {
        if (codes[j].norm === norm) return true;
      }
    }
    return false;
  }


  /**
   * The main search.
   * @param {string} rawQuery  — what the user typed or what OCR returned
   * @param {object} opts      — { pkg: 'all' | 'SOT-23', limit: N }
   * @returns {object} { results: [...], suggestions: [...], strategy: {...} }
   */
  function search(rawQuery, opts) {
    opts = opts || {};
    var limit = opts.limit || 40;
    var q = normalize(rawQuery);
    var out = {
      query: rawQuery,
      normalized: q,
      results: [],
      suggestions: [],
      strategy: { attempts: [], usedVariant: null, usedTruncation: null }
    };
    if (!q) return out;

    var db = (global.SMD_DB && global.SMD_DB.parts) || [];
    var best = {}; // part|pkg -> best result

    function offer(part, res, via) {
      var key = part.part + '|' + part.pkg;
      var prev = best[key];
      if (!prev || res.score > prev.score) {
        best[key] = {
          part: part,
          score: res.score,
          reasons: res.reasons.slice(),
          matchedCode: res.matchedCode,
          codeKind: res.codeKind,
          via: via,
          dist: res.dist
        };
      }
    }

    function runPass(queryToTry, label, penalty) {
      var hits = 0;
      for (var i = 0; i < db.length; i++) {
        var part = db[i];
        var codes = (part.markings || []).map(function (m) {
          // a marking may be a string or an object { c: 'A7', conf: 'low' }
          return (m && typeof m === 'object')
            ? { code: m.c, kind: 'marking', conf: m.conf }
            : { code: m, kind: 'marking', conf: part.conf };
        });
        codes.push({ code: part.part, kind: 'part', conf: part.conf });
        for (var j = 0; j < codes.length; j++) {
          var res = scoreCode(queryToTry, codes[j].code, codes[j].kind, part, opts, codes[j].conf);
          if (!res) continue;
          if (penalty) {
            res.score = Math.max(0, res.score - penalty);
            res.reasons = res.reasons.slice();
            res.reasons.push(label);
          }
          offer(part, res, label);
          hits++;
        }
      }
      out.strategy.attempts.push({ query: queryToTry, label: label, hits: hits });
      return hits;
    }

    // Pass 1: the query as typed
    runPass(q, 'direct search', 0);

    // Pass 2: variants with similar characters substituted (OCR errors)
    if (q.length <= 8) {
      var vars = variants(q, 40);
      for (var v = 1; v < vars.length; v++) {
        var hit = hasExactCode(vars[v]);
        if (hit) {
          out.strategy.usedVariant = vars[v];
          runPass(vars[v], 'corrected “' + q + '” → “' + vars[v] + '”', 2);
          break;
        }
      }
    }

    // Pass 3: strip the lot/date code
    if (q.length >= 3) {
      var tr = truncations(q);
      for (var t = 0; t < tr.length; t++) {
        var cand = tr[t];
        var hit2 = hasExactCode(cand.query);
        if (hit2) {
          out.strategy.usedTruncation = cand;
          runPass(cand.query, 'stripped ' + (cand.side === 'suffix' ? 'suffix' : 'prefix') + ' “' + cand.cut + '”', 4);
          break;
        }
      }
    }

    var list = Object.keys(best).map(function (k) { return best[k]; });
    list.sort(function (a, b) {
      return b.score - a.score || a.part.part.localeCompare(b.part.part);
    });
    out.results = list.slice(0, limit);

    // "Did you mean": close codes that did not make the top
    if (out.results.length === 0) {
      var sug = [];
      for (var i2 = 0; i2 < db.length; i2++) {
        var p = db[i2];
        var codes2 = codeList(p);
        for (var j2 = 0; j2 < codes2.length; j2++) {
          var cc = codes2[j2].norm;
          var cd2 = confusionDistance(q, cc);
          if (cd2 > 0 && isPlausibleOcrError(q, cc, cd2)) {
            sug.push({ code: codes2[j2].raw, part: p.part, dist: cd2 });
          }
        }
      }
      sug.sort(function (a, b) { return a.dist - b.dist; });
      var seen = {};
      out.suggestions = sug.filter(function (s) {
        if (seen[s.code]) return false;
        seen[s.code] = true;
        return true;
      }).slice(0, 6);
    }

    return out;
  }

  /* ------------------------------------------------------------------ export */

  global.SMDMatcher = {
    search: search,
    normalize: normalize,
    levenshtein: levenshtein,
    confusionDistance: confusionDistance,
    variants: variants,
    truncations: truncations,
    pkgFamily: pkgFamily,
    CONFUSION_MAP: CONFUSION_MAP
  };
})(window);
