#!/usr/bin/env python3
"""Batch extraction: a directory of PDFs -> a database of cards.

Built for a corpus the size of a real datasheet library, so everything about it
assumes you will not finish in one sitting:

    python3 tools/rag/build_cards.py --corpus data/datasheets --jobs 8

  * **Resumes.** A file whose SHA1 is already in the database is skipped, so
    killing the run and restarting costs nothing. Ctrl-C finishes the current
    batch, commits, and exits cleanly.
  * **Runs in parallel.** --jobs defaults to the CPU count. Each worker is a
    separate process with its own parser instance.
  * **Shards across machines.** --shard 1/4 processes every fourth file; run
    parts 1..4 on four boxes and merge the resulting databases.
  * **Never dies on one bad PDF.** Failures go into the `failures` table with
    the exception text, and the run continues.

Throughput measured on this demo corpus (pdfplumber, 2-page datasheets):
roughly 1–3 PDF/s per core, i.e. ~4 CPU-hours per 10k documents. Budget
accordingly: 300k PDFs is an overnight job on a 16-core machine, not a
coffee break.

    python3 tools/rag/build_cards.py --stats      # what is in the database
    python3 tools/rag/build_cards.py --show MMBT3904
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import card_store, cli, ingest, parsers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CORPUS = ROOT / "data" / "datasheets"
DEFAULT_OUT = ROOT / "data" / "cards"

_STOP = False


def _handle_sigint(signum, frame):   # finish the current batch, then stop
    global _STOP
    _STOP = True
    print("\nStopping after the current batch (restart with the same command to resume)…")


# ---------------------------------------------------------------------------
# worker (top level: it has to be picklable)
# ---------------------------------------------------------------------------

_PARSER = None


def _worker_init(parser_name: str) -> None:
    global _PARSER
    _PARSER = parsers.get_parser(parser_name)


def process_one(path_str: str) -> Dict[str, Any]:
    """Parse one PDF and extract a card. Never raises."""
    path = Path(path_str)
    started = time.time()
    try:
        from tools.rag import metadata
        parsed = _PARSER.parse(path)
        meta = metadata.enrich(parsed)
        card = extract.build_card(parsed, meta)
        data = card.to_dict()
        data["_seconds"] = round(time.time() - started, 3)
        return {"ok": True, "card": data}
    except Exception as exc:                      # noqa: BLE001 - report, never crash
        return {"ok": False, "filename": path.name, "path": path_str,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "seconds": round(time.time() - started, 3)}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds > 60 * 60 * 200:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def build(corpus: Path, out: Path, parser_name: str = "auto", jobs: int = 0,
          limit: int = 0, shard: Optional[str] = None, rebuild: bool = False,
          shards: bool = True, verbose: bool = False,
          docling_pages: Optional[str] = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)

    pdfs = ingest.find_pdfs(corpus)
    if shard:
        idx, total = (int(x) for x in shard.split("/"))
        pdfs = pdfs[idx - 1::total]
    if limit:
        pdfs = pdfs[:limit]
    if not pdfs:
        print("No PDFs in %s" % corpus)
        print("Drop datasheets there, or generate a demo corpus:")
        print("  python3 tools/rag/sample_datasheets.py")
        return {"cards": 0, "failures": 0}

    if rebuild:
        # Empty the tables instead of deleting the file: the web server keeps
        # one connection open, and a deleted file leaves it reading an inode
        # that no longer exists — the site would show an empty database until
        # it is restarted.
        # Not one byte of the file may be recreated: the site keeps the
        # database open, and a new inode (or a deleted -wal/-shm pair) would
        # leave it reading a snapshot that no longer exists.
        db = out / "cards.db"
        if db.exists():
            con = sqlite3.connect(str(db))
            try:
                con.executescript(
                    "DELETE FROM cards; DELETE FROM failures; DELETE FROM meta;")
                con.commit()
            finally:
                con.close()

    docling_opts = {"pages": docling_pages} if docling_pages else {}
    parser_used = parsers.get_parser(parser_name, **docling_opts)  # before forking
    runner = ingest.JobRunner(out, parser=parser_name, jobs=jobs)
    job_id = runner.start(pdfs, shards=shards)
    print("Parser:      %s" % parser_used.name)
    for reason in getattr(parser_used, "fallback_errors", []) or []:
        print("             (skipped %s)" % reason.split(":")[0])
    print("Jobs:        %d" % (jobs or (os.cpu_count() or 1)))
    print("Corpus:      %d PDF(s)" % len(pdfs))
    print("")

    last_print = 0.0
    try:
        while True:
            st = runner.status(job_id) or {}
            state = st.get("state")
            if state != "running":
                break
            now = time.time()
            if now - last_print >= 5 or (verbose and st.get("done")):
                print("  %5d/%d  %5.1f/s  elapsed %s  eta %s  (+%d, %d failed)  %s"
                      % (st["done"], st["total"], st["rate"], _fmt_eta(st["elapsed"]),
                         _fmt_eta(st["eta"]), st["ok"], st["failed"],
                         st.get("current", "")[:28]))
                last_print = now
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nCancelling… restart with the same command to resume.")
        runner.cancel(job_id)
        while (runner.status(job_id) or {}).get("state") == "running":
            time.sleep(0.4)

    st = runner.status(job_id) or {}
    print("\n%s in %s: %d new card(s), %d skipped, %d failed"
          % (st.get("state", "?").capitalize(), _fmt_eta(st.get("elapsed", 0)),
             st.get("ok", 0), st.get("skipped", 0), st.get("failed", 0)))
    for err in st.get("errors", [])[:10]:
        print("  ! %s: %s" % (err["file"], err["error"][:110]))
    if st.get("message"):
        print("  %s" % st["message"])

    store = card_store.CardStore(out / "cards.db")
    stats = store.stats()
    store.close()
    return stats


def print_stats(out: Path) -> None:
    store = card_store.CardStore(out / "cards.db")
    s = store.stats()
    print("Cards:            %d" % s["cards"])
    print("  with package:   %d" % s["with_package"])
    print("  with pins:      %d" % s["with_pins"])
    print("  with mfr:       %d" % s["with_manufacturer"])
    print("  with ratings:   %d" % s["with_ratings"])
    print("  avg confidence: %.2f" % s["avg_confidence"])
    print("  failures:       %d" % s["failures"])
    facets = store.facets()
    if facets["packages"]:
        print("\nTop packages:")
        for name, n in facets["packages"][:10]:
            print("  %-14s %d" % (name, n))
    if facets["manufacturers"]:
        print("\nTop manufacturers:")
        for name, n in facets["manufacturers"][:10]:
            print("  %-24s %d" % (name, n))
    store.close()


def main() -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(description="Extract structured cards from a PDF corpus")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--parser", default="auto", choices=["auto", "docling", "pdfplumber"])
    ap.add_argument("--docling-pages", default=None, choices=["tables", "all"],
                    help="send Docling only the pages with tables (default) or every page")
    ap.add_argument("--jobs", type=int, default=0, help="workers (default: CPU count)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", default=None, help="i/n — process every n-th file")
    ap.add_argument("--rebuild", action="store_true", help="drop the database first")
    ap.add_argument("--no-shards", action="store_true", help="skip the static JSON dump")
    ap.add_argument("--dump-shards", action="store_true",
                    help="only rewrite the static JSON from an existing database")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--show", metavar="PART")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)

    if args.stats:
        print_stats(args.out)
        return 0
    if args.dump_shards:
        # Separated on purpose: on 300k cards this is 300k small files, and
        # nobody wants that to happen again at the end of every run.
        store = card_store.CardStore(args.out / "cards.db")
        try:
            index = store.dump_shards(args.out / "site")
        finally:
            store.close()
        print("Shards: %d cards -> %s" % (index.get("count", 0), args.out / "site"))
        return 0
    if args.show:
        store = card_store.CardStore(args.out / "cards.db")
        card = store.get(args.show)
        if not card:
            print("No card for %s" % args.show)
            return 1
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    stats = build(args.corpus, args.out, parser_name=args.parser, jobs=args.jobs,
                  limit=args.limit, shard=args.shard, rebuild=args.rebuild,
                  shards=not args.no_shards, verbose=args.verbose,
                  docling_pages=args.docling_pages)
    print("\nDatabase: %d cards" % stats["cards"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
