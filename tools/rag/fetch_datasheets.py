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

Part list (CSV with a header, or JSON list):
    part,manufacturer,package,url
    STM32F103C8,STMicroelectronics,LQFP-48,https://www.st.com/resource/en/datasheet/stm32f103c8.pdf

Notes you should read before pointing this at a vendor:
  * Datasheets are the manufacturers' intellectual property. Downloading them
    for your own local database is normal practice; republishing them (or
    serving the PDFs from a public site) is not — link to the vendor page
    instead.
  * Distributor APIs (Octopart/Nexar, Digi-Key, Mouser, LCSC) are the
    sanctioned way to obtain part lists and datasheet URLs, and they are far
    more reliable than scraping. Get the list there, download the PDF here.
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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
        return rows

    def _record(self, row: dict) -> None:
        """Append one manifest line. Called by fetch_one, always."""
        try:
            with self.manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            print("cannot write manifest: %s" % exc, file=sys.stderr)

    # --------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> Optional[Any]:
        import urllib.robotparser
        host = urllib.parse.urlsplit(url)._replace(path="", query="", fragment="").geturl()
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(host + "/robots.txt")
            try:
                parser.read()
            except Exception:                      # noqa: BLE001 - no robots = allowed
                parser = None
            self._robots[host] = parser
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

    def _wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_request.get(host, 0.0)
        gap = time.time() - last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_request[host] = time.time()

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
        if not url:
            result["error"] = "no url"
            self.stats["failed"] += 1
            self._record(result)
            return result
        if not self.allowed(url):
            result["error"] = "robots.txt disallows this path (use a distributor API)"
            self.stats["blocked"] += 1
            self._record(result)
            return result

        status, body = self.get(url)
        result["status"] = status
        if status != 200 or not body:
            result["error"] = "HTTP %d" % status if status else "network error"
            self.stats["failed"] += 1
            self._record(result)
            return result
        if not body.startswith(PDF_MAGIC) or len(body) < MIN_PDF_BYTES:
            result["error"] = "not a PDF (%d bytes, starts with %r)" % (
                len(body), body[:8])
            self.stats["failed"] += 1
            self._record(result)
            return result

        sha1 = hashlib.sha1(body).hexdigest()
        result["sha1"] = sha1
        if sha1 in self.seen_sha1:
            result["error"] = "duplicate of %s" % self.seen_sha1[sha1]
            self.stats["duplicate"] += 1
            self._record(result)
            return result

        path = self._target(part, url)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.seen_sha1[sha1] = path.name
        result["file"] = path.name
        result["bytes"] = len(body)
        self.stats["ok"] += 1
        if self.verbose:
            print("  [%5.1f KB] %s <- %s" % (len(body) / 1024, path.name, url))
        self._record(result)
        return result


# --------------------------------------------------------------------- input

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Download datasheet PDFs into the corpus")
    ap.add_argument("--list", type=Path, help="CSV/JSON part list: part,manufacturer,package,url")
    ap.add_argument("--parts", type=Path, help="plain text file, one part number per line")
    ap.add_argument("--vendor", choices=sorted(VENDOR_PATTERNS),
                    help="build URLs from the part number (use with --parts)")
    ap.add_argument("--out", type=Path, default=Path("data/datasheets"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests, per host")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--user-agent", default="")
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print URLs, download nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

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

    started = time.time()
    for i, row in enumerate(rows, start=1):
        result = fetcher.fetch_one(row)
        if not args.verbose:
            tail = result["file"] or result["error"]
            print("  [%4d/%4d] %-22s %s" % (i, len(rows), row.get("part", "?"), tail))
        if result["error"] and args.verbose:
            print("         %s" % result["error"])

    print("\nDownloaded %d, duplicates %d, failed %d, blocked by robots %d in %.1fs"
          % (fetcher.stats["ok"], fetcher.stats["duplicate"], fetcher.stats["failed"],
             fetcher.stats["blocked"], time.time() - started))
    print("Manifest:  %s" % fetcher.manifest_path)
    print("Corpus:    %s — now run: python3 tools/rag/build_cards.py --corpus %s"
          % (args.out, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
