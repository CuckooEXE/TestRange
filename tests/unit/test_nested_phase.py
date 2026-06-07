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

from testrange.builders import CloudInitBuilder, ESXiKickstartBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.disk.libvirt import LibvirtOSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.esxi import ESXiHypervisor, ESXiProfile
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.exceptions import OrchestratorError
from testrange.gateways import SSHJumpGateway
from testrange.orchestrator import nested_phase as np
from testrange.orchestrator.nested_phase import (
    NestedHandle,
    NestedRun,
    _esxi_root_password,
    _synthesize_inner_binding,
    _write_keyfile,
    run_nested_phase,
    teardown_nested,
)
from testrange.utils import EcdsaKey, SSHKey
from testrange.vms import GuestHypervisor, VMSpec
from testrange.vms.handle import VMHandle
from tests.mock_driver import MockHypervisor

_KEY = SSHKey.generate(comment="nested-phase-test")
# ESXi's FIPS sshd rejects Ed25519, so its root cred needs an ECDSA key (CORE-63).
_ESXI_KEY = EcdsaKey.generate(comment="nested-phase-esxi")


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

    def test_unsupported_inner_rejected_at_construction(self) -> None:
        # GuestHypervisor.__post_init__ enforces the supported inner backends
        # (libvirt, ESXi) at the trust boundary, so a bad plan never reaches
        # run_nested_phase. A MockHypervisor inner is neither.
        with pytest.raises(TypeError, match="LibvirtHypervisor or ESXiHypervisor"):
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


def _esxi_guest(*, license: str | None = None, root: PosixCred | None = None) -> GuestHypervisor:
    return GuestHypervisor.esxi(
        spec=VMSpec(
            name="esxi-a",
            firmware="bios",
            devices=[
                CPU(4, nested=True),
                Memory(8192),
                LibvirtOSDrive("pool1", 33, bus="sata"),
                LibvirtNetworkIface("lab", model="e1000e"),
            ],
        ),
        root=root or PosixCred("root", password="VMware1!", ssh_key=_ESXI_KEY),
        installer_iso=CacheEntry("esxi-installer"),
        license=license,
    )


def _esxi_ctx() -> Any:
    return types.SimpleNamespace(
        agent_ready_timeout_s=1.0, resolved=types.SimpleNamespace(uplinks={"egress": "tr-egress"})
    )


class TestEsxiInner:
    def test_front_door_wires_builder_communicator_and_inner(self) -> None:
        guest = _esxi_guest(license="HG00K-03H8K-48929-8K1NP-3LUJ4")
        assert isinstance(guest.inner, ESXiHypervisor)
        assert isinstance(guest.builder, ESXiKickstartBuilder)
        assert isinstance(guest.communicator, SSHCommunicator)
        assert guest.communicator.username == "root"
        assert "serialnum --esx=HG00K-03H8K-48929-8K1NP-3LUJ4" in guest.builder.build_kickstart()

    def test_synthesize_binding_picks_esxi_profile_no_keyfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The vSphere readiness gate would dial a live host; stub it out so the
        # dispatch is exercised without pyvmomi / a network.
        seen: dict[str, Any] = {}

        def _fake_wait(host: str, user: str, password: str, **_kw: Any) -> None:
            seen["wait"] = (host, user, password)

        monkeypatch.setattr(np, "wait_esxi_ready", _fake_wait)
        profile, keyfile = _synthesize_inner_binding(
            _esxi_ctx(), _esxi_guest(), SSHCommunicator("root"), "10.50.0.9"
        )
        assert isinstance(profile, ESXiProfile)
        assert profile.host == "10.50.0.9" and profile.password == "VMware1!"
        assert profile.uplinks == {"egress": "tr-egress"}  # inherits outer uplink map
        assert keyfile is None  # pyVmomi bind is password-based, no key file
        assert seen["wait"] == ("10.50.0.9", "root", "VMware1!")  # readiness gated first

    def test_root_password_extracted_from_builder(self) -> None:
        assert _esxi_root_password(_esxi_guest()) == "VMware1!"

    def test_root_password_rejects_non_esxi_builder(self) -> None:
        # A GuestHypervisor with an ESXi inner is allowed, but its builder must be
        # an ESXiKickstartBuilder for the inner pyVmomi auth to have a root password.
        bad = GuestHypervisor(
            spec=VMSpec(name="x", devices=[CPU(2), Memory(2048), OSDrive("pool1", 20)]),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"), credentials=[PosixCred("admin", ssh_key=_KEY)]
            ),
            communicator=SSHCommunicator("root"),
            inner=ESXiHypervisor(),
        )
        with pytest.raises(OrchestratorError, match="not ESXiKickstartBuilder"):
            _esxi_root_password(bad)
