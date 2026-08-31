"""Storage for extracted cards: one row per part, plus static shards.

Two access paths, because the two situations are different:

  * SQLite (`cards.db`)   — the source of truth, searchable, used by /api/cards.
                            300k rows is an ordinary afternoon for SQLite.
  * JSON shards           — so the site also works from a dumb static host with
                            no API at all: brief/<prefix>.json for the grid and
                            card/<PART>.json for one part.

A part can appear in several PDFs (vendor datasheet, clone datasheet, app
note). `upsert` keeps the richest card and records the others as alternates.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    part          TEXT PRIMARY KEY,
    part_key      TEXT NOT NULL,
    manufacturer  TEXT,
    package       TEXT,
    family        TEXT,
    pin_count     INTEGER,
    confidence    REAL,
    pages         INTEGER,
    tables        INTEGER,
    filename      TEXT,
    sha1          TEXT,
    parser        TEXT,
    description   TEXT,
    features      TEXT,
    headline      TEXT,
    card          TEXT NOT NULL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_cards_pkg   ON cards(package);
CREATE INDEX IF NOT EXISTS idx_cards_mfr   ON cards(manufacturer);
CREATE INDEX IF NOT EXISTS idx_cards_key   ON cards(part_key);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    part, manufacturer, package, description, features,
    content='cards', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS failures (
    filename TEXT PRIMARY KEY,
    sha1     TEXT,
    error    TEXT,
    at       REAL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_SAFE = re.compile(r"[^A-Z0-9]+")


def shard_key(part: str) -> str:
    """'AMS1117-3.3' -> 'AM'. Two characters keeps ~300k parts spread thin
    enough that a single shard stays under a few hundred KB."""
    key = _SAFE.sub("", (part or "?").upper())
    return (key[:2] or "__")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def card_score(card: dict) -> float:
    """Richness of a card — used to decide which duplicate to keep."""
    return (
        card.get("confidence", 0.0) * 10
        + len(card.get("ratings") or []) * 0.5
        + len(card.get("specs") or []) * 0.5
        + (2.0 if card.get("pins") else 0.0)
        + (1.0 if card.get("dimensions") else 0.0)
        + (1.0 if card.get("description") else 0.0)
        + (1.0 if card.get("manufacturer") else 0.0)
        + (1.0 if card.get("package") else 0.0)
        + (1.5 if card.get("features") else 0.0)
    )


class CardStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- write

    def upsert(self, card: dict, replace_if_richer: bool = True) -> bool:
        """Insert or replace. Returns True if the row was written."""
        part = (card.get("part") or "").strip()
        if not part:
            return False
        cur = self.conn.execute(
            "SELECT card FROM cards WHERE part_key = ?", (part.upper(),)
        )
        row = cur.fetchone()
        if row:
            if replace_if_richer and card_score(card) <= card_score(json.loads(row["card"])):
                return False
            self.conn.execute("DELETE FROM cards WHERE part_key = ?", (part.upper(),))
        self.conn.execute(
            """INSERT INTO cards (part, part_key, manufacturer, package, family,
                   pin_count, confidence, pages, tables, filename, sha1, parser,
                   description, features, headline, card, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                part, part.upper(), card.get("manufacturer"), card.get("package"),
                card.get("family"), card.get("pin_count"), card.get("confidence", 0.0),
                card.get("pages", 0), card.get("tables", 0), card.get("filename"),
                card.get("sha1"), card.get("parser"), card.get("description", ""),
                _json(card.get("features") or []), _json(card.get("headline") or []),
                _json(card), time.time(),
            ),
        )
        self._index(part)
        return True

    def _index(self, part: str) -> None:
        row = self.conn.execute(
            "SELECT rowid, part, manufacturer, package, description, features FROM cards"
            " WHERE part_key = ?", (part.upper(),)
        ).fetchone()
        if not row:
            return
        self.conn.execute(
            "INSERT INTO cards_fts (rowid, part, manufacturer, package, description, features)"
            " VALUES (?,?,?,?,?,?)",
            (row["rowid"], row["part"], row["manufacturer"] or "",
             row["package"] or "", row["description"] or "", row["features"] or ""),
        )

    def rebuild_fts(self) -> None:
        self.conn.execute("DELETE FROM cards_fts")
        parts = [r[0] for r in self.conn.execute("SELECT part FROM cards").fetchall()]
        for part in parts:
            self._index(part)
        self.conn.commit()

    def add_failure(self, filename: str, sha1: str, error: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO failures (filename, sha1, error, at) VALUES (?,?,?,?)",
            (filename, sha1, error[:400], time.time()),
        )

    def known_sha1(self) -> set:
        return {r[0] for r in self.conn.execute("SELECT sha1 FROM cards").fetchall()}

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def commit(self) -> None:
        self.conn.commit()

    # ----------------------------------------------------------------- read

    def get(self, part: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT card FROM cards WHERE part_key = ?", (part.upper(),)
        ).fetchone()
        return json.loads(row["card"]) if row else None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

    def iter_cards(self) -> Iterator[dict]:
        for row in self.conn.execute("SELECT card FROM cards").fetchall():
            yield json.loads(row["card"])

    def search(self, q: str = "", package: str = "", manufacturer: str = "",
               family: str = "", limit: int = 50, offset: int = 0
               ) -> Tuple[List[dict], int]:
        """Full-text search with optional filters; returns (brief cards, total)."""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        where, args = [], []
        if package:
            where.append("c.package = ?"); args.append(package)
        if manufacturer:
            where.append("c.manufacturer = ?"); args.append(manufacturer)
        if family:
            where.append("c.family = ?"); args.append(family)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        if q:
            # FTS5: quote the query, then prefix the last token so "MMBT39" finds
            # MMBT3904 while typing.
            tokens = re.findall(r"[A-Za-z0-9.+-]+", q)
            if tokens:
                fts_q = " ".join('"%s"' % t for t in tokens[:-1]) + (
                    ' "%s"*' % tokens[-1] if tokens else "")
                sql = (
                    "SELECT c.part, c.manufacturer, c.package, c.family, c.pin_count,"
                    " c.confidence, c.description, c.headline, c.filename, c.pages,"
                    " bm25(cards_fts) AS rank"
                    " FROM cards_fts f JOIN cards c ON c.rowid = f.rowid"
                    " WHERE cards_fts MATCH ?" + (" AND " + " AND ".join(where) if where else "") +
                    " ORDER BY rank LIMIT ? OFFSET ?"
                )
                rows = self.conn.execute(sql, [fts_q] + args + [limit, offset]).fetchall()
                total = self.conn.execute(
                    "SELECT COUNT(*) FROM cards_fts f JOIN cards c ON c.rowid = f.rowid"
                    " WHERE cards_fts MATCH ?" + (" AND " + " AND ".join(where) if where else ""),
                    [fts_q] + args).fetchone()[0]
                return [self._brief(r) for r in rows], total

        rows = self.conn.execute(
            "SELECT part, manufacturer, package, family, pin_count, confidence,"
            " description, headline, filename, pages FROM cards c" + clause +
            " ORDER BY part LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        total = self.conn.execute(
            "SELECT COUNT(*) FROM cards c" + clause, args).fetchone()[0]
        return [self._brief(r) for r in rows], total

    @staticmethod
    def _brief(row) -> dict:
        return {
            "part": row["part"],
            "manufacturer": row["manufacturer"],
            "package": row["package"],
            "family": row["family"],
            "pin_count": row["pin_count"],
            "confidence": row["confidence"],
            "description": (row["description"] or "")[:180],
            "headline": json.loads(row["headline"] or "[]"),
            "filename": row["filename"],
            "pages": row["pages"],
        }

    def facets(self) -> Dict[str, List[Tuple[str, int]]]:
        return {
            "packages": self.conn.execute(
                "SELECT package, COUNT(*) n FROM cards WHERE package IS NOT NULL"
                " GROUP BY package ORDER BY n DESC LIMIT 200").fetchall(),
            "manufacturers": self.conn.execute(
                "SELECT manufacturer, COUNT(*) n FROM cards WHERE manufacturer IS NOT NULL"
                " GROUP BY manufacturer ORDER BY n DESC LIMIT 200").fetchall(),
        }

    def stats(self) -> dict:
        cards = self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        with_pins = self.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE pin_count IS NOT NULL").fetchone()[0]
        with_pkg = self.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE package IS NOT NULL").fetchone()[0]
        with_mfr = self.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE manufacturer IS NOT NULL").fetchone()[0]
        with_ratings = self.conn.execute(
            "SELECT COUNT(*) FROM cards WHERE card LIKE '%\"ratings\": [{%'").fetchone()[0]
        avg_conf = self.conn.execute(
            "SELECT AVG(confidence) FROM cards").fetchone()[0] or 0.0
        failures = self.conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return {
            "cards": cards,
            "with_pins": with_pins,
            "with_package": with_pkg,
            "with_manufacturer": with_mfr,
            "with_ratings": with_ratings,
            "avg_confidence": round(float(avg_conf), 2),
            "failures": failures,
        }

    # ---------------------------------------------------------- static dump

    def dump_shards(self, out_dir: Path) -> Dict[str, Any]:
        """Write brief/XX.json, card/<PART>.json and a small index."""
        out_dir = Path(out_dir)
        brief_dir = out_dir / "brief"
        card_dir = out_dir / "card"
        for d in (brief_dir, card_dir):
            if d.exists():
                for f in d.glob("*.json"):
                    f.unlink()
            d.mkdir(parents=True, exist_ok=True)

        shards: Dict[str, List[dict]] = {}
        for card in self.iter_cards():
            key = shard_key(card.get("part", ""))
            shards.setdefault(key, []).append({
                "part": card.get("part"),
                "manufacturer": card.get("manufacturer"),
                "package": card.get("package"),
                "family": card.get("family"),
                "pin_count": card.get("pin_count"),
                "confidence": card.get("confidence"),
                "description": (card.get("description") or "")[:180],
                "headline": card.get("headline") or [],
            })
            safe = _SAFE.sub("_", (card.get("part") or "?").upper())
            (card_dir / ("%s.json" % safe)).write_text(
                _json(card), encoding="utf-8")

        index = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": sum(len(v) for v in shards.values()),
            "shards": {k: len(v) for k, v in sorted(shards.items())},
        }
        for key, items in shards.items():
            (brief_dir / ("%s.json" % key)).write_text(
                json.dumps(sorted(items, key=lambda c: (c["part"] or "")),
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        (out_dir / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        return index

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def open_store(path: Path) -> CardStore:
    return CardStore(Path(path))
