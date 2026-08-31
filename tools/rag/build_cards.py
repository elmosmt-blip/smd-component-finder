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
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import card_store, extract, parsers  # noqa: E402

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
          shards: bool = True, verbose: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "cards.db"
    if rebuild and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()

    store = card_store.CardStore(db_path)
    parser_check = parsers.get_parser(parser_name)

    pdfs = sorted(corpus.rglob("*.pdf"))
    if shard:
        idx, total = (int(x) for x in shard.split("/"))
        pdfs = pdfs[idx - 1::total]
    if limit:
        pdfs = pdfs[:limit]

    done_sha = set() if rebuild else store.known_sha1()
    todo: List[Path] = []
    for p in pdfs:
        try:
            sha = parsers._sha1(p)
        except Exception:                          # noqa: BLE001
            sha = None
        if sha and sha in done_sha:
            continue
        todo.append(p)

    print("Parser:      %s" % parser_check.name)
    print("Jobs:        %d" % (jobs or (os.cpu_count() or 1)))
    print("Corpus:      %d PDF(s)" % len(pdfs))
    print("To process:  %d (%d already in the database)" % (len(todo), len(pdfs) - len(todo)))
    if not todo:
        print("\nNothing to do.")
        return store.stats()

    jobs = jobs or (os.cpu_count() or 1)
    processed = written = failed = 0
    started = time.time()
    last_report = started
    seconds_sum = 0.0

    with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init,
                             initargs=(parser_name,)) as pool:
        futures = {pool.submit(process_one, str(p)): p for p in todo}
        try:
            for fut in as_completed(futures):
                result = fut.result()
                processed += 1
                seconds_sum += result.get("seconds") or result.get("card", {}).get("_seconds", 0) or 0
                if result["ok"]:
                    card = result["card"]
                    card.pop("_seconds", None)
                    if store.upsert(card):
                        written += 1
                    if verbose:
                        print("  + %-22s %-12s %s" % (
                            card.get("part", "?"), card.get("package") or "—",
                            card.get("manufacturer") or "—"))
                else:
                    failed += 1
                    store.add_failure(result["filename"], "", result["error"])
                    if verbose:
                        print("  ! %s: %s" % (result["filename"], result["error"][:110]))

                if processed % 25 == 0:
                    store.commit()
                if time.time() - last_report >= 5:
                    elapsed = time.time() - started
                    rate = processed / elapsed if elapsed else 0
                    eta = (len(todo) - processed) / rate if rate else 0
                    print("  %6d/%d  %5.1f/s  elapsed %s  eta %s  (+%d new, %d failed)" % (
                        processed, len(todo), rate, _fmt_eta(elapsed), _fmt_eta(eta),
                        written, failed))
                    last_report = time.time()
                if _STOP:
                    print("Stopping: cancelling the remaining work…")
                    for f in futures:
                        f.cancel()
                    break
        except KeyboardInterrupt:
            _handle_sigint(signal.SIGINT, None)
        finally:
            store.commit()

    store.set_meta("parser", parser_check.name)
    store.set_meta("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    store.commit()

    elapsed = time.time() - started
    print("\nProcessed %d in %s (%.1f PDF/s, %.2fs per PDF)"
          % (processed, _fmt_eta(elapsed), processed / elapsed if elapsed else 0,
             seconds_sum / max(processed, 1)))
    print("New/updated cards: %d   failures: %d" % (written, failed))

    if shards:
        index = store.dump_shards(out / "site")
        print("Static shards: %d file(s), %d cards -> %s/site"
              % (len(index["shards"]), index["count"], out))

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
    ap = argparse.ArgumentParser(description="Extract structured cards from a PDF corpus")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--parser", default="auto", choices=["auto", "docling", "pdfplumber"])
    ap.add_argument("--jobs", type=int, default=0, help="workers (default: CPU count)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", default=None, help="i/n — process every n-th file")
    ap.add_argument("--rebuild", action="store_true", help="drop the database first")
    ap.add_argument("--no-shards", action="store_true", help="skip the static JSON dump")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--show", metavar="PART")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)

    if args.stats:
        print_stats(args.out)
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
                  shards=not args.no_shards, verbose=args.verbose)
    print("\nDatabase: %d cards" % stats["cards"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
