"""Tests for nested orchestration on the Proxmox backend.

Covers :meth:`ProxmoxOrchestrator.root_on_vm` (constructing an
inner orchestrator from a booted PVE Hypervisor VM) plus
:meth:`ProxmoxOrchestrator._enter_nested_orchestrators` (entering
inner orchestrators when this orchestrator hosts a Hypervisor).

No live PVE — every external interaction (the proxmoxer client,
the hypervisor's communicator, the inner orchestrator's
``__enter__``) is mocked.  Live coverage lives in
``tests/test_proxmox_live.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from testrange import Credential, Memory, vCPU, vNIC
from testrange.backends.proxmox.orchestrator import ProxmoxOrchestrator
from testrange.exceptions import OrchestratorError


# =====================================================================
# Helpers
# =====================================================================


def _hypervisor_with_communicator(host: str, password: str = "pw") -> MagicMock:
    """Build a fake hypervisor whose ``_require_communicator()``
    returns a communicator with ``_host = host`` and whose
    ``users`` carries one root credential with *password*."""
    hv = MagicMock()
    hv.name = "pve-hv"
    hv.users = [Credential("root", password)]
    hv.networks = []
    hv.vms = []

    comm = MagicMock()
    comm._host = host
    hv._require_communicator.return_value = comm

    # _wait_for_pveproxy is a two-stage gate: systemctl is-active
    # (stage 1) AND a curl probe to /api2/json/version (stage 2).
    # Both must short-circuit to "ready" so the readiness wait
    # doesn't eat real wall-clock in tests.  Dispatch on argv:
    # systemctl returns ``active``, curl returns ``200`` (any 2xx/4xx
    # is accepted), every other exec defaults to a generic success.
    def _exec(argv: list, **_kw):  # type: ignore[no-untyped-def]
        result = MagicMock()
        result.exit_code = 0
        result.stderr = b""
        if argv[:2] == ["systemctl", "is-active"]:
            result.stdout = b"active\n"
        elif argv[0] == "sh" and "curl" in (argv[2] if len(argv) > 2 else ""):
            result.stdout = b"200"
        else:
            result.stdout = b""
        return result
    hv.exec.side_effect = _exec
    return hv


def _outer_orchestrator(cache_root: Path | None = None) -> MagicMock:
    """A barebones outer orchestrator with the cache attribute the
    inner setup pulls from.  *cache_root* defaults to a tmp Path
    distinct from the live cache so concurrent runs don't race."""
    outer = MagicMock()
    outer._cache = MagicMock()
    outer._cache.root = cache_root or Path("/tmp/testrange-test-cache")
    return outer


# =====================================================================
# root_on_vm — happy path + error surface
# =====================================================================


class TestRootOnVm:
    def test_returns_unentered_proxmox_orchestrator(self) -> None:
        hv = _hypervisor_with_communicator("10.0.0.10", password="hunter2")
        outer = _outer_orchestrator()

        inner = ProxmoxOrchestrator.root_on_vm(hv, outer)

        assert isinstance(inner, ProxmoxOrchestrator)
        # Carries the host the communicator reported.
        assert inner._host == "10.0.0.10"
        # Default REST port.
        assert inner._port == 8006
        # Picked up the root credential's password as ticket auth.
        assert inner._user == "root@pam"
        assert inner._password == "hunter2"
        # PVE ships a self-signed cert; verify=False matches the
        # operator's only sane default.
        assert inner._verify_ssl is False
        # Cache root inherited from the outer orchestrator.
        assert inner._cache_root == Path("/tmp/testrange-test-cache")
        # Not yet entered.
        assert inner._client is None

    def test_inherits_inner_vms_and_networks(self) -> None:
        from testrange.backends.proxmox.network import ProxmoxVirtualNetwork

        hv = _hypervisor_with_communicator("10.0.0.10")
        # Stash some inner specs so we can verify they propagate.
        sentinel_vm = MagicMock()
        sentinel_vm.name = "inner-vm"
        # Use a real ProxmoxVirtualNetwork — the orchestrator's
        # __init__ promotes non-Proxmox networks, which involves
        # subnet validation that a bare MagicMock can't satisfy.
        sentinel_net = ProxmoxVirtualNetwork("inner-net", "10.42.0.0/24")
        hv.vms = [sentinel_vm]
        hv.networks = [sentinel_net]

        inner = ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

        # Inner spec list reflects what the hypervisor declared.
        # (ProxmoxOrchestrator.__init__ promotes GenericVM →
        # ProxmoxVM; our MagicMock is treated as already-native and
        # passes through the isinstance check unchanged.)
        assert inner._vm_list == [sentinel_vm]
        # Already a ProxmoxVirtualNetwork — promotion is a no-op so
        # the same instance comes through.
        assert inner._networks == [sentinel_net]

    def test_no_users_raises(self) -> None:
        hv = MagicMock()
        hv.name = "pve-hv"
        hv.users = []  # nobody to authenticate as
        with pytest.raises(OrchestratorError, match="no users"):
            ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

    def test_root_without_password_raises(self) -> None:
        hv = MagicMock()
        hv.name = "pve-hv"
        # No password — only an SSH key.  ProxmoxOrchestrator's
        # ticket auth path requires a plaintext password.
        hv.users = [Credential("root", None, ssh_key="ssh-ed25519 AAAA...")]
        hv.networks = []
        hv.vms = []
        with pytest.raises(OrchestratorError, match="no password"):
            ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

    def test_communicator_without_host_raises(self) -> None:
        hv = _hypervisor_with_communicator("10.0.0.10")
        # Patch the host out: the booted hypervisor's communicator
        # somehow doesn't carry one (DHCP without static-IP wiring).
        hv._require_communicator.return_value._host = None
        with pytest.raises(OrchestratorError, match="static IP"):
            ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

    def test_libvirt_network_is_promoted_to_proxmox(self) -> None:
        """The top-level ``testrange.VirtualNetwork`` re-export
        resolves to the libvirt backend's class.  When a user
        constructs ``Hypervisor(orchestrator=ProxmoxOrchestrator,
        networks=[VirtualNetwork(...)])`` the inner
        ProxmoxOrchestrator must promote those into
        ProxmoxVirtualNetwork — otherwise the libvirt-flavoured
        ``start()`` reaches for ``context._conn`` and crashes."""
        from testrange.backends.libvirt.network import (
            VirtualNetwork as LibvirtVirtualNetwork,
        )
        from testrange.backends.proxmox.network import ProxmoxVirtualNetwork

        hv = _hypervisor_with_communicator("10.0.0.10")
        hv.networks = [
            LibvirtVirtualNetwork("PublicNet", "10.42.0.0/24", internet=True),
        ]

        inner = ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

        # Inner network is now a ProxmoxVirtualNetwork — and the
        # five public fields round-trip identically.
        assert len(inner._networks) == 1
        promoted = inner._networks[0]
        assert isinstance(promoted, ProxmoxVirtualNetwork)
        assert promoted.name == "PublicNet"
        assert promoted.subnet == "10.42.0.0/24"
        assert promoted.internet is True
        assert promoted.dhcp is True
        assert promoted.dns is True

    def test_skips_pve_node_bootstrap(self) -> None:
        """``root_on_vm`` does NOT SSH the dnsmasq + repo-swap
        bootstrap onto the PVE node — that step is now baked into
        the cached install qcow2 by the install-phase first-boot
        script (see :meth:`ProxmoxAnswerBuilder.first_boot_script`).
        Running it again over SSH at run time would fail in airgapped
        run-phase networks (``internet=False``) where ``apt-get
        update`` can't reach the public mirror; the cached image
        already carries dnsmasq + the no-subscription repo so no
        further apt traffic is needed.

        Pinning the absence here (no ``bash -c`` bootstrap exec)
        guards against an accidental revival of the SSH bootstrap
        path that would re-break airgap topologies.
        """
        hv = _hypervisor_with_communicator("10.0.0.10")

        ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())

        # No exec call should be the bootstrap bash -c '...'.  Every
        # exec call should be a list whose first element is a
        # systemctl command (``is-active pveproxy``) — the readiness
        # wait, the only thing root_on_vm shells out for now.
        for call in hv.exec.call_args_list:
            argv = call.args[0]
            assert argv[0] != "bash", (
                f"root_on_vm should not SSH-bootstrap PVE; saw "
                f"bash exec: {argv!r}"
            )

    def test_bootstrap_classmethod_failure_raises(self) -> None:
        """``_bootstrap_pve_node`` is still callable as a manual
        escape hatch for builders that don't bake dnsmasq into the
        install image.  When invoked directly, a non-zero exit from
        the script must still surface as an :class:`OrchestratorError`
        with the bootstrap log path in the message.
        """
        hv = _hypervisor_with_communicator("10.0.0.10")

        # _bootstrap_pve_node only does one ``hv.exec`` call (the
        # bash-c bootstrap script).  Override the helper's argv-
        # dispatching side_effect — for this test we want ALL execs
        # to fail.
        hv.exec.side_effect = None
        bad = MagicMock(exit_code=42, stdout=b"", stderr=b"apt failed")
        hv.exec.return_value = bad

        with pytest.raises(OrchestratorError, match="bootstrap"):
            ProxmoxOrchestrator._bootstrap_pve_node(hv)

    def test_pveproxy_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If pveproxy never reaches ``active`` the readiness wait
        eventually surfaces an OrchestratorError so the outer
        ExitStack unwinds cleanly instead of silently producing
        a half-baked inner orchestrator."""
        hv = _hypervisor_with_communicator("10.0.0.10")
        # Force EVERY exec to look like ``inactive`` so the
        # readiness loop never short-circuits on either stage.  The
        # helper's argv-dispatching side_effect would otherwise
        # return ``active`` for systemctl and ``200`` for curl —
        # masking the failure mode this test tries to pin.
        hv.exec.side_effect = None
        bad = MagicMock()
        bad.exit_code = 3
        bad.stdout = b"inactive\n"
        bad.stderr = b"pveproxy not started"
        hv.exec.return_value = bad
        # Fast-forward time so the test doesn't actually sleep 120s.
        import testrange.backends.proxmox.orchestrator as _orch_mod

        clock = [0.0]

        def _now() -> float:
            return clock[0]

        def _advance(_secs: float) -> None:
            clock[0] += 5.0  # bigger than the 2s real sleep so loop ends

        monkeypatch.setattr(_orch_mod, "uuid", _orch_mod.uuid)  # touch to keep linter happy
        # The wait function imports time inside its body; patch the
        # module's time.monotonic + time.sleep.
        import time as _time
        monkeypatch.setattr(_time, "monotonic", _now)
        monkeypatch.setattr(_time, "sleep", _advance)

        with pytest.raises(OrchestratorError, match="pveproxy"):
            ProxmoxOrchestrator.root_on_vm(hv, _outer_orchestrator())


# =====================================================================
# _enter_nested_orchestrators — symmetric Hypervisor support
# =====================================================================


class TestEnterNestedOrchestrators:
    def test_no_hypervisors_is_a_noop(self) -> None:
        """A run with no Hypervisor VMs leaves the nested-stack
        attribute untouched."""
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
        )
        # No nested wiring before; should stay None after.
        assert orch._nested_stack is None
        orch._enter_nested_orchestrators()
        assert orch._nested_stack is None
        assert orch._inner_orchestrators == []

    def test_enters_each_hypervisor_and_stashes_them(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every Hypervisor in the run gets ``root_on_vm`` called on
        its declared orchestrator class; each returned inner
        orchestrator is entered into the ExitStack and stashed on
        ``_inner_orchestrators``."""
        from testrange.vms.hypervisor_base import AbstractHypervisor

        # Build two fake Hypervisor VMs whose orchestrator class
        # records the root_on_vm calls and produces an inner
        # orchestrator we can confirm got entered.
        entered: list[str] = []

        class _FakeInner:
            def __init__(self, label: str) -> None:
                self.label = label

            def __enter__(self):
                entered.append(self.label)
                return self

            def __exit__(self, *exc):
                return None

        class _FakeOrchClass:
            label = "inner-A"

            @classmethod
            def root_on_vm(cls, hypervisor, outer):
                return _FakeInner(cls.label)

        class _FakeOrchClassB(_FakeOrchClass):
            label = "inner-B"

        hv_a = MagicMock(spec=AbstractHypervisor)
        hv_a.name = "hv-a"
        hv_a.orchestrator = _FakeOrchClass

        hv_b = MagicMock(spec=AbstractHypervisor)
        hv_b.name = "hv-b"
        hv_b.orchestrator = _FakeOrchClassB

        plain_vm = MagicMock()  # not a Hypervisor — should be skipped

        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
        )
        orch._vm_list = [plain_vm, hv_a, hv_b]

        orch._enter_nested_orchestrators()

        assert entered == ["inner-A", "inner-B"]
        assert len(orch._inner_orchestrators) == 2
        assert orch._nested_stack is not None
        # Tidy: closing the stack must be safe.
        orch._nested_stack.close()

    def test_partial_failure_unwinds_already_entered(self) -> None:
        """If the second Hypervisor's root_on_vm raises, the first
        already-entered inner orchestrator must be torn down before
        the exception propagates — that's the ExitStack contract
        the outer orchestrator's __enter__ relies on."""
        from testrange.vms.hypervisor_base import AbstractHypervisor

        teardown_log: list[str] = []

        class _RecordingInner:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                teardown_log.append("torn-down")
                return None

        class _OrchA:
            @classmethod
            def root_on_vm(cls, *_a):
                return _RecordingInner()

        class _OrchB:
            @classmethod
            def root_on_vm(cls, *_a):
                raise RuntimeError("inner B blew up")

        hv_a = MagicMock(spec=AbstractHypervisor)
        hv_a.name = "hv-a"
        hv_a.orchestrator = _OrchA

        hv_b = MagicMock(spec=AbstractHypervisor)
        hv_b.name = "hv-b"
        hv_b.orchestrator = _OrchB

        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
        )
        orch._vm_list = [hv_a, hv_b]

        with pytest.raises(RuntimeError, match="inner B"):
            orch._enter_nested_orchestrators()

        # The first inner orchestrator was unwound before the
        # exception bubbled up.
        assert teardown_log == ["torn-down"]
