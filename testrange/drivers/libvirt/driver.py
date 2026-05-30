"""libvirt backend driver — assembled keystone (BACKEND-1).

``LibvirtHypervisor`` is the Plan-time entry selecting this backend;
``LibvirtDriver`` is the concrete :class:`HypervisorDriver`. The driver is the
single place the full ABC surface is assembled; it holds exactly one
:class:`LibvirtClient` and delegates every method to a focused concern module,
mirroring the Proxmox split:

- ``_conn`` — the libvirt-python connection (``qemu:///system``);
- ``_naming`` — pure deterministic resource names, MACs, and volume refs;
- ``_net`` — L2 fabric via the libvirt network API (isolated segment + the
  resolved host-bridge uplink segment for a NAT sidecar);
- ``_storage`` — per-run dir pools + stream volume I/O;
- ``_vm`` — VM lifecycle (domain XML) and snapshots;
- ``_guest`` — the QGA native guest agent (exec / read / write) over
  ``libvirt_qemu.qemuAgentCommand``;
- ``_serial`` — the build-result sink (the ``<serial type='unix'>`` socket).

The slices land incrementally (BACKEND-1.A…1.D); a concern not yet implemented
raises a clear, phase-tagged :class:`DriverError`, so the class is instantiable
and ``testrange describe`` works against a libvirt Plan today.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.libvirt import _guest, _naming, _net, _serial, _storage, _vm
from testrange.drivers.libvirt._conn import LibvirtClient, LibvirtConn
from testrange.hypervisor import Hypervisor
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    unknown_uplink_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class LibvirtHypervisor(Hypervisor):
    """Topology-only scheme marker selecting the ``libvirt`` backend (CORE-19).

    Identical in shape to the generic :class:`~testrange.Hypervisor`; its only
    job is to assert *this topology MUST run against libvirt* (e.g., a recipe
    that relies on a libvirt-specific NIC model) so preflight catches a
    mismatched ``--profile`` early. Connection (``uri``) and named uplinks live
    on :class:`LibvirtProfile`.
    """


class LibvirtDriver(HypervisorDriver):
    """libvirt backend. Holds exactly one :class:`LibvirtClient`."""

    DRIVER_NAME = "LibvirtDriver"

    def __init__(
        self,
        conn: LibvirtConn,
        *,
        client: LibvirtClient | None = None,
        uplinks: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        # ``client`` is injectable so unit tests pass a duck-typed fake (exposing
        # ``raw`` + the libvirt calls the concern modules use) and never import
        # libvirt or touch a real hypervisor.
        self._client = client if client is not None else LibvirtClient(conn)
        # Logical-uplink-name → host bridge (ADR-0016), from the profile. A
        # resolved uplink names an existing host network (e.g. tr-egress). Empty
        # for a from_uri teardown driver (it never wires NICs / creates switches).
        self._uplinks: dict[str, str] = dict(uplinks or {})
        # Composed network backend name → the switch's libvirt network name,
        # populated in create_network. The orchestrator passes the composed name
        # in network_refs, but a NIC attaches to the switch's shared libvirt
        # network (all Networks on a Switch share one bridge), so create_vm
        # translates through this. The uplink network (e.g. tr-egress) isn't in
        # the map and passes through unchanged. In-process only: teardown never
        # wires NICs.
        self._libvirt_net_by_network: dict[str, str] = {}

    @classmethod
    def from_uri(cls, uri: str) -> LibvirtDriver:
        return cls(LibvirtConn.from_uri(uri))

    @property
    def uri(self) -> str:
        return self._conn.to_uri()

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch
    ) -> PreflightReport:
        """Plan-side read-only checks (the named-uplink resolution check, ADR-0016).

        libvirt **supports** ``Switch(mgmt=True)`` — the daemon puts the host's
        ``.2`` adapter on the bridge — so it does NOT call
        ``mgmt_unsupported_findings`` (the gate other backends still apply until
        they realize mgmt; ADR-0009). Live libvirt-side checks (the resolved
        uplink network present on the host) land with the L2 phase.
        """
        del cache_manager
        switches = [*plan.hypervisor.all_switches]
        if build_switch is not None:
            switches.append(build_switch)
        findings: list[PreflightFinding] = list(unknown_uplink_findings(switches, self._uplinks))
        return PreflightReport(findings=tuple(findings))

    def _resolve_uplink(self, switch: Switch) -> str | None:
        """The host bridge ``switch.uplink`` maps to, or ``None`` if it declares none.

        A declared-but-unmapped name is left to surface in the concern module
        (preflight already flags it via ``unknown_uplink_findings``).
        """
        if switch.uplink is None:
            return None
        return self._uplinks.get(switch.uplink)

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        return _net.create_switch(
            self._client, switch, backend_name, resolved_uplink=self._resolve_uplink(switch)
        )

    def destroy_switch(self, backend_name: str) -> None:
        _net.destroy_switch(self._client, backend_name)

    def create_network(
        self, network: Network, switch: Switch, backend_name: str, *, switch_backend_name: str
    ) -> Any:
        libvirt_net = _net.create_network(
            self._client, network, switch, backend_name, switch_backend_name=switch_backend_name
        )
        # Remember composed name → switch's libvirt network so create_vm wires
        # NICs onto the real (shared) network, not the composed alias.
        self._libvirt_net_by_network[backend_name] = libvirt_net
        return libvirt_net

    def destroy_network(self, backend_name: str) -> None:
        _net.destroy_network(self._client, backend_name)

    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        return _storage.create_pool(self._client, pool, backend_name)

    def destroy_pool(self, backend_name: str) -> None:
        _storage.destroy_pool(self._client, backend_name)

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        return _storage.write_to_pool(self._client, target_ref, data)

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.create_blank_volume(self._client, target_ref, size_gb)

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.resize_volume(self._client, target_ref, size_gb)

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        return _storage.upload_to_pool(self._client, target_ref, source_path)

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        return _storage.download_from_pool(self._client, vol_ref, dest_path)

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        _storage.delete_volume(self._client, vol_ref)

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
        # Translate composed network names → the switch's shared libvirt network;
        # the uplink network (e.g. tr-egress) isn't in the map and passes through.
        resolved_refs = {
            name: self._libvirt_net_by_network.get(backend, backend)
            for name, backend in network_refs.items()
        }
        return _vm.create_vm(
            self._client,
            backend_name,
            spec,
            plan_name,
            os_disk_ref=os_disk_ref,
            seed_iso_ref=seed_iso_ref,
            network_refs=resolved_refs,
            data_disk_refs=data_disk_refs,
        )

    def start_vm(self, backend_name: str) -> None:
        _vm.start_vm(self._client, backend_name)

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        _vm.shutdown_vm(self._client, backend_name, timeout=timeout)

    def destroy_vm(self, backend_name: str) -> None:
        _vm.destroy_vm(self._client, backend_name)

    def get_vm_power_state(self, backend_name: str) -> str:
        return _vm.get_vm_power_state(self._client, backend_name)

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        return _guest.make_execute(self._client, backend_name)

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        return _guest.make_read_file(self._client, backend_name)

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        return _guest.make_write_file(self._client, backend_name)

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        return _serial.read_build_result_sink(self._client, backend_name)

    def create_snapshot(
        self, vm_backend_name: str, name: str, description: str = "", *, mem: bool = False
    ) -> None:
        _vm.create_snapshot(self._client, vm_backend_name, name, description, mem=mem)

    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        return _vm.list_snapshots(self._client, vm_backend_name)

    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.delete_snapshot(self._client, vm_backend_name, name)

    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.restore_snapshot(self._client, vm_backend_name, name)


register(
    hypervisor_cls=LibvirtHypervisor,
    driver_name=LibvirtDriver.DRIVER_NAME,
    scheme="libvirt",
    from_uri=LibvirtDriver.from_uri,
)


__all__ = ["LibvirtDriver", "LibvirtHypervisor"]
