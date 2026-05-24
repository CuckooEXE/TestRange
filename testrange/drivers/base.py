"""HypervisorDriver — abstract base for hypervisor backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NewType

from testrange.exceptions import DriverError
from testrange.preflight import PreflightReport

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
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
        build_switch: Switch,
    ) -> PreflightReport:
        """Read-only checks against the live backend.

        ``build_switch`` is the transient Switch the orchestrator will
        bring up for the build phase (sidecar-served DHCP+DNS+NAT, with
        the Hypervisor's ``build_uplink`` as the upstream NIC). Preflight
        includes it in CIDR-overlap checks so a colliding user Switch is
        caught here rather than blowing up at build time.
        """

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
    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        """Realize a Switch's L2 fabric on the backend.

        The driver owns *all* L2 topology — the orchestrator never names a
        bridge. How the fabric is realized is backend-specific:

        - libvirt: a host bridge (via pyroute2) that networks attach to
        - ESXi: a vSwitch; networks become port-groups on it
        - Proxmox: an SDN zone (or vmbr); networks become vnets
        - Hyper-V: a VMSwitch; networks become per-vNIC VLANs

        When ``switch.uplink`` is set and the switch's ``Sidecar`` has ``nat``,
        the driver also provisions an uplink-facing segment for the sidecar's
        second NIC and returns its
        backend network name; the orchestrator attaches the sidecar's
        ``eth1`` to it. Returns ``None`` when there is no uplink segment.
        ``destroy_switch`` tears the whole fabric down, including any uplink
        segment created here.
        """

    @abstractmethod
    def destroy_switch(self, backend_name: str) -> None:
        """Tear down a Switch's L2 fabric (and any uplink segment it owns)."""

    @abstractmethod
    def create_network(
        self,
        network: Network,
        switch: Switch,
        backend_name: str,
        *,
        switch_backend_name: str,
    ) -> Any:
        """Attach one Network (port-group) to an already-created Switch.

        ``switch_backend_name`` is the handle from the earlier
        ``create_switch`` call for ``switch``; the driver wires this network
        onto that fabric (ESXi port-group on the vSwitch, libvirt network in
        bridge mode against the switch's bridge, Proxmox vnet in the zone,
        Hyper-V VLAN on the VMSwitch).
        """

    @abstractmethod
    def destroy_network(self, backend_name: str) -> None: ...

    @abstractmethod
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        """Create a named storage namespace inside pre-existing backing storage.

        Not provisioning: the backing store (libvirt pool filesystem,
        Proxmox storage, ESXi datastore, Hyper-V volume/share) is static
        driver config. This carves a per-run namespace within it (a libvirt
        pool, a datastore subdirectory, a host directory). ``pool.size_gb``
        is a *minimum-capacity precondition* the driver verifies in
        ``preflight`` — not a quota it imposes here.
        """

    @abstractmethod
    def destroy_pool(self, backend_name: str) -> None: ...

    @abstractmethod
    def volume_suffix(self, kind: str) -> str:
        """File-extension suffix for a volume of ``kind`` on this backend.

        ``kind`` is one of the orchestrator's logical volume kinds
        (``build_disk``, ``run_disk``, ``data_disk``, ``base_image``,
        ``build_seed``). Drivers return the right extension for their
        on-disk format (e.g., ``.qcow2`` for libvirt disks, ``.iso`` for
        cloud-init seeds).
        """

    @abstractmethod
    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        """Write raw bytes as a new volume at ``target_ref``. Returns ``target_ref``.

        The caller pre-composes the target via ``compose_volume_ref(pool,
        name)``. Replace-if-exists: any pre-existing volume at the ref is
        deleted first.
        """

    @abstractmethod
    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        """Provision a blank, sized volume at ``target_ref``. Returns ``target_ref``.

        Used for data disks at build time (a guest formats and populates
        them during the build boot) and, later, for installer-based OS
        disks. The volume's content is undefined (zeroed / sparse) — the
        contract is only that it exists at ``size_gb``. Replace-if-exists.
        """

    @abstractmethod
    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        """Grow the volume at ``target_ref`` to ``size_gb``. Returns ``target_ref``.

        Used for the image-based OS disk before the build boot: the base
        image is uploaded onto the VM's own OS-disk ref, then grown to the
        declared ``OSDrive.size_gb`` so cloud-init's ``growpart``/``resize2fs``
        can expand the rootfs on first boot. ``size_gb`` must be ``>=`` the
        volume's current size; shrinking is not supported.
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
        after the build phase to ingest each built disk (OS + data) back into
        the host-side cache — the on-disk file may not be readable by the
        orchestrator process (different uid, remote hypervisor, ...).

        Invariant: the source volume must be self-contained (no backing
        chain). Every disk arrives by ``upload_to_pool`` (full content, no
        overlay) or ``create_blank_volume``, so this holds. ``dest_path``'s
        parent must already exist; the file is overwritten if present.
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
        data_disk_refs: Sequence[VolumeRef] = (),
    ) -> Any:
        """Define a VM on the backend.

        Args:
          backend_name:  Deterministic name for the VM on the backend
            (composed via ``compose_resource_name``).
          spec:          ``VMSpec`` from the Plan (CPU/memory/devices/NICs).
          plan_name:     User-facing Plan name (drivers that derive stable
            MACs from ``(plan_name, vm_name, nic_idx)`` use this).
          os_disk_ref:   Locator for the writable OS disk (pushed onto this
            VM's own ref via ``upload_to_pool``).
          seed_iso_ref:  Locator for the cloud-init seed ISO produced by an
            earlier ``write_to_pool`` call, or ``None`` for VMs that don't
            need a seed (run-phase VMs).
          network_refs:  ``{plan_network_name: backend_network_name}`` map
            so the driver can wire NICs declared in ``spec`` to the right
            backend network.
          data_disk_refs: Locators for the VM's ``HardDrive`` data disks, in
            spec order — attached alongside the OS disk. Empty for VMs with
            no data disks (the common case). Per ADR-0010 §4 a build VM boots
            with every writable disk attached so the install payload can
            populate it.
        """

    @abstractmethod
    def start_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None: ...

    @abstractmethod
    def destroy_vm(self, backend_name: str) -> None: ...

    @abstractmethod
    def get_vm_power_state(self, backend_name: str) -> str: ...

    # DHCP lease lookup is intentionally NOT a driver method: testrange's
    # per-Switch sidecar owns DHCP, so a lease lives in the sidecar's
    # dnsmasq lease file, not in anything the hypervisor manages. The
    # orchestrator reads it over the native guest-file transport below.

    # --- Native guest agent (optional capability) ---------------------
    # A backend with a native in-guest agent (QEMU Guest Agent, VMware
    # Tools, Hyper-V integration) overrides these to return VM-bound
    # callables and declares which ops it supports via
    # ``native_guest_capabilities``. They are non-abstract: the default for
    # a backend with no native agent is an empty capability set and a clean
    # DriverError, not a missing method.
    #
    # NOTE: backends whose guest channel requires per-call guest OS
    # credentials (VMware Tools, Hyper-V PowerShell Direct) will add an
    # optional ``credential`` keyword to these accessors when that driver
    # lands (see ADR-0008); QGA-style agents need none, so the parameter is
    # deliberately not introduced before a backend exercises it.

    def native_guest_capabilities(self) -> frozenset[str]:
        """The native-agent ops this backend supports.

        A subset of ``{"execute", "read_file", "write_file"}``. Preflight
        checks it against each VM's communicator so a missing op fails loud
        before run, not mid-run. Default: none.
        """
        return frozenset()

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        """A VM-bound callable that runs a command in the guest via the
        backend's native agent. Default: no native agent."""
        raise DriverError(f"{type(self).__name__}: no native guest agent")

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        """A VM-bound callable that reads a file from the guest via the
        backend's native agent. Default: no native agent."""
        raise DriverError(f"{type(self).__name__}: no native guest agent")

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        """A VM-bound callable that writes a file into the guest via the
        backend's native agent. Default: no native agent."""
        raise DriverError(f"{type(self).__name__}: no native guest agent")

    # --- Build-result sink (hypervisor capability, not agent-level) -------
    # The build phase keys success on a structured ``TESTRANGE-RESULT:``
    # record the builder writes to the guest's serial console; the
    # orchestrator reads it back host-side through this accessor. It is a
    # *hypervisor* capability — distinct from the native guest agent above —
    # because a guest may ship no QGA/VMware-Tools (OpenBSD, a bare installer)
    # yet still write to a 16550 UART, the most portable virtual device there
    # is. Absence of a native agent does not affect it; presence of one is no
    # substitute. The per-backend read mechanism differs (PVE termproxy ->
    # vncwebsocket, libvirt pty/file, ESXi datastore file), but it is serial
    # everywhere, so the builder emits to the console only and this hides the
    # host-side read.

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        """Open a live byte-stream of the build VM's serial console.

        Returns a generator of console ``bytes`` chunks the orchestrator tails
        for the ``TESTRANGE-RESULT:`` record. Two contract points let the
        orchestrator enforce its own build-timeout watchdog without being held
        hostage by a silent guest:

        - The generator MUST yield control periodically even when no new bytes
          are available; an empty ``b""`` chunk is the idiom for "nothing yet,
          check your deadline and call me again." A blocking transport honors
          this with a recv timeout; a file-backed sink polls and yields ``b""``
          between reads. Pacing is the sink's job — the orchestrator loops
          immediately on a heartbeat, so a tight ``b""`` loop busy-spins.
        - Iteration ends when the console closes — the build VM powered off or
          the transport hung up. A guest that powered off without emitting
          ``ok`` is a failure (crashed mid-provision).

        The orchestrator wraps the generator in ``contextlib.closing`` so a
        transport the driver opened (a Proxmox ``vncwebsocket``, a libvirt pty)
        is released via the generator's ``finally`` even when the loop breaks
        early on a record. Default: the backend exposes no serial sink and
        therefore cannot verify a build — a clean :class:`DriverError`, not a
        missing method, so a new backend that forgets to implement it fails
        loud at build time rather than silently caching an unverified disk.
        """
        raise DriverError(f"{type(self).__name__}: no build-result sink")

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

        Volume kinds (``build_disk``, ``build_seed``, ``data_disk``,
        ``run_disk``) require a ``pool_backend`` in ``metadata`` so the
        driver knows which pool to remove the volume from.
        """
        if kind in ("network", "build_network"):
            self.destroy_network(backend_name)
        elif kind in ("pool", "build_pool"):
            self.destroy_pool(backend_name)
        elif kind in ("vm", "build_vm", "sidecar_vm"):
            self.destroy_vm(backend_name)
        elif kind in (
            "build_disk",
            "build_seed",
            "run_disk",
            "data_disk",
            "base_image",
            "volume",
            "sidecar_disk",
            "sidecar_config",
        ):
            pool_backend = metadata.get("pool_backend")
            if not pool_backend:
                raise ValueError(
                    f"destroy({kind!r}): missing pool_backend metadata for volume kind"
                )
            self.delete_volume(self.compose_volume_ref(str(pool_backend), backend_name))
        elif kind in ("switch", "build_switch"):
            self.destroy_switch(backend_name)
        else:
            raise NotImplementedError(f"destroy({kind!r}) not implemented")
