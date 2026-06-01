"""Belt-and-suspenders enforcement of the subprocess ban.

Ruff's flake8-tidy-imports config also catches this at lint time; this
test is the CI safety net for any environment where ruff isn't run.
"""

from __future__ import annotations

import re
from pathlib import Path

_SUBPROCESS_PAT = re.compile(r"^\s*(import subprocess|from subprocess\b)", re.MULTILINE)

# The sanctioned subprocess modules: installer-ISO prep via xorriso (ADR-0022).
# Preserving the hybrid-boot layouts a pure-pycdlib rebuild strips needs xorriso;
# ADR-0001's escape hatch permits these sanctioned modules.
_SANCTIONED = {
    "testrange/builders/_proxmox_prepare.py",
    "testrange/builders/_esxi_prepare.py",
}


def test_no_subprocess_imports() -> None:
    pkg = Path(__file__).resolve().parents[2] / "testrange"
    assert pkg.exists(), pkg
    offenders: list[str] = []
    for f in pkg.rglob("*.py"):
        rel = str(f.relative_to(pkg.parent))
        if rel in _SANCTIONED:
            continue
        if _SUBPROCESS_PAT.search(f.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        f"subprocess imports are forbidden in v0 (PLAN.md decision 15). Offenders: {offenders}"
    )


def test_sanctioned_module_is_the_only_exception() -> None:
    # Guard the carve-out: the sanctioned module must exist and actually use
    # subprocess, so a stale whitelist entry can't silently widen the ban.
    pkg = Path(__file__).resolve().parents[2] / "testrange"
    for rel in _SANCTIONED:
        path = pkg.parent / rel
        assert path.exists(), f"sanctioned module {rel} is missing"
        assert _SUBPROCESS_PAT.search(path.read_text(encoding="utf-8")), (
            f"sanctioned module {rel} no longer imports subprocess; drop it from the whitelist"
        )
