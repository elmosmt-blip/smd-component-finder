#!/usr/bin/env python3
"""Static site + JSON API on one port.

The browser must never talk to a second origin (no http://localhost:xxxx in
client code — it would be blocked and would not work through the preview
proxy). So this server does both jobs:

    /                  static files from the repository root
    /api/health        index status
    /api/stats         how many datasheets / chunks / parts are indexed
    /api/search        hybrid retrieval  ?q=&part=&section=&k=
    /api/parts         distinct part numbers for the filter dropdown
    /pdfs/<file>.pdf   the original datasheet, for "open at page" links

Stdlib only — no Flask, no FastAPI, nothing to install.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import card_store, index_db
from tools.rag.embeddings import get_backend

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_PATH = ROOT / "data" / "rag" / "index.db"
CARDS_PATH = ROOT / "data" / "cards" / "cards.db"
PDF_DIR = ROOT / "data" / "datasheets"
SITE_CARDS_DIR = ROOT / "data" / "cards" / "site"


class Handler(SimpleHTTPRequestHandler):
    server_version = "SMDFinder/0.1"

    # ------------------------------------------------------------------ utils

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cards(self):
        """One shared connection per server process — SQLite dislikes churn."""
        store = getattr(self.server, "_card_store", None)
        if store is None:
            store = card_store.CardStore(CARDS_PATH)
            self.server._card_store = store
        return store

    def _index(self):
        return index_db.RagIndex(INDEX_PATH, embedding_backend=get_backend(
            self.server.embed_backend))

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # -------------------------------------------------------------------- API

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            return self._handle_api(path, urllib.parse.parse_qs(parsed.query))

        if path.startswith("/pdfs/"):
            return self._handle_pdf(path[len("/pdfs/"):])

        if path.startswith("/cards/"):
            return self._handle_static(path[len("/cards/"):], SITE_CARDS_DIR)

        return super().do_GET()

    def _handle_api(self, path: str, qs: dict) -> None:
        try:
            if path == "/api/health":
                index = self._index()
                stats = index.stats()
                index.close()
                cards = 0
                if CARDS_PATH.exists():
                    cards = self._cards().count()
                self._json({
                    "ok": True,
                    "indexed": stats["chunks"] > 0,
                    "cards": cards,
                    "stats": stats,
                })
                return

            if path == "/api/stats":
                index = self._index()
                self._json(index.stats())
                index.close()
                return

            if path == "/api/parts":
                index = self._index()
                self._json({"parts": index.parts()})
                index.close()
                return

            if path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                part = (qs.get("part") or [None])[0] or None
                section = (qs.get("section") or [None])[0] or None
                try:
                    k = int((qs.get("k") or ["8"])[0])
                except ValueError:
                    k = 8
                if not q:
                    self._json({"query": q, "results": [], "error": "empty query"})
                    return
                index = self._index()
                results = index.search(q, part=part, section=section, k=k)
                stats = index.stats()
                index.close()
                self._json({
                    "query": q,
                    "count": len(results),
                    "results": results,
                    "mode": "hybrid" if stats["vectors"] else "bm25",
                })
                return

            if path == "/api/cards":
                q = (qs.get("q") or [""])[0].strip()
                package = (qs.get("pkg") or [""])[0].strip()
                mfr = (qs.get("mfr") or [""])[0].strip()
                family = (qs.get("family") or [""])[0].strip()
                try:
                    limit = int((qs.get("limit") or ["60"])[0])
                    offset = int((qs.get("offset") or ["0"])[0])
                except ValueError:
                    limit, offset = 60, 0
                results, total = self._cards().search(
                    q=q, package=package, manufacturer=mfr, family=family,
                    limit=limit, offset=offset)
                self._json({"query": q, "count": len(results), "total": total,
                            "offset": offset, "results": results})
                return

            if path == "/api/card":
                part = (qs.get("part") or [""])[0].strip()
                if not part:
                    self._json({"error": "part is required"}, 400)
                    return
                card = self._cards().get(part)
                if not card:
                    self._json({"error": "no card for %s" % part}, 404)
                    return
                self._json(card)
                return

            if path == "/api/cards/stats":
                store = self._cards()
                payload = store.stats()
                payload["facets"] = {
                    "packages": [{"name": n, "count": c}
                                 for n, c in store.facets()["packages"][:40]],
                    "manufacturers": [{"name": n, "count": c}
                                      for n, c in store.facets()["manufacturers"][:40]],
                }
                self._json(payload)
                return

            self._json({"error": "unknown endpoint %s" % path}, 404)
        except Exception as exc:  # keep the server alive on any API error
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def _handle_static(self, name: str, base: Path) -> None:
        safe = Path(urllib.parse.unquote(name))
        target = (base / safe).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            self.send_error(404, "not found")
            return
        data = target.read_bytes()
        ctype = "application/json" if target.suffix == ".json" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def _handle_pdf(self, name: str) -> None:
        safe = Path(urllib.parse.unquote(name)).name
        target = PDF_DIR / safe
        if not target.exists() or PDF_DIR not in target.parents:
            self.send_error(404, "datasheet not found")
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the finder UI + RAG API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--embed", default="none",
                    choices=["none", "auto", "sentence-transformers", "st", "openai"])
    args = ap.parse_args()

    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")

    handler = lambda *a, **kw: Handler(*a, directory=str(ROOT), **kw)  # noqa: E731
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.embed_backend = args.embed

    stats = {}
    if INDEX_PATH.exists():
        try:
            idx = index_db.RagIndex(INDEX_PATH)
            stats = idx.stats()
            idx.close()
        except Exception as exc:
            print("index not readable: %s" % exc)

    print("Serving %s on http://%s:%d" % (ROOT, args.host, args.port))
    print("RAG index: %s" % (
        "%d chunks from %d datasheets (%d tables)" % (
            stats.get("chunks", 0), stats.get("docs", 0), stats.get("tables", 0))
        if stats.get("chunks") else "empty — run: python3 tools/rag/pipeline.py --rebuild"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
