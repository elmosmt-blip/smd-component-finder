"""Shared bits for the command-line entry points.

Kept tiny on purpose: everything in here exists because Windows and Unix
disagree about console encoding and about how to ask for memory usage.
"""

from __future__ import annotations

import os
import sys


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
