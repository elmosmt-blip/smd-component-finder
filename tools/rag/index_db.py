"""SQLite storage + hybrid retrieval (BM25 + optional dense vectors).

Why SQLite and not a vector database:
  * the corpus is a few thousand documents; SQLite FTS5 BM25 answers in
    milliseconds with zero infrastructure;
  * a single .db file is trivial to copy, back up and version;
  * dense vectors, when enabled, live in the same file as BLOBs and are
    compared with numpy cosine — no extra service.

Retrieval is hybrid with Reciprocal Rank Fusion: BM25 catches exact part
numbers, symbols and units ("VCEO", "200 mA"), dense vectors catch paraphrases.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:      # BM25-only mode works without numpy
    np = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id      TEXT PRIMARY KEY,
    filename    TEXT,
    sha1        TEXT,
    n_pages     INTEGER,
    parser      TEXT,
    part        TEXT,
    manufacturer TEXT,
    package     TEXT,
    family      TEXT,
    conf        REAL,
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT,
    part        TEXT,
    manufacturer TEXT,
    package     TEXT,
    section     TEXT,
    section_label TEXT,
    header      TEXT,
    page        INTEGER,
    is_table    INTEGER,
    text        TEXT,
    summary     TEXT,
    order_idx   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_part ON chunks(part);
CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    part UNINDEXED,
    section UNINDEXED,
    text,
    summary,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id TEXT PRIMARY KEY,
    dim      INTEGER,
    vec      BLOB
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _tokenize(query: str) -> List[str]:
    return [t for t in re.split(r"[^\w\-\.]+", query.lower()) if t]


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression: quoted tokens joined by AND."""
    tokens = _tokenize(query)
    if not tokens:
        return '""'
    return " AND ".join('"%s"' % t.replace('"', "") for t in tokens)


class RagIndex:
    def __init__(self, path: Path, embedding_backend=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.embedding = embedding_backend

    # ------------------------------------------------------------------ write

    def reset(self) -> None:
        self.conn.executescript(
            "DELETE FROM docs; DELETE FROM chunks; DELETE FROM vectors; "
            "DELETE FROM meta; DELETE FROM chunks_fts;"
        )
        self.conn.commit()

    def add_document(self, doc: dict, chunks: Sequence[dict]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO docs "
            "(doc_id, filename, sha1, n_pages, parser, part, manufacturer, "
            " package, family, conf, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc["doc_id"], doc["filename"], doc["sha1"], doc["n_pages"],
                doc["parser"], doc["part"], doc["manufacturer"], doc["package"],
                doc["family"], doc["conf"], time.time(),
            ),
        )
        for ch in chunks:
            self.conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(chunk_id, doc_id, part, manufacturer, package, section, "
                " section_label, header, page, is_table, text, summary, order_idx) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ch["id"], ch["doc_id"], ch["part"], ch["manufacturer"],
                    ch["package"], ch["section"], ch["section_label"],
                    ch["header"], ch["page"], 1 if ch["is_table"] else 0,
                    ch["text"], ch["summary"], ch["order"],
                ),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO chunks_fts (chunk_id, part, section, text, summary) "
                "VALUES (?,?,?,?,?)",
                (ch["id"], ch["part"] or "", ch["section"], ch["text"], ch["summary"]),
            )
        self.conn.commit()

    def build_vectors(self) -> int:
        if not self.embedding or self.embedding.name == "none":
            return 0
        if np is None:
            self.set_meta("vector_error", "numpy is not installed — vector search disabled")
            return 0
        rows = self.conn.execute(
            "SELECT c.chunk_id, c.text, c.summary FROM chunks c "
            "LEFT JOIN vectors v ON v.chunk_id = c.chunk_id "
            "WHERE v.chunk_id IS NULL"
        ).fetchall()
        if not rows:
            return 0
        texts = [((r["summary"] + "\n" if r["summary"] else "") + r["text"])[:4000] for r in rows]
        try:
            vecs = self.embedding.encode(texts)
        except Exception as exc:  # backend unavailable -> stay BM25-only
            self.set_meta("vector_error", str(exc))
            return 0
        for r, vec in zip(rows, vecs):
            self.conn.execute(
                "INSERT OR REPLACE INTO vectors (chunk_id, dim, vec) VALUES (?,?,?)",
                (r["chunk_id"], int(len(vec)), np.asarray(vec, dtype="float32").tobytes()),
            )
        self.conn.commit()
        self.set_meta("embedding_backend", self.embedding.describe())
        return len(rows)

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------- read

    def stats(self) -> dict:
        docs = self.conn.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
        chunks = self.conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        tables = self.conn.execute(
            "SELECT COUNT(*) n FROM chunks WHERE is_table = 1").fetchone()["n"]
        with_part = self.conn.execute(
            "SELECT COUNT(*) n FROM chunks WHERE part IS NOT NULL AND part != ''"
        ).fetchone()["n"]
        sections = {
            r["section"]: r["n"]
            for r in self.conn.execute(
                "SELECT section, COUNT(*) n FROM chunks GROUP BY section ORDER BY n DESC"
            )
        }
        parts = [
            r["part"] for r in self.conn.execute(
                "SELECT part, COUNT(*) n FROM chunks WHERE part IS NOT NULL AND part != '' "
                "GROUP BY part ORDER BY n DESC LIMIT 40"
            )
        ]
        vecs = self.conn.execute("SELECT COUNT(*) n FROM vectors").fetchone()["n"]
        return {
            "docs": docs,
            "chunks": chunks,
            "tables": tables,
            "chunks_with_part": with_part,
            "sections": sections,
            "parts": parts,
            "vectors": vecs,
            "embedding": self.get_meta("embedding_backend", "none"),
            "parser": self.get_meta("parser", "-"),
            "built_at": self.get_meta("built_at"),
            "vector_error": self.get_meta("vector_error"),
        }

    def parts(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT part, manufacturer, package, COUNT(*) n FROM chunks "
            "WHERE part IS NOT NULL AND part != '' "
            "GROUP BY part, manufacturer, package ORDER BY part"
        ).fetchall()
        return [dict(r) for r in rows]

    def _fts_search(self, query: str, part: Optional[str], section: Optional[str],
                    limit: int) -> List[Tuple[str, float, str]]:
        match = _fts_query(query)
        sql = (
            "SELECT c.chunk_id AS chunk_id, "
            "       bm25(chunks_fts, 10.0, 4.0) AS score, "
            "       snippet(chunks_fts, 3, '<<', '>>', ' … ', 24) AS snip "
            "FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id "
            "WHERE chunks_fts MATCH ?"
        )
        args: List = [match]
        if part:
            sql += " AND c.part = ? COLLATE NOCASE"
            args.append(part)
        if section:
            sql += " AND c.section = ?"
            args.append(section)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        try:
            rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["chunk_id"], float(r["score"]), r["snip"]) for r in rows]

    def _vector_search(self, query: str, part: Optional[str], section: Optional[str],
                       limit: int) -> List[Tuple[str, float]]:
        count = self.conn.execute("SELECT COUNT(*) n FROM vectors").fetchone()["n"]
        if not count or np is None:
            return []
        try:
            qv = np.asarray(self.embedding.encode([query])[0], dtype="float32")
        except Exception:
            return []
        qn = qv / (np.linalg.norm(qv) + 1e-9)

        sql = "SELECT v.chunk_id, v.vec, v.dim FROM vectors v JOIN chunks c ON c.chunk_id = v.chunk_id"
        args: List = []
        if part:
            sql += " WHERE c.part = ? COLLATE NOCASE"
            args.append(part)
        if section:
            sql += (" WHERE" if not part else " AND") + " c.section = ?"
            args.append(section)

        out: List[Tuple[str, float]] = []
        for row in self.conn.execute(sql, args):
            vec = np.frombuffer(row["vec"], dtype="float32")
            norm = np.linalg.norm(vec) + 1e-9
            out.append((row["chunk_id"], float(np.dot(vec / norm, qn))))
        out.sort(key=lambda x: -x[1])
        return out[:limit]

    def search(self, query: str, part: Optional[str] = None,
               section: Optional[str] = None, k: int = 8) -> List[dict]:
        k = max(1, min(int(k or 8), 50))
        pool = max(k * 4, 24)

        lex = self._fts_search(query, part, section, pool)
        vec = self._vector_search(query, part, section, pool) if self.embedding else []

        # Reciprocal Rank Fusion — no score normalisation needed between channels
        fused: Dict[str, float] = {}
        snippets: Dict[str, str] = {}
        for rank, (cid, _score, snip) in enumerate(lex, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
            snippets.setdefault(cid, snip)
        for rank, (cid, _score) in enumerate(vec, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)

        if not fused:
            return []

        ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        results = []
        for cid, score in ordered:
            row = self.conn.execute(
                "SELECT c.*, d.filename FROM chunks c JOIN docs d ON d.doc_id = c.doc_id "
                "WHERE c.chunk_id = ?", (cid,)
            ).fetchone()
            if not row:
                continue
            results.append({
                "chunk_id": cid,
                "part": row["part"],
                "manufacturer": row["manufacturer"],
                "package": row["package"],
                "section": row["section"],
                "section_label": row["section_label"],
                "header": row["header"],
                "page": row["page"],
                "is_table": bool(row["is_table"]),
                "text": row["text"],
                "summary": row["summary"],
                "snippet": snippets.get(cid, ""),
                "score": round(score, 6),
                "doc_id": row["doc_id"],
                "filename": row["filename"],
            })
        return results

    def close(self) -> None:
        self.conn.close()
