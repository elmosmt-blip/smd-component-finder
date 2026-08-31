"""Background ingestion: a folder of PDFs -> cards in SQLite.

This is the engine behind both the CLI (build_cards.py) and the web UI. It
exists because ingesting a real library is a long job that someone will want
to watch: the run reports how far it got, how fast it is going, what failed
and what it is chewing on right now.

    from tools.rag.ingest import JobRunner
    runner = JobRunner(Path("data/cards"))
    job_id = runner.start(paths, jobs=8)
    runner.status(job_id)   # -> {"state": "running", "done": 42, ...}

Two properties matter more than raw speed:

  * **Resumable.** A file whose SHA1 is already in the database is skipped, so
    an interrupted run costs only what it had in flight.
  * **Unkillable by one bad file.** Failures are recorded per file and the run
    continues; a corrupt PDF among 300 000 must not end the night.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.rag import card_store, extract, metadata, parsers

# ---------------------------------------------------------------------------
# worker (module level: it has to be picklable for the process pool)
# ---------------------------------------------------------------------------

_PARSER: Optional[Any] = None


def _make_parser(resolved: str) -> Any:
    """Build the parser from an ALREADY RESOLVED name.

    No is_available() / import attempt here: a child process inherits the
    import lock from its parent, and if another thread happened to hold it
    at fork time the child would deadlock on its first import. Everything
    the worker needs is warmed up before the fork instead (see _warmup).
    """
    return (parsers.DoclingParser() if resolved == "docling"
            else parsers.PdfPlumberParser())


def _worker_init(resolved: str) -> None:
    global _PARSER
    _PARSER = _make_parser(resolved)


def _warmup(parser_name: str) -> str:
    """Resolve the parser and import everything the children will need.

    Must run in the parent process, before the pool is forked: after this
    the child finds pdfplumber and friends already in sys.modules and never
    touches the import lock.
    """
    parser = parsers.get_parser(parser_name)     # may import docling -> fallback
    parser.is_available()                        # imports pdfplumber here
    return parser.name


def process_one(path_str: str) -> Dict[str, Any]:
    """Parse and extract one PDF. Never raises — the caller records the error."""
    path = Path(path_str)
    started = time.time()
    try:
        parsed = _PARSER.parse(path)
        meta = metadata.enrich(parsed)
        card = extract.build_card(parsed, meta).to_dict()
        return {
            "ok": True,
            "card": card,
            "filename": path.name,
            "sha1": parsed.sha1,
            "seconds": round(time.time() - started, 3),
        }
    except Exception as exc:                       # noqa: BLE001 - report, never crash
        return {
            "ok": False,
            "filename": path.name,
            "sha1": "",
            "error": "%s: %s" % (type(exc).__name__, exc),
            "seconds": round(time.time() - started, 3),
        }


# ---------------------------------------------------------------------------
# job state
# ---------------------------------------------------------------------------

@dataclass
class JobStatus:
    id: str
    state: str = "pending"            # pending | running | done | cancelled | error
    total: int = 0
    done: int = 0
    ok: int = 0
    unchanged: int = 0      # parsed fine, but the stored card was already as good
    failed: int = 0
    skipped: int = 0        # SHA1 already indexed — not even re-parsed
    current: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    errors: List[dict] = field(default_factory=list)
    message: str = ""
    traceback: str = ""

    def snapshot(self) -> dict:
        d = asdict(self)
        elapsed = (self.finished_at or time.time()) - (self.started_at or time.time())
        d["elapsed"] = round(max(elapsed, 0.0), 1)
        d["rate"] = round(self.done / elapsed, 2) if elapsed > 0.5 else 0.0
        d["eta"] = round((self.total - self.done) / d["rate"], 1) if d["rate"] > 0 else 0
        d["errors"] = self.errors[:25]
        return d


class IngestJob:
    """Parses a list of PDFs into a CardStore. Blocks until finished."""

    def __init__(self, out_dir: Path, parser: str = "auto", jobs: int = 0,
                 shards: bool = True, job_id: Optional[str] = None,
                 store: Optional[Any] = None):
        self.out_dir = Path(out_dir)
        self.parser_name = parser
        self.jobs = jobs or (os.cpu_count() or 1)
        self.shards = shards
        # A store handed in from outside (the web server's own connection) is
        # used as is and never closed: sharing one connection means the site
        # sees every card the moment it is written.
        self.external_store = store
        self.status = JobStatus(id=job_id or uuid.uuid4().hex[:10])
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ run

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, paths: List[Path]) -> JobStatus:
        st = self.status
        st.state = "running"
        st.started_at = time.time()
        st.total = len(paths)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.out_dir / "cards.db"
        shared = self.external_store is not None
        store = self.external_store or card_store.CardStore(db_path)

        try:
            known = store.known_sha1()
            todo: List[Tuple[Path, str]] = []
            for p in paths:
                try:
                    sha = parsers._sha1(p)
                except Exception:                   # noqa: BLE001
                    sha = ""
                if sha and sha in known:
                    st.skipped += 1
                    continue
                todo.append((p, sha))
            st.total = len(todo)
            if not todo:
                st.state = "done"
                st.message = "Nothing new: every file is already in the database"
                if self.shards:
                    store.dump_shards(self.out_dir / "site")
            else:
                from concurrent.futures import ProcessPoolExecutor, as_completed

                with ProcessPoolExecutor(max_workers=self.jobs, initializer=_worker_init,
                                         initargs=(self.parser_name,)) as pool:
                    futures = {pool.submit(process_one, str(p)): p for p, _ in todo}
                    for fut in as_completed(futures):
                        if self._cancel.is_set():
                            for f in futures:
                                f.cancel()
                            st.state = "cancelled"
                            st.message = "Cancelled — restart with the same command to resume"
                            break
                        result = fut.result()
                        st.done += 1
                        st.current = result.get("filename", "")
                        if result["ok"]:
                            if store.upsert(result["card"]):
                                st.ok += 1
                            else:
                                st.unchanged += 1
                        else:
                            st.failed += 1
                            store.add_failure(result["filename"], result.get("sha1", ""),
                                              result["error"])
                            st.errors.append({"file": result["filename"],
                                              "error": result["error"][:200]})
                        if st.done % 25 == 0:
                            store.commit()
        except Exception as exc:                    # noqa: BLE001 - keep the job alive
            st.state = "error"
            st.message = "%s: %s" % (type(exc).__name__, exc)
            # Where it happened is worth more than the message: a long run
            # fails in places nobody can guess from "closed database".
            traceback.print_exc(file=sys.stderr)
            st.traceback = traceback.format_exc()[-1200:]
        finally:
            if st.state == "running":
                st.state = "done"
            store.set_meta("parser", self.parser_name)
            store.set_meta("last_run", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            store.commit()
            stats = store.stats()
            if self.shards and st.ok:
                store.dump_shards(self.out_dir / "site")
            if not shared:
                store.close()
            st.finished_at = time.time()
            st.message = st.message or ("%d cards, %d failed" % (stats["cards"], st.failed))
        return st


class JobRunner:
    """Runs IngestJobs in background threads and remembers their status."""

    def __init__(self, out_dir: Path, parser: str = "auto", jobs: int = 0,
                 store: Optional[Any] = None):
        self.out_dir = Path(out_dir)
        self.parser = parser
        self.jobs = jobs
        self.store = store
        self._jobs: Dict[str, IngestJob] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, paths: List[Path], shards: bool = True) -> str:
        resolved = _warmup(self.parser)          # parent process, before forking
        job = IngestJob(self.out_dir, parser=resolved, jobs=self.jobs,
                        shards=shards, store=self.store)
        with self._lock:
            self._jobs[job.status.id] = job

        def target() -> None:
            try:
                job.run(list(paths))
            except Exception as exc:                 # noqa: BLE001 - last resort
                job.status.state = "error"
                job.status.message = "%s: %s" % (type(exc).__name__, exc)

        t = threading.Thread(target=target, daemon=True,
                             name="ingest-%s" % job.status.id)
        self._threads[job.status.id] = t
        t.start()
        return job.status.id

    def status(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        if not job:
            return None
        return job.status.snapshot()

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancel()
        return True

    def last(self) -> Optional[str]:
        return next(reversed(self._jobs), None) if self._jobs else None


def find_pdfs(folder: Path, recursive: bool = True) -> List[Path]:
    """Every .pdf under `folder`, sorted so runs are reproducible."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError("%s is not a directory" % folder)
    pattern = folder.rglob if recursive else folder.glob
    return sorted(p for p in pattern("*.pdf") if p.is_file())
