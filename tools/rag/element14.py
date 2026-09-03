#!/usr/bin/env python3
"""element14 / Farnell / Newark Product Search API -> part list for the corpus.

    python3 tools/rag/element14.py --check
    python3 tools/rag/element14.py --parts parts.txt --to-csv parts-e14.csv
    python3 tools/rag/element14.py --browse "any:microcontroller" --max-pages 5

Зачем это, если есть каталог JLCPCB/LCSC и `--prefer-vendor`:

* `--prefer-vendor` знает шаблон URL восьми производителей (TI, ST, NXP,
  onsemi, Diodes, Microchip, Vishay, ROHM). Всё остальное — сотни брендов —
  остаётся на копиях LCSC, часто сканах.
* element14 отдаёт **ссылку на PDF, имя производителя и уже разобранные
  атрибуты** (корпус, напряжения, температурный диапазон) по парт-номеру.
  Это те самые поля, которые наш парсер вытаскивает из PDF с трудом:
  производитель 32 %, габариты 5 %.

На тарифе «Product Search API: High»: **10 вызовов/с и 50 000 вызовов в сутки.**
Это и есть главное ограничение, поэтому в клиенте три вещи, без которых ключ
сгорает к обеду:

1. **ограничитель скорости** — не больше `--rps` (по умолчанию 8 из 10,
   чтобы оставить запас на чужие задержки);
2. **дневной бюджет** — счётчик вызовов на UTC-сутки лежит в кэше; перед
   каждым вызовом проверяется, и когда 50 000 исчерпаны, прогон останавливается
   с понятным сообщением, а не получает 403 шесть часов подряд;
3. **кэш в SQLite** — и найденные товары, и **пустые ответы**. Корпус растёт
   неделями, перезапуск — обычное дело; платить дважды за один и тот же
   парт-номер нельзя.

Ключ: `--api-key` или переменная окружения `SMD_E14_KEY`, витрина —
`--store` или `SMD_E14_STORE` (по умолчанию `uk.farnell.com`; полный список —
в документации API: `www.newark.com`, `de.farnell.com`, `cn.element14.com` …).

Важно про robots.txt: datasheet-ссылки, которые отдаёт API, ведут на
`www.farnell.com/datasheets/*.pdf`. Загрузчик по умолчанию спрашивает
robots.txt и может этот путь не пустить — мы пришли по API, а не краулером,
поэтому для этих ссылок разумно передать `--ignore-robots`. Команда для
следующего шага печатается в конце прогона целиком.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data"))
DEFAULT_CACHE = DATA_DIR / "cache" / "element14.sqlite3"
DEFAULT_STORE = "uk.farnell.com"

BASE_URL = "https://api.element14.com/catalog/products"
USER_AGENT = ("smd-component-finder/0.1 (+datasheet corpus building; "
              "contact: local user)")

# Квоты тарифа High. Не угадываем наверху, а держим при себе: превышение
# выглядит как 403 с X-Mashery-Error-Code, и к тому моменту сутки уже потрачены.
DEFAULT_RPS = 8.0
DEFAULT_DAY_LIMIT = 50000
DEFAULT_PER_PAGE = 100          # сколько товаров просим за вызов; потолок не
MAX_PER_PAGE = 100              # документирован — реальный ответ покажет сам

CSV_HEADER = ["part", "manufacturer", "package", "url", "sku", "store"]


class BudgetExhausted(RuntimeError):
    """Дневной лимит вызовов исчерпан — останавливаемся, а не долбим 403."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _default_opener(url: str, timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        # 403 от Mashery — это и есть ответ про квоту; тело и заголовки нужны.
        return exc.code, exc.read() or b"", dict(exc.headers or {})


class Element14:
    """Клиент Product Search API: кэш, ограничитель скорости, дневной бюджет."""

    def __init__(self, api_key: str, store: str = DEFAULT_STORE,
                 cache: Optional[Path] = None, rps: float = DEFAULT_RPS,
                 day_limit: int = DEFAULT_DAY_LIMIT, timeout: float = 30.0,
                 retries: int = 4, response_group: str = "large",
                 opener: Optional[Callable[..., Tuple[int, bytes, Dict[str, str]]]] = None,
                 verbose: bool = False) -> None:
        if not api_key:
            raise SystemExit(
                "Нет ключа element14. Передайте --api-key или положите его в "
                "SMD_E14_KEY. Ключ выдаётся на partner.element14.com, продукт "
                "«Product Search API».")
        self.api_key = api_key
        self.store = store
        self.timeout = timeout
        self.retries = retries
        self.response_group = response_group
        self.verbose = verbose
        self.rps = max(0.1, float(rps))
        self.day_limit = int(day_limit)
        self._opener = opener or _default_opener

        self.calls = 0               # вызовов за эту сессию
        self.cache_hits = 0
        self.errors: List[str] = []
        self.quota_headers: Dict[str, str] = {}

        self._lock = threading.Lock()
        self._times: List[float] = []
        self._db: Optional[sqlite3.Connection] = None
        if cache:
            self._db = _open_cache(cache)
        self._day = time.strftime("%Y-%m-%d", time.gmtime())

    # ------------------------------------------------------------------ quota

    def _spend(self) -> None:
        """Один вызов: сначала дневной бюджет, потом пауза под 10 вызовов/с."""
        if self._db is not None:
            spent = _quota_get(self._db, self._day)
            if spent >= self.day_limit:
                raise BudgetExhausted(
                    "дневной лимит %d вызовов исчерпан (%s UTC). Продолжим "
                    "завтра: кэш сохранился, сделанное не пропало"
                    % (self.day_limit, self._day))
        with self._lock:
            now = time.time()
            # простой скользящий ограничитель: не больше rps вызовов в секунду
            self._times = [t for t in self._times if now - t < 1.0]
            if len(self._times) >= int(self.rps):
                pause = 1.0 - (now - self._times[0]) + 0.01
                if pause > 0:
                    time.sleep(pause)
                now = time.time()
                self._times = [t for t in self._times if now - t < 1.0]
            self._times.append(now)

    def _charge(self) -> None:
        self.calls += 1
        if self._db is not None:
            _quota_add(self._db, self._day)

    def remaining_today(self) -> int:
        if self._db is None:
            return self.day_limit
        return max(0, self.day_limit - _quota_get(self._db, self._day))

    # ------------------------------------------------------------------- http

    def _url(self, params: Dict[str, Any]) -> str:
        query = dict(params)
        query.setdefault("callInfo.responseDataFormat", "json")
        query.setdefault("storeInfo.id", self.store)
        query.setdefault("callInfo.apiKey", self.api_key)
        query.setdefault("resultsSettings.responseGroup", self.response_group)
        return BASE_URL + "?" + urllib.parse.urlencode(query)

    def raw(self, term: str, offset: int = 0, number: int = 0,
            filters: str = "") -> Dict[str, Any]:
        """Один вызов API. Пустой dict — если API недоступен или квота кончилась."""
        params: Dict[str, Any] = {"term": term}
        if number:
            params["resultsSettings.offset"] = int(offset)
            params["resultsSettings.numberOfResults"] = int(number)
        if filters:
            params["resultsSettings.refinements.filters"] = filters

        cache_key = "%s|%s|%s|%s|%s" % (self.store, term, offset, number,
                                        self.response_group)
        cached = _cache_get(self._db, cache_key) if self._db is not None else None
        if cached is not None:
            self.cache_hits += 1
            return cached

        url = self._url(params)
        for attempt in range(self.retries):
            try:
                self._spend()
                status, body, headers = self._opener(url, self.timeout)
            except BudgetExhausted:
                raise
            except Exception as exc:                 # noqa: BLE001 - сеть всякая
                self.errors.append("сеть: %s" % exc)
                time.sleep(min(30.0, 2.0 ** attempt))
                continue
            self._charge()
            for key in ("X-RateLimit-Limit", "X-RateLimit-Remaining",
                        "X-Mashery-Error-Code", "Retry-After"):
                if key in headers:
                    self.quota_headers[key] = headers[key]
            if status == 200:
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    payload = {}
                if self._db is not None:
                    _cache_put(self._db, cache_key, payload)
                return payload
            # 403/429: квота. Mashery кладёт причину в X-Mashery-Error-Code.
            code = headers.get("X-Mashery-Error-Code", "")
            if status in (403, 429):
                message = ("квота element14: HTTP %d %s" % (status, code or "")).strip()
                self.errors.append(message)
                if "OVER_RATE" in code:      # дневной лимит — ждать бессмысленно
                    _quota_set(self._db, self._day, self.day_limit)
                    raise BudgetExhausted(message)
                time.sleep(min(60.0, 5.0 * (attempt + 1)))
                continue
            self.errors.append("HTTP %d" % status)
            if status < 500:
                break
            time.sleep(min(30.0, 2.0 ** attempt))
        return {}

    # --------------------------------------------------------------- products

    @staticmethod
    def products_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        block = payload.get("keywordSearchReturn") or payload.get(
            "manuPartNumSearchReturn") or {}
        products = block.get("products") or []
        if isinstance(products, dict):       # один товар — API отдаёт объектом
            products = [products]
        return [p for p in products if isinstance(p, dict)]

    @staticmethod
    def total_of(payload: Dict[str, Any]) -> int:
        block = payload.get("keywordSearchReturn") or payload.get(
            "manuPartNumSearchReturn") or {}
        try:
            return int(block.get("numberOfResults") or 0)
        except (TypeError, ValueError):
            return 0

    def lookup(self, part: str, in_stock: bool = False) -> Optional[Dict[str, Any]]:
        """Один парт-номер производителя -> лучший товар из каталога."""
        payload = self.raw("manuPartNum:%s" % part, number=MAX_PER_PAGE,
                           filters="inStock" if in_stock else "")
        best: Optional[Dict[str, Any]] = None
        for product in self.products_of(payload):
            row = self.to_row(product)
            if not row["part"]:
                continue
            # точное совпадение парт-номера важнее всего: manuPartNum ищет
            # и по вхождению, а пересортица по семействам нам не нужна
            exact = row["part"].upper().replace(" ", "") == part.upper().replace(" ", "")
            if best is None or (exact and not best["_exact"]):
                best = row
                best["_exact"] = exact
                if exact and row["url"]:
                    break
            elif exact and best["_exact"] and not best["url"] and row["url"]:
                best = row
                best["_exact"] = exact
        if best is not None:
            best.pop("_exact", None)
        return best

    def browse(self, term: str, max_pages: int = 5, per_page: int = DEFAULT_PER_PAGE,
               in_stock: bool = False) -> List[Dict[str, Any]]:
        """Постраничный проход по поиску: один вызов — до `per_page` товаров.

        Именно здесь кроется объём: если API отдаёт 100 товаров на вызов, то
        50 000 вызовов — это до 5 млн позиций в сутки, а не 50 тысяч. Потолок
        `numberOfResults` в документации не указан, поэтому реальное число
        печатается в отчёте.
        """
        per_page = max(1, min(int(per_page), MAX_PER_PAGE))
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for page in range(max_pages):
            payload = self.raw(term, offset=page * per_page, number=per_page,
                               filters="inStock" if in_stock else "")
            products = self.products_of(payload)
            if not products:
                break
            for product in products:
                row = self.to_row(product)
                if not row["part"] or row["part"] in seen:
                    continue
                seen.add(row["part"])
                out.append(row)
            if len(products) < per_page:
                break                       # страница неполная — дальше нечего
            if self.total_of(payload) and len(out) >= self.total_of(payload):
                break
        return out

    # ---------------------------------------------------------------- mapping

    def to_row(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """Товар API -> строка нашего списка деталей (и немного сверху)."""
        part = (product.get("translatedManufacturerPartNumber")
                or product.get("manufacturerPartNumber") or "").strip()
        manufacturer = (product.get("brandName") or "").strip()
        url = ""
        for item in product.get("datasheets") or []:
            if not isinstance(item, dict):
                continue
            candidate = (item.get("url") or "").strip()
            if candidate.lower().endswith(".pdf"):
                url = candidate
                break
            if not url:
                url = candidate
        attributes = self.attributes_of(product)
        stock = product.get("stock") or {}
        return {
            "part": part,
            "manufacturer": manufacturer,
            "package": _package_of(product, attributes),
            "url": url,
            "sku": str(product.get("sku") or ""),
            "store": self.store,
            "title": (product.get("displayName") or "").strip(),
            "stock": stock.get("level") if isinstance(stock, dict) else 0,
            "attributes": attributes,
        }

    @staticmethod
    def attributes_of(product: Dict[str, Any]) -> Dict[str, str]:
        """Атрибуты как «подпись -> значение с единицами».

        Это уже готовые поля карточки: «Operating Temperature Max» = «125 °C»,
        «Voltage Rating V DC» = «40 V». То, что парсер выдирает из PDF с
        трудом, а здесь лежит готовым.
        """
        out: Dict[str, str] = {}
        for item in product.get("attributes") or []:
            if not isinstance(item, dict):
                continue
            label = (item.get("attributeLabel") or "").strip()
            value = (item.get("attributeValue") or "").strip()
            if not label or not value:
                continue
            unit = (item.get("attributeUnit") or "").strip()
            out[label] = ("%s %s" % (value, unit)).strip()
        return out


def _package_of(product: Dict[str, Any], attributes: Dict[str, str]) -> str:
    """Корпус: ищем атрибут с 'case' или 'package' в подписи."""
    for label, value in attributes.items():
        low = label.lower()
        if "package" in low or "case" in low:
            return value.split(",")[0].strip()[:40]
    return ""


# --------------------------------------------------------------------------- #
# кэш и квота
# --------------------------------------------------------------------------- #

def _open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS cache ("
                "key TEXT PRIMARY KEY, payload TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS quota ("
                "day TEXT PRIMARY KEY, calls INTEGER)")
    con.commit()
    return con


def _cache_get(con: Optional[sqlite3.Connection], key: str) -> Optional[dict]:
    if con is None:
        return None
    row = con.execute("SELECT payload FROM cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _cache_put(con: Optional[sqlite3.Connection], key: str, payload: dict) -> None:
    if con is None:
        return
    con.execute("INSERT OR REPLACE INTO cache (key, payload, ts) VALUES (?,?,?)",
                (key, json.dumps(payload, ensure_ascii=False), time.time()))
    con.commit()


def _quota_get(con: sqlite3.Connection, day: str) -> int:
    row = con.execute("SELECT calls FROM quota WHERE day = ?", (day,)).fetchone()
    return int(row[0]) if row else 0


def _quota_add(con: Optional[sqlite3.Connection], day: str) -> None:
    if con is None:
        return
    con.execute("INSERT INTO quota (day, calls) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET calls = calls + 1", (day,))
    con.commit()


def _quota_set(con: Optional[sqlite3.Connection], day: str, value: int) -> None:
    if con is None:
        return
    con.execute("INSERT OR REPLACE INTO quota (day, calls) VALUES (?,?)",
                (day, int(value)))
    con.commit()


# --------------------------------------------------------------------------- #
# экспорт
# --------------------------------------------------------------------------- #

def read_parts(path: Path, limit: int = 0) -> List[str]:
    """Список парт-номеров: txt (по строке) или CSV/JSON с колонкой part."""
    text = path.read_text(encoding="utf-8")
    parts: List[str] = []
    if path.suffix.lower() == ".json":
        for item in json.loads(text):
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("part"):
                parts.append(str(item["part"]))
    elif path.suffix.lower() == ".csv":
        for row in csv.DictReader(text.splitlines()):
            if (row.get("part") or "").strip():
                parts.append(row["part"].strip())
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts.append(line)
    if limit:
        parts = parts[:limit]
    return parts


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> int:
    """Тот же формат, что ждёт `fetch_datasheets.py --list`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for row in rows:
            if not row.get("url"):
                continue
            writer.writerow([row.get("part", ""), row.get("manufacturer", ""),
                             row.get("package", ""), row.get("url", ""),
                             row.get("sku", ""), row.get("store", "")])
    return sum(1 for row in rows if row.get("url"))


def write_attributes(path: Path, rows: List[Dict[str, Any]]) -> int:
    """Атрибуты в JSONL — задел на обогащение карточек без повторных вызовов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if not row.get("attributes"):
                continue
            handle.write(json.dumps({
                "part": row.get("part", ""),
                "manufacturer": row.get("manufacturer", ""),
                "package": row.get("package", ""),
                "store": row.get("store", ""),
                "attributes": row["attributes"],
            }, ensure_ascii=False) + "\n")
            written += 1
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def check(client: Element14) -> int:
    """Один вызов: проверить ключ, витрину и что реально приходит."""
    print("Витрина:      %s" % client.store)
    print("Осталось сегодня: %d вызовов" % client.remaining_today())
    try:
        payload = client.raw("manuPartNum:MMBT3904", number=MAX_PER_PAGE)
    except BudgetExhausted as exc:
        print("\n[!!] %s" % exc)
        return 1
    if not payload:
        print("\n[!!] API ничего не вернул. Проверьте ключ:")
        print("     • ключ выдаётся на partner.element14.com, продукт "
              "«Product Search API»,")
        print("       а не на community.element14.com — это разные ключи;")
        for err in client.errors[:3]:
            print("     • %s" % err[:110])
        return 1
    products = client.products_of(payload)
    print("Вызовов:      %d (кэш: %d)" % (client.calls, client.cache_hits))
    print("Найдено:      %d товаров (всего по запросу: %d)"
          % (len(products), client.total_of(payload)))
    if not products:
        # Ключ жив, но товаров нет — обычно это не тот продукт на портале
        # (community вместо Product Search) или витрина без этой позиции.
        print("\n[!!] товаров не пришло. Что проверить:")
        print("     • ключ выдан на partner.element14.com именно для продукта "
              "«Product Search API»,")
        print("       ключ от community.element14.com здесь не работает;")
        print("     • витрина %s — попробуйте --store www.newark.com" % client.store)
        for err in client.errors[:3]:
            print("     • %s" % err[:110])
        return 1
    if client.quota_headers:
        print("Заголовки квоты: %s" % client.quota_headers)
    for product in products[:2]:
        row = client.to_row(product)
        print("\n  %s — %s" % (row["part"] or "?", row["manufacturer"] or "?"))
        print("    корпус:      %s" % (row["package"] or "—"))
        print("    склад:       %s" % row["stock"])
        print("    datasheet:   %s" % (row["url"] or "—"))
        head = list(row["attributes"].items())[:4]
        for label, value in head:
            print("    %-28s %s" % (label[:28] + ":", value))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(
        description="Поиск деталей и datasheet-ссылок через API element14")
    ap.add_argument("--api-key", default=os.environ.get("SMD_E14_KEY", ""),
                    help="ключ Product Search API (или SMD_E14_KEY)")
    ap.add_argument("--store", default=os.environ.get("SMD_E14_STORE", DEFAULT_STORE),
                    help="витрина: uk.farnell.com, www.newark.com, de.farnell.com…")
    ap.add_argument("--parts", type=Path, help="txt/csv/json со списком парт-номеров")
    ap.add_argument("--browse", default="",
                    help="поисковый запрос вместо списка: any:fuse, any:mosfet…")
    ap.add_argument("--to-csv", type=Path, default=Path("parts-element14.csv"),
                    help="куда писать список (формат fetch_datasheets --list)")
    ap.add_argument("--attributes-out", type=Path, default=None,
                    help="JSONL с атрибутами — задел на обогащение карточек")
    ap.add_argument("--limit", type=int, default=0, help="сколько парт-номеров взять")
    ap.add_argument("--max-pages", type=int, default=5,
                    help="страниц в режиме --browse")
    ap.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE,
                    help="товаров на вызов (потолок %d)" % MAX_PER_PAGE)
    ap.add_argument("--in-stock", action="store_true", help="только то, что на складе")
    ap.add_argument("--rps", type=float, default=DEFAULT_RPS,
                    help="вызовов в секунду (лимит тарифа — 10)")
    ap.add_argument("--day-limit", type=int, default=DEFAULT_DAY_LIMIT,
                    help="дневной бюджет вызовов (50 000 на High)")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--no-cache", action="store_true",
                    help="не кэшировать (каждый прогон платит вызовами заново)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--check", action="store_true",
                    help="один пробный вызов и выход")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    client = Element14(api_key=args.api_key, store=args.store,
                       cache=None if args.no_cache else args.cache,
                       rps=args.rps, day_limit=args.day_limit,
                       timeout=args.timeout, verbose=args.verbose)

    if args.check:
        return check(client)

    started = time.time()
    rows: List[Dict[str, Any]] = []
    asked = 0
    try:
        if args.browse:
            rows = client.browse(args.browse, max_pages=args.max_pages,
                                 per_page=args.per_page, in_stock=args.in_stock)
            asked = client.total_of(client.raw(args.browse, number=1))
        elif args.parts:
            parts = read_parts(args.parts, args.limit)
            print("Парт-номеров: %d   осталось вызовов сегодня: %d"
                  % (len(parts), client.remaining_today()))
            for i, part in enumerate(parts, 1):
                try:
                    row = client.lookup(part, in_stock=args.in_stock)
                except BudgetExhausted as exc:
                    print("\nОстановлено: %s" % exc)
                    break
                if row:
                    rows.append(row)
                if args.verbose or i % 25 == 0 or i == len(parts):
                    with_pdf = sum(1 for r in rows if r.get("url"))
                    print("  %5d/%d  найдено %d  с PDF %d  вызовов %d (кэш %d)"
                          % (i, len(parts), len(rows), with_pdf, client.calls,
                             client.cache_hits), flush=True)
        else:
            ap.error("нужен --parts, --browse или --check")
    except BudgetExhausted as exc:
        print("\nОстановлено: %s" % exc)

    with_pdf = sum(1 for r in rows if r.get("url"))
    with_mfr = sum(1 for r in rows if r.get("manufacturer"))
    written = write_csv(args.to_csv, rows)
    print("\nГотово за %s" % _fmt_time(time.time() - started))
    print("  позиций:            %d" % len(rows))
    print("  с datasheet-ссылкой: %d" % with_pdf)
    print("  с производителем:    %d" % with_mfr)
    print("  вызовов API:         %d (из кэша %d)" % (client.calls, client.cache_hits))
    print("  осталось на сегодня: %d" % client.remaining_today())
    print("  список:              %s" % args.to_csv)
    if asked:
        print("  всего по запросу «%s»: %d позиций" % (args.browse, asked))
    if args.attributes_out:
        n = write_attributes(args.attributes_out, rows)
        print("  атрибуты:            %s (%d строк)" % (args.attributes_out, n))
    for err in client.errors[:5]:
        print("  ! %s" % err[:110])
    if written:
        print("\nДальше — скачать PDF:")
        print("  python3 tools/rag/fetch_datasheets.py --list %s "
              "--out data/datasheets --workers 8 --delay 0.3 --ignore-robots"
              % args.to_csv)
        print("(--ignore-robots: ссылки на farnell.com/datasheets выданы самим API,")
        print(" мы пришли по нему, а не обходом сайта)")
    return 0


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return "%dh %02dm %02ds" % (h, m, sec) if h else "%dm %02ds" % (m, sec)


if __name__ == "__main__":
    raise SystemExit(main())
