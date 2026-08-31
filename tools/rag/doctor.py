#!/usr/bin/env python3
"""One command that tells you what is wrong — as text you can paste anywhere.

    python3 tools/rag/doctor.py
    python3 tools/rag/doctor.py > report.txt      # then paste report.txt

It prints the machine, the installed packages, whether OpenSearch answers,
what is in the databases, the last parsing failures with their messages, and
whether the site is up. Everything it prints is safe to share: no paths to
personal files beyond the project, no credentials.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rag import cli  # noqa: E402

DATA_DIR = Path(os.environ.get("SMD_DATA_DIR") or (ROOT / "data"))
CARDS = DATA_DIR / "cards" / "cards.db"
INDEX = DATA_DIR / "rag" / "index.db"

LINE = "-" * 70


def head(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def ok(bad: bool, text: str) -> None:
    print("  [%s] %s" % ("!!" if bad else "ok", text))


def run(cmd: list) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr or "").strip().splitlines()[0][:120]
    except Exception as exc:                      # noqa: BLE001
        return "не удалось: %s" % type(exc).__name__


def machine() -> None:
    head("Машина")
    distro = run(["lsb_release", "-ds"])
    print("  ОС:        %s" % (distro or platform.platform()))
    print("  Ядро:      %s" % platform.release())
    print("  CPU:       %s, %d логических ядер" % (platform.processor() or platform.machine(),
                                                   os.cpu_count() or 1))
    try:
        ram = [l for l in Path("/proc/meminfo").read_text().splitlines()
               if l.startswith("MemTotal:")]
        print("  Память:    %s" % ram[0].split(":", 1)[1].strip() if ram else "?")
    except OSError:
        pass
    total, used, free = shutil.disk_usage(str(ROOT))
    print("  Диск:      свободно %.1f ГБ из %.1f ГБ" % (free / 2**30, total / 2**30))
    print("  Python:    %s (%s)" % (sys.version.split()[0], sys.executable))


def packages() -> None:
    head("Пакеты")
    for mod, label in (("pdfplumber", "парсер текста и таблиц"),
                       ("llama_index", "чанкование (LlamaIndex)"),
                       ("rank_bm25", "резервный ранкер"),
                       ("reportlab", "демо-PDF и тесты"),
                       ("numpy", "векторы"),
                       ("docling", "парсер №1 (тяжёлый)")):
        try:
            mod_obj = __import__(mod)
            version = getattr(mod_obj, "__version__", "?")
            print("  [ok] %-12s %-10s %s" % (mod, version, label))
        except ImportError:
            needed = mod in ("pdfplumber", "llama_index", "rank_bm25", "reportlab")
            print("  [%s] %-12s %-10s %s" % ("!!" if needed else "--", mod, "нет", label))
    print("  Установить всё разом: pip install -r tools/requirements.txt")
    print("  Docling отдельно:     pip install docling")


def opensearch() -> None:
    head("OpenSearch")
    url = os.environ.get("SMD_OPENSEARCH_URL")
    if not url:
        print("  [--] SMD_OPENSEARCH_URL не задан — поиск работает на SQLite")
        print("      поднять: docker compose -f tools/rag/opensearch-compose.yml up -d")
    else:
        print("  URL: %s" % url)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                info = json.loads(r.read().decode("utf-8"))
            print("  [ok] кластер отвечает, версия %s"
                  % (info.get("version", {}).get("number", "?")))
        except Exception as exc:                  # noqa: BLE001
            print("  [!!] кластер не отвечает: %s: %s" % (type(exc).__name__, exc))
    print("  Docker: %s" % run(["docker", "--version"]))
    if shutil.which("docker"):
        print("  Контейнер: %s"
              % run(["docker", "inspect", "-f", "{{.State.Status}}", "smd-opensearch"]))


def database() -> None:
    head("База карточек")
    if not CARDS.exists():
        print("  [!!] %s нет — прогон ещё не запускали" % CARDS)
        return
    print("  Файл: %s (%.1f МБ)" % (CARDS, CARDS.stat().st_size / 1e6))
    con = sqlite3.connect(str(CARDS))
    con.row_factory = sqlite3.Row
    try:
        cards = con.execute("SELECT COUNT(*) n FROM cards").fetchone()["n"]
        pins = con.execute("SELECT COUNT(*) n FROM cards WHERE pin_count IS NOT NULL").fetchone()["n"]
        pkgs = con.execute("SELECT COUNT(*) n FROM cards WHERE package IS NOT NULL").fetchone()["n"]
        fails = con.execute("SELECT COUNT(*) n FROM failures").fetchone()["n"]
        print("  [ok] карточек: %d (с распиновкой %d, с корпусом %d)" % (cards, pins, pkgs))
        ok(fails > 0 and cards == 0, "сбоев разбора: %d" % fails)
        rows = con.execute(
            "SELECT filename, error FROM failures ORDER BY rowid DESC LIMIT 5").fetchall()
        if rows:
            print("  Последние сбои:")
            for r in rows:
                print("    %s: %s" % (r["filename"], (r["error"] or "")[:150]))
        parts = con.execute(
            "SELECT part FROM cards ORDER BY part LIMIT 8").fetchall()
        if parts:
            print("  Примеры: %s" % ", ".join(p["part"] for p in parts))
    except sqlite3.Error as exc:
        print("  [!!] не читается: %s" % exc)
    finally:
        con.close()

    head("Индекс поиска")
    if not INDEX.exists():
        print("  [--] индекс не собран: python3 tools/rag/pipeline.py --rebuild")
        return
    con = sqlite3.connect(str(INDEX))
    con.row_factory = sqlite3.Row
    try:
        chunks = con.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        docs = con.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
        tables = con.execute("SELECT COUNT(*) n FROM chunks WHERE is_table = 1").fetchone()["n"]
        vecs = con.execute("SELECT COUNT(*) n FROM vectors").fetchone()["n"]
        print("  [ok] чанков: %d из %d документов (таблиц %d, векторов %d)"
              % (chunks, docs, tables, vecs))
    except sqlite3.Error as exc:
        print("  [!!] не читается: %s" % exc)
    finally:
        con.close()


def site() -> None:
    head("Сайт")
    url = os.environ.get("SMD_SITE_URL") or "http://localhost:8000"
    for path, label in (("/api/health", "здоровье"), ("/api/cards?limit=1", "карточки"),
                        ("/api/search?q=test&k=1", "поиск")):
        try:
            started = time.time()
            with urllib.request.urlopen(url + path, timeout=6) as r:
                body = json.loads(r.read().decode("utf-8"))
            ms = int((time.time() - started) * 1000)
            extra = ""
            if path == "/api/health":
                extra = " карточек=%s" % body.get("cards")
            elif path.startswith("/api/cards"):
                extra = " всего=%s" % body.get("total")
            print("  [ok] %-24s %4d мс%s" % (label, ms, extra))
        except urllib.error.HTTPError as exc:
            print("  [!!] %-24s HTTP %d" % (label, exc.code))
        except Exception as exc:                  # noqa: BLE001
            print("  [--] %-24s %s: %s" % (label, type(exc).__name__, exc))
    print("  Если сайт не отвечает: python3 tools/rag/serve.py --port 8000 --jobs 4")


def corpus() -> None:
    head("Корпус PDF")
    folder = DATA_DIR / "datasheets"
    if not folder.exists():
        print("  [!!] нет папки %s" % folder)
        return
    pdfs = sorted(folder.rglob("*.pdf"))
    size = sum(p.stat().st_size for p in pdfs) / 1e6
    ok(not pdfs, "%d PDF, %.1f МБ в %s" % (len(pdfs), size, folder))
    uploaded = list((folder / "uploaded").rglob("*.pdf")) if (folder / "uploaded").exists() else []
    if uploaded:
        print("  Загружено через браузер: %d файл(ов)" % len(uploaded))


def main() -> int:
    cli.fix_windows_console()
    print(LINE)
    print("Диагнотика SMD Component Finder — %s"
          % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Каталог данных: %s" % DATA_DIR)
    machine()
    packages()
    opensearch()
    database()
    corpus()
    site()
    print("\n" + LINE)
    print("Готово. Этот вывод можно целиком вставить в чат — секретов в нём нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
