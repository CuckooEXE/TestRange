"""libvirt backend driver — assembled keystone (BACKEND-1).

``LibvirtHypervisor`` is the Plan-time entry selecting this backend;
``LibvirtDriver`` is the concrete :class:`HypervisorDriver`. The driver owns L2
(isolated host bridges via pyroute2), storage (libvirt storage pools + stream
volume I/O), VM lifecycle (domain XML), the QGA native transport, and the serial
build-result sink — each in its own concern module, mirroring the Proxmox split.

This is the vertical slice (BACKEND-1.1): connection, naming, and preflight are
live; the L2 / storage / VM / agent / snapshot surface raises a clear
``DriverError`` until its phase lands, so the class is instantiable and
``testrange describe`` works against a libvirt Plan today.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.libvirt import _naming
from testrange.drivers.libvirt._conn import LibvirtClient, LibvirtConn
from testrange.exceptions import DriverError
from testrange.networks.validate import validate_hypervisor_plan
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    mgmt_unsupported_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.networks.base import ManagedBuildSwitch, ManagedEgress, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class LibvirtHypervisor:
    """Plan-time config selecting the libvirt :class:`LibvirtDriver`.

    Only the topology (``networks``/``pools``/``vms``) is required; the rest
    defaults to a local system QEMU:

    - ``uri`` — the libvirt connect URI (``qemu:///system`` needs root). Remote
      ``qemu+ssh://`` connects, but host-local L2 can't reach a remote host
      (BACKEND-5), so the L2 surface assumes a local hypervisor.
    - ``backing_pool`` — name of a pre-existing libvirt **dir** storage pool the
      per-run pools carve into (static driver config, not provisioned here).
    - ``build_switch`` — user-declared build network (``Switch |
      ManagedBuildSwitch | None``, ADR-0014); ``None`` => isolated, no egress.
    """

    networks: Sequence[Switch] = ()
    pools: Sequence[StoragePool] = ()
    vms: Sequence[VMRecipe] = ()
    build_switch: Switch | ManagedBuildSwitch | None = None
    uri: str = "qemu:///system"
    backing_pool: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "networks", tuple(self.networks))
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "vms", tuple(self.vms))
        if not self.uri:
            raise ValueError("LibvirtHypervisor.uri must be a non-empty libvirt connect URI")
        # build_switch self-validates in Switch / ManagedBuildSwitch construction.
        validate_hypervisor_plan(self.networks, self.pools, self.vms)

    @property
    def all_switches(self) -> tuple[Switch, ...]:
        return tuple(self.networks)

    def conn(self) -> LibvirtConn:
        return LibvirtConn(libvirt_uri=self.uri, backing_pool=self.backing_pool)

    @property
    def driver_uri(self) -> str:
        """The teardown URI persisted into ``state.json`` (cleanup entry point)."""
        return self.conn().to_uri()


class LibvirtDriver(HypervisorDriver):
    """libvirt backend. Holds exactly one :class:`LibvirtClient`."""

    DRIVER_NAME = "LibvirtDriver"

    # Flipped on in BACKEND-1.2 when the managed-egress realization (a libvirt
    # NAT network + nwfilter fence) lands; until then preflight rejects a
    # ManagedBuildSwitch on this backend (managed_build_egress_findings).
    supports_managed_build_egress = False

    def __init__(self, conn: LibvirtConn, *, client: LibvirtClient | None = None) -> None:
        self._conn = conn
        # ``client`` is injectable so unit tests pass a duck-typed fake (exposing
        # ``raw`` + the libvirt calls the concern modules use) and never import
        # libvirt or touch a real hypervisor.
        self._client = client if client is not None else LibvirtClient(conn)

    # -- construction paths ------------------------------------------------

    @classmethod
    def from_hypervisor(cls, hyp: LibvirtHypervisor) -> LibvirtDriver:
        return cls(hyp.conn())

    @classmethod
    def from_uri(cls, uri: str) -> LibvirtDriver:
        return cls(LibvirtConn.from_uri(uri))

    @property
    def uri(self) -> str:
        return self._conn.to_uri()

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch
    ) -> PreflightReport:
        """Plan-side read-only checks.

        Runs the cross-driver findings (mgmt gating, managed-egress
        capability). Live libvirt-side checks (backing pool present, uplink
        bridge present) land with their phases (storage / L2).
        """
        del cache_manager, build_switch
        findings: list[PreflightFinding] = list(mgmt_unsupported_findings(plan))
        findings.extend(self.managed_build_egress_findings(plan))
        return PreflightReport(findings=tuple(findings))

    # -- naming (pure) -----------------------------------------------------

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    # -- not-yet-implemented surface (later BACKEND-1.x phases) ------------

    def _todo(self, method: str, phase: str) -> NoReturn:
        raise DriverError(f"LibvirtDriver.{method}: not implemented yet ({phase})")

    # L2 — BACKEND-1.2
    def create_switch(
        self, switch: Switch, backend_name: str, *, managed_egress: ManagedEgress | None = None
    ) -> str | None:
        self._todo("create_switch", "BACKEND-1.2")

    def destroy_switch(self, backend_name: str) -> None:
        self._todo("destroy_switch", "BACKEND-1.2")

    def create_network(
        self, network: Network, switch: Switch, backend_name: str, *, switch_backend_name: str
    ) -> Any:
        self._todo("create_network", "BACKEND-1.2")

    def destroy_network(self, backend_name: str) -> None:
        self._todo("destroy_network", "BACKEND-1.2")

    # Storage — BACKEND-1.3
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        self._todo("create_pool", "BACKEND-1.3")

    def destroy_pool(self, backend_name: str) -> None:
        self._todo("destroy_pool", "BACKEND-1.3")

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        self._todo("write_to_pool", "BACKEND-1.3")

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        self._todo("create_blank_volume", "BACKEND-1.3")

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        self._todo("resize_volume", "BACKEND-1.3")

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        self._todo("upload_to_pool", "BACKEND-1.3")

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        self._todo("download_from_pool", "BACKEND-1.3")

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        self._todo("delete_volume", "BACKEND-1.3")

    # VM lifecycle — BACKEND-1.4
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
        self._todo("create_vm", "BACKEND-1.4")

    def start_vm(self, backend_name: str) -> None:
        self._todo("start_vm", "BACKEND-1.4")

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        self._todo("shutdown_vm", "BACKEND-1.4")

    def destroy_vm(self, backend_name: str) -> None:
        self._todo("destroy_vm", "BACKEND-1.4")

    def get_vm_power_state(self, backend_name: str) -> str:
        self._todo("get_vm_power_state", "BACKEND-1.4")

    # Snapshots — BACKEND-1.6
    def create_snapshot(
        self,
        vm_backend_name: str,
        name: str,
        description: str = "",
        *,
        mem: bool = False,
    ) -> None:
        self._todo("create_snapshot", "BACKEND-1.6")

    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        self._todo("list_snapshots", "BACKEND-1.6")

    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        self._todo("delete_snapshot", "BACKEND-1.6")

    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        self._todo("restore_snapshot", "BACKEND-1.6")


register(
    hypervisor_cls=LibvirtHypervisor,
    driver_name=LibvirtDriver.DRIVER_NAME,
    from_hypervisor=LibvirtDriver.from_hypervisor,
    from_uri=LibvirtDriver.from_uri,
)


__all__ = ["LibvirtDriver", "LibvirtHypervisor"]
