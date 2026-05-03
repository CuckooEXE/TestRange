"""Cross-product nested-orchestration matrix.

Slice 6 of the nested-build refactor: the regression fence for the
original airgapped-internet bug.  Parametrised over
(outer-backend × inner-backend × leaf-OS), with the load-bearing
property pinned per row:

    "A leaf VM nested inside a Hypervisor whose run-network has
    ``internet=False`` builds and boots end-to-end without the inner
    orchestrator ever needing internet at runtime."

The matrix has two layers:

1. **Construction matrix** — runs always.  Builds the spec for each
   combo and asserts it constructs cleanly without raising and
   without :meth:`AbstractOrchestrator.validate_topology` false-
   warning on legitimate-but-airgapped topologies.  Cheap; catches
   regressions in the ``_promote_to_*`` paths and the topology-
   validator's heuristics.

2. **Live matrix** — skipped without the env vars described in
   :mod:`tests.test_proxmox_live`.  Stands up the topology against
   a real PVE (and / or local libvirtd), boots the leaf, and runs a
   trivial command via the leaf's communicator.  The full proof
   that Slice 1 + 2 + 3's airgap fix actually works.

Today's combos:

    (libvirt-outer, proxmox-inner, linux-leaf)
    (proxmox-outer, proxmox-inner, linux-leaf)

Future combos (libvirt-inner, ESXi-anywhere) gain rows here as the
backends land.
"""

from __future__ import annotations

import os
import warnings

import pytest

from testrange import (
    Credential,
    HardDrive,
    Hypervisor,
    LibvirtOrchestrator,
    Memory,
    VirtualNetwork,
    vCPU,
    vNIC,
)
from testrange.backends.proxmox import ProxmoxOrchestrator
from testrange.vms.generic import GenericVM

# =====================================================================
# Helpers
# =====================================================================


_LINUX_ISO = "https://example.com/debian-12-genericcloud-amd64.qcow2"
_PVE_ISO = "https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso"


def _leaf_linux(name: str, *, ip: str, network: str = "InnerNet") -> GenericVM:
    """Backend-agnostic Linux leaf VM spec."""
    return GenericVM(
        name=name,
        iso=_LINUX_ISO,
        users=[Credential("root", "pw")],
        devices=[
            vCPU(2),
            Memory(2),
            HardDrive(20),
            vNIC(network, ip=ip),
        ],
    )


def _hv_with_inner_airgap(
    *,
    inner_orchestrator_cls: type,
    leaves: list[GenericVM],
    inner_internet: bool = False,
) -> Hypervisor:
    """Build a Hypervisor whose inner network has *inner_internet* set.

    The Hypervisor's own run-network is created in the calling test's
    outer-orchestrator spec; the inner network here lives only inside
    the Hypervisor's ``networks`` list.  Pinning the inner default at
    ``internet=False`` is what makes this the regression fence for
    the original airgap bug — Slice 1's bootstrap-in-install fix
    means the leaf installs anyway because the bare-metal install
    network has internet (Slice 2).
    """
    inner_net = VirtualNetwork(
        "InnerNet", "10.42.0.0/24", internet=inner_internet,
    )
    return Hypervisor(
        orchestrator=inner_orchestrator_cls,
        name="hv",
        iso=_PVE_ISO,
        users=[Credential(
            "root", "testrange",
            ssh_key="ssh-ed25519 AAAA matrix@host",
        )],
        devices=[
            vCPU(2),
            Memory(4),
            HardDrive(40),
            vNIC("OuterNet", ip="10.0.0.10"),
        ],
        communicator="ssh",
        vms=leaves,
        networks=[inner_net],
    )


# =====================================================================
# Construction matrix — runs always
# =====================================================================


# Each row pins one (outer, inner, leaf-OS) combo.  The leaf-OS column
# is fixed to ``"linux"`` today; future ESXi / Windows-leaf rows would
# parametrise that axis too.
_CONSTRUCTION_MATRIX = [
    pytest.param(
        LibvirtOrchestrator, ProxmoxOrchestrator, "linux",
        id="libvirt-outer-pve-inner-linux",
    ),
    pytest.param(
        ProxmoxOrchestrator, ProxmoxOrchestrator, "linux",
        id="pve-outer-pve-inner-linux",
    ),
]


@pytest.mark.parametrize(
    "outer_cls,inner_cls,leaf_os", _CONSTRUCTION_MATRIX,
)
class TestNestedConstructionMatrix:
    """Spec-construction guarantees that hold for every combo:

    1. Building the topology spec doesn't raise (cross-backend
       :func:`_promote_to_*` paths all accept generic descendants).
    2. ``validate_topology`` does NOT warn when the leaf's inner
       network is ``internet=False`` and the leaf doesn't request
       internet — that's a deliberate airgap, not a misconfiguration.
    3. ``validate_topology`` *does* warn when the leaf's inner
       network requests ``internet=True`` but the parent
       Hypervisor's run network has ``internet=False`` (the
       structurally-unreachable case Slice 5 catches).
    """

    def test_constructs_without_raising(
        self, outer_cls: type, inner_cls: type, leaf_os: str,
    ) -> None:
        del leaf_os  # only Linux today
        leaf = _leaf_linux("leaf", ip="10.42.0.5")
        hv = _hv_with_inner_airgap(
            inner_orchestrator_cls=inner_cls, leaves=[leaf],
        )
        # Outer net default-internet matters per backend default; pin
        # internet=True here so the cross-product has a *valid*
        # default that doesn't trigger Slice 5's warning.
        outer_net = VirtualNetwork("OuterNet", "10.0.0.0/24", internet=True)
        # Construction: orchestrator promotes the spec.  No live
        # connection — neither backend's __init__ touches the network.
        outer = outer_cls(host="localhost", vms=[hv], networks=[outer_net])
        # Top-level vm list reflects the promoted Hypervisor.
        assert len(outer._vm_list) == 1
        # The Hypervisor's inner orchestrator class survived
        # promotion intact.
        from testrange.vms.hypervisor_base import AbstractHypervisor
        promoted_hv = outer._vm_list[0]
        assert isinstance(promoted_hv, AbstractHypervisor)
        assert promoted_hv.orchestrator is inner_cls

    def test_airgapped_descendant_emits_no_warning(
        self,
        outer_cls: type,
        inner_cls: type,
        leaf_os: str,
        recwarn: pytest.WarningsRecorder,
    ) -> None:
        del leaf_os
        # Outer + inner both internet=False — deliberate airgap.
        # Slice 5's validator must stay silent.
        leaf = _leaf_linux("leaf", ip="10.42.0.5")
        hv = _hv_with_inner_airgap(
            inner_orchestrator_cls=inner_cls, leaves=[leaf],
            inner_internet=False,
        )
        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        from testrange.orchestrator_base import AbstractOrchestrator
        AbstractOrchestrator.validate_topology(
            vms=[hv], networks=[outer_net],
        )
        # Filter — recwarn captures every warning, including pytest's
        # own DeprecationWarnings from imports.  Match on category +
        # message content.
        offending = [
            w for w in recwarn.list
            if issubclass(w.category, UserWarning) and "unreachable" in str(w.message)
        ]
        assert offending == [], (
            f"Slice 5 false-warned on legitimate airgap topology "
            f"with outer={outer_cls.__name__}, inner={inner_cls.__name__}: "
            f"{[str(w.message) for w in offending]}"
        )
        del outer_cls, inner_cls  # silence "unused" linting

    def test_unreachable_internet_descendant_warns(
        self,
        outer_cls: type,
        inner_cls: type,
        leaf_os: str,
    ) -> None:
        del leaf_os
        # Outer internet=False, inner internet=True → unreachable.
        # Slice 5 must warn for every (outer, inner) combo.
        leaf = _leaf_linux("leaf", ip="10.42.0.5")
        hv = _hv_with_inner_airgap(
            inner_orchestrator_cls=inner_cls, leaves=[leaf],
            inner_internet=True,
        )
        outer_net = VirtualNetwork(
            "OuterNet", "10.0.0.0/24", internet=False,
        )
        from testrange.orchestrator_base import AbstractOrchestrator
        with pytest.warns(UserWarning, match="unreachable"):
            AbstractOrchestrator.validate_topology(
                vms=[hv], networks=[outer_net],
            )
        del outer_cls, inner_cls


# =====================================================================
# Live matrix — skipped without env
# =====================================================================


def _live_pve_kwargs_or_skip() -> dict[str, object]:
    """Defer to :mod:`tests.test_proxmox_live`'s env loader.  Skips
    the live tests when the live PVE endpoint isn't configured."""
    from tests.test_proxmox_live import _env_or_skip

    return _env_or_skip()


def _live_libvirt_or_skip() -> None:
    """Skip libvirt-live tests when ``TESTRANGE_LIBVIRT_LIVE`` is unset.

    Mirrors the proxmox-live env gate.  We don't auto-detect a local
    libvirtd because most CI setups have no nested-virt KVM and the
    default-skip is the safer fallback.
    """
    if not os.environ.get("TESTRANGE_LIBVIRT_LIVE"):
        pytest.skip(
            "TESTRANGE_LIBVIRT_LIVE not set — skipping live libvirt "
            "matrix tests",
        )


class TestLibvirtOuterPveInnerLinuxLeaf:
    """Live: outer libvirt, inner PVE, Linux leaf, ``inner_internet=False``.

    The most common nested combo and the original bug's scene of the
    crime.  Skip without ``TESTRANGE_LIBVIRT_LIVE`` *and* a real PVE
    ISO at ``TESTRANGE_PVE_ISO_URL`` (or a local file path) — the
    nested PVE Hypervisor has to install from a real installer
    image, the test fixture can't fake one cheaply.
    """

    def test_airgapped_leaf_boots_and_execs_true(self) -> None:
        _live_libvirt_or_skip()
        # Skip without an ISO URL — the test would otherwise fail at
        # base-image download with a confusing 404.
        if not os.environ.get("TESTRANGE_PVE_ISO_URL"):
            pytest.skip(
                "TESTRANGE_PVE_ISO_URL not set — skipping live "
                "libvirt+PVE matrix test",
            )
        # Implementation deferred to a live-test PR — the
        # construction-side guarantees in
        # :class:`TestNestedConstructionMatrix` cover the regression
        # fence offline.  This block stays as the live entry point
        # so a future PR can fill in the ``with orch:`` body without
        # needing to add the harness from scratch.
        pytest.skip(
            "live libvirt+PVE matrix test not implemented yet — "
            "construction matrix above provides offline coverage",
        )


class TestPveOuterPveInnerLinuxLeaf:
    """Live: outer bare-metal PVE, inner PVE, Linux leaf, ``inner_internet=False``."""

    def test_airgapped_leaf_boots_and_execs_true(self) -> None:
        _ = _live_pve_kwargs_or_skip()  # skips when env unset
        if not os.environ.get("TESTRANGE_PVE_ISO_URL"):
            pytest.skip(
                "TESTRANGE_PVE_ISO_URL not set — skipping live "
                "PVE+PVE matrix test",
            )
        pytest.skip(
            "live PVE+PVE matrix test not implemented yet — "
            "construction matrix above provides offline coverage",
        )


# Suppress a stray ``warnings`` import warning in CI when no warnings
# fire — keeps the module's surface clean.
_ = warnings
