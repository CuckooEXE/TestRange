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


# =====================================================================
# recursive_vm_iter — Slice 2 plumbing.  Walks an outer ``_vm_list``
# depth-first, descending into every Hypervisor's inner ``vms`` so the
# bare-metal install loop sees descendant leaves too.  Backend-neutral
# (lives on the ABC), so the helper applies to libvirt and proxmox
# alike.
# =====================================================================


def _leaf_vm(name: str) -> VM:
    """Minimal Linux VM spec — used as a stand-in for nested leaves."""
    return VM(
        name=name,
        iso="https://example.com/debian.qcow2",
        users=[Credential("root", "pw")],
        devices=[vNIC("Inner", ip="10.42.0.5")],
    )


class TestRecursiveVmIter:
    """``recursive_vm_iter`` walks every VM in a (possibly nested) tree
    of Hypervisor specs.  Slice 2's bare-metal-builds-everything
    refactor needs this so the outer orchestrator's install loop sees
    descendant VMs, not just its own ``_vm_list``."""

    def test_flat_list_passes_through_unchanged(self) -> None:
        from testrange.orchestrator_base import recursive_vm_iter
        a, b = _leaf_vm("a"), _leaf_vm("b")
        assert list(recursive_vm_iter([a, b])) == [a, b]

    def test_empty_list_yields_nothing(self) -> None:
        from testrange.orchestrator_base import recursive_vm_iter
        assert list(recursive_vm_iter([])) == []

    def test_hypervisor_yields_itself_and_descendants(self) -> None:
        # The Hypervisor *is* a VM (it boots like one), so it's
        # emitted alongside its children — descendants don't replace
        # the parent.
        from testrange.orchestrator_base import recursive_vm_iter
        leaf1 = _leaf_vm("leaf1")
        leaf2 = _leaf_vm("leaf2")
        hv = _hypervisor(vms=[leaf1, leaf2])

        result = list(recursive_vm_iter([hv]))

        assert hv in result
        assert leaf1 in result
        assert leaf2 in result
        assert len(result) == 3

    def test_nested_hypervisor_traverses_full_tree(self) -> None:
        # Two-level nesting: outer Hypervisor → inner Hypervisor →
        # innermost leaf.  No live runtime support for triple-nest yet,
        # but the iter must descend the whole tree so a future slice
        # can rely on it.
        from testrange.orchestrator_base import recursive_vm_iter

        deep_leaf = _leaf_vm("deep")
        # Reuse the libvirt-friendly _hypervisor helper at both levels.
        inner_hv = _hypervisor(vms=[deep_leaf])
        inner_hv._name = "inner-hv"  # type: ignore[assignment]
        outer_hv = _hypervisor(vms=[inner_hv])
        outer_hv._name = "outer-hv"  # type: ignore[assignment]

        result = list(recursive_vm_iter([outer_hv]))

        assert outer_hv in result
        assert inner_hv in result
        assert deep_leaf in result
        assert len(result) == 3

    def test_pre_order_ancestor_before_descendants(self) -> None:
        # Ordering matters for the install phase: the parent's IP
        # allocation has to happen before its children compute their
        # own slot in the install network.  Pre-order (parent before
        # children) keeps that invariant.
        from testrange.orchestrator_base import recursive_vm_iter

        leaf = _leaf_vm("leaf")
        hv = _hypervisor(vms=[leaf])
        result = list(recursive_vm_iter([hv]))
        assert result.index(hv) < result.index(leaf)

    def test_mixed_flat_and_nested(self) -> None:
        # The outer ``_vm_list`` typically mixes plain VMs and
        # Hypervisors.  Both must be visited.
        from testrange.orchestrator_base import recursive_vm_iter

        flat = _leaf_vm("flat")
        leaf = _leaf_vm("leaf")
        hv = _hypervisor(vms=[leaf])
        hv._name = "hv"  # type: ignore[assignment]

        result = list(recursive_vm_iter([flat, hv]))

        assert flat in result
        assert hv in result
        assert leaf in result

    def test_does_not_treat_orchestrator_class_as_vm(self) -> None:
        # Defensive: a hypervisor's ``orchestrator`` class reference
        # must not be treated as a descendant.  Only objects that are
        # also AbstractVM / AbstractHypervisor get descended.
        from testrange.orchestrator_base import recursive_vm_iter
        hv = _hypervisor()
        result = list(recursive_vm_iter([hv]))
        # ``LibvirtOrchestrator`` is a class, not a VM — type-check
        # narrowing flags this comparison as "non-overlapping" but
        # the runtime behaviour is exactly what we want to pin.
        assert LibvirtOrchestrator not in result  # type: ignore[comparison-overlap]

    def test_accepts_tuple(self) -> None:
        from testrange.orchestrator_base import recursive_vm_iter
        a, b = _leaf_vm("a"), _leaf_vm("b")
        assert list(recursive_vm_iter((a, b))) == [a, b]

    def test_accepts_generator(self) -> None:
        from collections.abc import Iterator
        from testrange.orchestrator_base import recursive_vm_iter

        a, b = _leaf_vm("a"), _leaf_vm("b")

        def _gen() -> Iterator[VM]:
            yield a
            yield b

        assert list(recursive_vm_iter(_gen())) == [a, b]


# =====================================================================
# Slice 2 integration: the bare-metal install loop walks descendants
# alongside their parent Hypervisor.  Both backends register descendant
# install-network slots and call ``vm.build`` against the bare-metal
# orchestrator for each one.
# =====================================================================


class TestLibvirtInstallNetworkIncludesDescendants:
    """``_create_install_network`` must allocate IP slots for every VM
    in the nested tree, not just the top-level ``_vm_list``.  Without
    this, the bare-metal install loop would try to look up an IP for a
    descendant VM that was never registered and the install seed would
    miss its network-config block."""

    def test_descendants_get_install_network_slots(self) -> None:
        leaf1 = _leaf_vm("leaf1")
        leaf2 = _leaf_vm("leaf2")
        hv = _hypervisor(vms=[leaf1, leaf2])

        orch = LibvirtOrchestrator(
            host="localhost",
            vms=[hv],
            networks=[VirtualNetwork("OuterNet", "10.0.0.0/24")],
        )
        # ``_pick_install_subnet`` reaches into libvirt; short-circuit
        # to a fixed /24 so we exercise the IPAM-loop logic without
        # opening a connection.
        orch._pick_install_subnet = lambda: "192.168.250.0/24"  # type: ignore[method-assign]
        net = orch._create_install_network(run_id="cafedeadbeef")

        registered_names = {entry[0] for entry in net._vm_entries}
        # Hypervisor itself + both leaves all have install-network slots.
        assert "hv" in registered_names
        assert "leaf1" in registered_names
        assert "leaf2" in registered_names

    def test_pool_guard_counts_descendants(self) -> None:
        """The IP-pool-too-small NetworkError must fire when the
        recursive descendant count exceeds the subnet capacity, not
        just the top-level count."""
        # We don't realistically construct 253 VMs in a unit test;
        # instead, short-circuit the subnet picker to a /30 (2 host
        # slots, minus gateway = 1 slot) and confirm the guard fires
        # for one Hypervisor + one descendant (= 2 install-phase VMs).
        # No need to monkey-patch the module-level pool — patching
        # the orchestrator's bound method to return a /30 directly
        # avoids any test pollution risk if a subsequent test relied
        # on the real pool.
        leaf = _leaf_vm("leaf")
        hv = _hypervisor(vms=[leaf])
        orch = LibvirtOrchestrator(
            host="localhost",
            vms=[hv],
            networks=[VirtualNetwork("OuterNet", "10.0.0.0/24")],
        )
        orch._pick_install_subnet = lambda: "192.168.250.0/30"  # type: ignore[method-assign]
        with pytest.raises(NetworkError, match="install network subnet"):
            orch._create_install_network(run_id="abcd")


# Suppress unused-import noise — NetworkError used above.
from testrange.exceptions import NetworkError  # noqa: E402


class TestProxmoxInstallNetworkIncludesDescendants:
    """Symmetric guarantee on the proxmox backend: its install vnet
    pre-registers descendant slots too."""

    def test_descendants_get_install_network_slots(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        from testrange.backends.proxmox.network import ProxmoxVirtualNetwork

        leaf = _leaf_vm("leaf")
        hv = _hypervisor(vms=[leaf])

        # Skip the proxmoxer-availability + zone-existence preflights
        # so the orchestrator constructs.  We're only exercising
        # ``_create_install_network``'s loop.
        outer_net = ProxmoxVirtualNetwork("OuterNet", "10.0.0.0/24")
        orch = ProxmoxOrchestrator(
            host="localhost", vms=[hv], networks=[outer_net],
        )
        # Bind the run id and short-circuit subnet picking the same
        # way the libvirt test does.
        orch._run_id = "cafedeadbeef"
        monkeypatch.setattr(
            orch, "_pick_install_subnet", lambda: "192.168.240.0/24",
        )
        net = orch._create_install_network()

        registered_names = {entry[0] for entry in net._vm_entries}
        assert "hv" in registered_names
        assert "leaf" in registered_names


class TestValidateTopology:
    """``AbstractOrchestrator.validate_topology`` walks the nested-VM
    tree and warns on structurally-unreachable internet requirements.

    A descendant declared on an ``internet=True`` network whose
    parent Hypervisor's run-phase network has ``internet=False`` can
    install (Slice 2 builds on the bare-metal install network with
    internet) but cannot reach the internet at runtime — its parent
    has no upstream.  The warning surfaces this at orchestrator-entry
    so the operator hits a clear pytest message instead of cryptic
    ``apt`` / ``curl`` failures mid-test.

    User chose ``UserWarning`` (not ``raise``) at slice planning time
    so out-of-band routing setups the validator can't see (manual
    iptables, sidecar gateway containers, …) keep working when the
    operator knows what they're doing.
    """

    def test_clean_topology_emits_no_warning(
        self, recwarn: pytest.WarningsRecorder,
    ) -> None:
        from testrange.orchestrator_base import AbstractOrchestrator

        # Outer net + inner net both internet=True — no problem.
        outer_net = VirtualNetwork("OuterNet", "10.0.0.0/24", internet=True)
        inner_net = VirtualNetwork("InnerNet", "10.42.0.0/24", internet=True)
        leaf = VM(
            name="leaf",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[leaf], networks=[inner_net])

        AbstractOrchestrator.validate_topology(
            vms=[hv], networks=[outer_net],
        )
        assert len(recwarn.list) == 0

    def test_warns_when_descendant_needs_internet_but_parent_has_none(
        self,
    ) -> None:
        from testrange.orchestrator_base import AbstractOrchestrator

        # Hypervisor on an internet=False network; descendant on an
        # internet=True inner network → unreachable.
        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        inner_net = VirtualNetwork(
            "InnerNet", "10.42.0.0/24", internet=True,
        )
        leaf = VM(
            name="leaf",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[leaf], networks=[inner_net])

        with pytest.warns(UserWarning, match="unreachable"):
            AbstractOrchestrator.validate_topology(
                vms=[hv], networks=[outer_net],
            )

    def test_warning_names_descendant_and_hypervisor(
        self,
    ) -> None:
        from testrange.orchestrator_base import AbstractOrchestrator

        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        inner_net = VirtualNetwork(
            "InnerNet", "10.42.0.0/24", internet=True,
        )
        leaf = VM(
            name="leaf",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[leaf], networks=[inner_net])

        with pytest.warns(UserWarning) as record:
            AbstractOrchestrator.validate_topology(
                vms=[hv], networks=[outer_net],
            )
        # Operator-readable: must mention both VM and parent so the
        # offending pair is identifiable from the warning alone.
        msg = str(record[0].message)
        assert "leaf" in msg
        assert "hv" in msg

    def test_no_warning_when_descendant_does_not_need_internet(
        self, recwarn: pytest.WarningsRecorder,
    ) -> None:
        from testrange.orchestrator_base import AbstractOrchestrator

        # Both outer and inner have internet=False — descendant is
        # airgapped by design; that's a deliberate test topology, not
        # an unreachable misconfiguration.  No warning.
        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        inner_net = VirtualNetwork(
            "InnerNet", "10.42.0.0/24", internet=False,
        )
        leaf = VM(
            name="leaf",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[leaf], networks=[inner_net])

        AbstractOrchestrator.validate_topology(
            vms=[hv], networks=[outer_net],
        )
        assert len(recwarn.list) == 0

    def test_no_warning_when_no_hypervisors(
        self, recwarn: pytest.WarningsRecorder,
    ) -> None:
        from testrange.orchestrator_base import AbstractOrchestrator

        # Flat topology — no Hypervisors, no descendants, nothing to
        # validate.  The check must short-circuit cleanly.
        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        flat = VM(
            name="flat",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("OuterNet", ip="10.0.0.5")],
        )
        AbstractOrchestrator.validate_topology(
            vms=[flat], networks=[outer_net],
        )
        assert len(recwarn.list) == 0

    def test_no_warning_when_descendant_targets_unknown_network(
        self, recwarn: pytest.WarningsRecorder,
    ) -> None:
        # If a descendant's vNIC.ref doesn't match any
        # ``Hypervisor.networks`` entry, we can't reason about its
        # reachability.  Stay silent rather than emit a possibly-
        # confusing warning — the orchestrator's own attach loop will
        # surface the misconfigured ref as a clear error later.
        from testrange.orchestrator_base import AbstractOrchestrator

        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        leaf = VM(
            name="leaf",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("MissingNet", ip="10.42.0.5")],
        )
        hv = _hypervisor(vms=[leaf], networks=[])

        AbstractOrchestrator.validate_topology(
            vms=[hv], networks=[outer_net],
        )
        assert len(recwarn.list) == 0

    def test_warns_for_each_offending_descendant(
        self,
    ) -> None:
        # Two descendants, both needing internet, parent has none —
        # both should be flagged.
        from testrange.orchestrator_base import AbstractOrchestrator

        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        inner_net = VirtualNetwork(
            "InnerNet", "10.42.0.0/24", internet=True,
        )
        leaf1 = VM(
            name="leaf1",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        leaf2 = VM(
            name="leaf2",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.6")],
        )
        hv = _hypervisor(vms=[leaf1, leaf2], networks=[inner_net])

        with pytest.warns(UserWarning) as record:
            AbstractOrchestrator.validate_topology(
                vms=[hv], networks=[outer_net],
            )
        # One warning per offending descendant — operator can act on
        # each independently without scanning a single mega-warning.
        names = [str(w.message) for w in record]
        assert any("leaf1" in m for m in names)
        assert any("leaf2" in m for m in names)


class TestNestedTreeBuildOrder:
    """``_provision_vms``-shaped helpers that drive ``recursive_vm_iter``
    must emit Hypervisor parents before their inner VMs.  IP allocation
    in ``_create_install_network`` relies on this for deterministic
    cross-run slot assignment."""

    def test_libvirt_pool_assigns_parent_before_descendants(self) -> None:
        leaf = _leaf_vm("leaf")
        hv = _hypervisor(vms=[leaf])
        orch = LibvirtOrchestrator(
            host="localhost", vms=[hv],
            networks=[VirtualNetwork("OuterNet", "10.0.0.0/24")],
        )
        orch._pick_install_subnet = lambda: "192.168.251.0/24"  # type: ignore[method-assign]
        net = orch._create_install_network(run_id="abcd")
        # Tuples are (vm_name, mac, ip) in registration order.  Parent
        # is registered before child → parent gets a numerically-lower
        # IP.
        order = [entry[0] for entry in net._vm_entries]
        assert order.index("hv") < order.index("leaf")
