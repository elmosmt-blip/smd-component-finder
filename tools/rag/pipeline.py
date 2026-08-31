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

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.rag import chunking, index_db, metadata, opensearch_index, parsers
from tools.rag.embeddings import get_backend

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CORPUS = ROOT / "data" / "datasheets"
DEFAULT_OUT = ROOT / "data" / "rag"


def build(corpus: Path, out: Path, parser_name: str = "auto", embed: str = "none",
          rebuild: bool = False, limit: int = 0, verbose: bool = False,
          backend: str = "auto", docling_pages: Optional[str] = None) -> dict:
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

    splitter = chunking.ElementSplitter()
    print("Chunker:     %s" % splitter.backend)
    print("Documents:   %d\n" % len(pdfs))

    t0 = time.time()
    fallback_used = False
    ok = skipped = 0
    total_chunks = 0

    for i, pdf in enumerate(pdfs, start=1):
        try:
            parsed = parser.parse(pdf)
        except parsers.ParserUnavailable as exc:
            if parser.name == "docling":
                print("  ! Docling unavailable (%s) — falling back to pdfplumber" % exc)
                parser = parsers.get_parser("pdfplumber")
                fallback_used = True
                try:
                    parsed = parser.parse(pdf)
                except Exception as exc2:
                    print("  x %s: %s" % (pdf.name, exc2))
                    skipped += 1
                    continue
            else:
                print("  x %s: %s" % (pdf.name, exc))
                skipped += 1
                continue
        except Exception as exc:
            print("  x %s: %s" % (pdf.name, exc))
            skipped += 1
            continue

        meta = metadata.enrich(parsed)
        chunks = chunking.chunk_document(parsed, meta, splitter=splitter)

        # keep the markdown so the pipeline can be debugged without re-parsing
        md_path = out / "parsed" / (parsed.doc_id + ".md")
        md_path.write_text(parsed.markdown, encoding="utf-8")
        (out / "parsed" / (parsed.doc_id + ".meta.json")).write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        index.add_document(
            {
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
            },
            [c.to_dict() for c in chunks],
        )

        total_chunks += len(chunks)
        ok += 1
        print(
            "  [%2d/%2d] %-34s part=%-14s mfr=%-22s pkg=%-10s pages=%-3d chunks=%-3d conf=%.2f"
            % (i, len(pdfs), parsed.filename[:34], meta.part or "-",
               (meta.manufacturer or "-")[:22], meta.package or "-",
               parsed.n_pages, len(chunks), meta.confidence)
        )

    built_vectors = index.build_vectors() if embedder.name != "none" else 0
    index.set_meta("parser", (parser.name + (" (fallback)" if fallback_used else "")))
    index.set_meta("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    index.set_meta("chunker", splitter.backend)

    stats = index.stats()
    print(
        "\nDone in %.1fs: %d indexed, %d skipped, %d chunks (%d tables), %d vectors"
        % (time.time() - t0, ok, skipped, total_chunks, stats["tables"], built_vectors)
    )
    index.close()
    return stats


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
    build(args.corpus, args.out, args.parser, args.embed, args.rebuild, args.limit,
          args.verbose, backend=args.backend or "auto",
          docling_pages=args.docling_pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
