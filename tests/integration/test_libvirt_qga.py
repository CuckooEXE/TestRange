"""Integration test: QGACommunicator end-to-end against real libvirt.

Brings up a VM with ``qemu-guest-agent`` installed and round-trips
``execute`` / ``read_file`` / ``write_file`` over the QGA channel by
running ``examples/qga.py``. Skipped when libvirt is unreachable or the
``debian-13`` base image is not cached.

Run via ``pytest -m libvirt``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.libvirt

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "qga.py"


def _libvirt_available() -> bool:
    try:
        import libvirt
    except ImportError:
        return False
    try:
        conn = libvirt.open("qemu:///system")
        if conn is None:
            return False
        conn.close()
        return True
    except Exception:
        return False


def _base_cached() -> bool:
    from testrange.cache import CacheEntry, CacheManager
    from testrange.exceptions import CacheError

    try:
        CacheManager().resolve(CacheEntry("debian-13"), fetch=False)
        return True
    except CacheError:
        return False


if not _libvirt_available():
    pytest.skip("qemu:///system unreachable", allow_module_level=True)
if not _base_cached():
    pytest.skip("debian-13 base image not cached", allow_module_level=True)


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("qga_example", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_qga_example_round_trips() -> None:
    """examples/qga.py brings up a VM, reaches it over QGA, all tests pass."""
    from testrange import run_tests

    mod = _load_example()
    results = run_tests(mod.TESTS, mod.PLAN)
    assert results, "no test results"
    for r in results:
        assert r.passed, f"qga example test failed: {r}"
