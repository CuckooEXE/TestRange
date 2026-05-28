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
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.proxmox import _guest, _naming, _sdn, _serial, _storage, _vm
from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn
from testrange.networks.validate import validate_hypervisor_plan
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    mgmt_unsupported_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import ManagedBuildSwitch, ManagedEgress, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class ProxmoxHypervisor:
    """Plan-time config selecting the :class:`ProxmoxDriver`.

    A test author gives the **host** (+ password) plus their networks/pools/vms::

        ProxmoxHypervisor(host="10.0.0.5", password="Target123!",
                          networks=[...], pools=[...], vms=[...])

    Everything operational defaults sanely, because authors care that their
    tests run, not where: ``user="root@pam"`` (PVE's default realm — a bare
    ``"root"`` is normalised to ``root@pam``); ``node=""`` auto-detects the
    host's single node; ``backing_storage="local"`` is PVE's near-universal
    default; the SDN zone isn't a field at all (TestRange mints one per run and
    tears it down). All stay as keyword overrides for the less-common cases (a
    multi-node cluster, an NFS store, a different realm).

    Build-time internet egress is **opt-in** (ADR-0014): set ``build_switch`` to
    a ``Switch`` (bring-your-own uplink + sidecar) or a ``ManagedBuildSwitch``
    (TestRange manufactures and fences the egress segment). Left unset, the build
    network is isolated (DHCP+DNS only) — a build that needs apt/pip must declare
    one.

    SSH (used only for ``download_from_pool``) reuses the API user/password by
    default, since ``root@pam`` is the host's system root.
    """

    host: str
    networks: Sequence[Switch] = ()
    pools: Sequence[StoragePool] = ()
    vms: Sequence[VMRecipe] = ()
    user: str = "root@pam"
    password: str = ""
    port: int = 8006
    # User-declared build switch (ADR-0014): a Switch (BYO uplink) or a
    # ManagedBuildSwitch (TestRange manufactures + fences the egress segment).
    # None => isolated build network, no internet egress. Replaces the former
    # build_uplink / build_uplink_addr knobs.
    build_switch: Switch | ManagedBuildSwitch | None = None
    node: str = ""  # "" => auto-detect the single node
    backing_storage: str = "local"
    verify_ssl: bool = False
    ssh_user: str | None = None  # default: API user's local part (root@pam -> root)
    ssh_password: str | None = None  # default: reuse the API password
    ssh_port: int = 22

    def __post_init__(self) -> None:
        object.__setattr__(self, "networks", tuple(self.networks))
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "vms", tuple(self.vms))
        if not self.host:
            raise ValueError("ProxmoxHypervisor.host must be a non-empty string (the PVE host/IP)")
        # build_switch self-validates in Switch / ManagedBuildSwitch construction.
        validate_hypervisor_plan(self.networks, self.pools, self.vms)

    @property
    def all_switches(self) -> tuple[Switch, ...]:
        return tuple(self.networks)

    def conn(self) -> ProxmoxConn:
        """The :class:`ProxmoxConn` this hypervisor reaches the backend with.

        SSH creds default to the API creds (``root@pam`` is system root).
        """
        # PVE authenticates against a realm; default to `pam` for a bare
        # username (the common `user="root"`). An explicit realm — `root@pam`,
        # `user@pve`, `user@ldap` — is preserved.
        user = self.user if "@" in self.user else f"{self.user}@pam"
        ssh_user = self.ssh_user or user.split("@", 1)[0]
        ssh_password = self.ssh_password if self.ssh_password is not None else self.password
        return ProxmoxConn(
            host=self.host,
            node=self.node,
            user=user,
            password=self.password,
            verify_ssl=self.verify_ssl,
            port=self.port,
            backing_storage=self.backing_storage,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_port=self.ssh_port,
        )

    @property
    def driver_uri(self) -> str:
        """The teardown URI the orchestrator persists into ``state.json``.

        It is an internal serialization (the ``proxmox://`` round-trip lives on
        :class:`ProxmoxConn`), not the author surface — it carries the resolved
        operational params (storage, ssh, node) cleanup needs, so a later
        ``cleanup`` rebuilds the driver via :meth:`ProxmoxDriver.from_uri` with
        the same backing store and SSH creds.
        """
        return self.conn().to_uri()


class ProxmoxDriver(HypervisorDriver):
    """Proxmox VE backend. Holds exactly one :class:`ProxmoxClient`."""

    DRIVER_NAME = "ProxmoxDriver"

    # Proxmox realizes ManagedBuildSwitch via an SDN snat=1 vnet + VNet firewall
    # fence (ADR-0014; see _sdn._create_egress_vnet / _fence_egress_vnet).
    supports_managed_build_egress = True

    def __init__(self, conn: ProxmoxConn, *, client: ProxmoxClient | None = None) -> None:
        self._conn = conn
        # ``client`` is injectable so unit tests pass a duck-typed fake (api /
        # node / storage / zone / wait_task / sftp_get) and never touch a real
        # PVE or import proxmoxer.
        self._client = client if client is not None else ProxmoxClient(conn)
        # Per-run SDN zone, minted once per driver instance (one driver == one
        # run via from_hypervisor). 8-char alnum, leading letter — PVE's SDN-id
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
    def from_hypervisor(cls, hyp: ProxmoxHypervisor) -> ProxmoxDriver:
        return cls(hyp.conn())

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
        findings: list[PreflightFinding] = list(mgmt_unsupported_findings(plan))
        findings.extend(self.managed_build_egress_findings(plan))
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
        """Verify every ``uplink+nat`` Switch names an existing host bridge.

        On Proxmox the sidecar's ``eth1`` bridges to an existing host bridge
        (``switch.uplink``) to reach the upstream LAN for NAT egress — including
        the transient build Switch (resolved from the Hypervisor's
        ``build_switch``), which is why ``testrange run`` can auto-build here. A
        typo or a missing bridge would otherwise surface as an opaque
        create-time failure.
        """
        wanted: set[str] = {
            sw.uplink
            for sw in plan.hypervisor.all_switches
            if sw.uplink and sw.sidecar is not None and sw.sidecar.nat
        }
        if (
            build_switch is not None
            and build_switch.uplink
            and build_switch.sidecar is not None
            and build_switch.sidecar.nat
        ):
            wanted.add(build_switch.uplink)
        if not wanted:
            return ()
        bridges = {
            b["iface"] for b in self._client.api.nodes(self._client.node).network.get(type="bridge")
        }
        return tuple(
            PreflightFinding(
                code="proxmox-uplink-bridge-missing",
                message=(
                    f"uplink {name!r} is not an existing bridge on node "
                    f"{self._client.node!r} (have: {sorted(bridges)})"
                ),
                fix_hint=(
                    "on Proxmox, a Switch/ManagedBuildSwitch uplink names an existing host "
                    "bridge with upstream connectivity (e.g. 'vmbr0', the one carrying the "
                    "default gateway)"
                ),
            )
            for name in sorted(wanted)
            if name not in bridges
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

    def create_switch(
        self, switch: Switch, backend_name: str, *, managed_egress: ManagedEgress | None = None
    ) -> str | None:
        return _sdn.create_switch(
            self._client, self._sdn_zone, switch, backend_name, managed_egress=managed_egress
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
    from_hypervisor=ProxmoxDriver.from_hypervisor,
    from_uri=ProxmoxDriver.from_uri,
)


__all__ = ["ProxmoxDriver", "ProxmoxHypervisor"]
