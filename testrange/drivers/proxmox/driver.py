"""ProxmoxDriver — the Proxmox VE backend (proxmoxer REST + two byte-egress exceptions).

Defines the Plan-time :class:`ProxmoxHypervisor` data type and the
:class:`ProxmoxDriver` concrete the orchestrator drives. The driver is the
single place the full :class:`HypervisorDriver` ABC surface is assembled; it
holds one :class:`ProxmoxClient` and delegates every method to a focused
concern module:

- ``_client`` — transports: the proxmoxer REST session for everything PVE
  supports, plus the two sanctioned non-proxmoxer byte-egress channels
  (paramiko SFTP for ``download_from_pool``; a ``vncwebsocket`` for the serial
  build-result read — ADR-0008 §6, ADR-0012);
- ``_naming`` — pure deterministic resource names, MACs, and volume refs;
- ``_sdn`` — L2 fabric (per-Switch SDN vnet in a ``simple`` zone);
- ``_storage`` — pool + volume I/O (the "Option-2" stateless disk re-resolution);
- ``_vm`` — VM lifecycle (import-from OS disk, config-lock wait, NICs/MACs,
  start/shutdown/destroy/power-state) and snapshots;
- ``_guest`` — the QGA native guest agent (exec / read / write);
- ``_serial`` — the build-result sink (serial0 over termproxy→vncwebsocket).

The whole backend is live-validated piecewise (PVE-1…8) against PVE 9.x; the
end-to-end ``testrange run`` smoke is PVE-9.
"""

from __future__ import annotations

import secrets
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.proxmox import _guest, _naming, _sdn, _serial, _storage, _vm
from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn
from testrange.hypervisor import Hypervisor
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    mgmt_unsupported_findings,
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
class ProxmoxHypervisor(Hypervisor):
    """Topology-only scheme marker selecting the ``proxmox`` backend (CORE-19).

    Identical in shape to the generic :class:`~testrange.Hypervisor`; its only
    job is to assert *this topology MUST run on Proxmox VE* (e.g., a recipe
    relying on a PVE-specific CPU type or SDN feature) so preflight catches a
    mismatched ``--profile`` early. Connection (host/user/password/node/...)
    and build egress live on :class:`ProxmoxProfile`; the per-run SDN zone is
    still minted on the driver, never an author surface.
    """


class ProxmoxDriver(HypervisorDriver):
    """Proxmox VE backend. Holds exactly one :class:`ProxmoxClient`."""

    DRIVER_NAME = "ProxmoxDriver"

    def __init__(
        self,
        conn: ProxmoxConn,
        *,
        client: ProxmoxClient | None = None,
        uplinks: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        # Logical-uplink-name → host bridge (ADR-0016), from the profile. On PVE a
        # resolved uplink names an existing host bridge (e.g. vmbr0). Empty for a
        # teardown driver rebuilt from_uri (it never wires NICs / creates switches).
        self._uplinks: dict[str, str] = dict(uplinks or {})
        # ``client`` is injectable so unit tests pass a duck-typed fake (api /
        # node / storage / zone / wait_task / sftp_get) and never touch a real
        # PVE or import proxmoxer.
        self._client = client if client is not None else ProxmoxClient(conn)
        # Per-run SDN zone, minted once per driver instance (one driver == one
        # run via the profile path). 8-char alnum, leading letter — PVE's SDN-id
        # limit. TestRange owns the zone end-to-end (created in create_switch,
        # self-discovered + dropped in destroy_switch), so it is never an author
        # knob and never needs to be deterministic — a teardown driver rebuilt
        # from_uri reads the zone off the vnet rather than recomputing it.
        self._sdn_zone = f"tr{secrets.token_hex(3)}"
        # Composed network backend name → SDN vnet id, populated in
        # create_network. The orchestrator passes the *composed name* in
        # network_refs, but a PVE NIC attaches to the vnet id (an 8-char derived
        # id, not the composed name), so create_vm translates through this. The
        # uplink segment (an existing bridge like vmbr0) is not in the map and
        # passes through unchanged. In-process only: teardown never wires NICs.
        self._vnet_by_network: dict[str, str] = {}

    # -- construction paths ------------------------------------------------

    @classmethod
    def from_uri(cls, uri: str) -> ProxmoxDriver:
        return cls(ProxmoxConn.from_uri(uri))

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
        """Plan-side checks plus a live uplink-bridge existence check.

        Storage-side checks (pool min-capacity floor; the ``import`` content
        type the upload path needs) land with PVE-3 alongside ``_storage``.
        """
        del cache_manager
        switches = [*plan.hypervisor.all_switches]
        if build_switch is not None:
            switches.append(build_switch)
        findings: list[PreflightFinding] = list(mgmt_unsupported_findings(plan))
        findings.extend(unknown_uplink_findings(switches, self._uplinks))
        findings.extend(self._uplink_bridge_findings(plan, build_switch))
        findings.extend(self._import_content_findings())
        return PreflightReport(findings=tuple(findings))

    def _import_content_findings(self) -> tuple[PreflightFinding, ...]:
        """Require the backing storage to enable the ``import`` content type.

        ``upload_to_pool`` stages base/built images as ``import`` content so
        ``create_vm`` can ``import-from`` them; PVE only resolves a
        ``<storage>:import/…`` volid when ``import`` is in the storage's content
        list. A fresh ``dir`` store typically has ``iso`` but not ``import``.
        """
        storage = self._client.storage
        content = str(self._client.api.storage(storage).get().get("content", ""))
        if "import" in {c.strip() for c in content.split(",")}:
            return ()
        return (
            PreflightFinding(
                code="proxmox-import-content-missing",
                message=(
                    f"storage {storage!r} does not enable the 'import' content type "
                    "(needed to stage base/built disks for create_vm)"
                ),
                fix_hint=f"pvesm set {storage} --content <existing>,import",
            ),
        )

    def _uplink_bridge_findings(
        self, plan: Plan, build_switch: Switch | None
    ) -> tuple[PreflightFinding, ...]:
        """Verify every ``uplink+nat`` Switch resolves to an existing host bridge.

        On Proxmox the sidecar's ``eth1`` bridges to an existing host bridge to
        reach the out-of-band network for NAT egress — including the transient
        build Switch, which is why ``testrange run`` can auto-build here. The
        Switch's ``uplink`` is a logical name (ADR-0016) the profile maps to the
        bridge; here we resolve mapped names and check the bridge exists (an
        *unmapped* name is already flagged by ``unknown_uplink_findings``). A
        missing bridge would otherwise surface as an opaque create-time failure.
        """
        switches = list(plan.hypervisor.all_switches)
        if build_switch is not None:
            switches.append(build_switch)
        # (logical name, resolved bridge) for each nat+uplink switch whose name
        # the profile actually maps.
        wanted: set[tuple[str, str]] = {
            (sw.uplink, self._uplinks[sw.uplink])
            for sw in switches
            if sw.uplink
            and sw.uplink in self._uplinks
            and sw.sidecar is not None
            and sw.sidecar.nat
        }
        if not wanted:
            return ()
        bridges = {
            b["iface"] for b in self._client.api.nodes(self._client.node).network.get(type="bridge")
        }
        return tuple(
            PreflightFinding(
                code="proxmox-uplink-bridge-missing",
                message=(
                    f"uplink {name!r} maps to bridge {bridge!r}, which does not exist on node "
                    f"{self._client.node!r} (have: {sorted(bridges)})"
                ),
                fix_hint=(
                    "on Proxmox, an uplink must map (via the profile's [uplinks]) to an "
                    "existing host bridge with upstream connectivity (e.g. 'vmbr0', the one "
                    "carrying the default gateway)"
                ),
            )
            for name, bridge in sorted(wanted)
            if bridge not in bridges
        )

    # -- deterministic naming ----------------------------------------------

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(self._client.storage, pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    # -- switches & networks (driver owns L2; delegates to _sdn) -----------

    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        # Resolve the logical uplink name (ADR-0016) to the host bridge the
        # sidecar's eth1 rides; None when the switch declares no uplink.
        resolved_uplink = (
            self._uplinks[switch.uplink]
            if switch.uplink is not None and switch.uplink in self._uplinks
            else None
        )
        return _sdn.create_switch(
            self._client, self._sdn_zone, switch, backend_name, resolved_uplink=resolved_uplink
        )

    def destroy_switch(self, backend_name: str) -> None:
        _sdn.destroy_switch(self._client, backend_name)

    def create_network(
        self,
        network: Network,
        switch: Switch,
        backend_name: str,
        *,
        switch_backend_name: str,
    ) -> Any:
        vnet = _sdn.create_network(
            self._client, network, switch, backend_name, switch_backend_name=switch_backend_name
        )
        # Remember the composed name → vnet id so create_vm can wire NICs to the
        # real bridge (the orchestrator only keeps the composed name).
        self._vnet_by_network[backend_name] = vnet
        return vnet

    def destroy_network(self, backend_name: str) -> None:
        # Networks share their Switch's single vnet (see ``_sdn.create_network``),
        # so a Network owns no backend object of its own — the vnet is torn down
        # by ``destroy_switch``. Nothing to do here.
        del backend_name

    # -- pools & volumes (PVE-3; delegates to _storage) --------------------

    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        return _storage.create_pool(self._client, pool, backend_name)

    def destroy_pool(self, backend_name: str) -> None:
        _storage.destroy_pool(self._client, backend_name)

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        return _storage.write_to_pool(self._client, target_ref, data)

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.create_blank_volume(target_ref, size_gb)

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.resize_volume(target_ref, size_gb)

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        return _storage.upload_to_pool(self._client, target_ref, source_path)

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        return _storage.download_from_pool(self._client, vol_ref, dest_path)

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        _storage.delete_volume(self._client, vol_ref)

    # -- VM lifecycle (PVE-8; delegates to _vm) ----------------------------

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
        # Translate composed network names → SDN vnet ids for the NIC bridges;
        # the uplink segment (vmbr0) isn't in the map and passes through.
        resolved_refs = {
            name: self._vnet_by_network.get(backend, backend)
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

    # -- native guest agent (PVE-4; QGA via _guest) ------------------------

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        return _guest.make_execute(self._client, backend_name)

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        return _guest.make_read_file(self._client, backend_name)

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        return _guest.make_write_file(self._client, backend_name)

    # -- build-result sink (PVE-17; serial0 over websocket, delegates to _serial) --

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        return _serial.read_build_result_sink(self._client, backend_name)

    # -- snapshots (PVE-5; delegates to _vm) -------------------------------

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
    hypervisor_cls=ProxmoxHypervisor,
    driver_name=ProxmoxDriver.DRIVER_NAME,
    scheme="proxmox",
    from_uri=ProxmoxDriver.from_uri,
)


__all__ = ["ProxmoxDriver", "ProxmoxHypervisor"]
