/* ============================================================================
 * ocr.js — камера, кадрирование, предобработка и распознавание маркировки
 * ----------------------------------------------------------------------------
 * Почему это сложнее, чем "скопировать картинку в tesseract":
 *
 *  1. Маркировка SMD — это МАЛЕНЬКИЙ текст (0.3–1 мм) на контрастном корпусе.
 *     Tesseract на кадре с телефона не увидит ничего: символы 8–15 пикселей.
 *     Нужно кадрировать и увеличивать в 4–6 раз.
 *  2. Лазерная маркировка на чёрном компаунде — это СВЕТЛЫЙ текст на ТЁМНОМ
 *     фоне. Tesseract ожидает обратного. Решение: бинаризация + инверсия,
 *     причём пробуем оба варианта и выбираем тот, что даёт лучший результат.
 *  3. Блики, размытие, низкий контраст. Решение: несколько вариантов
 *     предобработки (порог Оцу, локальный порог, агрессивное увеличение
 *     контраста) и прогон каждого. Затем результат прогоняется через matcher,
 *     который понимает типичные ошибки OCR (O↔0, I↔1, S↔5).
 *
 * Движок — tesseract.js, подгружается по сети ТОЛЬКО при первом использовании
 * OCR и ТОЛЬКО в браузере пользователя. Без OCR инструмент работает полностью
 * офлайн. Кадры никуда не отправляются, кроме локального WASM-движка.
 * ========================================================================= */
(function (global) {
  'use strict';

  var TESSERACT_CDN = 'https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js';
  var CHARSET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-';

  var state = {
    stream: null,
    facing: 'environment',
    worker: null,
    workerReady: false,
    loading: false
  };

  /* ------------------------------------------------------------ загрузка движка */

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.async = true;
      s.onload = resolve;
      s.onerror = function () { reject(new Error('Не удалось загрузить ' + src)); };
      document.head.appendChild(s);
    });
  }

  /**
   * Ленивая загрузка tesseract.js + инициализация worker.
   * @param {function} onProgress — колбэк для строки статуса
   */
  function ensureWorker(onProgress) {
    if (state.workerReady && state.worker) return Promise.resolve(state.worker);
    if (state.loading) {
      return new Promise(function (resolve, reject) {
        var t = setInterval(function () {
          if (state.workerReady) { clearInterval(t); resolve(state.worker); }
        }, 250);
        setTimeout(function () { clearInterval(t); reject(new Error('Таймаут загрузки OCR-движка')); }, 60000);
      });
    }
    state.loading = true;
    return Promise.resolve()
      .then(function () {
        if (global.Tesseract) return;
        if (onProgress) onProgress('Загружаю OCR-движок (первый раз ~10 МБ)…');
        return loadScript(TESSERACT_CDN);
      })
      .then(function () {
        if (!global.Tesseract) throw new Error('Tesseract не загрузился — проверьте доступ к CDN');
        if (onProgress) onProgress('Инициализирую распознавание…');
        return global.Tesseract.createWorker('eng', 1, {
          logger: function (m) {
            if (!onProgress || !m) return;
            if (m.status === 'recognizing text' && m.progress != null) {
              onProgress('Распознаю… ' + Math.round(m.progress * 100) + '%');
            } else if (m.status) {
              onProgress(m.status);
            }
          }
        });
      })
      .then(function (worker) {
        state.worker = worker;
        state.workerReady = true;
        state.loading = false;
        return worker;
      })
      .catch(function (err) {
        state.loading = false;
        throw err;
      });
  }

  /* ------------------------------------------------------------------- камера */

  var camera = {
    start: function (videoEl) {
      camera.stop();
      var constraints = {
        video: {
          facingMode: state.facing,
          width: { ideal: 1920 },
          height: { ideal: 1080 }
        },
        audio: false
      };
      return navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
        state.stream = stream;
        videoEl.srcObject = stream;
        return videoEl.play();
      });
    },
    stop: function () {
      if (state.stream) {
        state.stream.getTracks().forEach(function (t) { t.stop(); });
        state.stream = null;
      }
    },
    switchFacing: function () {
      state.facing = state.facing === 'environment' ? 'user' : 'environment';
      return state.facing;
    },
    facing: function () { return state.facing; },
    active: function () { return !!state.stream; }
  };

  /* ----------------------------------------------------------- предобработка */

  function grayFromImageData(imgData) {
    var d = imgData.data, len = d.length;
    var gray = new Uint8Array(len / 4);
    for (var i = 0, g = 0; i < len; i += 4, g++) {
      // взвешенная luminance: глаза/камера воспринимают зелёный ярче
      gray[g] = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114) | 0;
    }
    return gray;
  }

  /** Порог Оцу: классический автопорог по гистограмме. */
  function otsuThreshold(gray) {
    var hist = new Array(256).fill(0);
    for (var i = 0; i < gray.length; i++) hist[gray[i]]++;
    var total = gray.length;
    var sum = 0, t;
    for (t = 0; t < 256; t++) sum += t * hist[t];
    var sumB = 0, wB = 0, max = 0, thr = 127;
    for (t = 0; t < 256; t++) {
      wB += hist[t];
      if (!wB) continue;
      var wF = total - wB;
      if (!wF) break;
      sumB += t * hist[t];
      var mB = sumB / wB;
      var mF = (sum - sumB) / wF;
      var between = wB * wF * (mB - mF) * (mB - mF);
      if (between > max) { max = between; thr = t; }
    }
    return thr;
  }

  /**
   * Локальный (адаптивный) порог: усреднение по блоку.
   * Спасает при бликах и неравномерной подсветке — частая беда макросъёмки.
   */
  function localThreshold(gray, w, h, blockSize, bias) {
    var half = blockSize >> 1;
    // интегральное изображение для быстрого среднего
    var integral = new Float64Array((w + 1) * (h + 1));
    for (var y = 0; y < h; y++) {
      var rowSum = 0;
      for (var x = 0; x < w; x++) {
        rowSum += gray[y * w + x];
        integral[(y + 1) * (w + 1) + (x + 1)] = integral[y * (w + 1) + (x + 1)] + rowSum;
      }
    }
    var out = new Uint8Array(gray.length);
    for (var yy = 0; yy < h; yy++) {
      for (var xx = 0; xx < w; xx++) {
        var x0 = Math.max(0, xx - half), x1 = Math.min(w - 1, xx + half);
        var y0 = Math.max(0, yy - half), y1 = Math.min(h - 1, yy + half);
        var cnt = (x1 - x0 + 1) * (y1 - y0 + 1);
        var s = integral[(y1 + 1) * (w + 1) + (x1 + 1)]
              - integral[(y0) * (w + 1) + (x1 + 1)]
              - integral[(y1 + 1) * (w + 1) + (x0)]
              + integral[(y0) * (w + 1) + (x0)];
        var mean = s / cnt;
        out[yy * w + xx] = gray[yy * w + xx] < mean - bias ? 0 : 255;
      }
    }
    return out;
  }

  function otsuBinary(gray, w, h) {
    var thr = otsuThreshold(gray);
    var out = new Uint8Array(gray.length);
    for (var i = 0; i < gray.length; i++) out[i] = gray[i] < thr ? 0 : 255;
    return out;
  }

  /** Растяжение контраста по процентилям — убирает блеклость кадра. */
  function stretch(gray, lowPct, highPct) {
    var hist = new Array(256).fill(0);
    for (var i = 0; i < gray.length; i++) hist[gray[i]]++;
    var total = gray.length;
    var lo = 0, hi = 255, acc = 0;
    var lowCount = total * lowPct, highCount = total * highPct;
    for (var t = 0; t < 256; t++) {
      acc += hist[t];
      if (acc >= lowCount) { lo = t; break; }
    }
    acc = 0;
    for (var t2 = 255; t2 >= 0; t2--) {
      acc += hist[t2];
      if (acc >= highCount) { hi = t2; break; }
    }
    var span = Math.max(1, hi - lo);
    var out = new Uint8Array(gray.length);
    for (var j = 0; j < gray.length; j++) {
      var v = Math.round(Math.min(255, Math.max(0, (gray[j] - lo) * 255 / span)));
      out[j] = v;
    }
    return out;
  }

  /**
   * Собирает canvas из бинарного массива (0/255) с заданной инверсией
   * и коэффициентом масштабирования.
   * Tesseract лучше работает с высотой символов ~30–60 px, поэтому scale ≥ 4.
   */
  var MAX_DIM = 2000; // потолок размера канвы: tesseract резко тормозит на гигантах

  function binaryToCanvas(binary, w, h, invert, scale, pad) {
    scale = scale || 4;
    pad = pad == null ? 12 : pad;
    var iw = w + pad * 2, ih = h + pad * 2;

    // рисуем бинарный буфер через ImageData: построчный fillRect на большом
    // кадре занимал секунды, а тут — один проход по массиву
    var small = document.createElement('canvas');
    small.width = iw; small.height = ih;
    var sctx = small.getContext('2d');
    var img = sctx.createImageData(iw, ih);
    var d = img.data;
    var whiteBg = 255;
    // заливаем фон белым
    for (var k = 0; k < d.length; k += 4) {
      d[k] = whiteBg; d[k + 1] = whiteBg; d[k + 2] = whiteBg; d[k + 3] = 255;
    }
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var v = binary[y * w + x];
        var isInk = invert ? (v > 127) : (v < 128);
        if (!isInk) continue;
        var idx = ((y + pad) * iw + (x + pad)) * 4;
        d[idx] = 0; d[idx + 1] = 0; d[idx + 2] = 0;
      }
    }
    sctx.putImageData(img, 0, 0);

    // ограничиваем итоговый размер, иначе распознавание уходит в минуты
    var maxSide = Math.max(iw, ih);
    if (maxSide * scale > MAX_DIM) scale = Math.max(1, Math.floor(MAX_DIM / maxSide));

    var out = document.createElement('canvas');
    out.width = Math.round(iw * scale);
    out.height = Math.round(ih * scale);
    var octx = out.getContext('2d');
    octx.imageSmoothingEnabled = false; // nearest neighbour — края символов остаются резкими
    octx.drawImage(small, 0, 0, iw, ih, 0, 0, out.width, out.height);
    return out;
  }

  /**
   * Автокадрирование: ищем bounding-box "чернил" (тёмные или светлые пиксели)
   * в центре кадра, чтобы отрезать лишний корпус и фон платы.
   */
  function autoCropRect(gray, w, h) {
    var thr = otsuThreshold(gray);
    // считаем, чего в кадре меньше: считаем, что маркировка — меньшинство
    var dark = 0;
    for (var i = 0; i < gray.length; i++) if (gray[i] < thr) dark++;
    var minorityIsDark = dark < (gray.length / 2);
    var minX = w, minY = h, maxX = 0, maxY = 0, count = 0;
    var cx0 = Math.floor(w * 0.06), cx1 = Math.ceil(w * 0.94);
    var cy0 = Math.floor(h * 0.06), cy1 = Math.ceil(h * 0.94);
    for (var y = cy0; y < cy1; y++) {
      for (var x = cx0; x < cx1; x++) {
        var v = gray[y * w + x];
        var isInk = minorityIsDark ? (v < thr - 12) : (v > thr + 12);
        if (isInk) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
          count++;
        }
      }
    }
    if (!count || maxX - minX < 4 || maxY - minY < 4) return null;
    var m = Math.round(Math.min(w, h) * 0.12);
    return {
      x: Math.max(0, minX - m),
      y: Math.max(0, minY - m),
      w: Math.min(w - minX + m, maxX - minX + 2 * m),
      h: Math.min(h - minY + m, maxY - minY + 2 * m),
      minorityIsDark: minorityIsDark
    };
  }

  function rectFromPreset(w, h, preset) {
    // preset: 'small' | 'wide' | 'large' — доля кадра по ширине/высоте
    var fracW = preset === 'small' ? 0.40 : preset === 'wide' ? 0.70 : 0.90;
    var fracH = preset === 'small' ? 0.22 : preset === 'wide' ? 0.34 : 0.55;
    var rw = Math.round(w * fracW), rh = Math.round(h * fracH);
    return { x: Math.round((w - rw) / 2), y: Math.round((h - rh) / 2), w: rw, h: rh, minorityIsDark: true };
  }

  function clampRect(rect, w, h) {
    var x = Math.max(0, Math.min(rect.x, w - 4));
    var y = Math.max(0, Math.min(rect.y, h - 4));
    return {
      x: x, y: y,
      w: Math.max(4, Math.min(rect.w, w - x)),
      h: Math.max(4, Math.min(rect.h, h - y)),
      minorityIsDark: rect.minorityIsDark
    };
  }

  /**
   * Готовит набор вариантов предобработки для области кадра.
   * Возвращает массив { canvas, label, invert }.
   */
  function buildVariants(source, rect) {
    var w = Math.round(rect.w), h = Math.round(rect.h);
    var tmp = document.createElement('canvas');
    tmp.width = w; tmp.height = h;
    var tctx = tmp.getContext('2d', { willReadFrequently: true });
    tctx.drawImage(source, Math.round(rect.x), Math.round(rect.y), w, h, 0, 0, w, h);

    // базовое увеличение 2x перед анализом — на очень мелких кропах
    // пикселей слишком мало для осмысленной бинаризации
    var baseScale = Math.max(1, Math.ceil(120 / Math.max(w, h)));
    if (baseScale > 1) {
      var up = document.createElement('canvas');
      up.width = w * baseScale; up.height = h * baseScale;
      var uctx = up.getContext('2d', { willReadFrequently: true });
      uctx.imageSmoothingEnabled = true;
      uctx.imageSmoothingQuality = 'high';
      uctx.drawImage(tmp, 0, 0, up.width, up.height);
      tmp = up; w = up.width; h = up.height;
    }

    var imgData = tmp.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, w, h);
    var gray = grayFromImageData(imgData);
    var out = [];
    var scale = 3;

    // 1. Порог Оцу, прямая и инвертированная версии
    var b1 = otsuBinary(gray, w, h);
    out.push({ canvas: binaryToCanvas(b1, w, h, false, scale), label: 'Оцу (тёмный текст)', invert: false });
    out.push({ canvas: binaryToCanvas(b1, w, h, true, scale), label: 'Оцу инверсия (светлая маркировка)', invert: true });

    // 2. Контраст + Оцу — спасает блеклый кадр
    var st = stretch(gray, 0.02, 0.02);
    var b2 = otsuBinary(st, w, h);
    out.push({ canvas: binaryToCanvas(b2, w, h, false, scale), label: 'контраст + Оцу', invert: false });
    out.push({ canvas: binaryToCanvas(b2, w, h, true, scale), label: 'контраст + Оцу инверсия', invert: true });

    // 3. Локальный порог — спасает при блике на половине корпуса
    try {
      var b3 = localThreshold(st, w, h, Math.max(8, Math.min(48, Math.round(Math.min(w, h) / 6) | 1)), 4);
      out.push({ canvas: binaryToCanvas(b3, w, h, false, scale), label: 'локальный порог', invert: false });
      out.push({ canvas: binaryToCanvas(b3, w, h, true, scale), label: 'локальный порог инверсия', invert: true });
    } catch (e) { /* на очень больших кадрах локальный порог дорогой — пропускаем */ }

    return out;
  }

  /* ------------------------------------------------------------- распознавание */

  function cleanText(t) {
    return String(t || '')
      .toUpperCase()
      .replace(/[^A-Z0-9.\-]/g, '')
      .trim();
  }

  /**
   * Прогоняет OCR по набору вариантов.
   * @returns {Promise<Array>} [{ text, cleaned, conf, label }]
   */
  function recognizeAll(variants, onProgress) {
    return ensureWorker(onProgress).then(function (worker) {
      var chain = Promise.resolve();
      var results = [];
      variants.forEach(function (v, idx) {
        chain = chain.then(function () {
          if (onProgress) onProgress('Вариант ' + (idx + 1) + '/' + variants.length + ': ' + v.label);
          return worker.setParameters({
            tessedit_char_whitelist: CHARSET,
            tessedit_pageseg_mode: global.Tesseract.PSM.SINGLE_BLOCK,
            preserve_interword_spaces: '0'
          }).then(function () {
            return worker.recognize(v.canvas);
          }).then(function (res) {
            var text = (res && res.data && res.data.text) || '';
            var conf = (res && res.data && res.data.confidence) || 0;
            results.push({ text: text, cleaned: cleanText(text), conf: conf, label: v.label, canvas: v.canvas });
          }).catch(function (err) {
            results.push({ text: '', cleaned: '', conf: 0, label: v.label, error: String(err) });
          });
        });
      });
      return chain.then(function () { return results; });
    });
  }

  /**
   * Полный цикл: кадр -> кадрирование -> предобработка -> OCR -> чистка.
   * @param {HTMLCanvasElement|HTMLVideoElement|HTMLImageElement} source
   * @param {object} opts { crop: 'auto'|'center'|'full', preset: 'small'|'wide'|'large', rect: {...} }
   */
  function analyze(source, opts, onProgress) {
    opts = opts || {};
    var sw = source.videoWidth || source.naturalWidth || source.width;
    var sh = source.videoHeight || source.naturalHeight || source.height;

    // 'manual' — пользователь сам натянул рамку, это самый точный источник
    var rect;
    if (opts.crop === 'manual' && opts.rect) {
      rect = opts.rect;
    } else if (opts.crop === 'full') {
      rect = { x: 0, y: 0, w: sw, h: sh, minorityIsDark: true };
    } else if (opts.crop === 'auto') {
      // автокадрирование дешевле считать на уменьшенной копии кадра
      var probe = document.createElement('canvas');
      probe.width = Math.min(640, sw); probe.height = Math.round(sh * (probe.width / sw));
      var pctx = probe.getContext('2d', { willReadFrequently: true });
      pctx.drawImage(source, 0, 0, probe.width, probe.height);
      var pGray = grayFromImageData(pctx.getImageData(0, 0, probe.width, probe.height));
      var found = autoCropRect(pGray, probe.width, probe.height);
      if (found) {
        var kx = sw / probe.width, ky = sh / probe.height;
        rect = {
          x: found.x * kx, y: found.y * ky, w: found.w * kx, h: found.h * ky,
          minorityIsDark: found.minorityIsDark
        };
      } else {
        rect = rectFromPreset(sw, sh, opts.preset || 'wide');
      }
    } else {
      rect = rectFromPreset(sw, sh, opts.preset || 'wide');
    }

    rect = clampRect(rect, sw, sh);
    var variants = buildVariants(source, rect);
    return recognizeAll(variants, onProgress).then(function (results) {
      return { rect: rect, results: results, variants: variants };
    });
  }

  global.SMDOCR = {
    camera: camera,
    analyze: analyze,
    buildVariants: buildVariants,
    ensureWorker: ensureWorker,
    cleanText: cleanText,
    autoCropRect: autoCropRect,
    grayFromImageData: grayFromImageData,
    otsuThreshold: otsuThreshold,
    isEngineReady: function () { return state.workerReady; },
    CHARSET: CHARSET
  };
})(window);
