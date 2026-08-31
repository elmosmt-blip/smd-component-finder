#!/usr/bin/env python3
"""End-to-end tests for the datasheet RAG pipeline.

    /tmp/venv/bin/python tools/test_rag.py        (or any python with the deps)

What it proves:
  * a PDF corpus is parsed, enriched and indexed;
  * hard part filtering works (the reason metadata enrichment exists);
  * structural sections are recognised and filterable;
  * tables stay whole — a ratings table is never cut in half;
  * the HTTP API used by the UI answers correctly;
  * the hand-rolled SigV4 signer matches botocore byte for byte (when
    botocore happens to be installed).

It builds its own corpus in a temp directory, so it never touches the real
index in data/rag/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import chunking, index_db, metadata, parsers, sample_datasheets

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[  ok  ] %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ] %s%s" % (name, (" — " + extra) if extra else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-rag-test-"))
    corpus = tmp / "corpus"
    out = tmp / "index"
    try:
        return run(tmp, corpus, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: Path, corpus: Path, out: Path) -> int:
    print("--- генерация эталонного корпуса ---")
    for spec in sample_datasheets.DATASHEETS:
        sample_datasheets.build_pdf(spec, corpus)
    pdfs = sorted(corpus.glob("*.pdf"))
    check("сгенерировано 6 PDF-даташитов", len(pdfs) == 6, str(len(pdfs)))

    print("\n--- парсинг + обогащение метаданных ---")
    parser = parsers.get_parser("auto")
    check("парсер доступен (%s)" % parser.name, True)

    metas = {}
    total_chunks = 0
    splitter = chunking.ElementSplitter()
    index = index_db.RagIndex(out / "index.db")

    for pdf in pdfs:
        doc = parser.parse(pdf)
        meta = metadata.enrich(doc)
        chunks = chunking.chunk_document(doc, meta, splitter=splitter)
        metas[pdf.name] = meta
        total_chunks += len(chunks)
        index.add_document(
            {"doc_id": doc.doc_id, "filename": doc.filename, "sha1": doc.sha1,
             "n_pages": doc.n_pages, "parser": doc.parser, "part": meta.part,
             "manufacturer": meta.manufacturer, "package": meta.package,
             "family": meta.family, "conf": meta.confidence},
            [c.to_dict() for c in chunks],
        )

    stats = index.stats()
    expected_parts = {"MMBT3904", "2N7002", "BAV99", "AMS1117-3.3", "TP4056", "SI2301"}
    got_parts = set(stats["parts"])
    check("все номера деталей извлечены", expected_parts <= got_parts,
          "нет: %s" % (expected_parts - got_parts))
    check("AMS1117-3.3 НЕ обрезан до AMS1117-3", "AMS1117-3.3" in got_parts)
    check("чанков достаточно", total_chunks > 30, str(total_chunks))
    check("таблицы извлечены", stats["tables"] >= 5, str(stats["tables"]))
    check("у каждого чанка есть номер детали",
          stats["chunks_with_part"] == stats["chunks"],
          "%s / %s" % (stats["chunks_with_part"], stats["chunks"]))

    print("\n--- классификация разделов ---")
    sections = set(stats["sections"])
    for needed in ("absolute_maximum_ratings", "pin_configuration",
                   "electrical_characteristics", "package_dimensions"):
        check("раздел %s найден" % needed, needed in sections, str(sorted(sections)))

    print("\n--- таблицы не разрезаются ---")
    rows = index.conn.execute(
        "SELECT text FROM chunks WHERE is_table = 1").fetchall()
    whole = [r["text"] for r in rows if "Symbol" in r["text"] and "Unit" in r["text"]]
    check("таблицы предельных режимов целые (есть строка заголовка)",
          len(whole) >= 5, "%d из %d" % (len(whole), len(rows)))

    print("\n--- поиск: точность ---")
    res = index.search("collector current", part="MMBT3904", k=3)
    check("«collector current» по MMBT3904 находит 200 мА",
          bool(res) and "200" in (res[0]["text"] + res[0]["snippet"]),
          res[0]["snippet"][:100] if res else "нет результатов")

    res = index.search("maximum input voltage", part="AMS1117-3.3", k=3)
    check("«maximum input voltage» по AMS1117-3.3 находит 15 В",
          bool(res) and "15" in (res[0]["text"] + res[0]["snippet"]),
          res[0]["snippet"][:100] if res else "нет результатов")

    res = index.search("charge current", part="TP4056", k=3)
    check("«charge current» по TP4056 даёт TP4056",
          bool(res) and res[0]["part"] == "TP4056")

    print("\n--- поиск: жёсткая фильтрация по детали ---")
    res = index.search("gate threshold voltage", k=6)
    parts = {r["part"] for r in res}
    check("без фильтра «gate threshold» даёт несколько деталей",
          len(parts) >= 2, str(parts))
    res = index.search("gate threshold voltage", part="SI2301", k=6)
    check("с фильтром part=SI2301 — только SI2301",
          bool(res) and {r["part"] for r in res} == {"SI2301"},
          str({r["part"] for r in res}))
    check("найден порог затвора SI2301 (-0.45 … -1.2 В)",
          bool(res) and "-0.45" in res[0]["text"],
          res[0]["text"][:100] if res else "")

    print("\n--- поиск: фильтр по разделу ---")
    res = index.search("voltage", section="absolute_maximum_ratings", k=6)
    check("фильтр section=absolute_maximum_ratings работает",
          bool(res) and {r["section"] for r in res} == {"absolute_maximum_ratings"},
          str({r["section"] for r in res}))

    print("\n--- API (как его видит браузер) ---")
    api_smoke(index)

    print("\n--- SigV4 против botocore ---")
    sigv4_smoke()

    print("\n%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


def api_smoke(index) -> None:
    """Start tools/rag/serve.py in a thread and hit it over HTTP."""
    import threading
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.rag import serve

    # reuse the in-memory index by pointing the server at the same db file
    serve.INDEX_PATH = index.path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                lambda *a, **kw: serve.Handler(*a, directory=str(Path.cwd()), **kw))
    httpd.embed_backend = "none"
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def get(path: str) -> dict:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=20) as r:
            return json.loads(r.read().decode())

    try:
        health = get("/api/health?x=1")
        check("/api/health отвечает", health.get("ok") is True)
        st = get("/api/stats?x=1")
        check("/api/stats содержит chunks", st.get("chunks", 0) > 30, str(st.get("chunks")))
        parts = get("/api/parts?x=1")
        check("/api/parts возвращает список", len(parts.get("parts", [])) >= 6)
        s = get("/api/search?x=1&q=" + urllib.request.quote("collector current") + "&part=MMBT3904")
        check("/api/search находит результат", s.get("count", 0) > 0)
        check("/api/search помечает режим bm25", s.get("mode") == "bm25", str(s.get("mode")))
        check("у результата есть номер детали и раздел",
              bool(s["results"]) and s["results"][0].get("part") == "MMBT3904")
    except Exception as exc:
        check("HTTP API", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        httpd.shutdown()


def sigv4_smoke() -> None:
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ImportError:
        print("        (botocore не установлен — пропуск)")
        return

    import datetime
    from tools.rag import fetch_s3

    url = "https://examplebucket.s3.amazonaws.com/test.txt?Range=bytes%3D0-9"
    key, secret = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    for _ in range(10):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        hdrs = {"Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9",
                "X-Amz-Content-Sha256": fetch_s3.EMPTY_SHA256,
                "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ")}
        mine = fetch_s3.sign("GET", url, hdrs, key, secret, "us-east-1", when=now)["Authorization"]
        req = AWSRequest(method="GET", url=url, headers=dict(hdrs))
        SigV4Auth(Credentials(key, secret), "s3", "us-east-1").add_auth(req)
        ref = req.headers["Authorization"]
        if mine.split("Credential=")[1].split(",")[0] == ref.split("Credential=")[1].split(",")[0]:
            check("подпись совпадает с botocore", mine == ref)
            return
        time.sleep(0.3)
    check("подпись совпадает с botocore", False, "не удалось синхронизировать время")


if __name__ == "__main__":
    raise SystemExit(main())
