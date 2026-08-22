#!/usr/bin/env python
"""Run the test suite with the project's virtualenv, wherever it lives.

Used by the pre-push hook. The hook runs under whatever `python` git happens to
put on PATH, which is usually not the interpreter that has this project's
dependencies installed - so find the venv explicitly and hand off to it.
Stdlib only, so it works with any interpreter old or new.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _interpreter() -> str:
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        ROOT / ".venv" / "bin" / "python",  # POSIX
    ):
        if candidate.exists():
            return str(candidate)
    # No venv checked out (CI containers, a cloner running tests directly):
    # the current interpreter is the best guess.
    return sys.executable


def main() -> int:
    cmd = [_interpreter(), "-m", "pytest", "-q", *sys.argv[1:]]
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
