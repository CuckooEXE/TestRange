"""Unit tests for nested orchestration (Phase B).

Covers:

- :class:`AbstractHypervisor` — ABC shape + isinstance partitioning
- :meth:`LibvirtOrchestrator.root_on_vm` URI construction + error paths
- :meth:`ProxmoxOrchestrator.root_on_vm` returns a configured
  inner orchestrator (regression: previously raised
  NotImplementedError; live behaviour now lives in
  ``tests/test_proxmox_root_on_vm.py``)
- :class:`Hypervisor` concrete class — default libvirt packages +
  post-install commands
- Outer :meth:`LibvirtOrchestrator._enter_nested_orchestrators` walk —
  partition, enter, and LIFO-unwind exception handling
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange import (
    AbstractHypervisor,
    Credential,
    HardDrive,
    Hypervisor,
    LibvirtOrchestrator,
    LibvirtVM as VM,
    Memory,
    Orchestrator,
    OrchestratorError,
    VirtualNetwork,
    vNIC,
    vCPU,
)

SSH_PUB = "ssh-ed25519 AAAAtest deploy@host"


def _hypervisor(
    *,
    communicator: str = "ssh",
    users: list[Credential] | None = None,
    vms: list | None = None,
    networks: list | None = None,
) -> Hypervisor:
    return Hypervisor(
        name="hv",
        iso="https://example.com/debian.qcow2",
        users=users or [Credential("root", "pw", ssh_key=SSH_PUB)],
        devices=[
            vCPU(2),
            Memory(4),
            HardDrive(40),
            vNIC("OuterNet", ip="10.0.0.10"),
        ],
        communicator=communicator,
        orchestrator=LibvirtOrchestrator,
        vms=vms or [],
        networks=networks or [],
    )


class TestAbstractHypervisor:
    def test_hypervisor_is_abstract_vm(self) -> None:
        hv = _hypervisor()
        assert isinstance(hv, AbstractHypervisor)
        # Also an AbstractVM — so everything that consumes VMs still works.
        from testrange.vms.base import AbstractVM
        assert isinstance(hv, AbstractVM)

    def test_plain_vm_is_not_hypervisor(self) -> None:
        plain = VM(
            name="plain",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("Net", ip="10.0.0.5")],
        )
        assert not isinstance(plain, AbstractHypervisor)

    def test_fields_exposed_on_instance(self) -> None:
        inner_net = VirtualNetwork("InnerNet", "10.42.0.0/24")
        inner_vm = VM(
            name="inner",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[inner_vm], networks=[inner_net])
        assert hv.orchestrator is LibvirtOrchestrator
        assert hv.vms == [inner_vm]
        assert hv.networks == [inner_net]


class TestHypervisorDefaultPayload:
    """The :class:`Hypervisor` concrete class pre-loads the apt
    packages and post-install commands needed to run libvirtd inside
    the guest — the user shouldn't have to write them by hand."""

    def test_libvirt_packages_injected(self) -> None:
        hv = _hypervisor()
        pkg_names = {p.name for p in hv.pkgs}
        assert "libvirt-daemon-system" in pkg_names
        assert "qemu-system-x86" in pkg_names
        assert "qemu-utils" in pkg_names

    def test_caller_pkgs_appended(self) -> None:
        from testrange import Apt
        hv = Hypervisor(
            name="hv",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw", ssh_key=SSH_PUB)],
            pkgs=[Apt("tmux")],
            orchestrator=LibvirtOrchestrator,
            communicator="ssh",
            devices=[vNIC("Net", ip="10.0.0.10")],
        )
        pkg_names = [p.name for p in hv.pkgs]
        # Libvirt-needed packages first; caller extras after.
        assert pkg_names[-1] == "tmux"
        assert "libvirt-daemon-system" in pkg_names

    def test_libvirtd_enable_in_post_install(self) -> None:
        hv = _hypervisor()
        joined = "\n".join(hv.post_install_cmds)
        assert "systemctl enable --now libvirtd" in joined

    def test_user_added_to_libvirt_group(self) -> None:
        hv = _hypervisor()
        joined = "\n".join(hv.post_install_cmds)
        assert "usermod -aG libvirt,kvm root" in joined

    def test_caller_post_install_appended(self) -> None:
        hv = Hypervisor(
            name="hv",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw", ssh_key=SSH_PUB)],
            post_install_cmds=["echo hello"],
            orchestrator=LibvirtOrchestrator,
            communicator="ssh",
            devices=[vNIC("Net", ip="10.0.0.10")],
        )
        # Library cmds first, caller extras after — the caller's
        # commands should run only after libvirtd is up.
        assert hv.post_install_cmds[-1] == "echo hello"
        assert "systemctl enable --now libvirtd" in hv.post_install_cmds[0]


class TestRootOnVmLibvirt:
    def _booted_hv(
        self,
        host: str = "10.0.0.10",
        users: list[Credential] | None = None,
    ) -> Hypervisor:
        """Build a Hypervisor and fake a live communicator on it."""
        hv = _hypervisor(users=users)
        comm = MagicMock()
        comm._host = host
        hv._communicator = comm
        return hv

    def test_uri_and_return_type(self) -> None:
        hv = self._booted_hv()
        outer = LibvirtOrchestrator(host="localhost")
        inner = LibvirtOrchestrator.root_on_vm(hv, outer)
        assert isinstance(inner, Orchestrator)
        # New orchestrator is targeted at the hypervisor VM's SSH IP.
        assert inner._host == "qemu+ssh://root@10.0.0.10/system?no_verify=1"

    def test_prefers_credential_with_ssh_key(self) -> None:
        users = [
            Credential("root", "pw"),  # no ssh_key
            Credential("deploy", "pw2", ssh_key=SSH_PUB, sudo=True),
        ]
        hv = self._booted_hv(users=users)
        outer = LibvirtOrchestrator(host="localhost")
        inner = LibvirtOrchestrator.root_on_vm(hv, outer)
        assert "deploy@" in inner._host

    def test_falls_back_to_first_credential(self) -> None:
        users = [Credential("root", "pw")]
        hv = self._booted_hv(users=users)
        outer = LibvirtOrchestrator(host="localhost")
        inner = LibvirtOrchestrator.root_on_vm(hv, outer)
        assert "root@" in inner._host

    def test_inner_reuses_hypervisor_vms_and_networks(self) -> None:
        inner_net = VirtualNetwork("InnerNet", "10.42.0.0/24")
        inner_vm = VM(
            name="inner",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = self._booted_hv()
        hv.vms = [inner_vm]
        hv.networks = [inner_net]
        outer = LibvirtOrchestrator(host="localhost")
        inner = LibvirtOrchestrator.root_on_vm(hv, outer)
        assert inner._vm_list == [inner_vm]
        assert inner._networks == [inner_net]

    def test_raises_when_no_host(self) -> None:
        hv = _hypervisor()
        comm = MagicMock()
        comm._host = ""
        hv._communicator = comm
        outer = LibvirtOrchestrator(host="localhost")
        with pytest.raises(OrchestratorError, match="static IP"):
            LibvirtOrchestrator.root_on_vm(hv, outer)

    def test_raises_when_no_users(self) -> None:
        hv = _hypervisor()
        hv.users = []
        outer = LibvirtOrchestrator(host="localhost")
        with pytest.raises(OrchestratorError, match="at least one Credential"):
            LibvirtOrchestrator.root_on_vm(hv, outer)


class TestRootOnVmProxmox:
    def test_returns_configured_inner_orchestrator(self) -> None:
        """Smoke test: ``ProxmoxOrchestrator.root_on_vm`` returns a
        configured-but-not-entered inner orchestrator pointing at
        the hypervisor's reachable IP.  Detailed coverage lives in
        ``tests/test_proxmox_root_on_vm.py``."""
        from testrange.backends.proxmox import ProxmoxOrchestrator

        hv = _hypervisor()
        # Stand-in for what _require_communicator would normally
        # return after the outer orchestrator booted the VM.
        comm = MagicMock()
        comm._host = "10.0.0.10"
        hv._communicator = comm
        # Short-circuit pveproxy readiness — the live VM is mocked.
        ready = MagicMock()
        ready.exit_code = 0
        ready.stdout = b"active\n"
        hv._communicator.exec.return_value = ready
        # AbstractVM.exec → self._communicator.exec; mock that too.
        hv.exec = lambda *a, **kw: ready  # type: ignore[method-assign]

        outer = LibvirtOrchestrator(host="localhost")
        inner = ProxmoxOrchestrator.root_on_vm(hv, outer)

        assert isinstance(inner, ProxmoxOrchestrator)
        assert inner._host == "10.0.0.10"
        assert inner._client is None  # not yet entered


class TestNestedEnterExit:
    """Verifies the outer orchestrator partitions Hypervisor VMs and
    enters their inner orchestrators via ExitStack — with correct
    LIFO unwinding on failure."""

    def _orch_with_hypervisors(
        self,
        hypervisors: list[Hypervisor],
        extra_vms: list[VM] | None = None,
    ) -> Orchestrator:
        return LibvirtOrchestrator(
            host="localhost",
            vms=[*hypervisors, *(extra_vms or [])],
            networks=[VirtualNetwork("OuterNet", "10.0.0.0/24")],
        )

    def test_no_hypervisors_is_noop(self) -> None:
        orch = self._orch_with_hypervisors(hypervisors=[])
        orch._enter_nested_orchestrators()
        assert orch._nested_stack is None
        assert orch._inner_orchestrators == []

    def test_enters_each_hypervisor(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Fake inner orchestrator whose __enter__ returns itself.
        entered: list[object] = []
        exited: list[object] = []

        class _FakeInner:
            def __init__(self, tag: str) -> None:
                self.tag = tag
            def __enter__(self) -> _FakeInner:
                entered.append(self)
                return self
            def __exit__(self, *_: object) -> None:
                exited.append(self)

        def _make_fake_root_on_vm(tag: str):
            @classmethod
            def _root(cls, hv, outer):  # type: ignore[no-untyped-def]
                return _FakeInner(tag)
            return _root

        hv_a = _hypervisor()
        hv_a._name = "hv-a"  # type: ignore[assignment]
        hv_b = _hypervisor()
        hv_b._name = "hv-b"  # type: ignore[assignment]

        # Monkeypatch on the instance via a subclass so we can return
        # different fakes per hypervisor and preserve the classmethod
        # binding.
        class _DriverA(LibvirtOrchestrator):
            pass

        class _DriverB(LibvirtOrchestrator):
            pass

        _DriverA.root_on_vm = _make_fake_root_on_vm("A")  # type: ignore[method-assign]
        _DriverB.root_on_vm = _make_fake_root_on_vm("B")  # type: ignore[method-assign]
        hv_a.orchestrator = _DriverA
        hv_b.orchestrator = _DriverB

        orch = self._orch_with_hypervisors([hv_a, hv_b])
        orch._enter_nested_orchestrators()

        assert len(entered) == 2
        assert [e.tag for e in entered] == ["A", "B"]  # type: ignore[attr-defined]
        assert orch._nested_stack is not None
        assert len(orch._inner_orchestrators) == 2

        # Close the stack — inner orchestrators unwind LIFO.
        orch._nested_stack.close()
        assert [e.tag for e in exited] == ["B", "A"]  # type: ignore[attr-defined]

    def test_partial_failure_unwinds_already_entered(self) -> None:
        """If the second inner fails to enter, the first must be
        exited before the exception propagates."""
        entered: list[str] = []
        exited: list[str] = []

        class _FakeInner:
            def __init__(self, tag: str, fail: bool = False) -> None:
                self.tag = tag
                self.fail = fail
            def __enter__(self) -> _FakeInner:
                if self.fail:
                    raise RuntimeError(f"enter failed for {self.tag}")
                entered.append(self.tag)
                return self
            def __exit__(self, *_: object) -> None:
                exited.append(self.tag)

        class _DriverA(LibvirtOrchestrator):
            @classmethod
            def root_on_vm(cls, hv, outer) -> _FakeInner:  # type: ignore[override,no-untyped-def]
                return _FakeInner("A")

        class _DriverB(LibvirtOrchestrator):
            @classmethod
            def root_on_vm(cls, hv, outer) -> _FakeInner:  # type: ignore[override,no-untyped-def]
                return _FakeInner("B", fail=True)

        hv_a = _hypervisor()
        hv_a._name = "hv-a"  # type: ignore[assignment]
        hv_a.orchestrator = _DriverA
        hv_b = _hypervisor()
        hv_b._name = "hv-b"  # type: ignore[assignment]
        hv_b.orchestrator = _DriverB

        orch = self._orch_with_hypervisors([hv_a, hv_b])

        with pytest.raises(RuntimeError, match="enter failed for B"):
            orch._enter_nested_orchestrators()

        # A was entered and then unwound; B's enter raised so it was
        # never recorded as entered.
        assert entered == ["A"]
        assert exited == ["A"]
        assert orch._nested_stack is None
