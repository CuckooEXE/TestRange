"""HypervisorDriver — abstract base for hypervisor backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, NewType

from testrange.preflight import PreflightReport

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.networks.base import Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec


VolumeRef = NewType("VolumeRef", str)
"""Opaque hypervisor-side locator for a volume.

A string handle that identifies a volume on the hypervisor backend; the
orchestrator never inspects it. Each driver picks its own concrete form:

- libvirt: filesystem path on libvirtd's host
  (``/var/lib/libvirt/images/testrange/<pool>/<name>.qcow2``)
- ESXi (future): ``[datastore1] folder/foo.vmdk``
- Proxmox (future): ``local-lvm:vm-100-disk-0``

Using ``NewType`` instead of plain ``str`` lets mypy distinguish a
locator from any other string at function boundaries — e.g., a vol_name
(``"web.qcow2"``) is not a VolumeRef and won't be accepted where one is
expected.
"""


class HypervisorDriver(ABC):
    """Abstract base for hypervisor backends.

    Concrete drivers wrap a backend SDK (libvirt-python, proxmoxer,
    pyvmomi) and expose a uniform surface so the orchestrator never
    branches on driver type.

    Locator types
    -------------
    The ABC distinguishes orchestrator-host paths from hypervisor-side
    locators at the type level:

    - ``Path`` always means **orchestrator-host filesystem path**
      (e.g., a cache file the orchestrator opens directly).
    - ``VolumeRef`` always means **hypervisor-side opaque locator** for a
      volume. The orchestrator never inspects it; it just shuttles it
      between driver calls. See ``VolumeRef`` for per-driver formats.

    Two methods cross the host boundary:

    - ``upload_to_pool(source_path=Path, ...) -> VolumeRef``: read a
      local file, hand back a hypervisor-side locator.
    - ``download_from_pool(..., dest_path=Path) -> Path``: write the
      volume's bytes into a local file.
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

    @abstractmethod
    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        """Deterministic ``VolumeRef`` for ``(pool, vol_name)`` on this backend.

        Pure: same inputs → same ref. Lets callers that work in
        ``(pool, vol_name)`` space (e.g., the state-driven cleanup walker)
        produce a ref to feed into ref-taking driver methods.
        """

    @abstractmethod
    def create_network(self, network: Network, switch: Switch, backend_name: str) -> Any: ...

    @abstractmethod
    def destroy_network(self, backend_name: str) -> None: ...

    @abstractmethod
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any: ...

    @abstractmethod
    def destroy_pool(self, backend_name: str) -> None: ...

    @abstractmethod
    def volume_suffix(self, kind: str) -> str:
        """File-extension suffix for a volume of ``kind`` on this backend.

        ``kind`` is one of the orchestrator's logical volume kinds
        (``install_disk``, ``run_disk``, ``base_image``, ``install_seed``).
        Drivers return the right extension for their on-disk format
        (e.g., ``.qcow2`` for libvirt disks, ``.iso`` for cloud-init seeds).
        """

    @abstractmethod
    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        """Write raw bytes as a new volume at ``target_ref``. Returns ``target_ref``.

        The caller pre-composes the target via ``compose_volume_ref(pool,
        name)``. Replace-if-exists: any pre-existing volume at the ref is
        deleted first.
        """

    @abstractmethod
    def create_disk_from_base(
        self,
        target_ref: VolumeRef,
        source_ref: VolumeRef,
    ) -> VolumeRef:
        """Create a self-contained writable disk at ``target_ref``, initialized from ``source_ref``.

        The new disk is a full, independent copy of the source — writes go
        to the new disk and the source is untouched. This is the universal
        primitive across hypervisor backends (libvirt full clone, VMware
        full clone, Proxmox ``qm clone``, etc.). Returns ``target_ref``.
        """

    @abstractmethod
    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        """Upload bytes from ``source_path`` into the pool at ``target_ref``.

        Boundary crossing: ``source_path`` is an **orchestrator-host** file
        (typically a cache entry). Returns ``target_ref``. Idempotent — if a
        volume already exists at the ref, returns it without re-uploading.
        """

    @abstractmethod
    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        """Download a pool volume's bytes to ``dest_path`` on the orchestrator host.

        Boundary crossing: ``dest_path`` is an **orchestrator-host** file
        path; returns the same. Symmetric inverse of ``upload_to_pool``. Used
        after the install phase to ingest the post-install OS disk back into
        the host-side cache — the on-disk file may not be readable by the
        orchestrator process (different uid, remote hypervisor, ...).

        Invariant: the source volume must be self-contained (no backing
        chain). The orchestrator only ever uses ``create_disk_from_base``
        (full copies), so this holds. ``dest_path``'s parent must already
        exist; the file is overwritten if present.
        """

    @abstractmethod
    def delete_volume(self, vol_ref: VolumeRef) -> None: ...

    @abstractmethod
    def create_vm(
        self,
        backend_name: str,
        spec: VMSpec,
        plan_name: str,
        *,
        os_disk_ref: VolumeRef,
        seed_iso_ref: VolumeRef | None,
        network_refs: dict[str, str],
    ) -> Any:
        """Define a VM on the backend.

        Args:
          backend_name:  Deterministic name for the VM on the backend
            (composed via ``compose_resource_name``).
          spec:          ``VMSpec`` from the Plan (CPU/memory/devices/NICs).
          plan_name:     User-facing Plan name (drivers that derive stable
            MACs from ``(plan_name, vm_name, nic_idx)`` use this).
          os_disk_ref:   Locator for the writable OS disk produced by an
            earlier ``create_disk_from_base`` call.
          seed_iso_ref:  Locator for the cloud-init seed ISO produced by an
            earlier ``write_to_pool`` call, or ``None`` for VMs that don't
            need a seed (run-phase VMs).
          network_refs:  ``{plan_network_name: backend_network_name}`` map
            so the driver can wire NICs declared in ``spec`` to the right
            backend network.
        """

    @abstractmethod
    def start_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None: ...

    @abstractmethod
    def destroy_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def get_vm_power_state(self, backend_name: str) -> str: ...

    @abstractmethod
    def get_lease_ip(self, network_backend_name: str, mac: str) -> str | None:
        """Look up an IP leased to ``mac`` on ``network_backend_name``. ``None`` if not yet leased."""

    @abstractmethod
    def create_snapshot(
        self,
        vm_backend_name: str,
        name: str,
        description: str = "",
        *,
        mem: bool = False,
    ) -> None:
        """Snapshot the VM under ``name``.

        ``description`` is freeform text the backend stores alongside the
        snapshot. ``mem=True`` requests a memory-included snapshot
        (suspend-style — restores running RAM state); ``mem=False`` is
        disk-only. Drivers that don't support memory snapshots MUST raise
        :class:`DriverError` when ``mem=True``.

        Raises :class:`DriverError` if a snapshot with ``name`` already
        exists on this VM.
        """

    @abstractmethod
    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        """Return the names of all snapshots on this VM, oldest-first."""

    @abstractmethod
    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        """Delete the named snapshot. No-op if ``name`` doesn't exist."""

    @abstractmethod
    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        """Revert the VM to the named snapshot.

        Disk-only snapshots leave the VM in ``shutoff`` after revert; memory
        snapshots restore the running state. Raises :class:`DriverError` if
        the snapshot doesn't exist.
        """

    def destroy(self, kind: str, backend_name: str, **metadata: Any) -> None:
        """Destroy a resource by kind (default dispatch).

        Volume kinds (``install_disk``, ``install_seed``, ``run_disk``)
        require a ``pool_backend`` in ``metadata`` so the driver knows
        which pool to remove the volume from.
        """
        if kind in ("network", "install_network"):
            self.destroy_network(backend_name)
        elif kind == "pool":
            self.destroy_pool(backend_name)
        elif kind in ("vm", "install_vm"):
            self.destroy_vm(backend_name)
        elif kind in ("install_disk", "install_seed", "run_disk", "base_image", "volume"):
            pool_backend = metadata.get("pool_backend")
            if not pool_backend:
                raise ValueError(
                    f"destroy({kind!r}): missing pool_backend metadata for volume kind"
                )
            self.delete_volume(self.compose_volume_ref(str(pool_backend), backend_name))
        else:
            raise NotImplementedError(f"destroy({kind!r}) not implemented")
