"""Cross-backend contract tests.

Two layers:

1. **Signature contract** (the original ``Test*Contract`` classes).
   Encodes the shape of the hypervisor ABCs so a new backend can't
   silently skip or rename a method.  Pure introspection — never
   instantiates anything.

2. **Scenario contract** (the ``Scenario*`` classes added below).
   Exercises the *behaviour* every backend must share — construction
   defaults, GenericVM promotion, leak / cleanup semantics, backend
   identity, resource-name determinism.  Each scenario runs against
   every registered backend; backends whose ``__enter__`` isn't
   implemented yet (Proxmox today) hit construction-only tests and
   skip the rest.

When a new backend lands, add it to the parametrize lists at the
top — the same scenarios immediately exercise it, and any divergence
fails loudly.
"""

from __future__ import annotations

import inspect

import pytest

from testrange import AbstractOrchestrator, AbstractVirtualNetwork, AbstractVM
from testrange.backends.libvirt.network import VirtualNetwork as LibvirtNetwork
from testrange.backends.libvirt.orchestrator import Orchestrator as LibvirtOrch
from testrange.backends.libvirt.vm import LibvirtVM
from testrange.backends.proxmox import (
    ProxmoxOrchestrator,
    ProxmoxVirtualNetwork,
    ProxmoxVM,
)

ORCHESTRATORS = [LibvirtOrch, ProxmoxOrchestrator]
VM_CLASSES = [LibvirtVM, ProxmoxVM]
NETWORK_CLASSES = [LibvirtNetwork, ProxmoxVirtualNetwork]

# Backend triples for scenario tests.  Each entry is (orchestrator
# class, backend's native VM class, backend's network class) — the
# things needed to set up + tear down a minimal scenario.  Add a new
# triple here when adding a backend; every scenario test below picks
# it up automatically.
BACKEND_TRIPLES = [
    pytest.param(LibvirtOrch, LibvirtVM, LibvirtNetwork, id="libvirt"),
    pytest.param(
        ProxmoxOrchestrator, ProxmoxVM, ProxmoxVirtualNetwork, id="proxmox",
    ),
]


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
        """The kwargs every backend must accept (cross-backend contract)."""
        sig = inspect.signature(cls.__init__)
        expected = {
            "host", "networks", "vms", "cache_root",
            "cache", "cache_verify", "storage_backend",
        }
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
            GuestAgentCommunicator,
            LibvirtVM as VM,
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


# =====================================================================
# SCENARIO-LEVEL CONTRACT
#
# These exercise observable behaviour that the abstract layer
# promises.  Run against every backend in BACKEND_TRIPLES; backends
# whose ``__enter__`` is unimplemented run construction-only tests
# and skip the rest.
# =====================================================================


def _spec(name: str = "web", *, vm_cls=None):
    """Build a tiny VM spec.  When ``vm_cls`` is given, instantiates
    that backend's concrete class; otherwise returns a backend-
    agnostic GenericVM."""
    from testrange import Credential, GenericVM, Memory, vCPU
    kwargs = dict(
        name=name,
        iso="https://example.com/x.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1)],
    )
    return (vm_cls or GenericVM)(**kwargs)


class TestScenarioConstructionContract:
    """Construction-time invariants every backend must satisfy.

    These don't enter the orchestrator — safe to run even when the
    backend's ``__enter__`` raises NotImplementedError."""

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_constructs_with_no_args(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """An orchestrator with no networks + no VMs must still
        construct cleanly — used by ad-hoc scripts that build the
        spec list dynamically."""
        del vm_cls, net_cls
        orch = orch_cls()
        assert orch._vm_list == []
        assert orch._networks == []

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_cache_backend_name_matches_backend_type(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """The cache's backend_name (used as the HTTP-cache URL prefix)
        must match the orchestrator's own backend_type — otherwise
        artifacts pushed by one backend would land under the wrong
        prefix and never get found again."""
        del vm_cls, net_cls
        orch = orch_cls()
        assert orch._cache.backend_name == orch_cls.backend_type()

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_promotes_generic_vm_to_native(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """GenericVM is the backend-agnostic spec; every orchestrator
        must convert it to its own native VM type at __init__ so the
        rest of provisioning operates on backend-specific instances."""
        del net_cls

        from testrange import GenericVM
        spec = _spec("web")
        assert isinstance(spec, GenericVM)

        orch = orch_cls(vms=[spec])

        assert all(isinstance(v, vm_cls) for v in orch._vm_list)
        assert not any(isinstance(v, GenericVM) for v in orch._vm_list)

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_native_vm_passes_through_unchanged(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """Already-native VM specs must not be re-wrapped — that
        would lose any backend-specific options the user set
        explicitly on the concrete class."""
        del net_cls
        try:
            native = _spec("web", vm_cls=vm_cls)
            orch = orch_cls(vms=[native])
        except NotImplementedError:
            pytest.skip(f"{orch_cls.__name__} construction not implemented")
        assert orch._vm_list[0] is native


class TestScenarioBackendIdentityContract:
    """``backend_type()`` is the introspection hook test code uses to
    branch on which backend it's running against.  These pin the
    invariants every backend must honour."""

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_backend_type_is_lowercase_string(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        del vm_cls, net_cls
        bt = orch_cls.backend_type()
        assert isinstance(bt, str)
        assert bt and bt == bt.lower(), (
            f"{orch_cls.__name__}.backend_type() must be a non-empty "
            f"lowercase string, got {bt!r}"
        )

    def test_backend_types_are_unique(self) -> None:
        """No two backends may share a ``backend_type()`` — the
        identifier ends up in HTTP-cache URLs and CLI dispatch."""
        seen = [t.values[0].backend_type() for t in BACKEND_TRIPLES]
        assert len(seen) == len(set(seen)), (
            f"backend_type() collision in {seen}"
        )


class TestScenarioLifecycleContract:
    """The teardown contract: ``__exit__`` never raises (it would
    mask a more useful exception in the ``with`` block), ``leak()``
    is idempotent and skips resource teardown, and ``cleanup()`` is
    idempotent + best-effort even when called against runs whose
    resources don't exist."""

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_keep_alive_hints_returns_a_list(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """Always a list (eagerly evaluated, not a generator) so
        callers can iterate it twice without re-running it."""
        del vm_cls, net_cls
        orch = orch_cls()
        hints = orch.keep_alive_hints()
        assert isinstance(hints, list)
        assert all(isinstance(h, str) for h in hints)

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_leak_sets_flag_idempotently(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """Calling ``leak()`` once or twice must end in the same
        state — backend ``__exit__`` implementations check the flag
        and skip teardown when set."""
        del vm_cls, net_cls
        orch = orch_cls()
        assert orch._leaked is False
        orch.leak()
        assert orch._leaked is True
        orch.leak()
        assert orch._leaked is True

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_cleanup_either_works_or_documents_unimplemented(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """Cleanup is the SIGKILL-recovery hook (``testrange cleanup
        MODULE RUN_ID``).  Backends either implement it cleanly
        (idempotent best-effort) or raise a documented exception
        kind: ``NotImplementedError`` (backend hasn't wired it yet)
        or :class:`~testrange.exceptions.OrchestratorError` (couldn't
        connect, missing credentials, …).  Anything else — silent
        AttributeError, raw KeyError, or a generic ``Exception`` —
        breaks the recovery CLI."""
        from testrange.exceptions import OrchestratorError

        del vm_cls, net_cls
        orch = orch_cls()
        run_id = "00000000-0000-0000-0000-000000000000"
        try:
            orch.cleanup(run_id)
        except NotImplementedError as exc:
            assert orch_cls.__name__ in str(exc) or "cleanup" in str(exc).lower()
        except OrchestratorError:
            # Connection / credential failures are an acceptable
            # documented shape — same exception surface ``__enter__``
            # uses for the same kind of problem.
            pass


class TestScenarioDeterministicNaming:
    """Backend resource names that the spec implies (domains, networks)
    must be pure functions of (vm/net name, run_id).  This is what
    ``testrange cleanup`` relies on to reconstruct what to delete.

    The *mechanism* differs per backend (libvirt's
    :class:`VirtualNetwork` exposes ``bind_run(run_id)``; Proxmox's
    SDN-based one will use a different setup hook).  These tests
    cover only the cross-backend invariant — the methods exist and
    return strings; backend-specific naming logic is exercised in
    each backend's own test file (``test_vm_libvirt.py`` etc.)."""

    @pytest.mark.parametrize("orch_cls,vm_cls,net_cls", BACKEND_TRIPLES)
    def test_backend_name_is_a_string(
        self, orch_cls, vm_cls, net_cls,
    ) -> None:
        """Once the network has been initialised for a run, its
        backend_name() must return a non-empty string.  The
        initialisation hook is backend-specific: libvirt uses
        bind_run(), Proxmox uses its SDN setup."""
        del orch_cls, vm_cls
        net = net_cls(name="MyNet", subnet="10.0.0.0/24")
        run_id = "deadbeef-aaaa-bbbb-cccc-dddddddddddd"
        # Best-effort: try the libvirt-style hook first.
        if hasattr(net, "bind_run"):
            net.bind_run(run_id)
        try:
            name = net.backend_name()
        except (RuntimeError, NotImplementedError):
            pytest.skip(
                f"{type(net).__name__}.backend_name() requires backend-"
                "specific initialisation that's not yet wired"
            )
        assert isinstance(name, str) and name


