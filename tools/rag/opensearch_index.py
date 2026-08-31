"""OpenSearch backend for the datasheet index: BM25 + vectors on one node.

Why OpenSearch and not only SQLite FTS5:

  * A part number or a marking is a *short exact string*. `BAT54`, `KL1`,
    `SOT-23-5` — vector spaces smear those together, BM25 hits them exactly.
  * A question like "how do I reduce voiding on a QFN footprint" is the
    opposite: no keyword overlap, and only vectors find it.
  OpenSearch serves both from one index, so the hybrid query is one round
  trip per channel instead of two different storage engines.

The dependency is deliberately *not* `opensearch-py`: this talks HTTP with
urllib, the same way `fetch_s3.py` signs its own requests instead of pulling
in boto3. Fewer wheels to keep working in six months.

Configuration (environment):

    SMD_OPENSEARCH_URL      http://localhost:9200   (unset = backend disabled)
    SMD_OPENSEARCH_INDEX    smd-chunks
    SMD_OPENSEARCH_DIMS     vector dimension (default: from the embedder)
    SMD_OPENSEARCH_USER     basic auth, optional
    SMD_OPENSEARCH_PASS     basic auth, optional
    SMD_OPENSEARCH_SHARDS   1

Start a node on the same machine:

    docker compose -f tools/rag/opensearch-compose.yml up -d
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "smd-chunks"

# Reciprocal Rank Fusion constant. 60 is the value from the original paper and
# it is what the SQLite path uses, so both backends rank identically.
RRF_K = 60


class OpenSearchError(RuntimeError):
    """The cluster said no, or did not answer at all."""


class _Client:
    """Just enough HTTP for the handful of endpoints we use."""

    def __init__(self, url: str, user: str = "", password: str = "",
                 timeout: float = 15.0, verify: bool = True):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self._auth = ""
        if user:
            token = ("%s:%s" % (user, password or "")).encode()
            self._auth = "Basic " + base64.b64encode(token).decode("ascii")
        self._opener = urllib.request.build_opener()

    def _request(self, method: str, path: str, body: Optional[Any] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            if isinstance(body, (bytes, str)):
                data = body.encode("utf-8") if isinstance(body, str) else body
                headers["Content-Type"] = "application/x-ndjson"
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read() or b"{}"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise OpenSearchError("%s %s -> %d %s" %
                                  (method, path, exc.code, detail)) from exc
        except OSError as exc:                      # refused, timeout, DNS
            raise OpenSearchError("%s %s -> %s" % (method, path, exc)) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # -- thin wrappers -------------------------------------------------
    def get(self, path: str, **kw) -> Any:
        return self._request("GET", path, **kw)

    def put(self, path: str, body: Any = None, **kw) -> Any:
        return self._request("PUT", path, body, **kw)

    def post(self, path: str, body: Any = None, **kw) -> Any:
        return self._request("POST", path, body, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self._request("DELETE", path, None, **kw)


def _ndjson(actions: Sequence[dict]) -> str:
    return "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in actions)


class OpenSearchIndex:
    """Drop-in replacement for `index_db.RagIndex` backed by OpenSearch."""

    #: Chunk fields, minus the vector. Mirrors the SQLite `chunks` table so
    #: both backends return the same rows.
    SOURCE_EXCLUDES = ["vector"]

    def __init__(self, url: Optional[str] = None, index: Optional[str] = None,
                 embedding_backend=None, dims: Optional[int] = None,
                 timeout: float = 15.0, user: str = "", password: str = "",
                 shards: int = 0, replicas: int = 0,
                 client: Optional[_Client] = None):
        self.url = url or os.environ.get("SMD_OPENSEARCH_URL") or DEFAULT_URL
        self.index = index or os.environ.get("SMD_OPENSEARCH_INDEX") or DEFAULT_INDEX
        self.meta_index = self.index + "__meta"
        self.embedding = embedding_backend
        self.dims = int(dims or os.environ.get("SMD_OPENSEARCH_DIMS") or 0)
        if self.dims == 0 and self.embedding and getattr(self.embedding, "name", "none") != "none":
            self.dims = int(getattr(self.embedding, "dim", 0) or 0)
        self.shards = int(shards or os.environ.get("SMD_OPENSEARCH_SHARDS") or 1)
        self.replicas = replicas
        self.client = client or _Client(
            self.url, user or os.environ.get("SMD_OPENSEARCH_USER", ""),
            password or os.environ.get("SMD_OPENSEARCH_PASS", ""), timeout)
        self._buffer: List[dict] = []
        self._bulk_size = 500
        self._pending = 0

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def configured(cls) -> bool:
        """True when the environment points at a cluster."""
        return bool(os.environ.get("SMD_OPENSEARCH_URL"))

    def ping(self) -> bool:
        try:
            info = self.client.get("/")
        except OpenSearchError:
            return False
        return bool(info.get("version"))

    def create_index(self, recreate: bool = False) -> None:
        exists = self._exists(self.index)
        if exists and not recreate:
            return
        if exists:
            self.client.delete("/" + self.index)
        self.client.put("/" + self.index, self._mapping())
        self.client.put("/" + self.meta_index, {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"properties": {
                "key": {"type": "keyword"},
                "value": {"type": "keyword"},
            }},
        })

    def _exists(self, name: str) -> bool:
        try:
            self.client.get("/" + name)
            return True
        except OpenSearchError:
            return False

    def _mapping(self) -> dict:
        props: Dict[str, Any] = {
            "chunk_id": {"type": "keyword"},
            "doc_id": {"type": "keyword"},
            "filename": {"type": "keyword"},
            "part": {"type": "keyword"},
            "part_text": {"type": "text", "analyzer": "part_analyzer"},
            "manufacturer": {"type": "keyword"},
            "package": {"type": "keyword"},
            "section": {"type": "keyword"},
            "section_label": {"type": "keyword"},
            "header": {"type": "text"},
            "page": {"type": "integer"},
            "order": {"type": "integer"},
            "is_table": {"type": "boolean"},
            # The reason we are here: exact strings must match exactly.
            "text": {"type": "text", "analyzer": "part_analyzer"},
            "summary": {"type": "text", "analyzer": "part_analyzer"},
        }
        settings: Dict[str, Any] = {
            "number_of_shards": self.shards,
            "number_of_replicas": self.replicas,
            "analysis": {
                "analyzer": {
                    # Datasheet text is full of "SOT-23-5", "V(BR)CEO" and
                    # "0.35mm": the standard analyzer splits those into noise,
                    # which is exactly how `KL1` stops being findable.
                    "part_analyzer": {
                        "type": "custom",
                        "tokenizer": "part_tokenizer",
                        "filter": ["lowercase", "part_ngram"],
                    },
                },
                "tokenizer": {
                    "part_tokenizer": {
                        "type": "pattern",
                        "pattern": "[^A-Za-z0-9]+",
                    },
                },
                "filter": {
                    "part_ngram": {"type": "ngram", "min_gram": 2, "max_gram": 12},
                },
            },
        }
        if self.dims:
            settings["knn"] = True
            settings["knn.space_type"] = "cosinesimil"
            props["vector"] = {
                "type": "knn_vector",
                "dimension": self.dims,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                    "parameters": {"ef_construction": 128, "m": 16},
                },
            }
        return {"settings": settings, "mappings": {"properties": props}}

    # ---------------------------------------------------------------- write

    def reset(self) -> None:
        self._buffer = []
        self.create_index(recreate=True)
        self.client.post("/" + self.index + "/_refresh")

    def add_document(self, doc: dict, chunks: Sequence[dict]) -> None:
        """Buffer the chunks; they are sent by `flush` or the batch size."""
        vectors: Optional[List[List[float]]] = None
        if self.dims and self.embedding and getattr(self.embedding, "name", "none") != "none":
            texts = [c.get("text") or "" for c in chunks]
            vectors = self._encode(texts)
        for i, ch in enumerate(chunks):
            body = {
                "chunk_id": ch["id"],
                "doc_id": ch["doc_id"],
                "filename": doc.get("filename", ""),
                "part": ch.get("part") or "",
                "part_text": ch.get("part") or "",
                "manufacturer": ch.get("manufacturer") or "",
                "package": ch.get("package") or "",
                "section": ch.get("section") or "",
                "section_label": ch.get("section_label") or "",
                "header": ch.get("header") or "",
                "page": ch.get("page") or 0,
                "order": ch.get("order") or i,
                "is_table": bool(ch.get("is_table")),
                "text": ch.get("text") or "",
                "summary": ch.get("summary") or "",
            }
            if vectors is not None and i < len(vectors):
                body["vector"] = vectors[i]
            self._buffer.append({"index": {"_index": self.index, "_id": ch["id"]}})
            self._buffer.append(body)
            self._pending += 1
        if len(self._buffer) >= self._bulk_size * 2:
            self.flush()

    def _encode(self, texts: Sequence[str]) -> List[List[float]]:
        try:
            matrix = self.embedding.encode(list(texts))
        except Exception as exc:                    # noqa: BLE001 - never fatal
            raise OpenSearchError("embedding failed: %s" % exc) from exc
        return [list(map(float, row)) for row in matrix]

    def flush(self) -> int:
        if not self._buffer:
            return 0
        body = _ndjson(self._buffer)
        sent = self._pending
        result = self.client.post("/_bulk", body, params={"refresh": "false"})
        if result.get("errors"):
            first = next((i for i in result.get("items", [])
                          if i.get("index", {}).get("error")), None)
            raise OpenSearchError("bulk index failed: %s" % json.dumps(first)[:400])
        self._buffer = []
        self._pending = 0
        return sent

    def refresh(self) -> None:
        self.flush()
        self.client.post("/" + self.index + "/_refresh")

    def build_vectors(self) -> int:
        """Chunks are embedded as they are added; this only counts them."""
        self.flush()
        if not self.dims:
            return 0
        res = self.client.post("/" + self.index + "/_count", {
            "query": {"exists": {"field": "vector"}}})
        return int(res.get("count", 0))

    # ----------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self.client.put("/%s/_doc/%s" % (self.meta_index, key),
                        {"key": key, "value": str(value)})

    def get_meta(self, key: str, default=None):
        try:
            res = self.client.get("/%s/_doc/%s" % (self.meta_index, key))
        except OpenSearchError:
            return default
        return (res.get("_source") or {}).get("value", default)

    # ---------------------------------------------------------------- stats

    def stats(self) -> dict:
        counts = {}
        for name, query in (("docs_all", {"match_all": {}}),
                            ("tables", {"term": {"is_table": True}}),
                            ("with_part", {"exists": {"field": "part"}}),
                            ("vectors", {"exists": {"field": "vector"}})):
            res = self.client.post("/" + self.index + "/_count", {"query": query})
            counts[name] = int(res.get("count", 0))
        agg = self.client.post("/" + self.index + "/_search", {
            "size": 0,
            "aggs": {
                "sections": {"terms": {"field": "section", "size": 20}},
                "parts": {"terms": {"field": "part", "size": 40}},
                "docs": {"cardinality": {"field": "doc_id"}},
            },
        })
        buckets = ((agg.get("aggregations") or {}).get("sections") or {}).get("buckets") or []
        sections = {b["key"]: b["doc_count"] for b in buckets if b.get("key")}
        pbuckets = ((agg.get("aggregations") or {}).get("parts") or {}).get("buckets") or []
        parts = [b["key"] for b in pbuckets if b.get("key")]
        doc_card = ((agg.get("aggregations") or {}).get("docs") or {}).get("value", 0)
        return {
            "docs": int(doc_card),
            "chunks": counts["docs_all"],
            "tables": counts["tables"],
            "chunks_with_part": counts["with_part"],
            "sections": sections,
            "parts": parts,
            "vectors": counts["vectors"],
            "embedding": self.get_meta("embedding_backend", "none"),
            "parser": self.get_meta("parser", "-"),
            "built_at": self.get_meta("built_at"),
            "index": self.index,
            "backend": "opensearch",
            "url": self.url,
        }

    def parts(self) -> List[dict]:
        agg = self.client.post("/" + self.index + "/_search", {
            "size": 0,
            "aggs": {"parts": {"terms": {"field": "part", "size": 500}}},
        })
        buckets = ((agg.get("aggregations") or {}).get("parts") or {}).get("buckets") or []
        return [{"part": b["key"], "n": b["doc_count"]} for b in buckets if b.get("key")]

    # ---------------------------------------------------------------- search

    def _filters(self, part: Optional[str], section: Optional[str]) -> List[dict]:
        out = []
        if part:
            out.append({"term": {"part": part}})
        if section:
            out.append({"term": {"section": section}})
        return out

    def _lexical_query(self, query: str, part: Optional[str],
                       section: Optional[str], size: int) -> dict:
        filters = self._filters(part, section)
        return {
            "size": size,
            "query": {
                "bool": {
                    "must": [{
                        "multi_match": {
                            "query": query,
                            "fields": ["part_text^4", "header^3", "text^2", "summary"],
                            "type": "best_fields",
                            "operator": "and" if len(query.split()) > 2 else "or",
                        }
                    }],
                    "filter": filters,
                }
            },
            "highlight": {
                "fields": {"text": {"fragment_size": 220, "number_of_fragments": 1},
                           "header": {}},
            },
            "_source": {"excludes": self.SOURCE_EXCLUDES},
        }

    def _knn_query(self, vector: Sequence[float], part: Optional[str],
                   section: Optional[str], size: int) -> dict:
        filters = self._filters(part, section)
        knn = {"vector": {"vector": list(vector), "k": size}}
        query: Dict[str, Any] = {"knn": knn}
        if filters:
            # Efficient filtering: the k-NN plugin honours a bool filter
            # around the knn clause (Lucene engine, OpenSearch 2.4+).
            query = {"bool": {"must": [{"knn": knn}], "filter": filters}}
        return {"size": size, "query": query,
                "_source": {"excludes": self.SOURCE_EXCLUDES}}

    def _hits(self, response: dict) -> List[dict]:
        out = []
        for hit in (response.get("hits") or {}).get("hits") or []:
            src = hit.get("_source") or {}
            highlight = hit.get("highlight") or {}
            snippet = ""
            if highlight.get("text"):
                snippet = highlight["text"][0]
            elif highlight.get("header"):
                snippet = highlight["header"][0]
            out.append({
                "chunk_id": src.get("chunk_id") or hit.get("_id"),
                "doc_id": src.get("doc_id", ""),
                "filename": src.get("filename", ""),
                "part": src.get("part") or None,
                "manufacturer": src.get("manufacturer") or None,
                "package": src.get("package") or None,
                "section": src.get("section") or "",
                "section_label": src.get("section_label") or "",
                "header": src.get("header") or "",
                "page": src.get("page") or 0,
                "is_table": bool(src.get("is_table")),
                "text": src.get("text") or "",
                "summary": src.get("summary") or "",
                "snippet": snippet,
                "score": float(hit.get("_score") or 0.0),
                "_id": hit.get("_id"),
            })
        return out

    def search(self, query: str, part: Optional[str] = None,
               section: Optional[str] = None, k: int = 8) -> List[dict]:
        k = max(1, min(int(k or 8), 50))
        pool = max(k * 4, 24)

        lex_hits = self._hits(self.client.post(
            "/" + self.index + "/_search",
            self._lexical_query(query, part, section, pool)))

        vec_hits: List[dict] = []
        if self.dims and self.embedding and getattr(self.embedding, "name", "none") != "none":
            try:
                vector = self._encode([query])[0]
            except OpenSearchError:
                vector = []
            if vector:
                vec_hits = self._hits(self.client.post(
                    "/" + self.index + "/_search",
                    self._knn_query(vector, part, section, pool)))

        # Reciprocal Rank Fusion: BM25 scores and cosine scores live on
        # different scales, ranks do not.
        fused: Dict[str, float] = {}
        found: Dict[str, dict] = {}
        for rank, hit in enumerate(lex_hits, start=1):
            cid = hit["chunk_id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            hit.setdefault("channels", []).append("bm25")
            found.setdefault(cid, hit)
        for rank, hit in enumerate(vec_hits, start=1):
            cid = hit["chunk_id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            merged = found.get(cid)
            if merged is not None:
                merged.setdefault("channels", []).append("vector")
            else:
                hit.setdefault("channels", []).append("vector")
                found.setdefault(cid, hit)

        ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        results = []
        for cid, score in ordered:
            hit = dict(found[cid])
            hit["score"] = round(score, 6)
            hit["snippet"] = hit.get("snippet") or (hit.get("text") or "")[:220]
            results.append(hit)
        return results

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        try:
            self.flush()
        except OpenSearchError:
            pass

    def __enter__(self) -> "OpenSearchIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


_PING_CACHE: Dict[str, Any] = {"ok": False, "at": 0.0}
_PING_TTL = 10.0


def open_index(path, embedding_backend=None, prefer: Optional[str] = None,
               url: Optional[str] = None, index: Optional[str] = None,
               verbose: bool = False, **kwargs):
    """Open the index: OpenSearch when there is one, SQLite FTS5 otherwise.

    One function, because every caller (site, CLI, tests) wants the same
    thing — "give me the index" — and should not care which engine is behind
    it. `prefer` / SMD_INDEX_BACKEND: auto | opensearch | sqlite.
    """
    from tools.rag import index_db                 # local: keeps the import cheap

    choice = (prefer or os.environ.get("SMD_INDEX_BACKEND") or "auto").lower()
    wants_os = choice in ("opensearch", "os", "opensearch-index")

    if wants_os or choice in ("auto", ""):
        if url:
            os.environ["SMD_OPENSEARCH_URL"] = url
        if not (url or OpenSearchIndex.configured()) and wants_os:
            raise OpenSearchError(
                "SMD_OPENSEARCH_URL is not set — point it at the cluster, e.g. "
                "SMD_OPENSEARCH_URL=http://localhost:9200")
        if url or OpenSearchIndex.configured():
            fresh = time.time() - _PING_CACHE["at"] < _PING_TTL
            reachable = _PING_CACHE["ok"] if fresh else False
            if not fresh:
                probe = OpenSearchIndex(url=url, index=index,
                                        embedding_backend=embedding_backend, **kwargs)
                reachable = probe.ping()
                _PING_CACHE.update(ok=reachable, at=time.time())
            if reachable:
                idx = OpenSearchIndex(url=url, index=index,
                                      embedding_backend=embedding_backend, **kwargs)
                if verbose:
                    print("Index:      opensearch %s/%s" % (idx.url, idx.index))
                return idx
            if wants_os:
                raise OpenSearchError("cluster at %s did not answer"
                                      % (url or os.environ.get("SMD_OPENSEARCH_URL")))
            if verbose:
                print("Index:      sqlite (OpenSearch configured but unreachable)")

    if verbose:
        print("Index:      sqlite %s" % path)
    return index_db.RagIndex(Path(path), embedding_backend=embedding_backend)
