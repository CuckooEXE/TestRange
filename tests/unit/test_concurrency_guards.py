"""Regression tests for the ADR-0023 concurrency guards.

These pin the fixes from the post-merge review:

- ``--jobs`` validation rejects negatives at the CLI boundary (CORE-43); ``0``
  and ``1`` are accepted and both mean serial.
- the Proxmox driver serializes its cluster-global SDN control path so
  concurrent ``create_switch`` workers never overlap a ``PUT /cluster/sdn``
  apply or double-create the shared per-run zone (PVE-53);
- the Proxmox vnet resource map survives concurrent ``create_network`` writes
  with no lost entries (BACKEND-13).
- the Proxmox driver serializes its per-storage create_vm import critical
  section so concurrent ``--jobs>1`` run-phase workers never pile concurrent
  ``qmcreate`` import-froms onto the one storage's flock (PVE-56).
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import Any

import pytest

from testrange.cli import _jobs_arg, build_parser
from testrange.devices import CPU, Memory, OSDrive
from testrange.drivers.base import VolumeRef
from testrange.drivers.proxmox.driver import ProxmoxDriver
from testrange.handles import PoolHandle
from testrange.networks import Network, Switch
from testrange.orchestrator._parallel import parallel_map
from testrange.vms import VMSpec

# Reuse the proxmoxer-free fakes the driver suite already maintains.
from tests.unit.test_proxmox_driver import _conn, _FakeApi, _FakeClient


class TestJobsArg:
    def test_zero_and_positive_accepted(self) -> None:
        assert _jobs_arg("0") == 0
        assert _jobs_arg("1") == 1
        assert _jobs_arg("8") == 8

    def test_negative_rejected(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
            _jobs_arg("-1")

    def test_non_integer_rejected(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="integer"):
            _jobs_arg("eight")

    def test_parser_rejects_negative_jobs(self) -> None:
        # argparse renders an ArgumentTypeError as exit-code-2 SystemExit.
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "plan.py", "--jobs", "-4"])

    def test_parser_accepts_zero_as_serial(self) -> None:
        args = build_parser().parse_args(["run", "plan.py", "--jobs", "0"])
        assert args.jobs == 0


class _InstrumentedSdnApi(_FakeApi):
    """``_FakeApi`` that records SDN-apply concurrency and zone-create count.

    The apply path sleeps briefly so that, *without* the driver's serialization
    lock, concurrent ``create_switch`` workers would visibly overlap here — the
    test then asserts they never do.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clock = threading.Lock()
        self._apply_in_flight = 0
        self.max_apply_in_flight = 0
        self.zone_posts = 0

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path == "cluster/sdn/zones" and method == "post":
            with self._clock:
                self.zone_posts += 1
        if path == "cluster/sdn" and method == "put":
            with self._clock:
                self._apply_in_flight += 1
                self.max_apply_in_flight = max(self.max_apply_in_flight, self._apply_in_flight)
            try:
                time.sleep(0.02)  # widen the critical-section window
                return super()._call(method, path, kwargs)
            finally:
                with self._clock:
                    self._apply_in_flight -= 1
        return super()._call(method, path, kwargs)


def _instrumented_driver() -> tuple[ProxmoxDriver, _InstrumentedSdnApi]:
    client = _FakeClient()
    api = _InstrumentedSdnApi()
    client.api = api
    drv = ProxmoxDriver(_conn(), client=client, uplinks={})  # type: ignore[arg-type]
    return drv, api


class TestProxmoxSdnSerialization:
    def test_concurrent_create_switch_never_overlaps_apply(self) -> None:
        drv, api = _instrumented_driver()
        switches = [Switch(f"sw{i}", Network(f"n{i}"), cidr=f"10.0.{i}.0/24") for i in range(6)]
        names = [f"tr-switch-{i}" for i in range(6)]

        parallel_map(lambda i: drv.create_switch(switches[i], names[i]), range(6), jobs=6)

        # The cluster-wide PUT /cluster/sdn is serialized: never two at once.
        assert api.max_apply_in_flight == 1, (
            f"SDN applies overlapped (peak {api.max_apply_in_flight}); "
            "the per-driver _state_lock did not serialize the SDN path"
        )
        # The shared per-run zone is created exactly once (no check-then-act race).
        assert api.zone_posts == 1
        assert len(api.vnets) == 6


class _InstrumentedCreateApi(_FakeApi):
    """``_FakeApi`` extended with the qemu-create path, recording its concurrency.

    The create POST sleeps briefly so that, *without* the driver's per-storage
    import lock, concurrent ``create_vm`` workers would visibly overlap on the
    storage critical section here — the test asserts they never do (PVE-56).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clock = threading.Lock()
        self._create_in_flight = 0
        self.max_create_in_flight = 0
        self._nextid = 100

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path == "cluster/nextid" and method == "get":
            with self._clock:
                self._nextid += 1
                return str(self._nextid)
        if path.endswith("/qemu") and method == "post":
            with self._clock:
                self._create_in_flight += 1
                self.max_create_in_flight = max(self.max_create_in_flight, self._create_in_flight)
            try:
                time.sleep(0.02)  # widen the storage critical-section window
                return None  # not a UPID → create_vm does not wait on a task
            finally:
                with self._clock:
                    self._create_in_flight -= 1
        if path.endswith("/config") and method == "get":
            return {}  # never config-locked → _wait_unlocked returns immediately
        return super()._call(method, path, kwargs)


def _run_spec() -> VMSpec:
    # No NICs: keeps network_refs empty so the create path exercises only the
    # storage critical section under test.
    return VMSpec(name="web", devices=[CPU(2), Memory(1024), OSDrive(PoolHandle("pool1"), 8)])


class TestProxmoxStorageImportSerialization:
    def test_concurrent_create_vm_never_overlaps_storage_import(self) -> None:
        client = _FakeClient()
        api = _InstrumentedCreateApi()
        client.api = api
        drv = ProxmoxDriver(_conn(), client=client, uplinks={})  # type: ignore[arg-type]
        spec = _run_spec()

        parallel_map(
            lambda i: drv.create_vm(
                f"tr-vm-x-{i}",
                spec,
                "plan",
                os_disk_ref=VolumeRef(f"local:import/p__tr-vm-x-{i}.qcow2"),
                seed_iso_ref=None,  # run phase: import-from the cached disk
                network_refs={},
            ),
            range(6),
            jobs=6,
        )

        # PVE guards every storage-alloc op behind a single per-storage flock;
        # the driver's import lock keeps our own concurrent imports from racing
        # it — never two qmcreate import-froms in flight at once.
        assert api.max_create_in_flight == 1, (
            f"create_vm imports overlapped (peak {api.max_create_in_flight}); "
            "the per-driver storage import lock did not serialize the create path"
        )


class TestProxmoxVnetMapConcurrency:
    def test_concurrent_create_network_keeps_every_entry(self) -> None:
        drv, _api = _instrumented_driver()
        sw = Switch("sw", Network("n"), cidr="10.0.0.0/24")
        names = [f"tr-net-{i}" for i in range(40)]

        parallel_map(
            lambda i: drv.create_network(
                Network(f"n{i}"), sw, names[i], switch_backend_name=f"tr-switch-{i}"
            ),
            range(40),
            jobs=8,
        )

        # No write was lost under the thread pool: every composed name resolved.
        assert set(drv._vnet_by_network) == set(names)
