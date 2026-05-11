"""HypervisorDriver ABC — v0 driver surface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.preflight import PreflightReport

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.networks.base import Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec


class HypervisorDriver(ABC):
    """Abstract base for hypervisor backends.

    Concrete drivers wrap a backend SDK (libvirt-python, proxmoxer,
    pyvmomi) and expose a uniform surface so the orchestrator never
    branches on driver type.
    """

    DRIVER_NAME: str = "HypervisorDriver"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def preflight(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager,
    ) -> PreflightReport: ...

    @abstractmethod
    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str: ...

    @abstractmethod
    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str: ...

    # ---- network CRUD --------------------------------------------------

    @abstractmethod
    def create_network(self, network: Network, switch: Switch, backend_name: str) -> Any: ...

    @abstractmethod
    def destroy_network(self, backend_name: str) -> None: ...

    # ---- pool CRUD -----------------------------------------------------

    @abstractmethod
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any: ...

    @abstractmethod
    def destroy_pool(self, backend_name: str) -> None: ...

    # ---- volume operations --------------------------------------------

    @abstractmethod
    def write_to_pool(self, pool_backend_name: str, filename: str, data: bytes) -> Path:
        """Write raw bytes as a file into the pool directory. Returns the path."""

    @abstractmethod
    def create_overlay_disk(
        self,
        pool_backend_name: str,
        vol_name: str,
        source_path: Path,
    ) -> Path:
        """Create a qcow2 overlay backed by ``source_path``. Returns the new disk path."""

    @abstractmethod
    def delete_volume(self, pool_backend_name: str, vol_name: str) -> None: ...

    # ---- VM CRUD -------------------------------------------------------

    @abstractmethod
    def create_vm(
        self,
        backend_name: str,
        spec: VMSpec,
        plan_name: str,
        *,
        os_disk_path: Path,
        seed_iso_path: Path | None,
        network_refs: dict[str, str],
    ) -> Any: ...

    @abstractmethod
    def start_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None: ...

    @abstractmethod
    def destroy_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def get_vm_power_state(self, backend_name: str) -> str: ...

    # ---- generic dispatch ---------------------------------------------

    def destroy(self, kind: str, backend_name: str) -> None:
        """Destroy a resource by kind (default dispatch)."""
        if kind in ("network", "install_network"):
            self.destroy_network(backend_name)
        elif kind == "pool":
            self.destroy_pool(backend_name)
        elif kind in ("vm", "install_vm"):
            self.destroy_vm(backend_name)
        else:
            raise NotImplementedError(f"destroy({kind!r}) not implemented")
