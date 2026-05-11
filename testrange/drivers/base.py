"""HypervisorDriver ABC — Phase 2 surface.

Phase 2 wires:
  - connect / disconnect
  - preflight (read-only)
  - compose_resource_name (deterministic naming)
  - compose_mac (stable MACs from plan+vm+nic_idx; PLAN.md decision)
  - network + pool CRUD
  - destroy(kind, backend_name) for cleanup dispatch

Phase 3 adds VM CRUD; Phase 4 adds disk operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from testrange.preflight import PreflightReport

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.networks.base import Network, Switch
    from testrange.plan import Plan


class HypervisorDriver(ABC):
    """Abstract base for hypervisor backends.

    Concrete drivers wrap a backend SDK (libvirt-python, proxmoxer,
    pyvmomi) and expose a uniform CRUD surface so the orchestrator never
    branches on driver type.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the underlying connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the underlying connection. Idempotent."""

    @abstractmethod
    def preflight(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager,
    ) -> PreflightReport:
        """Run read-only host + plan checks.

        MUST NOT have side effects — no mkdir, no define, no probes that
        leave artifacts. This invariant is what makes it safe to call
        before state.json exists.
        """

    @abstractmethod
    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        """Deterministic backend name for one resource.

        Same (run_id, kind, name) → same backend name, every time. Cleanup
        needs no other state to find and destroy orphans by name.
        """

    @abstractmethod
    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        """Deterministic MAC for one NIC.

        Same (plan_name, vm_name, nic_idx) → same MAC. Required because
        cloud-init's rendered network-config on the cached disk can
        match interfaces by MAC; auto-MACs would silently break
        networking on every cache-hit run.

        Each driver picks its own OUI (libvirt/KVM: 52:54:00:…; VMware:
        00:50:56:…; etc.).
        """

    # ---- network CRUD --------------------------------------------------

    @abstractmethod
    def create_network(self, network: Network, switch: Switch, backend_name: str) -> Any:
        """Create a network on the backend. Returns a backend-specific ref."""

    @abstractmethod
    def destroy_network(self, backend_name: str) -> None:
        """Remove a network by name. Idempotent (tolerate not-found)."""

    # ---- pool CRUD -----------------------------------------------------

    @abstractmethod
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        """Create a storage pool on the backend."""

    @abstractmethod
    def destroy_pool(self, backend_name: str) -> None:
        """Remove a pool by name. Idempotent."""

    # ---- generic dispatch ---------------------------------------------

    def destroy(self, kind: str, backend_name: str) -> None:
        """Destroy a resource by kind. Default dispatch covers Phase-2 kinds."""
        if kind == "network":
            self.destroy_network(backend_name)
        elif kind == "pool":
            self.destroy_pool(backend_name)
        else:
            raise NotImplementedError(f"destroy({kind!r}) not implemented yet")
