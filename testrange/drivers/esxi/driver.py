"""ESXiDriver — the standalone-ESXi backend (pyVmomi SOAP + datastore /folder).

Defines the Plan-time :class:`ESXiHypervisor` marker and the :class:`ESXiDriver`
concrete the orchestrator drives. The driver is the single place the full
:class:`HypervisorDriver` ABC surface is assembled; it holds one
:class:`EsxiClient` and delegates every method to a focused concern module:

- ``_client`` — transports: the pyVmomi ``ServiceInstance`` for the control
  plane, plus the sanctioned datastore ``/folder`` HTTPS byte channel;
- ``_naming`` — pure deterministic names, MACs (VMware manual range), volume refs;
- ``_net`` — L2 fabric (standard vSwitch + portgroup);
- ``_storage`` — datastore pool + volume I/O (qcow2↔vmdk at the boundary, CORE-2);
- ``_vm`` — VM lifecycle (CreateVM_Task, power, destroy) and snapshots;
- ``_guest`` — the VMware Tools native guest agent (exec / read / write);
- ``_serial`` — the build-result sink (datastore-file-backed serial port).

Standalone host only (ADR-0025): standard vSwitch + portgroup, no DVS/vCenter.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

from testrange.drivers import _diskconvert
from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.esxi import _guest, _naming, _net, _serial, _storage, _vm
from testrange.drivers.esxi._client import EsxiClient, EsxiConn
from testrange.exceptions import DriverError
from testrange.gateways import SSHJumpGateway
from testrange.hypervisor import Hypervisor
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    builder_origin_findings,
    preflight_switches,
    unknown_uplink_findings,
    unsupported_firmware_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.credentials.base import Credential
    from testrange.devices.pool.base import StoragePool
    from testrange.gateways.base import GuestGateway
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import BuildNic, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec

_P = ParamSpec("_P")
_R = TypeVar("_R")


@functools.cache
def _esxi_error_types() -> tuple[type[BaseException], ...]:
    """pyVmomi / requests exception families to translate at the driver boundary.

    Resolved lazily and tolerantly: the driver module imports with pyvmomi
    absent (the SDK is optional), and when it is absent no SOAP call can have
    run, so there is nothing to translate.
    """
    types: list[type[BaseException]] = []
    try:
        from pyVmomi import vmodl

        types.append(vmodl.MethodFault)
    except ImportError:  # pragma: no cover - pyvmomi present wherever SOAP runs
        pass
    try:
        from requests import RequestException

        types.append(RequestException)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def _translates(
    method: Callable[Concatenate[ESXiDriver, _P], _R],
) -> Callable[Concatenate[ESXiDriver, _P], _R]:
    """Wrap a public driver method so a raw pyVmomi/``requests`` fault escaping
    the control plane surfaces as a :class:`DriverError`.

    The orchestrator's teardown and error handling key on ``DriverError``; an
    untranslated ``vmodl.MethodFault`` (a permission fault, a transient SOAP
    error) would slip past it and leave the backend dirty. ``DriverError`` (incl.
    ``GuestAgentError``) already conforms and is re-raised by virtue of not being
    in that set, and our own bugs (``KeyError``, ``TypeError``, …) still
    propagate as themselves rather than being masked.
    """

    @functools.wraps(method)
    def wrapper(self: ESXiDriver, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(self, *args, **kwargs)
        except _esxi_error_types() as e:
            raise DriverError(f"ESXi {method.__name__} failed: {e}") from e

    return cast("Callable[Concatenate[ESXiDriver, _P], _R]", wrapper)


@dataclass(frozen=True)
class ESXiHypervisor(Hypervisor):
    """Topology-only scheme marker selecting the ``esxi`` backend (CORE-19).

    Identical in shape to the generic :class:`~testrange.Hypervisor`; its only
    job is to assert *this topology MUST run on standalone ESXi* so preflight
    catches a mismatched ``--profile`` early. Connection (host/user/password/
    datastore/…) lives on :class:`ESXiProfile`, never an author surface.
    """


class ESXiDriver(HypervisorDriver):
    """Standalone ESXi backend. Holds exactly one :class:`EsxiClient`."""

    DRIVER_NAME = "ESXiDriver"

    # Firmware this backend realizes (BUILD-1b). bios is certified; uefi is
    # accepted-but-unvalidated (gated in preflight — ESXI-9).
    SUPPORTED_FIRMWARES = frozenset({"bios", "uefi"})

    def __init__(
        self,
        conn: EsxiConn,
        *,
        client: EsxiClient | None = None,
        uplinks: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        # Logical-uplink-name → physical vmnic (ADR-0016), from the profile. On
        # ESXi a resolved uplink names a free pNIC (e.g. vmnic1) the build/NAT
        # vSwitch enslaves. Empty for a teardown driver rebuilt from_uri.
        self._uplinks: dict[str, str] = dict(uplinks or {})
        # ``client`` is injectable so unit tests pass a duck-typed fake and never
        # touch a real host or import pyvmomi.
        self._client = client if client is not None else EsxiClient(conn)
        # Serializes host-global L2 mutation (AddVirtualSwitch/AddPortGroup) and
        # the vSwitch→network name map across concurrent run-phase workers
        # (ADR-0023). The host network system is a single mutable object; two
        # concurrent reconfigures race. Slow disk transfers take no such lock.
        self._state_lock = threading.Lock()
        # Composed switch backend name → realized vSwitch name; composed network
        # backend name → portgroup name. Populated in create_switch/create_network
        # so create_vm can wire a NIC to the right portgroup. In-process only.
        self._vswitch_by_switch: dict[str, str] = {}
        self._portgroup_by_network: dict[str, str] = {}

    @classmethod
    def from_uri(cls, uri: str) -> ESXiDriver:
        return cls(EsxiConn.from_uri(uri))

    @property
    def uri(self) -> str:
        return self._conn.to_uri()

    @_translates
    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    # -- pure naming (ESXI-7) ---------------------------------------------

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(self._conn.datastore, pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    # -- preflight (ESXI-9) -----------------------------------------------

    @_translates
    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch | None
    ) -> PreflightReport:
        """Plan-side checks + live host checks (pNIC / datastore / CIDR / qemu-img).

        ESXi **supports** ``Switch(mgmt=True)`` (the host's ``.2`` VMkernel NIC,
        ``_net``), so it does not call ``mgmt_unsupported_findings`` — it is a
        backend that realizes mgmt. uefi is in ``SUPPORTED_FIRMWARES``
        (accepted-but-unvalidated; bios is certified), so the firmware check
        does not flag it; the caveat is documentation, not a blocker.

        ``build_switch`` is ``None`` for a cache-only run (``require_cache``, e.g.
        a nested inner run): it never realizes its build switch, so the live pNIC
        check must not validate the build switch's uplink — which, for a nested
        inner run, carries the *outer* backend's uplink vocabulary (a libvirt
        bridge name, not a vmnic). ``preflight_switches`` drops it (CORE-65).
        """
        del cache_manager
        switches = preflight_switches(plan, build_switch)
        findings: list[PreflightFinding] = list(unknown_uplink_findings(switches, self._uplinks))
        findings.extend(builder_origin_findings(plan))
        findings.extend(
            unsupported_firmware_findings(
                plan, self.SUPPORTED_FIRMWARES, driver_name=self.DRIVER_NAME
            )
        )
        findings.extend(self._uplink_pnic_findings(switches))
        findings.extend(self._datastore_capacity_findings(plan))
        findings.extend(self._cidr_overlap_findings(switches))
        findings.extend(self._qemu_img_findings(plan))
        return PreflightReport(findings=tuple(findings))

    def _uplink_pnic_findings(self, switches: list[Switch]) -> tuple[PreflightFinding, ...]:
        """Each mapped uplink must resolve to a physical NIC present on the host."""
        wanted = {
            (sw.uplink, self._uplinks[sw.uplink])
            for sw in switches
            if sw.uplink and sw.uplink in self._uplinks
        }
        if not wanted:
            return ()
        pnics = {p.device for p in self._client.host.config.network.pnic}
        return tuple(
            PreflightFinding(
                code="esxi-uplink-pnic-missing",
                message=(
                    f"uplink {name!r} maps to physical NIC {pnic!r}, which does not exist "
                    f"on the host (have: {sorted(pnics)})"
                ),
                fix_hint="map the uplink to a free vmnic that exists on the ESXi host",
            )
            for name, pnic in sorted(wanted)
            if pnic not in pnics
        )

    def _datastore_capacity_findings(self, plan: Plan) -> tuple[PreflightFinding, ...]:
        """The datastore's free space must cover the declared pools' minimum sizes."""
        needed_gb = sum(p.size_gb for p in plan.hypervisor.pools)
        free_gb = self._client.datastore.summary.freeSpace / (1024**3)
        if free_gb >= needed_gb:
            return ()
        return (
            PreflightFinding(
                code="esxi-datastore-capacity",
                message=(
                    f"datastore {self._client.datastore_name!r} has {free_gb:.1f} GiB free, "
                    f"below the declared pools' minimum {needed_gb} GiB"
                ),
                fix_hint="free datastore space or lower the StoragePool size_gb minimums",
            ),
        )

    def _cidr_overlap_findings(self, switches: list[Switch]) -> tuple[PreflightFinding, ...]:
        """No two Switch subnets (incl. the transient build switch) may overlap."""
        out: list[PreflightFinding] = []
        for i, a in enumerate(switches):
            for b in switches[i + 1 :]:
                if a.network.overlaps(b.network):
                    out.append(
                        PreflightFinding(
                            code="esxi-cidr-overlap",
                            message=(
                                f"switch {a.name!r} ({a.network}) overlaps switch "
                                f"{b.name!r} ({b.network})"
                            ),
                            fix_hint="give each Switch (and the build switch) a disjoint cidr",
                        )
                    )
        return tuple(out)

    def _qemu_img_findings(self, plan: Plan) -> tuple[PreflightFinding, ...]:
        """qemu-img must be present for image-origin builds (qcow2->vmdk, CORE-2).

        Needed when any VM has an OS-disk base image or any switch carries a
        sidecar (the sidecar is itself an image-origin build). Installer-origin
        builds land a blank VMFS disk and need no conversion.
        """
        needs_convert = any(
            vm.builder.os_disk_base() is not None for vm in plan.hypervisor.vms
        ) or any(sw.needs_sidecar for sw in plan.hypervisor.all_switches)
        if not needs_convert or _diskconvert.qemu_img_path() is not None:
            return ()
        return (
            PreflightFinding(
                code="esxi-qemu-img-missing",
                message=(
                    "qemu-img is not on PATH, but this plan has image-origin builds that "
                    "convert qcow2->vmdk at the ESXi boundary (CORE-2)"
                ),
                fix_hint="install QEMU tools on the orchestrator host (e.g. apt install qemu-utils)",
            ),
        )

    # -- L2 fabric (ESXI-2) -----------------------------------------------

    @_translates
    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        # Resolve the logical uplink name (ADR-0016) to a physical NIC; None when
        # the switch declares no uplink. A *declared but unmapped* uplink is a
        # hard error here, not a silent drop — preflight (ESXI-9) flags it first,
        # but the driver enforces the invariant itself rather than trusting it ran.
        resolved_uplink: str | None = None
        if switch.uplink is not None:
            if switch.uplink not in self._uplinks:
                raise DriverError(
                    f"switch {switch.name!r} declares uplink {switch.uplink!r}, which the "
                    f"profile's [uplinks] map does not resolve (have: {sorted(self._uplinks)})"
                )
            resolved_uplink = self._uplinks[switch.uplink]
        # Serialize host-global network-system mutation across concurrent switch
        # workers (ADR-0023): AddVirtualSwitch/AddPortGroup/AddVirtualNic all
        # reconfigure the one host network system, and the shared uplink vSwitch
        # is created check-then-act.
        with self._state_lock:
            up_pg = _net.create_switch(
                self._client, switch, backend_name, resolved_uplink=resolved_uplink
            )
            self._vswitch_by_switch[backend_name] = _naming.vswitch_name(backend_name)
        return up_pg

    @_translates
    def destroy_switch(self, backend_name: str) -> None:
        with self._state_lock:
            _net.destroy_switch(self._client, backend_name)

    @_translates
    def create_network(
        self, network: Network, switch: Switch, backend_name: str, *, switch_backend_name: str
    ) -> Any:
        with self._state_lock:
            pg = _net.create_network(
                self._client,
                network,
                switch,
                backend_name,
                switch_backend_name=switch_backend_name,
            )
            self._portgroup_by_network[backend_name] = pg
        return pg

    @_translates
    def destroy_network(self, backend_name: str) -> None:
        with self._state_lock:
            _net.destroy_network(self._client, backend_name)

    # -- datastore pool + volumes (ESXI-3) --------------------------------

    @_translates
    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        return _storage.create_pool(self._client, pool, backend_name)

    @_translates
    def destroy_pool(self, backend_name: str) -> None:
        _storage.destroy_pool(self._client, backend_name)

    @_translates
    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        return _storage.write_to_pool(self._client, target_ref, data)

    @_translates
    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.create_blank_volume(self._client, target_ref, size_gb)

    @_translates
    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.resize_volume(self._client, target_ref, size_gb)

    @_translates
    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        return _storage.upload_to_pool(self._client, target_ref, source_path)

    @_translates
    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        return _storage.download_from_pool(self._client, vol_ref, dest_path)

    @_translates
    def delete_volume(self, vol_ref: VolumeRef) -> None:
        _storage.delete_volume(self._client, vol_ref)

    # -- VM lifecycle (ESXI-4) --------------------------------------------

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
        return _vm.create_vm(
            self._client,
            backend_name,
            spec,
            plan_name,
            os_disk_ref=os_disk_ref,
            seed_iso_ref=seed_iso_ref,
            network_refs=network_refs,
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

    # -- native guest agent: VMware Tools (ESXI-5) ------------------------

    def native_guest_execute(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestExec:
        return _guest.make_execute(self._client, backend_name, credential)

    def native_guest_read_file(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestReadFile:
        return _guest.make_read_file(self._client, backend_name, credential)

    def native_guest_write_file(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestWriteFile:
        return _guest.make_write_file(self._client, backend_name, credential)

    def guest_gateway(self) -> GuestGateway:
        """Reach guests by SSH-jumping through the ESXi host (PROXY-1 analog).

        The orchestrator runs off-box; guests sit on isolated portgroups it
        cannot route to, but the ESXi host can — it carries the mgmt ``.2``
        VMkernel NIC for a ``mgmt=True`` switch (``_net``). So ``SSHCommunicator``
        transports tunnel through the host's SSH endpoint with the root
        credentials (the same the API uses). VMware Tools transports don't consult
        this — they ride the SOAP control plane. Requires SSH enabled on the host
        with TCP forwarding allowed.
        """
        return SSHJumpGateway(
            host=self._conn.host,
            username=self._conn.user,
            password=self._conn.password or None,
        )

    # -- build-result sink (ESXI-8) ---------------------------------------

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        return _serial.read_build_result_sink(self._client, backend_name)

    # -- snapshots (ESXI-6) -----------------------------------------------

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
    hypervisor_cls=ESXiHypervisor,
    driver_name=ESXiDriver.DRIVER_NAME,
    scheme="esxi",
    from_uri=ESXiDriver.from_uri,
)


__all__ = ["ESXiDriver", "ESXiHypervisor"]
