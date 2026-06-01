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

import functools
import secrets
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.proxmox import _guest, _naming, _sdn, _serial, _storage, _vm
from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn
from testrange.exceptions import DriverError
from testrange.gateways import SSHJumpGateway
from testrange.hypervisor import Hypervisor
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    builder_origin_findings,
    unknown_uplink_findings,
    unsupported_firmware_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.gateways.base import GuestGateway
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import BuildNic, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec

_P = ParamSpec("_P")
_R = TypeVar("_R")


@functools.cache
def _pve_error_types() -> tuple[type[BaseException], ...]:
    """proxmoxer/requests exception families to translate at the driver boundary.

    Resolved lazily and tolerantly: the driver module imports with proxmoxer
    absent (the SDK is optional), and when it is absent no REST call can have
    run, so there is nothing to translate.
    """
    try:
        from proxmoxer.core import AuthenticationError, ResourceException
        from requests import RequestException
    except ImportError:  # pragma: no cover - proxmoxer present wherever REST runs
        return ()
    return (AuthenticationError, ResourceException, RequestException)


def _translates(
    method: Callable[Concatenate[ProxmoxDriver, _P], _R],
) -> Callable[Concatenate[ProxmoxDriver, _P], _R]:
    """Wrap a public driver method so a raw proxmoxer/``requests`` exception
    escaping the control plane surfaces as a :class:`DriverError` (H1 / PVE-39).

    The orchestrator's teardown and error handling key on ``DriverError``; an
    untranslated ``ResourceException`` (a 403, a transient 595) would slip past
    it and leave the backend dirty. Only the proxmoxer/``requests`` families are
    caught — ``DriverError`` (incl. ``GuestAgentError``) already conforms and is
    re-raised by virtue of not being in that set, and our own bugs (``KeyError``,
    ``TypeError``, …) still propagate as themselves rather than being masked.
    """

    @functools.wraps(method)
    def wrapper(self: ProxmoxDriver, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(self, *args, **kwargs)
        except _pve_error_types() as e:
            raise DriverError(f"PVE {method.__name__} failed: {e}") from e

    return cast("Callable[Concatenate[ProxmoxDriver, _P], _R]", wrapper)


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

    # Firmware this backend realizes (BUILD-1b): bios via SeaBIOS, uefi via OVMF
    # (`bios=ovmf` + an efidisk0 on q35) — see _vm.create_vm.
    SUPPORTED_FIRMWARES = frozenset({"bios", "uefi"})

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
        # Serializes the SDN mutation path and the vnet map. The run phase
        # provisions independent switches concurrently (ADR-0023), but PVE SDN is
        # cluster-global: every create_switch shares one ``_sdn_zone`` and ends in
        # ``PUT /cluster/sdn``, which commits the *entire* staged config and
        # spawns a reload. Without this lock concurrent workers double-create the
        # zone (check-then-act) and stomp each other's applies. The same lock
        # guards the ``_vnet_by_network`` write (create_network) / read
        # (create_vm). The slow disk/volume transfers take no lock, so they still
        # overlap — only the SDN control path serializes. Not re-entrant: nothing
        # here nests an acquire, and a plain Lock surfaces an accidental nest as a
        # deadlock in test rather than hiding it.
        self._state_lock = threading.Lock()
        # Serializes the storage-allocation critical section of create_vm across
        # concurrent run-phase workers (PVE-56). PVE guards every storage op
        # (import-from, blank/efidisk alloc, disk resize) behind a single
        # per-storage flock (/var/lock/pve-manager/pve-storage-<storage>) with a
        # bounded timeout, so N concurrent qmcreate import-froms against the one
        # 'local' store pile up on it and one fails ("cant lock file ... got
        # timeout"). This driver targets exactly one storage (client.storage), so
        # one Lock IS the per-storage lock. We hold it across the whole create
        # (POST → import-task wait → resize) because PVE serializes those imports
        # regardless: we lose no real parallelism — the imports could never run
        # concurrently — and trade a hard timeout for an orderly wait. Non-storage
        # parallelism is untouched: switches, snapshots, guest exec, and
        # start_vm/readiness take no storage lock. Distinct from _state_lock (the
        # SDN control path) and acquired without nesting it, so the two never
        # contend or deadlock.
        self._storage_import_lock = threading.Lock()

    @classmethod
    def from_uri(cls, uri: str) -> ProxmoxDriver:
        return cls(ProxmoxConn.from_uri(uri))

    @property
    def uri(self) -> str:
        return self._conn.to_uri()

    @_translates
    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    @_translates
    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch
    ) -> PreflightReport:
        """Plan-side checks plus a live uplink-bridge existence check.

        No ``mgmt_unsupported_findings``: Proxmox realizes ``Switch(mgmt=True)``
        as the host's ``.2`` adapter on the vnet (ADR-0009 B, ``_sdn``), so it is
        the "drops the gate" path other unrealized backends still keep.
        """
        del cache_manager
        # build_switch is always a concrete Switch here — the orchestrator runs
        # it through resolve_build_switch (synthesizing the default isolated
        # switch when the plan declares none), so there is no None to guard (H2).
        switches = [*plan.hypervisor.all_switches, build_switch]
        findings: list[PreflightFinding] = list(unknown_uplink_findings(switches, self._uplinks))
        findings.extend(builder_origin_findings(plan))
        findings.extend(
            unsupported_firmware_findings(
                plan, self.SUPPORTED_FIRMWARES, driver_name=self.DRIVER_NAME
            )
        )
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
        self, plan: Plan, build_switch: Switch
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
        switches = [*plan.hypervisor.all_switches, build_switch]
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

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(self._client.storage, pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    @_translates
    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        # Resolve the logical uplink name (ADR-0016) to the host bridge the
        # sidecar's eth1 rides; None when the switch declares no uplink. A
        # *declared but unmapped* uplink is a hard error here, not a silent drop
        # to None — otherwise a NAT switch would come up with no egress path and
        # fail opaquely at build time. preflight's unknown_uplink_findings flags
        # this earlier; this is the driver enforcing the invariant itself rather
        # than trusting preflight to have run.
        resolved_uplink: str | None = None
        if switch.uplink is not None:
            if switch.uplink not in self._uplinks:
                raise DriverError(
                    f"switch {switch.name!r} declares uplink {switch.uplink!r}, which the "
                    f"profile's [uplinks] map does not resolve (have: {sorted(self._uplinks)})"
                )
            resolved_uplink = self._uplinks[switch.uplink]
        # Serialize the whole SDN critical section (zone-ensure → vnet post →
        # cluster-wide apply) across concurrent switch workers — see _state_lock.
        with self._state_lock:
            return _sdn.create_switch(
                self._client, self._sdn_zone, switch, backend_name, resolved_uplink=resolved_uplink
            )

    @_translates
    def destroy_switch(self, backend_name: str) -> None:
        # destroy_switch has the same check-then-act + cluster apply as create;
        # hold the same lock so it is safe even if teardown ever parallelizes.
        with self._state_lock:
            _sdn.destroy_switch(self._client, backend_name)

    @_translates
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
        with self._state_lock:
            self._vnet_by_network[backend_name] = vnet
        return vnet

    def destroy_network(self, backend_name: str) -> None:
        # Networks share their Switch's single vnet (see ``_sdn.create_network``),
        # so a Network owns no backend object of its own — the vnet is torn down
        # by ``destroy_switch``. Nothing to do here.
        del backend_name

    @_translates
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        return _storage.create_pool(self._client, pool, backend_name)

    @_translates
    def destroy_pool(self, backend_name: str) -> None:
        _storage.destroy_pool(self._client, backend_name)

    @_translates
    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        return _storage.write_to_pool(self._client, target_ref, data)

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.create_blank_volume(target_ref, size_gb)

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.resize_volume(target_ref, size_gb)

    @_translates
    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        return _storage.upload_to_pool(self._client, target_ref, source_path)

    @_translates
    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        return _storage.download_from_pool(self._client, vol_ref, dest_path)

    @_translates
    def delete_volume(self, vol_ref: VolumeRef) -> None:
        _storage.delete_volume(self._client, vol_ref)

    @_translates
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
        build_nic: BuildNic | None = None,
        boot_media_ref: VolumeRef | None = None,
    ) -> Any:
        # Translate composed network names → SDN vnet ids for the NIC bridges;
        # the uplink segment (vmbr0) isn't in the map and passes through.
        with self._state_lock:
            resolved_refs = {
                name: self._vnet_by_network.get(backend, backend)
                for name, backend in network_refs.items()
            }
        # Serialize the per-storage import critical section (PVE-56) — see
        # _storage_import_lock. The _state_lock above is released first, so the
        # two never nest.
        with self._storage_import_lock:
            return _vm.create_vm(
                self._client,
                backend_name,
                spec,
                plan_name,
                os_disk_ref=os_disk_ref,
                seed_iso_ref=seed_iso_ref,
                network_refs=resolved_refs,
                data_disk_refs=data_disk_refs,
                build_nic=build_nic,
                boot_media_ref=boot_media_ref,
            )

    @_translates
    def start_vm(self, backend_name: str) -> None:
        _vm.start_vm(self._client, backend_name)

    @_translates
    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        _vm.shutdown_vm(self._client, backend_name, timeout=timeout)

    @_translates
    def destroy_vm(self, backend_name: str) -> None:
        _vm.destroy_vm(self._client, backend_name)

    @_translates
    def get_vm_power_state(self, backend_name: str) -> str:
        return _vm.get_vm_power_state(self._client, backend_name)

    def guest_gateway(self) -> GuestGateway:
        """Reach guests by SSH-jumping through the PVE host.

        The orchestrator runs off-box; guests live on isolated SDN vnets it
        cannot route to, but the PVE host can (it carries the mgmt ``.2`` for a
        ``mgmt=True`` switch — PVE-44/ADR-0009). So SSH transports tunnel through
        the host's SSH endpoint, reusing the same host credentials the SFTP
        byte-egress path already uses (``ssh_user``/``ssh_password``, derived
        from the API user/password when unset). QGA transports don't consult this
        — they ride the REST control plane.
        """
        return SSHJumpGateway(
            host=self._conn.host,
            username=self._conn.ssh_user,
            password=self._conn.ssh_password or self._conn.password or None,
        )

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        return _guest.make_execute(self._client, backend_name)

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        return _guest.make_read_file(self._client, backend_name)

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        return _guest.make_write_file(self._client, backend_name)

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        return _serial.read_build_result_sink(self._client, backend_name)

    @_translates
    def create_snapshot(
        self, vm_backend_name: str, name: str, description: str = "", *, mem: bool = False
    ) -> None:
        _vm.create_snapshot(self._client, vm_backend_name, name, description, mem=mem)

    @_translates
    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        return _vm.list_snapshots(self._client, vm_backend_name)

    @_translates
    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.delete_snapshot(self._client, vm_backend_name, name)

    @_translates
    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.restore_snapshot(self._client, vm_backend_name, name)


register(
    hypervisor_cls=ProxmoxHypervisor,
    driver_name=ProxmoxDriver.DRIVER_NAME,
    scheme="proxmox",
    from_uri=ProxmoxDriver.from_uri,
)


__all__ = ["ProxmoxDriver", "ProxmoxHypervisor"]
