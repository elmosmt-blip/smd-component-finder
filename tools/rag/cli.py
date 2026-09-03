"""Shared bits for the command-line entry points.

Kept tiny on purpose: everything in here exists because Windows and Unix
disagree about console encoding, about stack size, and about how to ask for
memory usage.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

# Windows gives python.exe a 1 MB main-thread stack, and torch's
# table-transformer recurses deep enough to exhaust it. The process then dies
# with 0xC00000FD (STATUS_STACK_OVERFLOW), which no `except` can catch — a
# 45-minute audit run ended exactly that way, taking every result with it.
# New threads honour `threading.stack_size`, so the same code gets room to
# breathe. 256 MB is virtual address space only; nothing is committed.
BIG_STACK_MB = 256


def run_with_big_stack(fn: Callable[..., T], *args: Any,
                       stack_mb: int = BIG_STACK_MB, **kwargs: Any) -> T:
    """Run `fn` on a thread whose stack is `stack_mb` megabytes.

    Falls back to a plain call wherever `threading.stack_size` is unavailable
    or refuses the size (some platforms cap it), so this is always safe to use
    — it just quietly does nothing on those systems.
    """
    try:
        threading.stack_size(stack_mb * 1024 * 1024)
    except (ValueError, RuntimeError, OverflowError):
        return fn(*args, **kwargs)

    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:            # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    try:
        threading.stack_size(0)                 # restore the default for others
    except (ValueError, RuntimeError, OverflowError):
        pass
    if "error" in box:
        raise box["error"]
    return box.get("value")                     # type: ignore[return-value]


def fix_windows_console() -> None:
    """Print UTF-8 on a Windows console too.

    PowerShell and cmd default to cp1251/cp866, which turns °, µ and → into
    mojibake and makes `bench.py` output unreadable. Setting PYTHONIOENCODING
    by hand works, but a tool that needs an environment variable to print its
    own numbers is a tool with a bug.
    """
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")   # for child processes
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
