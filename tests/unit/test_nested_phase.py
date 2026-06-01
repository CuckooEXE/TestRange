"""nested_phase guards, keyfile handling, handle delegation, LIFO teardown (ORCH-20).

The live recursion (entering a real inner Orchestrator over qemu+ssh) needs a
libvirt host and is covered by the integration suite; here we cover the pure
surface: input guards that must fire *before* any backend work, the temp-keyfile
contract, the NestedHandle forwarding, and teardown ordering.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.exceptions import OrchestratorError
from testrange.gateways import SSHJumpGateway
from testrange.orchestrator.nested_phase import (
    NestedHandle,
    NestedRun,
    _write_keyfile,
    run_nested_phase,
    teardown_nested,
)
from testrange.utils import SSHKey
from testrange.vms import GuestHypervisor, VMSpec
from testrange.vms.handle import VMHandle
from tests.mock_driver import MockHypervisor

_KEY = SSHKey.generate(comment="nested-phase-test")


def _guest(
    *,
    comm: Any,
    inner: Any | None = None,
    builder: CloudInitBuilder | None = None,
) -> GuestHypervisor:
    return GuestHypervisor(
        spec=VMSpec(
            name="host-a", devices=[CPU(2, nested=True), Memory(2048), OSDrive("pool1", 20)]
        ),
        builder=builder
        or CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
        ),
        communicator=comm,
        inner=inner if inner is not None else LibvirtHypervisor(),
    )


def _ctx(*vms: Any) -> Any:
    return types.SimpleNamespace(
        plan=types.SimpleNamespace(hypervisor=types.SimpleNamespace(vms=list(vms))),
        agent_ready_timeout_s=1.0,
    )


class TestRunNestedPhaseGuards:
    def test_no_guests_is_noop(self) -> None:
        runs, handles = run_nested_phase(_ctx())  # no GuestHypervisor entries
        assert runs == [] and handles == {}

    def test_non_ssh_communicator_rejected(self) -> None:
        with pytest.raises(OrchestratorError, match="SSHCommunicator"):
            run_nested_phase(_ctx(_guest(comm=NativeCommunicator())))

    def test_non_libvirt_inner_rejected_at_construction(self) -> None:
        # GuestHypervisor.__post_init__ enforces the libvirt-only inner at the
        # trust boundary, so a bad plan never reaches run_nested_phase.
        with pytest.raises(TypeError, match="LibvirtHypervisor"):
            _guest(comm=SSHCommunicator("admin"), inner=MockHypervisor())

    def test_unbound_communicator_has_no_host(self) -> None:
        # SSH + libvirt inner, but the communicator was never bound (no address).
        with pytest.raises(OrchestratorError, match="no resolved address"):
            run_nested_phase(_ctx(_guest(comm=SSHCommunicator("admin"))))

    def test_gateway_bound_communicator_rejected(self) -> None:
        # A guest reached only through a jump gateway (remote L0) can't be the
        # target of a direct inner qemu+ssh dial — fail loud, don't hang.
        comm = SSHCommunicator("admin")
        comm.bind(
            host="10.50.0.9",
            credential=PosixCred("admin", ssh_key=_KEY, admin=True),
            gateway=SSHJumpGateway(host="jump", username="root", pkey_text=None, port=22),
        )
        with pytest.raises(OrchestratorError, match="bound via a gateway"):
            run_nested_phase(_ctx(_guest(comm=comm)))

    def test_admin_credential_without_key_rejected(self) -> None:
        comm = SSHCommunicator("admin")
        comm.bind(host="10.50.0.9", credential=PosixCred("admin", password="x"), gateway=None)
        guest = _guest(
            comm=comm,
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"), credentials=[PosixCred("admin", password="x")]
            ),
        )
        with pytest.raises(OrchestratorError, match="ssh_key"):
            run_nested_phase(_ctx(guest))


class TestWriteKeyfile:
    def test_writes_private_key_at_0600(self) -> None:
        path = _write_keyfile(_KEY.priv)
        try:
            assert path.read_text() == _KEY.priv
            assert (path.stat().st_mode & 0o777) == 0o600
        finally:
            path.unlink(missing_ok=True)


class TestNestedHandle:
    def test_forwards_to_inner(self) -> None:
        inner: Any = types.SimpleNamespace(vms={"webapp": "vh"}, driver="drv", run_id="rid")
        host = VMHandle(name="host-a", backend_name="tr-vm-x", communicator=NativeCommunicator())
        h = NestedHandle(host=host, inner=inner)
        assert h.host is host
        assert h.vms is inner.vms
        assert h.driver is inner.driver
        assert h.run_id == "rid"


class _RecordingOrch:
    """A stand-in inner Orchestrator that records its teardown order."""

    def __init__(self, name: str, log: list[str], *, fail: bool = False) -> None:
        self._name = name
        self._log = log
        self._fail = fail

    def __exit__(self, *_exc: object) -> None:
        self._log.append(self._name)
        if self._fail:
            raise RuntimeError(f"inner {self._name} teardown blew up")


def _run(name: str, log: list[str], keyfile: Any, *, fail: bool = False) -> NestedRun:
    host = VMHandle(name=name, backend_name=f"tr-{name}", communicator=NativeCommunicator())
    inner: Any = types.SimpleNamespace(vms={}, driver=None, run_id=name)
    return NestedRun(
        orchestrator=_RecordingOrch(name, log, fail=fail),  # type: ignore[arg-type]
        handle=NestedHandle(host=host, inner=inner),
        keyfile=keyfile,
    )


class TestTeardownNested:
    def test_lifo_order_and_keyfiles_unlinked(self, tmp_path: Any) -> None:
        log: list[str] = []
        runs = []
        for name in ("a", "b", "c"):
            kf = tmp_path / f"{name}.key"
            kf.write_text("k")
            runs.append(_run(name, log, kf))
        teardown_nested(runs)
        assert log == ["c", "b", "a"]  # reversed
        assert all(not (tmp_path / f"{n}.key").exists() for n in ("a", "b", "c"))

    def test_one_failure_does_not_abort_the_rest(self, tmp_path: Any) -> None:
        log: list[str] = []
        kf_a, kf_b = tmp_path / "a.key", tmp_path / "b.key"
        kf_a.write_text("k")
        kf_b.write_text("k")
        runs = [_run("a", log, kf_a), _run("b", log, kf_b, fail=True)]
        teardown_nested(runs)  # must not raise
        assert log == ["b", "a"]  # b failed but a still torn down
        assert not kf_a.exists() and not kf_b.exists()  # both keyfiles cleaned
