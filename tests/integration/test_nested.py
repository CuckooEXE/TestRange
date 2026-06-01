"""Nested-virt integration: the shipped capabilities-nested example, end-to-end.

Runs ``examples/capabilities-nested.py`` against a live local libvirt (ADR-0021):
build host-a's libvirt stack + the inner VM on L0, boot host-a, recurse into its
inner plan over ``qemu+ssh``, and run the example's TESTS against the inner VM.

Heavily gated — marked ``libvirt`` (excluded from the default gate) and skipped
unless every precondition for a real nested run holds: ``qemu:///system``
reachable, the ``debian-13`` base and ``testrange-sidecar`` images cached, host
nested KVM enabled, and the ``tr-egress`` uplink bridge present. The full build +
nested bring-up takes minutes, so this is a manual/CI-marked check, not part of
the fast suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from testrange.cache import CacheEntry, CacheManager
from testrange.drivers.libvirt import LibvirtProfile
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.drivers.libvirt.driver import LibvirtDriver, _probe_host_nested_kvm
from testrange.exceptions import DriverError
from testrange.orchestrator.runner import run_tests

pytestmark = pytest.mark.libvirt

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "capabilities-nested.py"
_UPLINK_BRIDGE = "tr-egress"


def _require_preconditions() -> None:
    if _probe_host_nested_kvm() is not True:
        pytest.skip("host KVM nesting is disabled (/sys/module/kvm_*/parameters/nested != Y)")
    d = LibvirtDriver(LibvirtConn())
    try:
        d.connect()
    except DriverError as e:
        pytest.skip(f"libvirt qemu:///system not reachable: {e}")
    try:
        net = d._client.lookup_network(_UPLINK_BRIDGE)
    finally:
        d.disconnect()
    cache = CacheManager()
    for name in ("debian-13", "testrange-sidecar"):
        if cache.resolve(CacheEntry(name), fetch=False).path is None:
            pytest.skip(f"{name} image not in the local cache")
    if net is None and not Path(f"/sys/class/net/{_UPLINK_BRIDGE}").exists():
        pytest.skip(f"uplink bridge {_UPLINK_BRIDGE!r} not present")


def _load_example() -> tuple[Any, list[Any]]:
    spec = importlib.util.spec_from_file_location("_nested_example", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PLAN, module.TESTS


def test_capabilities_nested_example_passes() -> None:
    _require_preconditions()
    plan, tests = _load_example()
    profile = LibvirtProfile(uplinks={"egress": _UPLINK_BRIDGE})
    results = run_tests(tests, plan, profile=profile)
    failed = [r for r in results if not r.passed]
    assert not failed, "nested example tests failed:\n" + "\n".join(r.report_line() for r in failed)
