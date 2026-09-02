#!/usr/bin/env python3
"""End-to-end test of the web ingestion path.

Starts the real server (tools/rag/serve.py) on a free port against a throwaway
data directory and drives it exactly like the browser does:

    POST /api/ingest/path      → job id
    GET  /api/ingest/status    → poll until it stops
    POST /api/ingest/upload    → multipart, as the folder picker sends it
    POST /api/ingest/cancel    → stop a long run
    GET  /api/cards            → the cards really landed in SQLite

    python3 tools/test_ingest.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rag import sample_datasheets  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[  ok  ]  %s" % name)
    else:
        FAIL += 1
        print("[ FAIL ]  %s%s" % (name, (" — " + detail) if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict) -> tuple:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post_files(self, path: str, files: list) -> tuple:
        boundary = "----smdtestboundary"
        body = b""
        for file in files:
            body += (
                '--%s\r\nContent-Disposition: form-data; name="files[]"; '
                'filename="%s"\r\nContent-Type: application/pdf\r\n\r\n'
                % (boundary, file.name)
            ).encode() + file.read_bytes() + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


def wait_for_job(client: Client, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/ingest/status?id=%s" % job_id)
        if st.get("state") not in ("running", "pending"):
            return st
        time.sleep(0.3)
    return client.get("/api/ingest/status?id=%s" % job_id)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smd-ingest-"))
    data_dir = tmp / "data"
    corpus = tmp / "folder"
    (data_dir / "datasheets").mkdir(parents=True)
    corpus.mkdir(parents=True)

    port = free_port()
    # Start from the real environment, not from a hand-made one: on Windows a
    # stripped env loses SystemRoot, and Winsock then cannot start in the
    # child — every socket() fails with WSAStartup / WinError 10106.
    env = dict(os.environ)
    env["SMD_DATA_DIR"] = str(data_dir)
    env["PYTHONUNBUFFERED"] = "1"
    log_path = tmp / "server.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u",
         str(Path(__file__).resolve().parent / "rag" / "serve.py"),
         "--port", str(port), "--jobs", "2"],
        stdout=log_fh, stderr=subprocess.STDOUT, env=env, text=True)

    client = Client("http://127.0.0.1:%d" % port)
    try:
        print("--- запуск сервера ---")
        up = False
        for _ in range(60):
            try:
                client.get("/api/health")
                up = True
                break
            except Exception:                       # noqa: BLE001
                time.sleep(0.3)
        check("сервер поднялся", up)
        if not up:
            print(proc.stdout.read()[:800] if proc.stdout else "")
            return 1

        generated = [sample_datasheets.build_pdf(spec, corpus)
                     for spec in sample_datasheets.DATASHEETS]
        (corpus / "broken.pdf").write_bytes(b"definitely not a pdf")
        check("корпус подготовлен", len(generated) == 6)

        print("\n--- POST /api/ingest/path ---")
        status, body = client.post_json(
            "/api/ingest/path", {"path": str(corpus), "recursive": True})
        check("задача запущена", status == 200 and "job_id" in body, str(body))
        check("в ответе число файлов", body.get("files") == 7, str(body.get("files")))
        st = wait_for_job(client, body["job_id"])
        check("задача завершилась", st["state"] == "done", st["state"])
        check("извлечено 6 карточек", st["ok"] == 6, str(st))
        check("битый файл записан в ошибки", st["failed"] == 1, str(st))
        check("ошибка содержит имя файла",
              any(e["file"] == "broken.pdf" for e in st.get("errors", [])),
              str(st.get("errors")))

        print("\n--- повторный прогон (докачка) ---")
        status, body2 = client.post_json(
            "/api/ingest/path", {"path": str(corpus), "recursive": True})
        st2 = wait_for_job(client, body2["job_id"])
        check("повторно ничего не пересчитывается",
              st2["ok"] == 0 and st2["skipped"] >= 6, str(st2))
        check("битый файл не залипает", st2["failed"] <= 1, str(st2))

        print("\n--- POST /api/ingest/upload (multipart) ---")
        upload = [corpus / "MMBT3904_datasheet.pdf"]
        status, body3 = client.post_files("/api/ingest/upload", upload)
        check("файлы приняты", status == 200 and body3.get("files") == 1, str(body3))
        st3 = wait_for_job(client, body3["job_id"])
        check("загруженный файл разобран",
              st3["state"] == "done" and (st3["ok"] + st3["unchanged"]) == 1, str(st3))
        check("загрузка сохранена на диск",
              any((data_dir / "datasheets" / "uploaded").rglob("*.pdf")),
              str(list((data_dir / "datasheets" / "uploaded").rglob("*.pdf"))[:2]))

        print("\n--- карточки в базе и API ---")
        cards = client.get("/api/cards?q=&limit=20")
        check("карточки видны через /api/cards", cards["total"] == 6, str(cards["total"]))
        found = client.get("/api/cards?q=MMBT3904")
        check("поиск находит карточку", found["total"] == 1, str(found["total"]))
        full = client.get("/api/card?part=MMBT3904")
        check("полная карточка отдаётся", full.get("pin_count") == 3, str(full.get("pin_count")))
        stats = client.get("/api/cards/stats")
        check("статистика считает карточки", stats["cards"] == 6, str(stats))
        check("сбои попадают в статистику", stats["failures"] == 1, str(stats["failures"]))

        print("\n--- отмена ---")
        big = tmp / "big"
        big.mkdir()
        for i in range(40):
            shutil.copy(corpus / "MMBT3904_datasheet.pdf", big / ("copy_%d.pdf" % i))
        status, body4 = client.post_json("/api/ingest/path", {"path": str(big)})
        client.post_json("/api/ingest/cancel", {"id": body4["job_id"]})
        st4 = wait_for_job(client, body4["job_id"], timeout=60)
        check("задача остановлена",
              st4["state"] in ("cancelled", "done"), st4["state"])
        check("отмена не уронила сервер",
              client.get("/api/health").get("ok") is True)

        print("\n--- ошибки ввода ---")
        status, body5 = client.post_json("/api/ingest/path", {"path": "/no/such/folder"})
        check("несуществующий путь — 400", status == 400 and "error" in body5, str(body5))
        status, body6 = client.post_json("/api/ingest/path", {"path": ""})
        check("пустой путь — 400", status == 400, str(body6))
        status, body7 = client.post_files("/api/ingest/upload", [])
        check("пустая загрузка — 400", status == 400, str(body7))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            log_fh.close()
            out = log_path.read_text(errors="replace")
        except Exception:                           # noqa: BLE001
            out = ""
        if FAIL and out:
            print("\n=== журнал сервера ===")
            print(out[-2500:])
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n--- итог ---")
    print("%d пройдено, %d провалено" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
