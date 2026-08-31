#!/usr/bin/env python3
"""Tests for the OpenSearch backend — against a fake cluster.

The point of OpenSearch here is hybrid retrieval: BM25 for exact strings
(`BAT54`, `KL1`) plus vectors for questions that share no keywords. These
tests check the requests we send (mapping, bulk, query DSL) and the fusion of
the two channels — all with a stub HTTP server, so no cluster is needed.

    python3 tools/test_opensearch.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import opensearch_index as osi  # noqa: E402

PASS = FAIL = 0
REQUESTS: List[Tuple[str, str, Any]] = []       # (method, path, parsed body)

# What the fake cluster returns for a search, per channel.
BM25_HITS = [
    {"_id": "c1", "_score": 12.0, "_source": {
        "chunk_id": "c1", "doc_id": "d1", "filename": "a.pdf", "part": "BAT54",
        "manufacturer": "Nexperia", "package": "SOT-23", "section": "features",
        "section_label": "Features", "header": "Features", "page": 1,
        "is_table": False, "text": "BAT54 dual Schottky diode",
        "summary": "dual Schottky"},
     "highlight": {"text": ["BAT54 dual <em>Schottky</em> diode"]}},
    {"_id": "c2", "_score": 9.0, "_source": {
        "chunk_id": "c2", "doc_id": "d1", "filename": "a.pdf", "part": "BAT54",
        "section": "pin", "section_label": "Pinning", "header": "Pinning",
        "page": 2, "is_table": True, "text": "Pin 1 anode", "summary": "pinout"}},
]
VEC_HITS = [
    {"_id": "c3", "_score": 0.91, "_source": {
        "chunk_id": "c3", "doc_id": "d2", "filename": "b.pdf", "part": "BAT54",
        "section": "ratings", "section_label": "Ratings", "header": "Max ratings",
        "page": 3, "is_table": True, "text": "how to reduce voiding on QFN",
        "summary": "soldering advice"}},
    {"_id": "c1", "_score": 0.80, "_source": BM25_HITS[0]["_source"]},
]
INDEX_EXISTS = {"exists": False}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):            # quiet
        pass

    def _read(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        text = raw.decode("utf-8")
        if text.lstrip().startswith("{"):
            try:
                return json.loads(text)
            except ValueError:
                return text
        # NDJSON bulk body: keep the raw text, tests parse it
        return text

    def _send(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str) -> None:
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        body = self._read()
        REQUESTS.append((method, path, body))

        if path == "/":
            return self._send({"version": {"number": "2.11.0"},
                               "tagline": "The OpenSearch Project"})

        if method == "GET" and path.count("/") == 1 and "__meta" not in path:
            if INDEX_EXISTS["exists"]:
                return self._send({"smd-chunks": {"aliases": {}}})
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "PUT" and path.count("/") == 1:
            INDEX_EXISTS["exists"] = True
            return self._send({"acknowledged": True, "index": path.lstrip("/")})

        if path.endswith("/_bulk"):
            return self._send({"errors": False, "items": []})

        if path.endswith("/_refresh"):
            return self._send({"_shards": {"total": 1, "successful": 1}})

        if path.endswith("/_count"):
            want_vector = "vector" in json.dumps(body or {})
            return self._send({"count": 7 if want_vector else 42})

        if "__meta" in path:
            if method == "GET":
                return self._send({"_source": {"key": "parser", "value": "docling"}})
            return self._send({"result": "updated"})

        if path.endswith("/_search"):
            payload = body if isinstance(body, dict) else {}
            if payload.get("aggs"):
                aggs = payload["aggs"]
                if "parts" in aggs and len(aggs) == 1:
                    return self._send({"aggregations": {"parts": {"buckets": [
                        {"key": "BAT54", "doc_count": 5},
                        {"key": "MMBT3904", "doc_count": 3}]}}})
                return self._send({"aggregations": {
                    "sections": {"buckets": [{"key": "features", "doc_count": 10},
                                             {"key": "ratings", "doc_count": 4}]},
                    "parts": {"buckets": [{"key": "BAT54", "doc_count": 5}]},
                    "docs": {"value": 13}}})
            if "knn" in json.dumps(payload):
                return self._send({"hits": {"hits": VEC_HITS, "total": {"value": 2}}})
            return self._send({"hits": {"hits": BM25_HITS, "total": {"value": 2}}})

        return self._send({"ok": True})

    def do_GET(self):
        self._route("GET")

    def do_PUT(self):
        self._route("PUT")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


class _FakeEmbedder:
    """Deterministic vectors, no model weights."""
    name = "fake"
    dim = 4

    def encode(self, texts: List[str]):
        out = []
        for t in texts:
            h = float(abs(hash(t)) % 1000) / 1000.0
            out.append([h, 1.0 - h, h * 0.5, 0.25])
        return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    global REQUESTS
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % port

    try:
        print("--- подключение ---")
        idx = osi.OpenSearchIndex(url=url, index="smd-chunks",
                                  embedding_backend=_FakeEmbedder(), dims=4)
        check("ping видит кластер", idx.ping() is True)
        check("размерность взята у эмбеддера", idx.dims == 4, str(idx.dims))
        check("недоступный кластер — ping False",
              osi.OpenSearchIndex(url="http://127.0.0.1:1").ping() is False)

        print("\n--- маппинг индекса ---")
        idx.create_index()
        put = [r for r in REQUESTS if r[0] == "PUT" and r[1] == "/smd-chunks"]
        check("индекс создан", bool(put))
        mapping = put[0][2] if put else {}
        props = mapping["mappings"]["properties"]
        check("векторное поле есть", props.get("vector", {}).get("type") == "knn_vector")
        check("размерность в маппинге", props["vector"]["dimension"] == 4)
        check("knn включён в настройках", mapping["settings"].get("knn") is True)
        check("точные поля — keyword",
              all(props[k]["type"] == "keyword"
                  for k in ("part", "manufacturer", "package", "section")))
        analyzer = mapping["settings"]["analysis"]["analyzer"]["part_analyzer"]
        check("свой анализатор для парт-номеров",
              analyzer["tokenizer"] == "part_tokenizer")
        check("ngram 2..12 для коротких маркировок",
              mapping["settings"]["analysis"]["filter"]["part_ngram"]["min_gram"] == 2)

        print("\n--- запись ---")
        doc = {"doc_id": "d1", "filename": "a.pdf"}
        chunks = [
            {"id": "c1", "doc_id": "d1", "part": "BAT54", "manufacturer": "Nexperia",
             "package": "SOT-23", "section": "features", "section_label": "Features",
             "header": "Features", "page": 1, "is_table": False,
             "text": "BAT54 dual Schottky", "summary": "dual", "order": 0},
            {"id": "c2", "doc_id": "d1", "part": "BAT54", "manufacturer": "",
             "package": "", "section": "pin", "section_label": "Pinning",
             "header": "Pinning", "page": 2, "is_table": True,
             "text": "1 anode 2 cathode", "summary": "pinout", "order": 1},
        ]
        idx.add_document(doc, chunks)
        check("в буфере два чанка", idx._pending == 2, str(idx._pending))
        sent = idx.flush()
        check("flush отправил оба", sent == 2, str(sent))
        bulk = [r for r in REQUESTS if r[1].endswith("/_bulk")]
        check("bulk ушёл", bool(bulk))
        lines = [ln for ln in (bulk[0][2] or "").split("\n") if ln.strip()]
        check("bulk — NDJSON: действие + документ", len(lines) == 4, str(len(lines)))
        first = json.loads(lines[0])
        check("bulk пишет по chunk_id",
              first["index"]["_id"] == "c1" and first["index"]["_index"] == "smd-chunks")
        second = json.loads(lines[1])
        check("вектор посчитан при записи", len(second.get("vector", [])) == 4)
        check("метаданные чанка на месте",
              second["part"] == "BAT54" and second["is_table"] is False)

        print("\n--- гибридный поиск ---")
        REQUESTS.clear()
        hits = idx.search("BAT54 Schottky", k=5)
        searches = [r for r in REQUESTS if r[1].endswith("/_search")]
        check("два запроса: BM25 и векторный", len(searches) == 2, str(len(searches)))
        lex = searches[0][2]
        mm = lex["query"]["bool"]["must"][0]["multi_match"]
        fields = {f.split("^")[0] for f in mm["fields"]}
        check("BM25 по text/summary/part", {"text", "summary", "part_text"} <= fields,
              str(fields))
        check("парт-номер усилен", any("^4" in f for f in mm["fields"]))
        check("подсветка запрошена", "highlight" in lex)
        check("вектор excluded из _source",
              lex["_source"]["excludes"] == ["vector"])
        knn = searches[1][2]
        check("второй запрос — knn", "knn" in json.dumps(knn))
        check("knn просит k из пула", knn["query"]["knn"]["vector"]["k"] >= 20)

        check("результаты склеены", len(hits) == 3, str(len(hits)))
        top = hits[0]
        check("попавший в оба канала — первый", top["chunk_id"] == "c1", top["chunk_id"])
        check("оба канала отмечены", set(top.get("channels", [])) == {"bm25", "vector"},
              str(top.get("channels")))
        expected = round(1.0 / (60 + 1) + 1.0 / (60 + 2), 6)
        check("RRF считается по рангам", abs(top["score"] - expected) < 1e-6,
              "%s != %s" % (top["score"], expected))
        check("сниппет взят из подсветки", "Schottky" in top["snippet"])
        check("векторный хит без подсветки всё равно попал",
              any(h["chunk_id"] == "c3" for h in hits))

        print("\n--- фильтры ---")
        REQUESTS.clear()
        idx.search("voiding", part="BAT54", section="ratings", k=3)
        lex = [r for r in REQUESTS if r[1].endswith("/_search")][0][2]
        filt = lex["query"]["bool"]["filter"]
        check("part и section — term-фильтры",
              {"term": {"part": "BAT54"}} in filt and {"term": {"section": "ratings"}} in filt,
              str(filt))
        knn = [r for r in REQUESTS if r[1].endswith("/_search")][1][2]
        check("фильтр применён и к knn",
              knn["query"]["bool"]["filter"] == filt, str(knn["query"]))

        print("\n--- только BM25, без векторов ---")
        REQUESTS.clear()
        plain = osi.OpenSearchIndex(url=url, index="smd-chunks")
        plain.search("BAT54", k=5)
        searches = [r for r in REQUESTS if r[1].endswith("/_search")]
        check("без эмбеддера — один запрос", len(searches) == 1, str(len(searches)))
        check("без эмбеддера нет векторного поля", plain.dims == 0)

        print("\n--- статистика и фасеты ---")
        stats = idx.stats()
        check("чанки посчитаны", stats["chunks"] == 42, str(stats["chunks"]))
        check("векторы посчитаны отдельным запросом", stats["vectors"] == 7)
        check("секции из агрегации", stats["sections"].get("features") == 10)
        check("документы — cardinality", stats["docs"] == 13, str(stats["docs"]))
        check("бэкенд помечен", stats["backend"] == "opensearch")
        parts = idx.parts()
        check("parts() из terms-агрегации",
              [p["part"] for p in parts] == ["BAT54", "MMBT3904"], str(parts))
        check("мета читается", idx.get_meta("parser") == "docling")

        print("\n--- устойчивость ---")
        broken = osi.OpenSearchIndex(url="http://127.0.0.1:1", index="x")
        raised = False
        try:
            broken.stats()
        except osi.OpenSearchError:
            raised = True
        check("нет кластера — понятная ошибка", raised)
        check("ошибка — это RuntimeError", issubclass(osi.OpenSearchError, RuntimeError))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
