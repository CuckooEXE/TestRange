"""Belt-and-suspenders enforcement of the subprocess ban.

Ruff's flake8-tidy-imports config also catches this at lint time; this
test is the CI safety net for any environment where ruff isn't run.
"""

from __future__ import annotations

import re
from pathlib import Path

_SUBPROCESS_PAT = re.compile(r"^\s*(import subprocess|from subprocess\b)", re.MULTILINE)


def test_no_subprocess_imports() -> None:
    pkg = Path(__file__).resolve().parents[2] / "testrange"
    assert pkg.exists(), pkg
    offenders: list[str] = []
    for f in pkg.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if _SUBPROCESS_PAT.search(text):
            offenders.append(str(f.relative_to(pkg.parent)))
    assert (
        not offenders
    ), f"subprocess imports are forbidden in v0 (PLAN.md decision 15). Offenders: {offenders}"
