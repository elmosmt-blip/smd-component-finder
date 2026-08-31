#!/usr/bin/env python3
"""Measure the parser on THIS machine before starting the big run.

The whole 300k question is "how many PDFs per second, and with how many
workers". Everything else — how many hours, whether to leave it overnight,
how much RAM it takes — follows from that number, and it depends on your CPU,
your disk and on how long the datasheets are. So measure it here.

    python3 tools/rag/bench.py --corpus /mnt/datasheets --jobs 8,16,24,32

Nothing is copied and nothing is written outside a temporary directory: a
sample of the real corpus is parsed once per worker count, then the result is
extrapolated to --target files.

    python3 tools/rag/bench.py --corpus /mnt/datasheets --files 200 --jobs 16
"""

from __future__ import annotations

import argparse
import os
import platform
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List

if __package__ in (None, ""):            # started as a script, not a module
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import ingest, parsers  # noqa: E402

BAR = "-" * 64


def pick_sample(pdfs: List[Path], count: int) -> List[Path]:
    """A sample spread over the corpus that also includes the big files.

    Folder listings are alphabetical, so "the first 200 files" can easily be
    200 two-page diodes — and the estimate would be off by an order of
    magnitude. Take the worst decile by size plus an even spread of the rest.
    """
    if len(pdfs) <= count:
        return list(pdfs)
    by_size = sorted(pdfs, key=lambda p: p.stat().st_size, reverse=True)
    big = by_size[: max(1, count // 10)]
    big_set = set(big)
    rest = [p for p in pdfs if p not in big_set]
    step = max(1, len(rest) // (count - len(big)))
    return big + rest[::step][: count - len(big)]


def human(seconds: float) -> str:
    seconds = int(round(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "%dd %02d:%02d:%02d" % (days, hours, minutes, secs)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def total_ram_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1048576.0
    except OSError:
        pass
    return 0.0


def peak_child_rss_mb() -> float:
    """Highest RSS any worker reached so far, in MB (Linux: ru_maxrss is KB).

    This is a high-water mark for the whole run and never goes down, so the
    number for a later configuration includes the earlier ones. It is here to
    answer "does 32 workers fit in my RAM", not to compare configurations.
    """
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark the PDF parser on this machine")
    ap.add_argument("--corpus", type=Path, default=Path("data/datasheets"))
    ap.add_argument("--files", type=int, default=150,
                    help="how many PDFs to parse per configuration (default 150)")
    ap.add_argument("--jobs", default="",
                    help="worker counts, comma separated (default: around the CPU count)")
    ap.add_argument("--target", type=int, default=300000,
                    help="extrapolate to this many files (default 300000)")
    ap.add_argument("--parser", default="auto", choices=["auto", "pdfplumber", "docling"])
    args = ap.parse_args()

    corpus = args.corpus.expanduser()
    if not corpus.is_dir():
        print("No such folder: %s" % corpus)
        return 1

    pdfs = ingest.find_pdfs(corpus, recursive=True)
    if not pdfs:
        print("No PDFs under %s" % corpus)
        return 1

    cpus = os.cpu_count() or 1
    if args.jobs:
        configs = [int(j) for j in args.jobs.split(",") if j.strip()]
    else:
        configs = sorted({max(1, cpus // 2), cpus, cpus * 2})

    sample = pick_sample(pdfs, args.files)
    total_mb = sum(p.stat().st_size for p in sample) / 1e6
    biggest_mb = max(p.stat().st_size for p in sample) / 1e6
    ram = total_ram_gb()

    print(BAR)
    print("Machine:  %s, %d logical cores, %.0f GB RAM"
          % (platform.processor() or platform.machine(), cpus, ram))
    print("Corpus:   %s — %d PDFs" % (corpus, len(pdfs)))
    print("Sample:   %d PDFs, %.1f MB (includes the largest files)" % (len(sample), total_mb))
    print(BAR)

    parser_used = parsers.get_parser(args.parser)
    print("Parser:   %s" % parser_used.name)
    for reason in getattr(parser_used, "fallback_errors", []) or []:
        print("          (skipped %s)" % reason.split(":")[0])
    ingest._warmup(args.parser)          # parent process, before forking
    print(BAR)

    tmp = Path(tempfile.mkdtemp(prefix="smd-bench-"))
    rows = []
    try:
        # Cold caches make the first number a lie: warm up and throw it away.
        warm = ingest.IngestJob(tmp / "warm", parser=args.parser,
                                jobs=min(configs), shards=False)
        warm.run(sample[: min(5, len(sample))])
        print("warmed up\n")

        for jobs in configs:
            out = tmp / ("j%d" % jobs)
            job = ingest.IngestJob(out, parser=args.parser, jobs=jobs, shards=False)
            started = time.time()
            st = job.run(sample)
            elapsed = max(time.time() - started, 1e-6)
            done = max(st.done, 1)
            rate = done / elapsed
            rows.append({"jobs": jobs, "seconds": elapsed, "rate": rate,
                         "mb_s": total_mb / elapsed, "ok": st.ok,
                         "failed": st.failed, "rss": peak_child_rss_mb(),
                         "hours": args.target / rate if rate else 0})
            print("%4d workers: %6.2f s · %6.2f PDF/s · %5.1f MB/s · "
                  "RSS peak %6.0f MB · ok=%d failed=%d"
                  % (jobs, elapsed, rate, total_mb / elapsed,
                     peak_child_rss_mb(), st.ok, st.failed))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not rows:
        return 1

    best = max(rows, key=lambda r: r["rate"])
    single = best["rate"] / best["jobs"]
    print(BAR)
    print("Best rate:  %d workers, %.2f PDF/s (%.2f PDF/s per worker)"
          % (best["jobs"], best["rate"], single))
    print(BAR)
    print("Extrapolated to %s PDFs:" % "{:,}".format(args.target).replace(",", " "))
    print("   %s   with %d workers" % (human(best["hours"]), best["jobs"]))
    print("   %s   with 1 worker, for reference" % human(args.target / single))
    print(BAR)
    print("Memory: the highest RSS any worker reached was %.0f MB "
          "(high-water mark for the whole run)." % best["rss"])
    if ram:
        headroom = ram - best["rss"] / 1024.0
        print("You have %.0f GB, so this leaves about %.0f GB free%s."
              % (ram, max(headroom, 0),
                 "" if headroom > 8 else " — close, keep an eye on it"))
    print("Largest file in the sample is %.1f MB; every worker holds one PDF,"
          % biggest_mb)
    print("so real datasheets need more RAM than this sample suggests.")
    print(BAR)
    print("Run it like this:")
    print("   python3 tools/rag/build_cards.py --corpus %s --jobs %d --no-shards"
          % (corpus, best["jobs"]))
    print("   python3 tools/rag/serve.py --port 8000 --jobs %d"
          % max(2, min(best["jobs"], 8)))
    print()
    print("Stopping is free: a rerun resumes by SHA1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
