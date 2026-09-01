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
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import fetch_datasheets as fd  # noqa: E402
from tools.rag.sample_datasheets import DATASHEETS, build_pdf  # noqa: E402

PASS = FAIL = 0
STATE = {"flaky_hits": 0}
ROBOTS = {"mode": "allow"}


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
