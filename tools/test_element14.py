#!/usr/bin/env python3
"""Tests for the element14 Product Search API client — without the API.

Nothing here touches the network: the client takes an `opener` (or falls back
to `element14._default_opener`, which the tests replace), so what is checked is
the part that decides whether a 50 000-calls-a-day key survives a 300 000-part
corpus: the request, the mapping, the cache, the rate limiter and the daily
budget.

    python3 tools/test_element14.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import element14, fetch_datasheets  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


# --------------------------------------------------------------------------- #
# поддельный транспорт
# --------------------------------------------------------------------------- #

class Fake:
    """Records every request and answers from a script."""

    def __init__(self, responses: List[Tuple[int, Any, Dict[str, str]]] = None):
        self.urls: List[str] = []
        self.bodies: List[Dict[str, str]] = []
        self.script = list(responses or [])
        self.default: Tuple[int, Any, Dict[str, str]] = (200, {"keywordSearchReturn":
                                                              {"numberOfResults": 0,
                                                               "products": []}}, {})

    def __call__(self, url: str, timeout: float = 30.0):
        self.urls.append(url)
        status, body, headers = self.script.pop(0) if self.script else self.default
        payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return status, payload, headers

    def query(self, index: int = 0) -> Dict[str, str]:
        """Разобранные параметры i-го запроса."""
        import urllib.parse
        pair = self.urls[index].split("?", 1)[1]
        return dict(urllib.parse.parse_qsl(pair))


def product(part: str, brand: str = "ONSEMI", sku: str = "1",
            datasheet: str = "", attributes: List[dict] = None,
            stock: int = 0) -> dict:
    item = {
        "sku": sku,
        "displayName": "%s - %s - THING" % (brand, part),
        "brandName": brand,
        "translatedManufacturerPartNumber": part,
        "stock": {"level": stock, "status": 1 if stock else 0},
    }
    if datasheet:
        item["datasheets"] = [{"type": "T", "description": "Technical Data Sheet",
                               "url": datasheet}]
    if attributes is not None:
        item["attributes"] = attributes
    return item


def payload(products: List[dict], total: int = None) -> dict:
    return {"keywordSearchReturn": {
        "numberOfResults": total if total is not None else len(products),
        "products": products}}


def client(fake: Fake, tmp: Path, **kwargs) -> element14.Element14:
    params = {"api_key": "testkey000000000000000abc", "cache": tmp / "e14.sqlite3",
              "rps": 1000.0, "opener": fake}
    params.update(kwargs)
    return element14.Element14(**params)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-e14-"))

    print("--- запрос ---")
    fake = Fake([(200, payload([product("MMBT3904", datasheet="https://x/a.pdf")]), {})])
    e14 = client(fake, tmp)
    row = e14.lookup("MMBT3904")
    q = fake.query()
    check("вызов идёт на api.element14.com/catalog/products",
          fake.urls[0].startswith("https://api.element14.com/catalog/products?"),
          fake.urls[0][:60])
    check("ключ уезжает в callInfo.apiKey", q.get("callInfo.apiKey", "").startswith("testkey"),
          str(q.get("callInfo.apiKey")))
    check("витрина — в storeInfo.id", q.get("storeInfo.id") == element14.DEFAULT_STORE,
          str(q.get("storeInfo.id")))
    check("формат ответа — json", q.get("callInfo.responseDataFormat") == "json",
          str(q.get("callInfo.responseDataFormat")))
    check("парт-номер ищется по manuPartNum", q.get("term") == "manuPartNum:MMBT3904",
          str(q.get("term")))
    check("на вызов просят максимум товаров",
          q.get("resultsSettings.numberOfResults") == str(element14.MAX_PER_PAGE),
          str(q.get("resultsSettings.numberOfResults")))

    print("\n--- разбор ответа ---")
    check("парт-номер, производитель и ссылка на месте",
          row["part"] == "MMBT3904" and row["manufacturer"] == "ONSEMI"
          and row["url"] == "https://x/a.pdf", str(row))
    attrs = [{"attributeLabel": " Operating Temperature Max", "attributeUnit": "°C",
              "attributeValue": "150"},
             {"attributeLabel": " Transistor Case Style", "attributeUnit": "",
              "attributeValue": "SOT-23"},
             {"attributeLabel": " пустой", "attributeUnit": "", "attributeValue": ""}]
    row2 = e14.to_row(product("BC847", brand="NEXPERIA", datasheet="https://x/b.pdf",
                             attributes=attrs, stock=42))
    check("атрибуты собраны в «подпись -> значение с единицами»",
          row2["attributes"].get("Operating Temperature Max") == "150 °C",
          str(row2["attributes"]))
    check("корпус взят из атрибута про case/package",
          row2["package"] == "SOT-23", str(row2["package"]))
    check("пустой атрибут не попал в словарь",
          "пустой" not in row2["attributes"], str(list(row2["attributes"])))
    check("склад сохранён", row2["stock"] == 42, str(row2["stock"]))
    check("товар без datasheet даёт пустую ссылку",
          e14.to_row(product("NOPDF"))["url"] == "")

    print("\n--- выбор лучшего товара ---")
    # manuPartNum ищет и по вхождению: MMBT3904 найдёт и MMBT3904LT1.
    many = [product("MMBT3904LT1", sku="2", datasheet="https://x/lt1.pdf"),
            product("MMBT3904", sku="1", datasheet="https://x/exact.pdf")]
    fake2 = Fake([(200, payload(many), {})])
    got = client(fake2, tmp / "b").lookup("MMBT3904")
    check("точное совпадение парт-номера выигрывает",
          got["part"] == "MMBT3904" and got["url"] == "https://x/exact.pdf", str(got))
    fake3 = Fake([(200, payload([]), {})])
    check("чего нет в каталоге — None, а не исключение",
          client(fake3, tmp / "c").lookup("NOPE123") is None)

    print("\n--- кэш: не платим дважды ---")
    cache_dir = tmp / "cachecase"
    fake4 = Fake([(200, payload([product("ABC123", datasheet="https://x/c.pdf")]), {}),
                  (200, payload([product("ZZZ999", datasheet="https://x/z.pdf")]), {})])
    e14 = client(fake4, cache_dir)
    e14.lookup("ABC123")
    check("первый раз — реальный вызов", e14.calls == 1, str(e14.calls))
    again = client(fake4, cache_dir)
    row_again = again.lookup("ABC123")
    check("второй прогон берёт ответ из кэша",
          again.calls == 0 and again.cache_hits == 1,
          "%d вызовов, %d из кэша" % (again.calls, again.cache_hits))
    check("из кэша приходит тот же парт-номер",
          row_again["part"] == "ABC123", str(row_again))
    check("пустой ответ тоже кэшируется (не долбим API по пустышкам)",
          client(Fake([(200, payload([]), {})]), cache_dir).lookup("MISS1") is None
          and client(Fake([]), cache_dir).lookup("MISS1") is None)

    print("\n--- дневной бюджет 50 000 вызовов ---")
    fake5 = Fake([(200, payload([product("P%d" % i, datasheet="https://x/%d.pdf" % i)]), {})
                  for i in range(5)])
    e14 = client(fake5, tmp / "budget", day_limit=2)
    check("в начале дня бюджет цел", e14.remaining_today() == 2,
          str(e14.remaining_today()))
    e14.lookup("P0")
    e14.lookup("P1")
    check("после двух вызовов остаток — ноль", e14.remaining_today() == 0,
          str(e14.remaining_today()))
    raised = False
    try:
        e14.lookup("P2")
    except element14.BudgetExhausted:
        raised = True
    check("третий вызов останавливается, а не долбит 403", raised)
    check("истраченное не пропадает между прогонами",
          client(fake5, tmp / "budget", day_limit=50000).remaining_today() == 49998,
          str(client(fake5, tmp / "budget", day_limit=50000).remaining_today()))

    print("\n--- квота от Mashery ---")
    fake6 = Fake([(403, {"fault": "quota"}, {"X-Mashery-Error-Code":
                                             "ERR_403_DEVELOPER_OVER_QPS"})
                  for _ in range(4)])
    e14 = client(fake6, tmp / "qps", retries=2)
    got = e14.lookup("ANY")
    check("превышение скорости не роняет прогон", got is None, str(got))
    check("причина записана", any("квота" in e for e in e14.errors), str(e14.errors))

    fake7 = Fake([(403, {"fault": "quota"}, {"X-Mashery-Error-Code":
                                             "ERR_403_DEVELOPER_OVER_RATE"})])
    e14 = client(fake7, tmp / "rate")
    daily_stop = False
    try:
        e14.lookup("ANY")
    except element14.BudgetExhausted:
        daily_stop = True
    check("дневной лимит распознаётся отдельно и останавливает прогон", daily_stop)
    check("после него остаток обнуляется, чтобы не тратить вызовы впустую",
          e14.remaining_today() == 0, str(e14.remaining_today()))

    print("\n--- ограничитель скорости ---")
    fake8 = Fake([(200, payload([]), {}) for _ in range(3)])
    e14 = client(fake8, tmp / "rps", rps=2)
    started = time.time()
    for i in range(3):
        e14.raw("any:x%d" % i, number=1)      # разный term: иначе сработает кэш
    elapsed = time.time() - started
    check("больше rps вызовов в секунду не уходит", elapsed >= 0.4,
          "%.2f с на 3 вызова при rps=2" % elapsed)

    print("\n--- постраничный проход ---")
    page1 = payload([product("P%d" % i, datasheet="https://x/%d.pdf" % i)
                     for i in range(3)], total=4)
    page2 = payload([product("P2", datasheet="https://x/2.pdf"),
                     product("P3", datasheet="https://x/3.pdf")], total=4)
    fake9 = Fake([(200, page1, {}), (200, page2, {})])
    e14 = client(fake9, tmp / "browse")
    rows = e14.browse("any:thing", max_pages=5, per_page=3)
    check("проход собирает товары со всех страниц", len(rows) == 4,
          str([r["part"] for r in rows]))
    check("дубли по парт-номеру отброшены",
          len({r["part"] for r in rows}) == 4, str([r["part"] for r in rows]))
    check("offset первой страницы — 0", fake9.query(0).get("resultsSettings.offset") == "0",
          str(fake9.query(0).get("resultsSettings.offset")))
    check("offset второй — на размер страницы",
          fake9.query(1).get("resultsSettings.offset") == "3",
          str(fake9.query(1).get("resultsSettings.offset")))
    check("неполная страница останавливает проход",
          len(client(Fake([(200, page2, {})]), tmp / "b2").browse(
              "any:x", max_pages=5, per_page=3)) == 2)

    print("\n--- список для загрузчика ---")
    csv_path = tmp / "out" / "parts.csv"
    rows = [e14.to_row(product("MMBT3904", datasheet="https://farnell.com/a.pdf",
                              attributes=attrs)),
            e14.to_row(product("NOPDF"))]
    written = element14.write_csv(csv_path, rows)
    check("в CSV попадают только строки со ссылкой", written == 1, str(written))
    back = fetch_datasheets.read_list(csv_path)
    check("загрузчик понимает этот файл",
          back and back[0]["part"] == "MMBT3904"
          and back[0]["url"] == "https://farnell.com/a.pdf", str(back[:1]))
    check("производитель и корпус доезжают до загрузчика",
          back[0]["manufacturer"] == "ONSEMI" and back[0]["package"] == "SOT-23",
          str(back[0]))

    attrs_path = tmp / "attrs.jsonl"
    n = element14.write_attributes(attrs_path, rows)
    check("атрибуты пишутся в JSONL", n == 1, str(n))
    saved = json.loads(attrs_path.read_text(encoding="utf-8").splitlines()[0])
    check("в JSONL есть и парт-номер, и атрибуты",
          saved["part"] == "MMBT3904" and "Operating Temperature Max" in saved["attributes"],
          str(saved))

    parts_txt = tmp / "parts.txt"
    parts_txt.write_text("MMBT3904\n# comment\n\nSI2301\n", encoding="utf-8")
    check("txt-список читается, комментарии пропускаются",
          element14.read_parts(parts_txt) == ["MMBT3904", "SI2301"],
          str(element14.read_parts(parts_txt)))
    check("--limit обрезает список",
          element14.read_parts(parts_txt, limit=1) == ["MMBT3904"])

    print("\n--- --check ---")
    fake10 = Fake([(200, payload([product("MMBT3904", datasheet="https://x/a.pdf",
                                         stock=900, attributes=attrs)]), {})])
    import io as _io
    import contextlib as _ctx
    element14._default_opener = fake10
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        rc = element14.main(["--check", "--api-key", "testkey000000000000000abc",
                             "--no-cache"])
    text = buf.getvalue()
    check("--check проходит", rc == 0, str(rc))
    check("--check показывает ссылку на datasheet", "https://x/a.pdf" in text, text[:200])
    check("--check показывает остаток вызовов", "Осталось сегодня" in text, text[:200])
    check("--check показывает атрибуты", "150 °C" in text, text[-300:])

    fake11 = Fake([(403, {"fault": "nope"}, {"X-Mashery-Error-Code":
                                             "ERR_403_DEVELOPER_INACTIVE"})])
    element14._default_opener = fake11
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        rc = element14.main(["--check", "--api-key", "testkey000000000000000abc",
                             "--no-cache"])
    check("нерабочий ключ объясняют, а не молчат",
          rc == 1 and "partner.element14.com" in buf.getvalue(),
          buf.getvalue()[:200])

    print("\n--- прогон по списку парт-номеров ---")
    parts_file = tmp / "list.txt"
    parts_file.write_text("MMBT3904\nMISSING1\nSI2301\n", encoding="utf-8")
    fake12 = Fake([
        (200, payload([product("MMBT3904", datasheet="https://x/a.pdf")]), {}),
        (200, payload([]), {}),
        # SI2301: сначала без datasheet, потом точное совпадение со ссылкой
        (200, payload([product("SI2301", datasheet="https://x/c.pdf")]), {}),
    ])
    element14._default_opener = fake12
    out_csv = tmp / "run" / "parts.csv"
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        rc = element14.main(["--parts", str(parts_file), "--to-csv", str(out_csv),
                             "--api-key", "testkey000000000000000abc",
                             "--cache", str(tmp / "run-cache.sqlite3")])
    text = buf.getvalue()
    check("прогон завершается сам", rc == 0, str(rc))
    check("воронка печатается: позиции / с PDF / с производителем",
          "с datasheet-ссылкой: 2" in text and "с производителем:    2" in text,
          text[-400:])
    check("вызовы посчитаны", "вызовов API:         3" in text, text[-400:])
    check("CSV записан", out_csv.exists() and len(fetch_datasheets.read_list(out_csv)) == 2,
          str(out_csv.exists()))
    check("команда для скачивания печатается целиком",
          "fetch_datasheets.py --list" in text and "--ignore-robots" in text,
          text[-300:])

    print("\n--- без ключа ---")
    no_key = False
    try:
        element14.Element14("")
    except SystemExit as exc:
        no_key = "SMD_E14_KEY" in str(exc) or "partner.element14.com" in str(exc)
    check("нет ключа — понятное сообщение, а не трейс", no_key)

    print("\n--- один вызов накрывает серию ---")
    # Запрос «RC0402FR-071KL» приносит и соседей по серии: manuPartNum ищет
    # по вхождению. На бюджете 50 000 вызовов это и есть объём.
    series = [product("RC0402FR-071KL", brand="YAGEO", datasheet="https://x/1k.pdf"),
              product("RC0402FR-072KL", brand="YAGEO", datasheet="https://x/2k.pdf"),
              product("RC0402FR-073KL", brand="YAGEO", datasheet="https://x/3k.pdf")]
    fake13 = Fake([(200, payload(series), {})])
    e14 = client(fake13, tmp / "series")
    best, found = e14.lookup_all("RC0402FR-071KL")
    check("lookup_all отдаёт и точное совпадение, и весь ответ",
          best["part"] == "RC0402FR-071KL" and len(found) == 3, str(found))
    check("на серию ушёл один вызов", e14.calls == 1, str(e14.calls))
    check("lookup по-прежнему отдаёт только лучший товар",
          e14.lookup("RC0402FR-071KL")["part"] == "RC0402FR-071KL")

    series_file = tmp / "series.txt"
    series_file.write_text("RC0402FR-071KL\nRC0402FR-072KL\nRC0402FR-073KL\n",
                           encoding="utf-8")
    fake14 = Fake([(200, payload(series), {})])
    element14._default_opener = fake14
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        element14.main(["--parts", str(series_file), "--to-csv", str(tmp / "s.csv"),
                        "--api-key", "testkey000000000000000abc",
                        "--cache", str(tmp / "s-cache.sqlite3")])
    text = buf.getvalue()
    check("прогон по серии покрыл все позиции одним вызовом",
          "накрыто 3 из 3" in text and "вызовов API:         1" in text, text[-500:])
    check("отчёт показывает детали на вызов", "деталей на вызов: 3.0" in text,
          text[-500:])
    check("и прогноз на сутки", "до 150000 деталей в сутки" in text, text[-500:])
    check("в CSV все три позиции",
          len(fetch_datasheets.read_list(tmp / "s.csv")) == 3)

    print("\n--- потолок пагинации ---")
    page_full = payload([product("P%d" % i) for i in range(3)], total=100)
    fake15 = Fake([(200, page_full, {}), (200, page_full, {}), (200, payload([]), {})])
    e14 = client(fake15, tmp / "probe")
    report = element14.probe_limits(e14, "any:mosfet", per_page=3, max_pages=10)
    check("сколько товаров реально приходит на вызов",
          report["per_page_got"] == 3, str(report))
    check("глубина пагинации измерена",
          report["pages"] == 2 and report["max_offset"] == 3, str(report))
    check("причина остановки названа", "пустая" in report["stopped_on"],
          report["stopped_on"])
    check("пустая первая страница не ломает отчёт",
          element14.probe_limits(client(Fake([(200, payload([]), {})]), tmp / "p2"),
                                 per_page=3)["per_page_got"] == 0)
    check("неполная страница — тоже причина остановки",
          "неполная" in element14.probe_limits(
              client(Fake([(200, payload([product("A")]), {})]), tmp / "p3"),
              per_page=3)["stopped_on"])

    print("\n--- обход каталога списком запросов ---")
    q_file = tmp / "queries.txt"
    q_file.write_text("any:mosfet\n# comment\nany:ldo\n", encoding="utf-8")
    fake16 = Fake([(200, payload([product("M1", datasheet="https://x/m1.pdf"),
                                  product("M2", datasheet="https://x/m2.pdf")]), {}),
                   (200, payload([product("M2", datasheet="https://x/m2.pdf"),
                                  product("L1", datasheet="https://x/l1.pdf")]), {})])
    e14 = client(fake16, tmp / "sweep")
    rows = element14.sweep(e14, q_file.read_text(encoding="utf-8").splitlines(),
                           per_page=3, max_pages=2)
    check("обход собирает позиции из всех запросов",
          [r["part"] for r in rows] == ["M1", "M2", "L1"],
          str([r["part"] for r in rows]))
    check("комментарии в списке запросов пропущены", e14.calls == 2, str(e14.calls))
    check("дубли между запросами отброшены",
          len({r["part"] for r in rows}) == 3)

    state_path = tmp / "state.json"
    fake17 = Fake([(200, payload([product("M1", datasheet="https://x/m1.pdf")]), {}),
                   (200, payload([product("L1", datasheet="https://x/l1.pdf")]), {})])
    element14._default_opener = fake17
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        element14.main(["--queries", str(q_file), "--state", str(state_path),
                        "--to-csv", str(tmp / "q.csv"),
                        "--api-key", "testkey000000000000000abc",
                        "--cache", str(tmp / "q-cache.sqlite3"), "--max-pages", "1"])
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    check("state запоминает сделанные запросы",
          sorted(saved["done"]) == ["any:ldo", "any:mosfet"], str(saved["done"]))
    check("state запоминает виденные позиции",
          sorted(saved["seen"]) == ["L1", "M1"], str(saved["seen"]))
    fake18 = Fake([])          # транспорт без ответов: всё должно быть в кэше
    element14._default_opener = fake18
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        rc = element14.main(["--queries", str(q_file), "--state", str(state_path),
                             "--to-csv", str(tmp / "q2.csv"),
                             "--api-key", "testkey000000000000000abc",
                             "--cache", str(tmp / "q-cache.sqlite3"),
                             "--max-pages", "1"])
    check("второй день не повторяет сделанные запросы",
          "Осталось" not in buf.getvalue() and rc == 0, buf.getvalue()[:200])
    check("и не тратит ни одного вызова",
          "вызовов API:         0" in buf.getvalue(), buf.getvalue()[-400:])

    print("\n--- не тратим вызовы на то, что уже есть ---")
    cards_db = tmp / "cards.db"
    import sqlite3 as _sql
    con = _sql.connect(str(cards_db))
    con.execute("CREATE TABLE cards (part TEXT PRIMARY KEY, part_key TEXT NOT NULL,"
                " manufacturer TEXT, package TEXT, confidence REAL, card TEXT)")
    con.executemany(
        "INSERT INTO cards VALUES (?,?,?,?,?,?)",
        [("GOOD1", "GOOD1", "ONSEMI", "SOT-23", 0.9, "{}"),
         ("NOPKG", "NOPKG", "ONSEMI", "", 0.9, "{}"),      # нет корпуса — идём в API
         ("WEAK1", "WEAK1", "ONSEMI", "SOT-23", 0.2, "{}"),  # низкая уверенность
         ("NOMFR", "NOMFR", "", "SOT-23", 0.9, "{}")])
    con.commit(); con.close()
    good = element14.load_known_good(cards_db)
    # порог 0 — «любая карточка с производителем и корпусом годилась»
    check("хорошие карточки распознаны", good == {"GOOD1", "WEAK1"}, str(good))
    check("без корпуса или производителя — не помеха",
          "NOPKG" not in good and "NOMFR" not in good, str(good))
    check("порог уверенности работает",
          "WEAK1" in element14.load_known_good(cards_db, min_confidence=0.1)
          and "WEAK1" not in element14.load_known_good(cards_db, min_confidence=0.5),
          str(element14.load_known_good(cards_db, min_confidence=0.5)))
    check("нет файла — просто пустое множество",
          element14.load_known_good(tmp / "nope.db") == set())

    print("\n--- прогноз по дням ---")
    check("всё покрыто — без дней", element14.plan_eta(0, 50000, 8) == "всё покрыто",
          element14.plan_eta(0, 50000, 8))
    check("50 000 деталей — один день", "1 дн." in element14.plan_eta(50000, 50000, 8),
          element14.plan_eta(50000, 50000, 8))
    check("300 000 деталей — шесть дней",
          "6 дн." in element14.plan_eta(300000, 50000, 8),
          element14.plan_eta(300000, 50000, 8))
    check("время работы в день посчитано", "1.7 ч" in element14.plan_eta(300000, 50000, 8),
          element14.plan_eta(300000, 50000, 8))

    print("\n--- прогон: пропуск готового и остаток на завтра ---")
    parts_file = tmp / "plan.txt"
    parts_file.write_text("GOOD1\nNEW1\nNEW2\n", encoding="utf-8")
    fake19 = Fake([(200, payload([product("NEW1", datasheet="https://x/n1.pdf")]), {}),
                   (200, payload([]), {})])
    element14._default_opener = fake19
    plan_out = tmp / "tomorrow.txt"
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        element14.main(["--parts", str(parts_file), "--to-csv", str(tmp / "p.csv"),
                        "--api-key", "testkey000000000000000abc",
                        "--cache", str(tmp / "p-cache.sqlite3"),
                        "--skip-good", str(cards_db),
                        "--plan-out", str(plan_out), "--day-limit", "1"])
    text = buf.getvalue()
    check("по готовой карточке вызов не делается",
          "уже есть хорошая карточка: 1" in text and "к обработке: 2" in text,
          text[:300])
    check("прогноз напечатан", "Прогноз:" in text, text[:300])
    check("NEW2 остался на завтра (бюджет кончился на NEW1)",
          plan_out.exists() and plan_out.read_text(encoding="utf-8").split() == ["NEW2"],
          str(plan_out.read_text(encoding="utf-8") if plan_out.exists() else None))

    print("\n--- чего нет в каталоге, считаем отдельно ---")
    fake20 = Fake([(200, payload([product("NEW1", datasheet="https://x/n1.pdf")]), {}),
                   (200, payload([]), {})])
    element14._default_opener = fake20
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        element14.main(["--parts", str(parts_file), "--to-csv", str(tmp / "p2.csv"),
                        "--api-key", "testkey000000000000000abc",
                        "--cache", str(tmp / "p2-cache.sqlite3"),
                        "--skip-good", str(cards_db)])
    text = buf.getvalue()
    check("чего нет в каталоге, посчитано отдельно",
          "нет в каталоге element14: 1" in text, text[-500:])
    check("и это не попадает в остаток на завтра",
          "осталось необработанных" not in text, text[-500:])

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
