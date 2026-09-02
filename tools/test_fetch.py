#!/usr/bin/env python3
"""Tests for the datasheet downloader — against a local HTTP server.

Outbound network is unavailable in most sandboxes (and pointless in CI), so
the server here is a stub: it hands out real PDF bytes, HTML error pages,
404s and a 500 that heals after two tries. What is being tested is the
behaviour that matters when you download 300k files — does it verify what it
got, does it retry, does it de-duplicate, does it respect robots.

    python3 tools/test_fetch.py
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import fetch_datasheets as fd  # noqa: E402
from tools.rag.sample_datasheets import DATASHEETS, build_pdf  # noqa: E402

PASS = FAIL = 0
STATE = {"flaky_hits": 0, "inflight": 0, "max_inflight": 0}
ROBOTS = {"mode": "allow"}
SLOW_PDFS = {}          # /slow/<name>.pdf -> distinct bytes, filled in main()


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


class _Handler(BaseHTTPRequestHandler):
    pdf_a = b""
    pdf_b = b""
    pdf_c = b""
    pdf_d = b""

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/robots.txt":
            if ROBOTS["mode"] == "deny":
                return self._send(200, b"User-agent: *\nDisallow: /\n", "text/plain")
            return self._send(200, b"User-agent: *\nAllow: /\n", "text/plain")
        if path == "/good.pdf":
            return self._send(200, self.pdf_a, "application/pdf")
        if path == "/good2.pdf":
            return self._send(200, self.pdf_b, "application/pdf")
        if path == "/same.pdf":
            return self._send(200, self.pdf_a, "application/pdf")
        if path == "/html.pdf":
            return self._send(200, b"<html><body>Sign in to continue</body></html>",
                              "text/html")
        if path == "/tiny.pdf":
            return self._send(200, b"%PDF-1.4 broken", "application/pdf")
        if path == "/flaky.pdf":
            STATE["flaky_hits"] += 1
            if STATE["flaky_hits"] < 3:
                return self._send(500, b"server error", "text/plain")
            return self._send(200, self.pdf_b, "application/pdf")
        if path == "/good3.pdf":
            return self._send(200, self.pdf_d, "application/pdf")
        if path == "/no-extension":
            return self._send(200, self.pdf_c, "application/pdf")
        if path.startswith("/slow/"):
            # Deliberately slow: a parallel run must overlap these, a
            # sequential one cannot. The high-water mark is the proof.
            name = path.rsplit("/", 1)[-1]
            with threading.Lock():
                STATE["inflight"] += 1
                STATE["max_inflight"] = max(STATE["max_inflight"], STATE["inflight"])
            time.sleep(0.2)
            with threading.Lock():
                STATE["inflight"] -= 1
            return self._send(200, SLOW_PDFS.get(name, self.pdf_a), "application/pdf")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-fetch-"))
    source = tmp / "source"
    source.mkdir()
    pdf_a = build_pdf(DATASHEETS[0], source).read_bytes()
    spec_b = dict(DATASHEETS[1]) if len(DATASHEETS) > 1 else dict(DATASHEETS[0])
    spec_b["part"] = "OTHERPART"
    pdf_b = build_pdf(spec_b, source).read_bytes()
    spec_c = dict(DATASHEETS[0])
    spec_c["part"] = "THIRDPART"
    pdf_c = build_pdf(spec_c, source).read_bytes()
    spec_d = dict(DATASHEETS[0])
    spec_d["part"] = "FOURTHPART"
    pdf_d = build_pdf(spec_d, source).read_bytes()
    _Handler.pdf_a, _Handler.pdf_b = pdf_a, pdf_b
    _Handler.pdf_c, _Handler.pdf_d = pdf_c, pdf_d
    for i in range(8):                      # distinct bytes, or dedup eats them
        spec = dict(DATASHEETS[0])
        spec["part"] = "SLOWPART%d" % i
        SLOW_PDFS["slow%d.pdf" % i] = build_pdf(spec, source).read_bytes()

    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    out = tmp / "corpus"

    try:
        fetcher = fd.Fetcher(out, delay=0.0, retries=3, timeout=10, verbose=False)

        print("--- скачивание и проверка содержимого ---")
        row = fetcher.fetch_one({"part": "MMBT3904", "manufacturer": "onsemi",
                                 "package": "SOT-23", "url": base + "/good.pdf"})
        check("PDF сохранён", bool(row["file"]) and (out / row["file"]).exists(),
              str(row))
        check("в манифесте есть sha1", len(row["sha1"]) == 40, str(row.get("sha1")))
        check("размер совпадает", row["bytes"] == len(pdf_a), str(row["bytes"]))
        check("имя взято из URL", row["file"] == "good.pdf", row["file"])
        check("происхождение записано", row["url"].endswith("/good.pdf"))

        bad = fetcher.fetch_one({"part": "SIGNIN", "url": base + "/html.pdf"})
        check("HTML вместо PDF отклонён", "not a PDF" in bad["error"], bad["error"])
        check("HTML не сохранён на диск", not bad["file"])

        tiny = fetcher.fetch_one({"part": "TINY", "url": base + "/tiny.pdf"})
        check("обрезанный PDF отклонён", "not a PDF" in tiny["error"], tiny["error"])

        missing = fetcher.fetch_one({"part": "GONE", "url": base + "/nope.pdf"})
        check("404 помечен как HTTP 404", missing["status"] == 404, str(missing["status"]))

        print("\n--- повторы при 500 ---")
        flaky = fetcher.fetch_one({"part": "FLAKY", "url": base + "/flaky.pdf"})
        check("после двух 500 файл получен", bool(flaky["file"]), str(flaky))
        check("попыток было три", STATE["flaky_hits"] == 3, str(STATE["flaky_hits"]))

        print("\n--- дедупликация ---")
        before = len(list(out.glob("*.pdf")))
        dup = fetcher.fetch_one({"part": "MMBT3904", "url": base + "/same.pdf"})
        after = len(list(out.glob("*.pdf")))
        check("тот же PDF не сохранён второй раз", dup["error"].startswith("duplicate"),
              dup["error"])
        check("файлов на диске не прибавилось", before == after, "%d -> %d" % (before, after))

        print("\n--- имя файла, когда в URL нет .pdf ---")
        noext = fetcher.fetch_one({"part": "NOEXT", "url": base + "/no-extension"})
        check("имя собрано из парт-номера", noext["file"] == "NOEXT.pdf", noext["file"])

        print("\n--- манифест ---")
        manifest_rows = []
        for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                manifest_rows.append(json.loads(line))
        check("в манифесте по строке на попытку", len(manifest_rows) == 7,
              str(len(manifest_rows)))
        check("у каждой строки есть время и статус",
              all("downloaded_at" in r and "status" in r for r in manifest_rows))
        reloaded = fd.Fetcher(out, delay=0.0)
        reloaded.load_manifest()
        check("sha1 подхватывается из манифеста", pdf_a_sha1(reloaded, pdf_a))

        print("\n--- robots.txt ---")
        ROBOTS["mode"] = "deny"
        polite = fd.Fetcher(out, delay=0.0, respect_robots=True)
        polite.load_manifest()
        blocked = polite.fetch_one({"part": "BLOCKED", "url": base + "/good3.pdf"})
        check("запрещённый путь не качается", blocked["error"].startswith("robots"),
              blocked["error"])
        rude = fd.Fetcher(out, delay=0.0, respect_robots=False)
        rude.load_manifest()
        forced = rude.fetch_one({"part": "BLOCKED", "url": base + "/good3.pdf"})
        check("--ignore-robots качает всё-таки", bool(forced["file"]), str(forced))
        ROBOTS["mode"] = "allow"

        print("\n--- список деталей ---")
        csv_path = tmp / "parts.csv"
        csv_path.write_text(
            "part,manufacturer,package,url\n"
            "MMBT3904,onsemi,SOT-23,%s/good.pdf\n"
            "NOPDF,Vendor,SOT-23,\n" % base, encoding="utf-8")
        rows = fd.read_list(csv_path)
        check("CSV читается", len(rows) == 2 and rows[0]["part"] == "MMBT3904", str(rows[:1]))
        check("у второй строки url пуст", rows[1].get("url") in ("", None))

        json_path = tmp / "parts.json"
        json_path.write_text(json.dumps([{"part": "A", "url": base + "/good.pdf"},
                                         "B"]), encoding="utf-8")
        rows_j = fd.read_list(json_path)
        check("JSON читается, строки допустимы", len(rows_j) == 2 and rows_j[1]["part"] == "B")

        parts_txt = tmp / "parts.txt"
        parts_txt.write_text("STM32F103C8\n\n# comment\nNE555\n", encoding="utf-8")
        rows_v = fd.read_list(parts_txt, vendor="st", parts_file=parts_txt)
        check("пустые строки и комментарии отброшены", len(rows_v) == 2, str(rows_v))
        check("URL собран по шаблону вендора",
              rows_v[0]["url"].endswith("/stm32f103c8.pdf"), rows_v[0]["url"])
        check("шаблон onsemi добавляет -d",
              fd.build_url("onsemi", "MMBT3904").endswith("mmbt3904-d.pdf"),
              fd.build_url("onsemi", "MMBT3904"))
        check("шаблон microchip сохраняет регистр",
              "PIC32MM" in fd.build_url("microchip", "PIC32MM"))

        print("\n--- смешанные производители в одном списке ---")
        mixed = tmp / "mixed.csv"
        mixed.write_text(
            "part,manufacturer,package\n"
            "MMBT3904,onsemi,SOT-23\n"
            "STM32F103C8,STMicroelectronics,LQFP-48\n"
            "PIC32MM,Microchip,QFN\n"
            "NE555,Texas Instruments,SOIC-8\n", encoding="utf-8")
        rows_m = fd.read_list(mixed)
        urls = {r["part"]: r.get("url", "") for r in rows_m}
        check("у каждой детали URL своего вендора",
              "onsemi.com" in urls["MMBT3904"]
              and "st.com" in urls["STM32F103C8"]
              and "microchip.com" in urls["PIC32MM"]
              and "ti.com" in urls["NE555"], str(urls))
        check("--vendor не подменяет чужой URL",
              "st.com" in fd.read_list(mixed, vendor="st")[0]["url"] is False
              or "onsemi" in fd.read_list(mixed, vendor="st")[0]["url"],
              fd.read_list(mixed, vendor="st")[0]["url"])
        check("алиас 'Texas Instruments' понимается",
              fd.vendor_of({"manufacturer": "Texas Instruments"}) == "ti")
        check("алиас 'Diodes Incorporated' понимается",
              fd.vendor_of({"manufacturer": "Diodes Incorporated"}) == "diodes")
        check("неизвестный производитель — пусто",
              fd.vendor_of({"manufacturer": "Mystery Corp"}) == "")
        print("\n--- параллельная загрузка (--workers) ---")
        par_out = tmp / "parallel"
        par_out.mkdir()
        par = fd.Fetcher(par_out, delay=0.02, retries=1, timeout=10)
        many = [{"part": "SLOWPART%d" % i, "manufacturer": "test",
                 "package": "SOT-23", "url": base + "/slow/slow%d.pdf" % i}
                for i in range(8)]
        STATE["max_inflight"] = 0
        started = time.time()
        results = par.fetch_many(many, workers=8)
        parallel_elapsed = time.time() - started
        check("все 8 файлов скачаны",
              sum(1 for r in results if r.get("file")) == 8,
              str([r.get("file") or r.get("error") for r in results]))
        check("порядок результатов совпадает с порядком заявки",
              [r.get("file") for r in results] == ["slow%d.pdf" % i for i in range(8)],
              str([r.get("file") for r in results]))
        check("запросы шли параллельно, не по одному",
              STATE["max_inflight"] >= 3, str(STATE["max_inflight"]))
        check("параллельно быстрее последовательного",
              parallel_elapsed < 0.2 * 8 * 0.75, "%.2fs" % parallel_elapsed)
        files = sorted(p.name for p in par_out.glob("*.pdf"))
        check("на диске 8 разных файлов", len(files) == 8 and len(set(files)) == 8,
              str(files))
        manifest = [json.loads(l) for l in
                    (par_out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        check("манифест не перетёрся потоками", len(manifest) == 8, str(len(manifest)))
        check("каждая строка манифеста — валидный json",
              all(r.get("part", "").startswith("SLOWPART") for r in manifest),
              str(manifest[:2]))
        check("счётчики совпадают с фактом",
              par.stats["ok"] == 8 and par.stats["failed"] == 0, str(par.stats))

        # two rows, same file: dedup has to survive the threads too
        dup_out = tmp / "parallel-dup"
        dup_out.mkdir()
        duper = fd.Fetcher(dup_out, delay=0.0, retries=1, timeout=10)
        dup_rows = [{"part": "DUPA", "url": base + "/slow/slow0.pdf"},
                    {"part": "DUPB", "url": base + "/slow/slow0.pdf"}]
        dup_results = duper.fetch_many(dup_rows, workers=4)
        check("одинаковый файл сохранён один раз и в параллельном режиме",
              sum(1 for r in dup_results if r.get("file")) == 1,
              str([r.get("file") or r.get("error") for r in dup_results]))
        check("на диске один PDF", len(list(dup_out.glob("*.pdf"))) == 1)

        seq_out = tmp / "sequential"
        seq_out.mkdir()
        seq = fd.Fetcher(seq_out, delay=0.0, retries=1, timeout=10)
        seq_results = seq.fetch_many(many, workers=1)
        check("workers=1 даёт тот же результат, что и раньше",
              sum(1 for r in seq_results if r.get("file")) == 8,
              str([r.get("file") or r.get("error") for r in seq_results]))

        print("\n--- экспорт списка из каталога JLCPCB/LCSC ---")
        db = tmp / "cache.sqlite3"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT, subcategory TEXT);
            CREATE TABLE parts (
                lcsc TEXT, mfr TEXT, package TEXT, joints INTEGER,
                manufacturer_id INTEGER, category_id INTEGER,
                basic INTEGER, description TEXT, datasheet TEXT, stock INTEGER);
        """)
        con.executemany("INSERT INTO manufacturers VALUES (?,?)",
                        [(1, "onsemi"), (2, "Texas Instruments")])
        con.executemany("INSERT INTO categories VALUES (?,?,?)",
                        [(1, "Transistors", "Bipolar"), (2, "Power", "LDO")])
        con.executemany(
            "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?)",
            [("C1", "MMBT3904", "SOT-23", 3, 1, 1, 1, "NPN", "https://x/MMBT3904.pdf", 500),
             ("C2", "TPS7A05", "SOT-23-5", 5, 2, 2, 0, "LDO", "https://x/TPS7A05.pdf", 0),
             ("C3", "NODATA", "SOT-23", 3, 1, 1, 1, "no link", "", 100)])
        con.commit(); con.close()

        exported = tmp / "parts_from_db.csv"
        n = fd.export_from_sqlite(db, exported)
        rows_e = fd.read_list(exported)
        check("экспортировано столько, сколько со ссылками", n == 2, str(n))
        check("колонки на месте", list(rows_e[0].keys())[:4] ==
              ["part", "manufacturer", "package", "url"], str(list(rows_e[0].keys())))
        check("производитель подтянут из отдельной таблицы",
              {r["part"]: r["manufacturer"] for r in rows_e} ==
              {"MMBT3904": "onsemi", "TPS7A05": "Texas Instruments"}, str(rows_e))
        check("корпус и URL попали в CSV",
              rows_e[0]["package"] == "SOT-23" and rows_e[0]["url"].endswith(".pdf"))

        only_basic = tmp / "basic.csv"
        fd.export_from_sqlite(db, only_basic, basic_only=True)
        check("--basic-only отфильтровал",
              [r["part"] for r in fd.read_list(only_basic)] == ["MMBT3904"],
              str([r["part"] for r in fd.read_list(only_basic)]))

        in_stock = tmp / "stock.csv"
        fd.export_from_sqlite(db, in_stock, min_stock=1000)
        check("--min-stock отфильтровал", len(fd.read_list(in_stock)) == 0)

        by_cat = tmp / "cat.csv"
        fd.export_from_sqlite(db, by_cat, category="Bipolar")
        check("--category отфильтровал",
              [r["part"] for r in fd.read_list(by_cat)] == ["MMBT3904"],
              str([r["part"] for r in fd.read_list(by_cat)]))

        print("\n--- база другой формы не ломает экспорт ---")
        db2 = tmp / "other.sqlite3"
        con = sqlite3.connect(str(db2))
        con.executescript("""
            CREATE TABLE parts (part_number TEXT, footprint TEXT, datasheet_url TEXT);
            INSERT INTO parts VALUES ('NE555','SOIC-8','https://x/ne555.pdf');
        """)
        con.commit(); con.close()
        out2 = tmp / "other.csv"
        n2 = fd.export_from_sqlite(db2, out2)
        check("альтернативные имена колонок понимаются", n2 == 1, str(n2))
        check("корпус взят из footprint",
              fd.read_list(out2)[0]["package"] == "SOIC-8",
              str(fd.read_list(out2)[0]))

        print("\n--- настоящая схема jlcparts: components + view ---")
        dbj = tmp / "jlc-real.sqlite3"
        con = sqlite3.connect(str(dbj))
        con.executescript("""
            CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT,
                                     subcategory TEXT);
            CREATE TABLE components (
                lcsc INTEGER PRIMARY KEY, category_id INTEGER, mfr TEXT,
                package TEXT, joints INTEGER, manufacturer_id INTEGER,
                basic INTEGER, preferred INTEGER DEFAULT 0, description TEXT,
                datasheet TEXT, stock INTEGER, price TEXT, last_update INTEGER,
                extra TEXT, flag INTEGER DEFAULT 0);
            INSERT INTO manufacturers VALUES (1,'onsemi'),(2,'Texas Instruments');
            INSERT INTO categories VALUES (1,'Transistors','Bipolar'),
                                          (2,'Power Management','LDO');
            INSERT INTO components VALUES
                (12345,1,'MMBT3904','SOT-23',3,1,1,0,'NPN',
                 'https://x/MMBT3904.pdf',900,'[]',0,'{}',0),
                (12346,2,'TPS7A05','SOT-23-5',5,2,0,1,'LDO',
                 'https://x/TPS7A05.pdf',10,'[]',0,'{}',0),
                (12347,1,'NODATA','SOT-23',3,1,1,0,'?','',0,'[]',0,'{}',0);
            CREATE VIEW v_components AS
                SELECT c.lcsc AS lcsc, c.category_id AS category_id,
                       cat.category AS category, cat.subcategory AS subcategory,
                       c.mfr AS mfr, c.package AS package, c.joints AS joints,
                       m.name AS manufacturer, c.basic AS basic,
                       c.preferred AS preferred, c.description AS description,
                       c.datasheet AS datasheet, c.stock AS stock
                FROM components c
                LEFT JOIN manufacturers m ON c.manufacturer_id = m.id
                LEFT JOIN categories cat ON c.category_id = cat.id;
        """)
        con.commit(); con.close()

        real = tmp / "real.csv"
        n_real = fd.export_from_sqlite(dbj, real)
        rows_real = fd.read_list(real)
        check("экспортированы обе позиции со ссылкой", n_real == 2, str(n_real))
        check("mfr — это парт-номер, а не производитель",
              {r["part"] for r in rows_real} == {"MMBT3904", "TPS7A05"},
              str([r["part"] for r in rows_real]))
        check("производитель взят из view",
              {r["part"]: r["manufacturer"] for r in rows_real} ==
              {"MMBT3904": "onsemi", "TPS7A05": "Texas Instruments"}, str(rows_real))
        check("код LCSC приведён к виду C12345",
              rows_real[0]["lcsc"] == "C12345", str(rows_real[0].get("lcsc")))

        forced = tmp / "forced.csv"
        fd.export_from_sqlite(dbj, forced, table="components")
        check("та же выгрузка и без view, через join",
              [r["part"] for r in fd.read_list(forced)] ==
              [r["part"] for r in rows_real],
              str([r["part"] for r in fd.read_list(forced)]))

        pref = tmp / "pref.csv"
        fd.export_from_sqlite(dbj, pref, basic_only=True)
        check("--basic-only берёт и basic, и preferred",
              [r["part"] for r in fd.read_list(pref)] == ["MMBT3904", "TPS7A05"],
              str([r["part"] for r in fd.read_list(pref)]))

        stocky = tmp / "stocky.csv"
        fd.export_from_sqlite(dbj, stocky, min_stock=100)
        check("--min-stock считает по колонке stock",
              [r["part"] for r in fd.read_list(stocky)] == ["MMBT3904"],
              str([r["part"] for r in fd.read_list(stocky)]))

        cats = tmp / "cats.csv"
        fd.export_from_sqlite(dbj, cats, category="LDO")
        check("--category ищет и по подкатегории",
              [r["part"] for r in fd.read_list(cats)] == ["TPS7A05"],
              str([r["part"] for r in fd.read_list(cats)]))

        print("\n--- v2-схема кэша: jlc_components без колонки basic ---")
        # Именно это и случилось на настоящем кэше: колонки `basic` нет,
        # --basic-only молча фильтрует по `preferred`, а их ~1000 на 7.1 млн —
        # отсюда выгрузка на 974 строки вместо миллиона.
        dbv = tmp / "jlc-v2.sqlite3"
        con = sqlite3.connect(str(dbv))
        con.executescript("""
            CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jlc_components (
                lcsc INTEGER PRIMARY KEY, mfr TEXT, package TEXT,
                manufacturer_id INTEGER, description TEXT, datasheet TEXT,
                stock INTEGER, preferred INTEGER DEFAULT 0,
                library_type TEXT);
            INSERT INTO manufacturers VALUES (1,'onsemi'),(2,'Texas Instruments');
            -- одна серия резисторов: три позиции, один и тот же PDF
            INSERT INTO jlc_components VALUES
                (1,'RC0402FR-071KL','0402',1,'res 1k','https://x/series.pdf',900000,0,'base'),
                (2,'RC0402FR-072KL','0402',1,'res 2k','https://x/series.pdf',500,0,'base'),
                (3,'RC0402FR-073KL','0402',1,'res 3k','https://x/series.pdf',10,0,'base'),
                (4,'MMBT3904','SOT-23',1,'NPN','https://x/MMBT3904.pdf',700000,1,'base'),
                (5,'NODATA','SOT-23',2,'?','',0,0,'expand'),
                (6,'TPS7A05','SOT-23-5',2,'LDO','https://x/TPS7A05.pdf',42,0,'expand');
            CREATE VIEW v_components AS SELECT * FROM jlc_components;
        """)
        # v_components не должно перекрывать v2-таблицу — но если оно есть,
        # экспорт берёт его; проверим обе ветки:
        con.execute("DROP VIEW v_components")
        con.commit(); con.close()

        v_all = tmp / "v-all.csv"
        n_all = fd.export_from_sqlite(dbv, v_all, explain=False)
        check("без --basic-only выгружаются все позиции со ссылкой",
              n_all == 5, str(n_all))

        v_basic = tmp / "v-basic.csv"
        n_basic = fd.export_from_sqlite(dbv, v_basic, basic_only=True, explain=False)
        check("--basic-only без колонки basic даёт только preferred",
              [r["part"] for r in fd.read_list(v_basic)] == ["MMBT3904"],
              str([r["part"] for r in fd.read_list(v_basic)]))
        check("...и их действительно кот наплакал", n_basic == 1, str(n_basic))

        v_dedupe = tmp / "v-dedupe.csv"
        fd.export_from_sqlite(dbv, v_dedupe, dedupe=True, explain=False)
        urls = [r["url"] for r in fd.read_list(v_dedupe)]
        check("--dedupe оставляет одну строку на PDF",
              len(urls) == len(set(urls)) and len(urls) == 3,
              str(urls))

        v_pop = tmp / "v-pop.csv"
        fd.export_from_sqlite(dbv, v_pop, popular_first=True, explain=False)
        order = [r["part"] for r in fd.read_list(v_pop)]
        check("--popular-first: первым идёт самый складской",
              order[0] == "RC0402FR-071KL", str(order))

        v_pop2 = tmp / "v-pop2.csv"
        fd.export_from_sqlite(dbv, v_pop2, popular_first=True, dedupe=True,
                              limit=2, explain=False)
        check("--limit вместе с --popular-first берёт верхние по складу",
              [r["part"] for r in fd.read_list(v_pop2)] ==
              ["RC0402FR-071KL", "MMBT3904"],
              str([r["part"] for r in fd.read_list(v_pop2)]))

        v_lib = tmp / "v-lib.csv"
        fd.export_from_sqlite(dbv, v_lib, library_type="base", explain=False)
        check("--library-type фильтрует по типу библиотеки",
              {r["part"] for r in fd.read_list(v_lib)} ==
              {"RC0402FR-071KL", "RC0402FR-072KL", "RC0402FR-073KL", "MMBT3904"},
              str([r["part"] for r in fd.read_list(v_lib)]))

        import io as _io
        import contextlib as _ctx
        buf = _io.StringIO()
        with _ctx.redirect_stdout(buf):
            fd.export_from_sqlite(dbv, tmp / "v-explain.csv", basic_only=True,
                                  explain=True)
        printed = buf.getvalue()
        check("воронка считает строки по каждому фильтру",
              "строк всего" in printed and "basic/preferred = 1" in printed,
              printed[:120])
        check("про отсутствие колонки basic предупреждают вслух",
              "колонки `basic` в этой версии кэша нет" in printed, printed[-200:])

        probe = fd.probe_cache(dbv)
        splits = {label: n for label, n in probe.get("splits", [])}
        check("--probe показывает, сколько даёт каждый фильтр",
              splits.get("preferred = 1") == 1
              and splits.get("library_type = base") == 4
              and splits.get("library_type = expand") == 2
              and splits.get("есть ссылка на PDF") == 5, str(splits))
        check("--probe считает строки в самой таблице",
              any(t["name"] == "jlc_components" and t["rows"] == 6
                  for t in probe["tables"]), str(probe["tables"]))

        print("\n--- ссылки на сайт производителя ---")
        vendor_csv = tmp / "vendor.csv"
        fd.export_from_sqlite(dbj, vendor_csv, prefer_vendor=True)
        rows_v = fd.read_list(vendor_csv)
        urls_v = {r["part"]: r["url"] for r in rows_v}
        check("MMBT3904 ведёт на onsemi.com",
              "onsemi.com" in urls_v.get("MMBT3904", ""), str(urls_v))
        check("TPS7A05 ведёт на ti.com",
              "ti.com" in urls_v.get("TPS7A05", ""), str(urls_v))
        check("ссылка каталога сохранена в source_url",
              rows_v[0]["source_url"].startswith("https://x/"),
              str(rows_v[0].get("source_url")))
        check("без флага ссылки остаются каталожными",
              all("x/" in r["url"] for r in rows_real), str([r["url"] for r in rows_real]))

        mystery = tmp / "mystery.sqlite3"
        con = sqlite3.connect(str(mystery))
        con.executescript("""
            CREATE TABLE parts (mfr TEXT, manufacturer TEXT, package TEXT, datasheet TEXT);
            INSERT INTO parts VALUES ('X1','Mystery Corp','SOT-23','https://x/x1.pdf');
        """)
        con.commit(); con.close()
        mys_csv = tmp / "mystery.csv"
        fd.export_from_sqlite(mystery, mys_csv, prefer_vendor=True)
        row_m = fd.read_list(mys_csv)[0]
        check("незнакомый производитель — остаётся ссылка каталога",
              row_m["url"] == "https://x/x1.pdf", str(row_m))

        print("\n--- резервная ссылка, если URL производителя не сработал ---")
        fb_out = tmp / "fallback"
        fb_out.mkdir()
        fback = fd.Fetcher(fb_out, delay=0.0, retries=0, timeout=10)
        res = fback.fetch_one({"part": "FALLBACK", "url": base + "/nope.pdf",
                               "source_url": base + "/good3.pdf"})
        check("при 404 берётся ссылка из source_url",
              bool(res.get("file")) and res["url"].endswith("good3.pdf"), str(res))
        check("в манифесте отмечено, что сработал резерв",
              res.get("fallback") is True, str(res))
        check("файл реально сохранён",
              (fb_out / res["file"]).exists() if res.get("file") else False, str(res))

        print("\n--- повторный URL не качается второй раз ---")
        once_more = fd.Fetcher(out, delay=0.0, retries=1, timeout=10)
        once_more.load_manifest()
        again = once_more.fetch_one({"part": "MMBT3904", "url": base + "/good.pdf"})
        check("тот же URL не уходит в сеть повторно",
              again["error"].startswith("already fetched"), again["error"])
        check("повтор не считается новым файлом", once_more.stats["ok"] == 0,
              str(once_more.stats))

        print("\n--- разведка по кэшу каталога ---")
        probe = fd.probe_cache(dbj)
        names = [t["name"] for t in probe["tables"]]
        check("таблицы перечислены",
              "components" in names and "manufacturers" in names, str(names))
        check("представление видно с пометкой view",
              any(t["name"] == "v_components" and t["kind"] == "view"
                  for t in probe["tables"]), str(probe["tables"]))
        by_name = {t["name"]: t for t in probe["tables"]}
        check("число строк посчитано",
              by_name["components"]["rows"] == 3, str(by_name["components"]))
        check("колонки видны",
              "datasheet" in by_name["components"]["columns"],
              str(by_name["components"]["columns"][:8]))
        check("чужая база не роняет разведку",
              fd.probe_cache(tmp / "no-such-file.sqlite3")["error"] != "")

        print("\n--- отчёт по манифесту ---")
        fake = tmp / "manifest-report.jsonl"
        lines = []
        for i in range(3):
            lines.append(json.dumps({"part": "P%d" % i, "url": "https://a.test/x%d.pdf" % i,
                                     "file": "x%d.pdf" % i, "bytes": 1024 * 1024,
                                     "sha1": "a" * 40, "status": 200}))
        lines.append(json.dumps({"part": "DUP", "url": "https://a.test/x0.pdf",
                                 "file": "", "bytes": 0, "sha1": "b" * 40,
                                 "error": "duplicate of x0.pdf", "status": 200}))
        lines.append(json.dumps({"part": "DEAD", "url": "https://b.test/y.pdf",
                                 "file": "", "bytes": 0, "sha1": "",
                                 "error": "HTTP 404", "status": 404}))
        fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rep = fd.manifest_report(fake)
        check("попыток посчитано", rep["attempts"] == 5, str(rep["attempts"]))
        check("скачанных три", rep["counts"]["ok"] == 3, str(rep["counts"]))
        check("дубль посчитан", rep["counts"]["duplicate"] == 1, str(rep["counts"]))
        check("сбой посчитан", rep["counts"]["failed"] == 1, str(rep["counts"]))
        check("место посчитано", rep["bytes"] == 3 * 1024 * 1024, str(rep["bytes"]))
        check("средний размер посчитан", rep["avg_bytes"] == 1024 * 1024,
              str(rep["avg_bytes"]))
        check("доля дублей посчитана", rep["dup_rate"] == 20.0, str(rep["dup_rate"]))
        check("хосты собраны", ("a.test", 4) in rep["hosts"], str(rep["hosts"]))
        check("причины сбоев собраны", any("HTTP 404" in e for e, _ in rep["errors"]),
              str(rep["errors"]))
        check("прогноз на 300 000 считается без деления на ноль",
              fd.manifest_report(tmp / "empty.jsonl")["avg_bytes"] == 0
              if (tmp / "empty.jsonl").write_text("", encoding="utf-8") is None else True)

        db3 = tmp / "broken.sqlite3"
        con = sqlite3.connect(str(db3))
        con.executescript("CREATE TABLE parts (foo TEXT, bar TEXT);")
        con.commit(); con.close()
        try:
            fd.export_from_sqlite(db3, tmp / "never.csv")
            check("непохожая схема — понятная ошибка", False, "исключения не было")
        except SystemExit as exc:
            check("непохожая схема — понятная ошибка",
                  "cannot map" in str(exc), str(exc)[:60])

    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


def pdf_a_sha1(fetcher: fd.Fetcher, body: bytes) -> bool:
    import hashlib
    sha1 = hashlib.sha1(body).hexdigest()
    return sha1 in fetcher.seen_sha1


if __name__ == "__main__":
    raise SystemExit(main())
