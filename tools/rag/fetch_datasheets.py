#!/usr/bin/env python3
"""Download datasheet PDFs into the corpus folder — politely and verifiably.

A 300k corpus is not downloaded by clicking links. This takes a *part list*
(CSV/JSON: part, manufacturer, package, url) and does the boring part: rate
limits, retries, content checks, de-duplication and a manifest with
provenance, so that six months later you still know where every file came
from.

    python3 tools/rag/fetch_datasheets.py --list parts.csv --out data/datasheets
    python3 tools/rag/fetch_datasheets.py --vendor st --parts st-parts.txt --out data/datasheets
    python3 tools/rag/fetch_datasheets.py --list parts.csv --dry-run   # show URLs

300k files is a queue, not a for-loop: `--workers 16 --delay 0.05` keeps
sixteen sockets busy while still spacing the requests out. Throughput is
governed by `--delay` (requests start 1/delay seconds apart *per host*);
`--workers` only stops network latency from eating that rate.

Part list (CSV with a header, or JSON list):
    part,manufacturer,package,url
    STM32F103C8,STMicroelectronics,LQFP-48,https://www.st.com/resource/en/datasheet/stm32f103c8.pdf

Notes you should read before pointing this at a vendor:
  * Datasheets are the manufacturers' intellectual property. Downloading them
    for your own local database is normal practice; republishing them (or
    serving the PDFs from a public site) is not — link to the vendor page
    instead.
  * Where the part list comes from: the open JLCPCB/LCSC catalogue dump
    (`--from-jlcparts cache.sqlite3`, see the README) — millions of SMD parts,
    no API key, no per-month limit. Vendor sites are for the gaps.
    Octopart/Nexar is not enough: the free plan is ~1000 parts a month.
    Digi-Key forbids bulk download and building a database from its API, so
    its API is for completing a known list, not for harvesting.
  * robots.txt is honoured by default. If a host disallows the path, this
    script tells you which host and refuses — use an API instead of
    --ignore-robots.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import cli  # noqa: E402

PDF_MAGIC = b"%PDF"
MIN_PDF_BYTES = 4096                 # smaller than this is an error page, not a datasheet

# Best-effort URL patterns. Vendors change these without notice; --dry-run
# first, and if a pattern 404s, put the real URL in the CSV instead.
VENDOR_PATTERNS: Dict[str, str] = {
    "ti": "https://www.ti.com/lit/ds/symlink/{part_lower}.pdf",
    "st": "https://www.st.com/resource/en/datasheet/{part_lower}.pdf",
    "microchip": "https://ww1.microchip.com/downloads/en/DeviceDoc/{part}.pdf",
    "nxp": "https://www.nxp.com/docs/en/data-sheet/{part}.pdf",
    "onsemi": "https://www.onsemi.com/pdf/datasheet/{part_lower}-d.pdf",
    "diodes": "https://www.diodes.com/assets/Datasheets/{part}.pdf",
    "vishay": "https://www.vishay.com/docs?docId={part}",
    "rohm": "https://fscdn.rohm.com/en/products/databook/datasheet/{part}.pdf",
}


# "Texas Instruments" in a CSV should not need a separate column to be
# recognised — map the usual spellings onto the URL patterns above.
MANUFACTURER_ALIASES: Dict[str, str] = {
    "ti": "ti", "texas instruments": "ti", "texas instrument": "ti",
    "st": "st", "stm": "st", "stmicroelectronics": "st",
    "microchip": "microchip", "atmel": "microchip",
    "nxp": "nxp", "nxp semiconductors": "nxp", "freescale": "nxp",
    "onsemi": "onsemi", "on semiconductor": "onsemi", "on semiconductors": "onsemi",
    "diodes": "diodes", "diodes incorporated": "diodes", "diodes inc": "diodes",
    "vishay": "vishay", "rohm": "rohm", "rohm semiconductor": "rohm",
}


def vendor_of(row: Dict[str, Any], default: str = "") -> str:
    """Vendor for one row: its own column first, then the --vendor fallback."""
    for key in ("vendor", "manufacturer", "mfr", "brand"):
        value = str(row.get(key) or "").strip().lower()
        if not value:
            continue
        if value in VENDOR_PATTERNS:
            return value
        if value in MANUFACTURER_ALIASES:
            return MANUFACTURER_ALIASES[value]
    return default


class Fetcher:
    """HTTP GET with delays, retries and a memory of what it already saw."""

    def __init__(self, out_dir: Path, delay: float = 1.0, retries: int = 3,
                 timeout: float = 45.0, user_agent: str = "", respect_robots: bool = True,
                 verbose: bool = False):
        self.out_dir = Path(out_dir)
        self.delay = max(0.0, delay)
        self.retries = max(0, retries)
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.verbose = verbose
        self.agent = user_agent or (
            "smd-component-finder/1.0 (+local datasheet database; contact: you@example.com)")
        self.opener = urllib.request.build_opener()
        self._last_request: Dict[str, float] = {}
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.seen_sha1: Dict[str, str] = {}      # sha1 -> filename already on disk
        self.seen_url: Dict[str, str] = {}       # url   -> file it produced once
        # One lock for the bookkeeping, one per host for the rate limit. The
        # host lock is held only while a start slot is reserved — never across
        # the socket — so N workers can have N requests in flight while the
        # starts stay `delay` seconds apart.
        self._lock = threading.RLock()
        self._host_locks: Dict[str, threading.Lock] = {}
        self.manifest_path = self.out_dir / "manifest.jsonl"
        self.stats: Dict[str, int] = {"ok": 0, "skipped": 0, "duplicate": 0,
                                      "failed": 0, "blocked": 0}

    # ------------------------------------------------------------- manifest

    def load_manifest(self) -> List[dict]:
        rows: List[dict] = []
        if not self.manifest_path.exists():
            return rows
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows.append(row)
            if row.get("sha1") and row.get("file"):
                self.seen_sha1[row["sha1"]] = row["file"]
            if row.get("url") and row.get("file"):
                self.seen_url[row["url"]] = row["file"]
        return rows

    def _record(self, row: dict) -> None:
        """Append one manifest line. Called by fetch_one, always.

        Serialised: with --workers the manifest is written from several
        threads, and interleaved half-written lines would make the one file
        that records where every PDF came from unreadable.
        """
        try:
            with self._lock:
                with self.manifest_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            print("cannot write manifest: %s" % exc, file=sys.stderr)

    # --------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> Optional[Any]:
        import urllib.robotparser
        host = urllib.parse.urlsplit(url)._replace(path="", query="", fragment="").geturl()
        with self._lock:
            cached = host in self._robots
        if not cached:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(host + "/robots.txt")
            try:
                parser.read()
            except Exception:                      # noqa: BLE001 - no robots = allowed
                parser = None
            with self._lock:
                self._robots[host] = parser
        with self._lock:
            return self._robots[host]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.agent, url)
        except Exception:                          # noqa: BLE001
            return True

    # ---------------------------------------------------------------- fetch

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = self._host_locks[host] = threading.Lock()
            return lock

    def _wait(self, url: str) -> None:
        """Reserve the next start slot on this host, then sleep until it.

        Sequential code got the same politeness for free; with --workers the
        slot has to be reserved *before* sleeping, otherwise every worker
        wakes up at the same moment and the host sees a burst.
        """
        host = urllib.parse.urlsplit(url).netloc
        with self._host_lock(host):
            now = time.time()
            start = max(now, self._last_request.get(host, 0.0) + self.delay)
            self._last_request[host] = start
        gap = start - time.time()
        if gap > 0:
            time.sleep(gap)

    def get(self, url: str) -> Tuple[int, bytes]:
        """Returns (status, body). Retries on 5xx and network errors."""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self._wait(url)
            req = urllib.request.Request(url, headers={
                "User-Agent": self.agent,
                "Accept": "application/pdf,*/*",
            })
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return int(resp.status or 200), resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404, 410):
                    return exc.code, b""
                last_error = exc
                if attempt == self.retries:
                    return exc.code, b""
            except Exception as exc:                # noqa: BLE001 - network, DNS, TLS
                last_error = exc
                if attempt == self.retries:
                    return 0, b""
            time.sleep(min(2.0 * (attempt + 1), 10.0))
        return 0, b""

    # ----------------------------------------------------------------- save

    def _target(self, part: str, url: str) -> Path:
        name = Path(urllib.parse.urlsplit(url).path).name or "datasheet.pdf"
        if not name.lower().endswith(".pdf"):
            name = (part or "datasheet") + ".pdf"
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        path = self.out_dir / safe
        if path.exists():                           # never overwrite silently
            stem, suffix = path.stem, path.suffix
            n = 1
            while path.exists():
                path = self.out_dir / ("%s_%d%s" % (stem, n, suffix))
                n += 1
        return path

    def fetch_one(self, row: Dict[str, Any]) -> dict:
        """Download one row of the part list. Never raises."""
        part = str(row.get("part") or "").strip()
        url = str(row.get("url") or "").strip()
        result = {
            "part": part,
            "manufacturer": row.get("manufacturer") or "",
            "package": row.get("package") or "",
            "url": url,
            "file": "",
            "bytes": 0,
            "sha1": "",
            "status": 0,
            "error": "",
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        def fail(message: str, key: str = "failed") -> dict:
            result["error"] = message
            with self._lock:
                self.stats[key] += 1
            self._record(result)
            return result

        if not url:
            return fail("no url")
        with self._lock:
            already = self.seen_url.get(url)
        if already:
            # Same URL, already on disk from an earlier run. Growing a corpus
            # is mostly rerunning it, and there is no reason to pay for the
            # bytes twice just to discover the hash matches.
            result["file"] = ""
            result["error"] = "already fetched as %s" % already
            with self._lock:
                self.stats["duplicate"] += 1
            return result
        if not self.allowed(url):
            return fail("robots.txt disallows this path (use a distributor API)",
                        "blocked")

        status, body = self.get(url)
        backup = str(row.get("source_url") or "").strip()
        if (status != 200 or not body) and backup and backup != url:
            # Vendor URL patterns are guesswork and vendors move files. The
            # catalogue link is the one we know exists, so use it instead of
            # losing the part — and say in the manifest which one worked.
            fstatus, fbody = self.get(backup)
            if fstatus == 200 and fbody:
                url, status, body = backup, fstatus, fbody
                result["url"] = backup
                result["fallback"] = True
        result["status"] = status
        if status != 200 or not body:
            return fail("HTTP %d" % status if status else "network error")
        if not body.startswith(PDF_MAGIC) or len(body) < MIN_PDF_BYTES:
            return fail("not a PDF (%d bytes, starts with %r)" % (
                len(body), body[:8]))

        sha1 = hashlib.sha1(body).hexdigest()
        result["sha1"] = sha1
        with self._lock:
            known = self.seen_sha1.get(sha1)
            if known:
                return fail("duplicate of %s" % known, "duplicate")
            # the whole name-claim-and-write is one critical section: two
            # threads downloading the same file name must not both pick it
            path = self._target(part, url)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            self.seen_sha1[sha1] = path.name
            self.seen_url[url] = path.name
            self.stats["ok"] += 1
        result["file"] = path.name
        result["bytes"] = len(body)
        if self.verbose:
            print("  [%5.1f KB] %s <- %s" % (len(body) / 1024, path.name, url))
        self._record(result)
        return result

    # --------------------------------------------------------------- in bulk

    def fetch_many(self, rows: List[Dict[str, Any]], workers: int = 1,
                   on_result: Optional[Callable[[int, dict, dict], None]] = None
                   ) -> List[dict]:
        """Download a list, in parallel if asked.

        Results come back in the order the rows were given, so a rerun prints
        the same summary as a sequential one — only the progress lines arrive
        in completion order, which is the point of going parallel.

        `workers` only helps to hide latency: the rate is set by `--delay`.
        """
        rows = list(rows)
        if workers <= 1 or len(rows) < 2:
            out = []
            for i, row in enumerate(rows):
                result = self.fetch_one(row)
                out.append(result)
                if on_result:
                    on_result(i, row, result)
            return out

        out: List[Optional[dict]] = [None] * len(rows)
        with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as pool:
            futures = {pool.submit(self.fetch_one, row): i
                       for i, row in enumerate(rows)}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    result = future.result()
                except Exception as exc:            # noqa: BLE001 - never kill the run
                    result = {"part": str(rows[i].get("part") or ""),
                              "error": "%s: %s" % (type(exc).__name__, exc),
                              "file": ""}
                    with self._lock:
                        self.stats["failed"] += 1
                out[i] = result
                if on_result:
                    on_result(i, rows[i], result)
        return [r if r is not None else {"part": "", "error": "no result", "file": ""}
                for r in out]


# --------------------------------------------------------------------- input

def probe_cache(db_path: Path, columns: int = 40) -> dict:
    """What is actually inside this cache file?

    The catalogue has changed its layout more than once (`components` ->
    `jlc_components`), and the error message you get when the shape moved is
    useless if you cannot see the shape. So: tables, row counts and columns,
    printed as text you can paste into a chat.
    """
    import sqlite3

    out: dict = {"tables": [], "error": ""}
    if not db_path.exists():
        # sqlite3.connect() happily creates an empty file, and "this cache is
        # empty" is a lie worth avoiding.
        out["error"] = "нет файла: %s" % db_path
        return out
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        objects = con.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for obj in objects:
            name, kind = obj["name"], obj["type"]
            try:
                rows = con.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            except sqlite3.Error:
                rows = -1
            try:
                cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % name)]
            except sqlite3.Error:
                cols = []
            out["tables"].append({"name": name, "kind": kind, "rows": rows,
                                  "columns": cols[:columns]})
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    finally:
        con.close()
    return out


def print_probe(db_path: Path, probe: dict) -> None:
    print("Кэш каталога: %s" % db_path)
    if probe["error"]:
        print("  [!!] не читается: %s" % probe["error"])
        return
    for table in probe["tables"]:
        rows = "%d строк" % table["rows"] if table["rows"] >= 0 else "не посчитать"
        print("  %-22s %-6s %s" % (table["name"], table["kind"], rows))
        if table["columns"]:
            print("      колонки: %s" % ", ".join(table["columns"]))
    print("")
    print("Дальше:")
    print("  python3 tools/rag/fetch_datasheets.py --from-jlcparts %s --to-csv parts.csv"
          % db_path)


def manifest_report(path: Path, top: int = 10) -> dict:
    """What did we actually download? Read it back out of the manifest.

    The interesting number is never "files downloaded": it is how many were
    duplicates, how many failed, how big they are on average and where they
    came from. On the first real corpus the duplicate rate was 58 %, which
    changes what "300 000 PDFs" even means.
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue

    counts = {"ok": 0, "duplicate": 0, "failed": 0, "blocked": 0}
    hosts: Dict[str, int] = {}
    errors: Dict[str, int] = {}
    sizes: List[int] = []
    for row in rows:
        if row.get("file"):
            counts["ok"] += 1
            sizes.append(int(row.get("bytes") or 0))
        error = (row.get("error") or "").strip()
        if error.startswith("duplicate") or error.startswith("already fetched"):
            counts["duplicate"] += 1
        elif error.startswith("robots"):
            counts["blocked"] += 1
        elif error:
            counts["failed"] += 1
            errors[error.split(":")[0][:60]] = errors.get(
                error.split(":")[0][:60], 0) + 1
        if row.get("url"):
            host = urllib.parse.urlsplit(row["url"]).netloc
            hosts[host] = hosts.get(host, 0) + 1

    unique = counts["ok"]
    total_attempts = len(rows)
    return {
        "path": str(path),
        "attempts": total_attempts,
        "counts": counts,
        "bytes": sum(sizes),
        "avg_bytes": int(sum(sizes) / unique) if unique else 0,
        "dup_rate": round(100.0 * counts["duplicate"] / total_attempts, 1) if total_attempts else 0.0,
        "hosts": sorted(hosts.items(), key=lambda kv: -kv[1])[:top],
        "errors": sorted(errors.items(), key=lambda kv: -kv[1])[:5],
        "top": top,
    }


def print_manifest_report(report: dict) -> None:
    c = report["counts"]
    print("Манифест: %s" % report["path"])
    print("  попыток (строк):        %d" % report["attempts"])
    print("  скачано файлов:         %d" % c["ok"])
    print("  дублей (не сохраняли):  %d  (%.1f%% попыток)"
          % (c["duplicate"], report["dup_rate"]))
    print("  сбоев:                  %d" % c["failed"])
    print("  запрещено robots.txt:   %d" % c["blocked"])
    if c["ok"]:
        print("  место на диске:         %.2f ГБ, средний файл %.0f КБ"
              % (report["bytes"] / 2 ** 30, report["avg_bytes"] / 1024.0))
        print("  прогноз на 300 000 PDF: %.0f ГБ (по среднему размеру)"
              % (300000 * report["avg_bytes"] / 2 ** 30))
    if report["hosts"]:
        print("")
        print("  откуда качали:")
        for host, n in report["hosts"]:
            print("    %-38s %d" % (host, n))
    if report["errors"]:
        print("")
        print("  почему не скачалось:")
        for error, n in report["errors"]:
            print("    %-38s %d" % (error, n))


def _fmt_hours(seconds: float) -> str:
    """'3.5 days' rather than '302400.0s' — the number people actually need
    before starting a 300k download."""
    if seconds < 90:
        return "%.0f s" % seconds
    hours = seconds / 3600.0
    if hours < 48:
        return "%.1f h" % hours
    return "%.1f days" % (hours / 24.0)


def build_url(vendor: str, part: str) -> str:
    pattern = VENDOR_PATTERNS[vendor]
    return pattern.format(part=part, part_lower=part.lower(),
                          part_upper=part.upper())


def read_list(path: Path, vendor: str = "", parts_file: Optional[Path] = None
              ) -> List[Dict[str, Any]]:
    """Rows from a CSV/JSON part list, or from a bare list of part numbers.

    A row is allowed to name its own vendor (`vendor` or `manufacturer`
    column). `--vendor` is only the fallback for rows that do not — otherwise
    passing `--vendor st` would build ST URLs for a Microchip part too.
    """
    rows: List[Dict[str, Any]] = []
    if parts_file is not None:
        for line in parts_file.read_text(encoding="utf-8").splitlines():
            part = line.strip()
            if not part or part.startswith("#"):
                continue
            rows.append({"part": part,
                         "url": build_url(vendor, part) if vendor else ""})
        return rows

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        items = json.loads(text)
    else:
        items = list(csv.DictReader(text.splitlines()))

    for item in items:
        if isinstance(item, str):
            item = {"part": item}
        if not (item.get("url") or "").strip():
            row_vendor = vendor_of(item, vendor)
            if row_vendor:
                item["url"] = build_url(row_vendor, str(item.get("part", "")).strip())
        rows.append(item)
    return rows


# ------------------------------------------------------- open parts catalogs

# Column names as they appear in the open JLCPCB/LCSC catalogue
# (https://yaqwsx.github.io/jlcparts/ — a downloadable SQLite, several million
# SMD parts with datasheet links). Matched loosely: the schema has changed
# before and will change again, so the exporter introspects the file instead
# of trusting a fixed layout.
JLC_PART_COLUMNS = ("mfr", "part", "mpn", "part_number")
JLC_PACKAGE_COLUMNS = ("package", "footprint")
JLC_URL_COLUMNS = ("datasheet", "url", "datasheet_url")


def _source_table(con, tables: List[str], views: List[str], wanted: str) -> str:
    """Which relation to read: the joined view if it is there, else the table.

    jlcparts ships `components` (mfr, package, datasheet, manufacturer_id,
    category_id) *and* a view `v_components` that already carries the
    manufacturer name and the category names. Reading the view saves two
    joins on a 7-million-row table — which on an 11 GB database is the
    difference between a minute and a coffee break.
    """
    if wanted in tables or wanted in views:
        return wanted
    # jlcparts moved to a "v2" layout in 2026: `jlc_components` and
    # `lcsc_components` replaced `components` / `v_components`. Both shapes
    # are listed, so a cache downloaded a year ago still works.
    for name in ("v_components", "components", "jlc_components",
                 "lcsc_components", "parts", "v_parts"):
        if name in views or name in tables:
            return name
    return ""


def _fmt_lcsc(value: Any) -> str:
    """jlcparts stores the LCSC code as 123456; humans write C123456."""
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if text.isdigit():
        return "C" + text
    return text


def export_from_sqlite(db_path: Path, out_csv: Path, table: str = "",
                       basic_only: bool = False, min_stock: int = 0,
                       category: str = "", limit: int = 0,
                       prefer_vendor: bool = False, verbose: bool = False) -> int:
    """Turn a parts database (jlcparts and friends) into a part list CSV.

    `prefer_vendor` is the interesting one. The catalogue links mostly point
    at LCSC's own copies — often a scan of the original. When the row names a
    manufacturer we know the URL pattern for (TI, ST, NXP, onsemi, Diodes,
    Microchip, Vishay, ROHM), the link is rebuilt to point at the
    manufacturer's own PDF. Same part, much better document. The catalogue
    link is kept in a `source_url` column and used as a fallback if the
    vendor URL 404s.

    Nothing here assumes a schema: columns are found by name, and if the file
    does not look like a parts catalogue the available columns are printed so
    you can map them yourself with a `--list` CSV of your own.

    The real jlcparts `cache.sqlite3` looks like this, and all three variants
    are handled:

        components(lcsc, mfr, package, datasheet, stock, basic, preferred,
                   manufacturer_id, category_id, ...)
        manufacturers(id, name)      categories(id, category, subcategory)
        v_components                 -- the same, with names joined in

    Note the trap: `mfr` is the *part number*, not the manufacturer. The
    manufacturer's name only exists in `manufacturers` (or in the view).
    """
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        objects = [(r[0], r[1]) for r in con.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')")]
        tables = [n for n, t in objects if t == "table"]
        views = [n for n, t in objects if t == "view"]
        source = _source_table(con, tables, views, table)
        if not source:
            raise SystemExit("no usable table in %s; available: %s"
                             % (db_path.name, ", ".join(tables[:12])))
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % source)]

        def pick(options: Iterable[str]) -> str:
            for name in options:
                if name in cols:
                    return name
            return ""

        part_col = pick(JLC_PART_COLUMNS)
        pkg_col = pick(JLC_PACKAGE_COLUMNS)
        url_col = pick(JLC_URL_COLUMNS)
        if not part_col or not url_col:
            raise SystemExit(
                "cannot map %s: need a part-number column %s and a datasheet "
                "column %s; %s has: %s"
                % (db_path.name, list(JLC_PART_COLUMNS), list(JLC_URL_COLUMNS),
                   source, ", ".join(cols)))

        mfr_col = pick(("manufacturer", "manufacturer_name", "brand", "mfr_name"))
        joins = ""
        if not mfr_col and "manufacturer_id" in cols and "manufacturers" in tables:
            joins += " LEFT JOIN manufacturers mn ON mn.id = p.manufacturer_id"
            mfr_col = "mn.name"
        lcsc_col = pick(("lcsc", "lcsc_part"))

        where, args = [], []
        where.append("TRIM(COALESCE(p.%s, '')) != ''" % url_col)
        if basic_only:
            flags = [c for c in ("basic", "preferred") if c in cols]
            if flags:
                where.append("(" + " OR ".join("p.%s = 1" % c for c in flags) + ")")
            else:
                print("warning: no basic/preferred column, --basic-only ignored")
        if min_stock:
            stock_col = pick(("stock", "last_on_stock", "quantity"))
            if stock_col:
                where.append("COALESCE(p.%s, 0) >= ?" % stock_col)
                args.append(min_stock)
        if category:
            like = "%%%s%%" % category
            cat_cols = [c for c in ("category", "subcategory") if c in cols]
            if cat_cols:                     # the view already has the names
                where.append("(" + " OR ".join(
                    "COALESCE(p.%s, '') LIKE ?" % c for c in cat_cols) + ")")
                args += [like] * len(cat_cols)
            elif "category_id" in cols and "categories" in tables:
                joins += " LEFT JOIN categories c ON c.id = p.category_id"
                where.append("(COALESCE(c.category, '') LIKE ? "
                             "OR COALESCE(c.subcategory, '') LIKE ?)")
                args += [like, like]
            else:
                print("warning: no categories table, --category ignored")

        select = ["p.%s AS part" % part_col,
                  "%s AS manufacturer" % mfr_col if mfr_col else "'' AS manufacturer",
                  "COALESCE(p.%s, '') AS package" % pkg_col if pkg_col else "'' AS package",
                  "p.%s AS url" % url_col]
        if lcsc_col and lcsc_col != part_col:
            select.append("p.%s AS lcsc" % lcsc_col)

        sql = ("SELECT DISTINCT %s FROM %s p%s WHERE %s%s"
               % (", ".join(select), source, joins, " AND ".join(where),
                  " LIMIT %d" % limit if limit else ""))
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    header = ["part", "manufacturer", "package", "url"]
    if lcsc_col and lcsc_col != part_col:
        header.append("lcsc")
    if prefer_vendor:
        header.append("source_url")

    vendor_hits = 0
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            part = row["part"] or ""
            manufacturer = row["manufacturer"] or ""
            catalog_url = row["url"] or ""
            url = catalog_url
            if prefer_vendor:
                vendor = vendor_of({"manufacturer": manufacturer})
                if vendor:
                    built = build_url(vendor, part)
                    if built:
                        url = built
                        vendor_hits += 1
            fields = [part, manufacturer, row["package"] or "", url]
            if header[-1] == "lcsc" or (lcsc_col and lcsc_col != part_col):
                fields.append(_fmt_lcsc(row["lcsc"]))
            if prefer_vendor:
                fields.append(catalog_url)
            writer.writerow(fields)
    if verbose:
        print("Exported %d parts from %s -> %s" % (len(rows), source, out_csv))
        if prefer_vendor:
            print("  vendor datasheet URLs: %d of %d (the rest keep the "
                  "catalogue link as a fallback)" % (vendor_hits, len(rows)))
    return len(rows)



def main() -> int:
    # Before anything is printed. This catalogue is full of Chinese part
    # names, and a Windows console defaults to cp1251: without this the very
    # first exotic marking kills a long run with UnicodeEncodeError.
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(description="Download datasheet PDFs into the corpus")
    ap.add_argument("--list", type=Path, help="CSV/JSON part list: part,manufacturer,package,url")
    ap.add_argument("--parts", type=Path, help="plain text file, one part number per line")
    ap.add_argument("--vendor", choices=sorted(VENDOR_PATTERNS),
                    help="build URLs from the part number (use with --parts)")
    ap.add_argument("--from-jlcparts", type=Path, metavar="CACHE.SQLITE3",
                    help="export a part list from the open JLCPCB/LCSC catalog")
    ap.add_argument("--to-csv", type=Path, metavar="PARTS.CSV",
                    help="where to write the exported list (default: parts.csv)")
    ap.add_argument("--basic-only", action="store_true",
                    help="JLCPCB basic/preferred parts only")
    ap.add_argument("--min-stock", type=int, default=0)
    ap.add_argument("--category", default="", help="substring of the category name")
    ap.add_argument("--prefer-vendor", action="store_true",
                    help="link to the manufacturer's own PDF instead of the "
                         "catalogue copy, when we know its URL pattern")
    ap.add_argument("--out", type=Path, default=Path("data/datasheets"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests, per host")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel downloads (1-32). The rate is still set by "
                         "--delay; workers only hide network latency")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--user-agent", default="")
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print URLs, download nothing")
    ap.add_argument("--probe", type=Path, metavar="CACHE.SQLITE3",
                    help="show what is inside a catalogue cache and exit")
    ap.add_argument("--report", type=Path, metavar="MANIFEST.JSONL",
                    help="summarise a download manifest and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.probe:
        if not args.probe.exists():
            print("No such file: %s" % args.probe)
            return 2
        print_probe(args.probe, probe_cache(args.probe))
        return 0

    if args.report:
        if not args.report.exists():
            print("No such file: %s" % args.report)
            return 2
        print_manifest_report(manifest_report(args.report))
        return 0

    if args.from_jlcparts:
        target = args.to_csv or Path("parts.csv")
        if not args.from_jlcparts.exists():
            print("No such file: %s" % args.from_jlcparts)
            print("Download cache.sqlite3 from https://yaqwsx.github.io/jlcparts/")
            return 2
        n = export_from_sqlite(args.from_jlcparts, target,
                               basic_only=args.basic_only,
                               min_stock=args.min_stock, category=args.category,
                               limit=args.limit, prefer_vendor=args.prefer_vendor,
                               verbose=True)
        print("Next: python3 tools/rag/fetch_datasheets.py --list %s "
              "--out %s --dry-run" % (target, args.out))
        return 0 if n else 1

    if not args.list and not args.parts:
        print("Give me a part list: --list parts.csv or --parts parts.txt [--vendor st]")
        return 2

    rows = read_list(args.list, args.vendor, args.parts) if args.list else \
        read_list(args.parts, args.vendor, args.parts)
    if args.limit:
        rows = rows[: args.limit]
    print("Part list: %d entries" % len(rows))

    if args.dry_run:
        for row in rows:
            print("  %-24s %s" % (row.get("part", "?"), row.get("url") or "NO URL"))
        missing = sum(1 for r in rows if not r.get("url"))
        if missing:
            print("\n%d entries have no URL — add a url column or --vendor" % missing)
        return 0

    fetcher = Fetcher(args.out, delay=args.delay, retries=args.retries,
                      timeout=args.timeout, user_agent=args.user_agent,
                      respect_robots=not args.ignore_robots, verbose=args.verbose)
    args.out.mkdir(parents=True, exist_ok=True)
    fetcher.load_manifest()

    workers = max(1, min(32, int(args.workers or 1)))
    done = [0]
    total = len(rows)

    def report(index: int, row: dict, result: dict) -> None:
        if args.verbose:
            if result.get("error"):
                print("         %s" % result["error"])
            return
        done[0] += 1
        tail = result.get("file") or result.get("error") or ""
        print("  [%4d/%4d] %-22s %s" % (done[0], total, row.get("part", "?"), tail))

    started = time.time()
    fetcher.fetch_many(rows, workers=workers, on_result=report)
    elapsed = max(time.time() - started, 1e-6)

    print("\nDownloaded %d, duplicates %d, failed %d, blocked by robots %d in %.1fs"
          % (fetcher.stats["ok"], fetcher.stats["duplicate"], fetcher.stats["failed"],
             fetcher.stats["blocked"], elapsed))
    if fetcher.stats["ok"]:
        print("Rate: %.2f files/s (%d workers, delay %.2fs) — 300 000 files would take %s"
              % (fetcher.stats["ok"] / elapsed, workers, args.delay,
                 _fmt_hours(300000 * elapsed / fetcher.stats["ok"])))
    print("Manifest:  %s" % fetcher.manifest_path)
    print("Corpus:    %s — now run: python3 tools/rag/build_cards.py --corpus %s"
          % (args.out, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
