#!/usr/bin/env python3
"""Orchestrator: PDF corpus -> parsed markdown -> enriched chunks -> index.

    python3 tools/rag/pipeline.py --corpus data/datasheets --rebuild
    python3 tools/rag/pipeline.py --embed sentence-transformers
    python3 tools/rag/pipeline.py --query "collector current" --part MMBT3904

Stages:
    1. PARSE    Docling (IBM) if it can run, otherwise pdfplumber.
    2. ENRICH   Part number / manufacturer / package from the first pages.
    3. CHUNK    By markdown section, tables kept whole (LlamaIndex
                MarkdownElementNodeParser), deterministic table summaries.
    4. INDEX    SQLite FTS5 (BM25) + optional dense vectors.
"""

from __future__ import annotations

import os

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import chunking, cli, index_db, metadata, opensearch_index, parsers
from tools.rag.embeddings import get_backend

ROOT = Path(__file__).resolve().parent.parent.parent
# Same rule as serve.py and doctor.py: SMD_DATA_DIR moves the whole data
# directory. Without it the index would land in ./data while the site and the
# diagnostics look somewhere else — and doctor.py would report "index not
# built" for an index that exists.
DATA_DIR = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data"))
DEFAULT_CORPUS = DATA_DIR / "datasheets"
DEFAULT_OUT = DATA_DIR / "rag"


# --------------------------------------------------------------------------- #
# workers (module level: they have to be picklable)
#
# Parsing and chunking is CPU-bound and independent per file, so it scales with
# cores. Writing to the index does not: SQLite takes one writer, so every
# `index.add_document` stays in the parent process and only the heavy part runs
# in the pool. Sequential measured 0.5 PDF/s, which put the remaining 20k files
# of a real corpus at half a day.
# --------------------------------------------------------------------------- #

_WORKER: Dict[str, Any] = {}


def _worker_init(parser_name: str, out_dir: str, docling_pages: str = "",
                 quiet: bool = True) -> None:
    """Per-process setup. `quiet` is False only in the sequential path, where
    the "worker" is the parent process and silencing it would silence the
    whole program."""
    opts = {"pages": docling_pages} if docling_pages else {}
    _WORKER["parser"] = parsers.get_parser(parser_name, **opts)
    _WORKER["splitter"] = chunking.ElementSplitter()
    _WORKER["out"] = Path(out_dir)
    # A worker has no business writing to the console. Docling alone emits
    # "MatchingPostProcessor WARNING Orphan pdf_cell" thousands of times per
    # thousand pages; on a Windows console the writer blocks long before the
    # reader catches up, the parent sits in map() waiting for results that are
    # queued behind a full pipe, and the run looks hung while burning nothing.
    # Point the standard streams at the void (SMD_WORKER_LOG=<file> keeps them
    # if you ever need to debug a parser) and tell Docling's loggers to shh.
    parsers.quiet_docling()
    if not quiet:
        return
    log_path = os.environ.get("SMD_WORKER_LOG", "")
    if log_path:
        try:
            fh = open("%s.%d.log" % (log_path, os.getpid()), "a", encoding="utf-8")
            sys.stdout = fh
            sys.stderr = fh
            return
        except OSError:
            pass
    _silence_streams()


def _silence_streams() -> None:
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = devnull
        sys.stderr = devnull
    except OSError:                                # noqa: BLE001 - best effort
        pass


def _worker_one(pdf_str: str) -> Dict[str, Any]:
    """Parse, enrich, chunk and cache one PDF. Never raises."""
    pdf = Path(pdf_str)
    out: Path = _WORKER["out"]
    try:
        try:
            parsed = _WORKER["parser"].parse(pdf)
            used_fallback = False
        except Exception:                          # noqa: BLE001 - see below
            # Docling that cannot read one file (a corrupt page, an OCR engine
            # that is not installed) must not cost us the document. pdfplumber
            # gets a second look, and only if that fails do we skip the file.
            if getattr(_WORKER["parser"], "name", "") != "docling":
                raise
            parsed = parsers.get_parser("pdfplumber").parse(pdf)
            used_fallback = True
        meta = metadata.enrich(parsed)
        chunks = chunking.chunk_document(parsed, meta, splitter=_WORKER["splitter"])
        chunk_dicts = [c.to_dict() for c in chunks]

        # keep the markdown so the pipeline can be debugged without re-parsing
        (out / "parsed" / (parsed.doc_id + ".md")).write_text(
            parsed.markdown, encoding="utf-8")
        (out / "parsed" / (parsed.doc_id + ".meta.json")).write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        doc = {
            "doc_id": parsed.doc_id,
            "filename": parsed.filename,
            "sha1": parsed.sha1,
            "n_pages": parsed.n_pages,
            "parser": parsed.parser,
            "part": meta.part,
            "manufacturer": meta.manufacturer,
            "package": meta.package,
            "family": meta.family,
            "conf": meta.confidence,
        }
        # The expensive part of this file is now on disk. A rerun (a crash, a
        # bigger --jobs, a new embedding backend) loads this instead of
        # parsing again: seconds instead of a night.
        try:
            st = pdf.stat()
            payload = {"doc_id": parsed.doc_id, "sha1": parsed.sha1,
                       "size": st.st_size, "mtime": st.st_mtime,
                       "doc": doc, "chunks": chunk_dicts}
            (out / "parsed" / (parsed.doc_id + ".chunks.json")).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:                            # noqa: BLE001 - cache is a bonus
            pass

        return {"ok": True, "doc": doc, "chunks": chunk_dicts,
                "n_chunks": len(chunks), "fallback": used_fallback}
    except Exception as exc:                       # noqa: BLE001 - one bad PDF is data
        return {"ok": False, "filename": pdf.name,
                "error": "%s: %s" % (type(exc).__name__, exc)}


def _cache_path(out: Path, doc_id: str) -> Path:
    return out / "parsed" / (doc_id + ".chunks.json")


def load_cache(out: Path, pdf: Path, doc_id: str = "") -> Optional[Dict[str, Any]]:
    """A finished parse of exactly this file, if we still have it.

    Matched on size + mtime rather than SHA1: reading 20 000 PDFs to hash
    them would cost a minute that the mtime check gives away for free, and a
    hardlink (how the corpus folders are built) keeps both.
    """
    path = _cache_path(out, doc_id or parsers.doc_id_for(pdf))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001 - a miss is a miss
        return None
    if not isinstance(data, dict) or not data.get("chunks"):
        return None
    try:
        st = pdf.stat()
    except OSError:
        return None
    if data.get("size") != st.st_size:
        return None
    if abs(float(data.get("mtime", 0)) - st.st_mtime) > 1:
        return None
    return data


def _indexed_docs(index: Any) -> set:
    getter = getattr(index, "doc_ids", None)
    if not callable(getter):
        return set()                               # backend cannot tell us
    try:
        return set(getter())
    except Exception:                              # noqa: BLE001
        return set()


def check_index(index: Any, repair: bool = False) -> str:
    """Is the FTS index consistent? Rebuild it from the chunks if it is not."""
    fn = getattr(index, "integrity", None)
    if not callable(fn):
        return "не проверяется этим бэкендом"
    ok, msg = fn()
    if ok:
        return "ok"
    if not repair or not hasattr(index, "repair"):
        return "ПОВРЕЖДЁН: %s (запустите с --repair)" % msg
    try:
        n = index.repair()
    except Exception as exc:                       # noqa: BLE001
        return "ПОВРЕЖДЁН: %s; восстановление не удалось (%s)" % (msg, exc)
    ok2, msg2 = index.integrity()
    return ("ПОВРЕЖДЁН: %s — пересобран из %d чанков, теперь %s"
            % (msg, n, "ok" if ok2 else msg2))


def build(corpus: Path, out: Path, parser_name: str = "auto", embed: str = "none",
          rebuild: bool = False, limit: int = 0, verbose: bool = False,
          backend: str = "auto", docling_pages: Optional[str] = None,
          jobs: int = 0, reuse: bool = True, resume: bool = True) -> dict:
    """Parse a corpus into the index.

    Three things make this survive a corpus the size of a real library:

      * **resume** — a document already in the index is not parsed again, so
        restarting after a crash costs only what was not finished;
      * **reuse**  — `parsed/<doc>.chunks.json` holds the finished chunks, so
        even a wiped index does not mean re-parsing (the night Docling spends
        on 3 500 files is not spent twice);
      * **isolation** — workers write nothing to the console, so the log flood
        that quietly stops a Windows run cannot happen.
    """
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "index.db"
    (out / "parsed").mkdir(exist_ok=True)

    embedder = get_backend(embed)
    index = opensearch_index.open_index(db_path, embedding_backend=embedder,
                                         prefer=backend, verbose=verbose)
    if rebuild:
        index.reset()

    docling_opts = {"pages": docling_pages} if docling_pages else {}
    parser = parsers.get_parser(parser_name, **docling_opts)
    print("Parser:      %s" % parser.name)
    for reason in getattr(parser, "fallback_errors", []) or []:
        print("             (skipped %s)" % reason.split(":")[0] +
              " — " + reason.split("Original error:")[-1].strip()[:90])
    print("Embeddings:  %s" % embedder.describe())

    pdfs = sorted(corpus.glob("*.pdf"))
    if not pdfs:
        print("\nNo PDFs in %s" % corpus)
        print("Drop datasheets there, or generate a demo corpus:")
        print("  python3 tools/rag/sample_datasheets.py")
        return index.stats()
    if limit:
        pdfs = pdfs[:limit]

    # Built once here and once per worker: it is cheap, and the workers need
    # their own copy (a LlamaIndex parser is not picklable across processes).
    chunker_name = chunking.ElementSplitter().backend
    print("Chunker:     %s" % chunker_name)
    jobs = max(1, int(jobs or os.cpu_count() or 1)) if len(pdfs) > 1 else 1
    print("Documents:   %d   workers: %d\n" % (len(pdfs), jobs))

    t0 = time.time()
    ok = skipped = total_chunks = 0
    fallbacks = 0
    fallback_used = False

    def store(result: Dict[str, Any], i: int) -> None:
        """Write one finished document into the index. Parent process only."""
        nonlocal ok, skipped, total_chunks, fallbacks
        if not result["ok"]:
            print("  x %s: %s" % (result["filename"], result["error"]), flush=True)
            skipped += 1
            return
        doc = result["doc"]
        index.add_document(doc, result["chunks"])
        total_chunks += result["n_chunks"]
        ok += 1
        if result.get("fallback"):
            fallbacks += 1
        if verbose:
            print(
                "  [%2d/%2d] %-34s part=%-14s mfr=%-22s pkg=%-10s pages=%-3d "
                "chunks=%-3d conf=%.2f"
                % (i, len(pdfs), doc["filename"][:34], doc["part"] or "-",
                   (doc["manufacturer"] or "-")[:22], doc["package"] or "-",
                   doc["n_pages"], result["n_chunks"], doc["conf"]), flush=True)
        elif i % 25 == 0 or i == total_work:
            # One line per 25 files, not per file: 3 500 lines of Cyrillic
            # into a Windows console is a measurable part of the runtime.
            print("  %5d/%d  %.1f файл/с  прошло %s"
                  % (i, total_work, i / max(1e-9, time.time() - t0),
                     _fmt_time(time.time() - t0)), flush=True)

    # ------------------------------------------------ what actually needs work
    indexed = _indexed_docs(index) if (resume and not rebuild) else set()
    todo: List[Path] = []
    from_cache: List[Dict[str, Any]] = []
    already = 0
    for pdf in pdfs:
        doc_id = parsers.doc_id_for(pdf)
        if doc_id in indexed:
            already += 1
            continue
        data = load_cache(out, pdf, doc_id) if reuse else None
        if data is not None:
            from_cache.append(data)
        else:
            todo.append(pdf)

    total_work = len(from_cache) + len(todo)
    if already or from_cache:
        print("Уже в индексе: %d   из кэша parsed/: %d   к разбору: %d"
              % (already, len(from_cache), len(todo)), flush=True)

    # 1) everything we already parsed: no worker, no Docling, seconds
    for i, data in enumerate(from_cache, 1):
        store({"ok": True, "doc": data["doc"], "chunks": data["chunks"],
               "n_chunks": len(data["chunks"])}, i)

    # 2) the rest, through the pool (or in-process at jobs=1)
    if todo:
        try:
            # Docling downloads its models on the first parse. If that fails,
            # it fails in every worker, so find out here — once — instead of
            # twenty times, and fall back the way the sequential path did.
            parser.parse(todo[0])
        except Exception as exc:                   # noqa: BLE001 - any parse error
            if parser.name == "docling":
                print("  ! Docling не смог разобрать первый файл (%s) — "
                      "работаю pdfplumber" % type(exc).__name__, flush=True)
                parser = parsers.get_parser("pdfplumber")
                fallback_used = True
            else:
                raise

        if jobs > 1 and len(todo) > 1:
            try:
                with ProcessPoolExecutor(
                        max_workers=min(jobs, len(todo)),
                        initializer=_worker_init,
                        initargs=(parser.name, str(out), docling_pages or "", True)) as pool:
                    for i, result in enumerate(
                            pool.map(_worker_one, [str(p) for p in todo],
                                     chunksize=1), len(from_cache) + 1):
                        store(result, i)
            except BrokenProcessPool as exc:
                # A worker died (OOM on a fat datasheet is the usual one).
                # Nothing is lost: every finished file left its cache behind,
                # so re-running picks up where this stopped.
                print("\n  !! воркер умер: %s" % exc, flush=True)
                print("  !! проиндексировано %d, повторите команду — "
                      "сделанные файлы возьмутся из кэша" % ok, flush=True)
        else:
            _worker_init(parser.name, str(out), docling_pages or "", quiet=False)
            for i, pdf in enumerate(todo, len(from_cache) + 1):
                store(_worker_one(str(pdf)), i)

    built_vectors = index.build_vectors() if embedder.name != "none" else 0
    index.set_meta("parser", (parser.name + (" (fallback)" if fallback_used else "")))
    index.set_meta("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    index.set_meta("chunker", chunker_name)

    verdict = check_index(index, repair=True)
    stats = index.stats()
    # The printed line always had `skipped`; callers could not see it.
    stats["indexed"] = ok
    stats["skipped"] = skipped
    stats["from_cache"] = len(from_cache)
    stats["already_indexed"] = already
    stats["fallbacks"] = fallbacks
    print(
        "\nDone in %.1fs: %d indexed (%d from cache, %d already in index), "
        "%d skipped, %d chunks (%d tables), %d vectors"
        % (time.time() - t0, ok, len(from_cache), already, skipped, total_chunks,
           stats["tables"], built_vectors), flush=True)
    if fallbacks:
        print("Docling не справился с %d файлами — они разобраны pdfplumber"
              % fallbacks, flush=True)
    print("Индекс FTS: %s" % verdict, flush=True)
    index.close()
    return stats


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return "%dh %02dm %02ds" % (h, m, sec) if h else "%dm %02ds" % (m, sec)


def query(out: Path, q: str, part: str | None = None, section: str | None = None,
          k: int = 6, embed: str = "none") -> None:
    index = opensearch_index.open_index(out / "index.db",
                                         embedding_backend=get_backend(embed))
    if not index.stats()["chunks"]:
        print("Index is empty — run the pipeline first.")
        return
    results = index.search(q, part=part, section=section, k=k)
    print('Query: "%s"%s%s\n' % (q, " part=%s" % part if part else "",
                                 " section=%s" % section if section else ""))
    if not results:
        print("  nothing found")
        return
    for i, r in enumerate(results, start=1):
        head = "  %d. [%s] %s" % (i, r["section_label"], r["part"] or r["filename"])
        if r["manufacturer"]:
            head += " — %s" % r["manufacturer"]
        if r["page"]:
            head += " (p.%d)" % r["page"]
        print(head)
        snippet = (r["snippet"] or r["summary"] or r["text"]).replace("\n", " ")
        print("     %s" % snippet[:220])
        print()


def main() -> int:
    cli.fix_windows_console()
    ap = argparse.ArgumentParser(description="Build and query the datasheet RAG index")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--docling-pages", default=None, choices=["tables", "all"],
                    help="Docling only on pages with tables (default) or on every page")
    ap.add_argument("--backend", default=None, choices=["auto", "opensearch", "sqlite"],
                    help="index engine: auto uses OpenSearch when it answers")
    ap.add_argument("--parser", default="auto", choices=["auto", "docling", "pdfplumber"])
    ap.add_argument("--embed", default="none",
                    choices=["none", "auto", "sentence-transformers", "st", "openai"])
    ap.add_argument("--rebuild", action="store_true", help="drop the existing index first")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N PDFs")
    ap.add_argument("--jobs", type=int, default=0,
                    help="parallel workers for parsing and chunking "
                         "(default: number of cores). The index write stays "
                         "in one process either way")
    ap.add_argument("--from-cards", type=Path, metavar="CARDS.DB",
                    help="индексировать не PDF, а готовые карточки: в 100 раз "
                         "меньше индекс и никакого Docling "
                         "(см. tools/rag/index_cards.py)")
    ap.add_argument("--enrich", type=Path, metavar="ATTRS.JSONL",
                    help="сначала добавить карточкам атрибуты дистрибьютора "
                         "(element14.py --attributes-out), потом индексировать. "
                         "Пустые поля заполняются, остальное идёт в отдельный "
                         "блок extra_specs, confidence не растёт")
    ap.add_argument("--no-reuse-parsed", action="store_true",
                    help="ignore parsed/*.chunks.json and parse everything again")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-parse files that are already in the index")
    ap.add_argument("--check", action="store_true",
                    help="only check the index for FTS corruption")
    ap.add_argument("--repair", action="store_true",
                    help="rebuild a broken FTS index from the chunks table "
                         "(no re-parsing, minutes instead of a night)")
    ap.add_argument("--query", help="run a search instead of building")
    ap.add_argument("--part", help="restrict the search to one part number")
    ap.add_argument("--section", help="restrict the search to one canonical section")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.query:
        query(args.out, args.query, args.part, args.section, args.k, args.embed)
        return 0
    if args.stats:
        idx = opensearch_index.open_index(args.out / "index.db")
        print(json.dumps(idx.stats(), indent=2, ensure_ascii=False))
        return 0
    if args.from_cards or args.enrich:
        from tools.rag import enrich_cards, index_cards
        cards_db = args.from_cards
        if args.enrich:
            cards_db = cards_db or Path(os.environ.get("SMD_CARDS_DB")
                                        or (args.out.parent / "cards" / "cards.db"))
            enrich_cards.enrich(cards_db, args.enrich, limit=args.limit,
                                verbose=args.verbose)
        index_cards.build(cards_db, args.out, rebuild=args.rebuild,
                          limit=0 if args.enrich else args.limit,
                          embed=args.embed,
                          backend=args.backend or "auto", verbose=args.verbose)
        return 0
    if args.check or args.repair:
        idx = opensearch_index.open_index(args.out / "index.db")
        print(json.dumps(idx.stats(), indent=2, ensure_ascii=False))
        print("Индекс FTS: %s" % check_index(idx, repair=args.repair))
        idx.close()
        return 0
    build(args.corpus, args.out, args.parser, args.embed, args.rebuild, args.limit,
          args.verbose, backend=args.backend or "auto",
          docling_pages=args.docling_pages, jobs=args.jobs,
          reuse=not args.no_reuse_parsed, resume=not args.no_resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
