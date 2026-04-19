"""Cross-backend contract tests.

These encode the shape of the hypervisor ABCs so a new backend can't
silently skip or rename a method.  They never *instantiate* backends
beyond libvirt (Proxmox raises on instantiation today) — signature
checks only.
"""

from __future__ import annotations

import inspect

import pytest

from testrange import AbstractOrchestrator, AbstractVirtualNetwork, AbstractVM
from testrange.backends.libvirt.network import VirtualNetwork as LibvirtNetwork
from testrange.backends.libvirt.orchestrator import Orchestrator as LibvirtOrch
from testrange.backends.libvirt.vm import VM as LibvirtVM
from testrange.backends.proxmox import (
    ProxmoxOrchestrator,
    ProxmoxVirtualNetwork,
    ProxmoxVM,
)

ORCHESTRATORS = [LibvirtOrch, ProxmoxOrchestrator]
VM_CLASSES = [LibvirtVM, ProxmoxVM]
NETWORK_CLASSES = [LibvirtNetwork, ProxmoxVirtualNetwork]


class TestOrchestratorContract:
    @pytest.mark.parametrize("cls", ORCHESTRATORS)
    def test_is_abstract_orchestrator_subclass(self, cls) -> None:
        assert issubclass(cls, AbstractOrchestrator)

    @pytest.mark.parametrize("cls", ORCHESTRATORS)
    def test_has_context_manager_protocol(self, cls) -> None:
        assert callable(getattr(cls, "__enter__", None))
        assert callable(getattr(cls, "__exit__", None))

    @pytest.mark.parametrize("cls", ORCHESTRATORS)
    def test_constructor_accepts_standard_kwargs(self, cls) -> None:
        """host/networks/vms/cache_root must be accepted by every backend."""
        sig = inspect.signature(cls.__init__)
        expected = {"host", "networks", "vms", "cache_root"}
        missing = expected - set(sig.parameters)
        assert not missing, (
            f"{cls.__name__} is missing kwargs: {missing}"
        )

    @pytest.mark.parametrize("cls", ORCHESTRATORS)
    def test_networks_kwarg_accepts_abstract_sequence(self, cls) -> None:
        sig = inspect.signature(cls.__init__)
        ann = sig.parameters["networks"].annotation
        # The annotation is ``Sequence[AbstractVirtualNetwork] | None``;
        # we just need to verify the ABC appears in the string form
        # (concrete backends may narrow to their own subclass).
        assert (
            "AbstractVirtualNetwork" in str(ann)
            or "VirtualNetwork" in str(ann)
        ), f"{cls.__name__}.networks= annotation = {ann}"


class TestVMContract:
    @pytest.mark.parametrize("cls", VM_CLASSES)
    def test_is_abstract_vm_subclass(self, cls) -> None:
        assert issubclass(cls, AbstractVM)

    @pytest.mark.parametrize("cls", VM_CLASSES)
    def test_has_build_method(self, cls) -> None:
        assert callable(getattr(cls, "build", None))

    @pytest.mark.parametrize("cls", VM_CLASSES)
    def test_has_start_run_method(self, cls) -> None:
        assert callable(getattr(cls, "start_run", None))

    @pytest.mark.parametrize("cls", VM_CLASSES)
    def test_build_takes_context_not_conn(self, cls) -> None:
        """Regression: the abstract method accepts ``context``; no
        backend should still expose the old libvirt-specific
        ``conn`` parameter name."""
        sig = inspect.signature(cls.build)
        assert "context" in sig.parameters
        assert "conn" not in sig.parameters


class TestNetworkContract:
    @pytest.mark.parametrize("cls", NETWORK_CLASSES)
    def test_is_abstract_subclass(self, cls) -> None:
        assert issubclass(cls, AbstractVirtualNetwork)

    @pytest.mark.parametrize("cls", NETWORK_CLASSES)
    def test_start_stop_take_context(self, cls) -> None:
        for method_name in ("start", "stop"):
            sig = inspect.signature(getattr(cls, method_name))
            assert "context" in sig.parameters
            assert "conn" not in sig.parameters


class TestLibvirtOrchestratorAlias:
    def test_alias_points_at_orchestrator(self) -> None:
        from testrange import LibvirtOrchestrator, Orchestrator
        assert LibvirtOrchestrator is Orchestrator

    def test_top_level_orchestrator_is_libvirt(self) -> None:
        """The documented default at the package top level resolves to
        the libvirt backend.  Other backends must be requested
        explicitly to avoid accidental cross-backend dispatch."""
        from testrange import Orchestrator
        assert Orchestrator is LibvirtOrch


class TestBackendType:
    """Introspection hook so test code can branch on the backend it's
    running against.  Exposed as a classmethod so callers can reason
    about a backend without instantiating it."""

    def test_libvirt(self) -> None:
        assert LibvirtOrch.backend_type() == "libvirt"

    def test_proxmox(self) -> None:
        assert ProxmoxOrchestrator.backend_type() == "proxmox"

    def test_callable_on_instance(self) -> None:
        """Classmethod-style access also works on instances, which is
        how test code typically hits it (``orch.backend_type()``)."""
        orch = ProxmoxOrchestrator()
        assert orch.backend_type() == "proxmox"

    def test_distinct_per_backend(self) -> None:
        assert LibvirtOrch.backend_type() != ProxmoxOrchestrator.backend_type()


class TestGuestAgentFactoryIsBackendOverridable:
    """Regression for the communicator factor-out: SSH/WinRM are
    shared across backends, but ``"guest-agent"`` delegates to a
    backend-specific method, so Proxmox VMs no longer crash with an
    unhelpful ``AttributeError`` when a test asks for the guest agent."""

    def test_libvirt_vm_produces_libvirt_guest_agent(self) -> None:
        """End-to-end: libvirt VM + ``communicator="guest-agent"`` must
        produce the libvirt-flavoured communicator."""
        from unittest.mock import MagicMock

        from testrange import Credential
        from testrange.backends.libvirt import (
            VM,
            GuestAgentCommunicator,
        )

        vm = VM(
            name="x",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
        )
        vm._domain = MagicMock()
        comm = vm._make_guest_agent_communicator()
        assert isinstance(comm, GuestAgentCommunicator)

    def test_proxmox_vm_raises_not_implemented_clearly(self) -> None:
        """Proxmox hasn't implemented guest-agent yet.  Until the real
        :class:`ProxmoxGuestAgentCommunicator` is wired into
        :meth:`ProxmoxVM._make_guest_agent_communicator`, the default
        :meth:`AbstractVM` fallback surfaces a clear error instead of
        the pre-refactor ``AttributeError: 'ProxmoxVM' has no attribute
        '_domain'``."""
        from testrange import Credential
        from testrange.backends.proxmox import ProxmoxVM
        from testrange.exceptions import VMBuildError

        vm = ProxmoxVM(
            name="x",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
        )
        with pytest.raises(VMBuildError, match="guest-agent"):
            vm._make_guest_agent_communicator()


