"""Libvirt driver.

Exports the ``LibvirtHypervisor`` Plan-time data type and the
``LibvirtDriver`` runtime. ``libvirt-python`` is imported lazily inside
methods so the rest of the package is importable on hosts without
``libvirt-dev`` installed.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import ipaddress
import json
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from testrange._log import get_logger
from testrange.devices.pool.base import StoragePool
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.exceptions import DriverError, GuestAgentError
from testrange.guest_io import ExecResult
from testrange.networks._addressing_consts import (
    SIDECAR_CACHE_NAME,
)
from testrange.networks.base import Network, Switch
from testrange.networks.validate import validate_addressing
from testrange.preflight import PreflightFinding, PreflightReport
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.plan import Plan

_log = get_logger(__name__)

# Locally-administered OUI for KVM/QEMU guests.
_KVM_OUI = "52:54:00"


@dataclass(frozen=True)
class LibvirtHypervisor:
    """Top-level Plan entry: a libvirt host with its declared topology.

    Driver class is inferred at orchestrator-construction time:
    ``LibvirtHypervisor -> LibvirtDriver(uri=connection)``.

    ``install_uplink`` is the physical NIC the install-phase sidecar uses
    for upstream egress (the install Switch needs internet for apt/pip).
    Required when at least one VM needs install (cache miss); preflight
    surfaces the missing kwarg as a finding rather than failing at
    construction (cache state is not known yet at Plan-time).
    """

    connection: str
    networks: tuple[Switch, ...]
    pools: tuple[StoragePool, ...]
    vms: tuple[VMRecipe, ...]
    install_uplink: str | None

    def __init__(
        self,
        *,
        connection: str,
        networks: Sequence[Switch] = (),
        pools: Sequence[StoragePool] = (),
        vms: Sequence[VMRecipe] = (),
        install_uplink: str | None = None,
    ) -> None:
        if not connection:
            raise ValueError("LibvirtHypervisor.connection must be a non-empty string")
        if install_uplink is not None and not install_uplink:
            raise ValueError(
                "LibvirtHypervisor.install_uplink must be a non-empty string or None"
            )
        switches = tuple(networks)
        ps = tuple(pools)
        rs = tuple(vms)

        net_names = {n.name for s in switches for n in s.networks}
        pool_names = {p.name for p in ps}
        vm_names = [r.name for r in rs]
        dup_vms = {n for n in vm_names if vm_names.count(n) > 1}
        if dup_vms:
            raise ValueError(f"LibvirtHypervisor.vms has duplicate names: {sorted(dup_vms)}")
        all_nets = [n.name for s in switches for n in s.networks]
        dup_nets = {n for n in all_nets if all_nets.count(n) > 1}
        if dup_nets:
            raise ValueError(f"LibvirtHypervisor networks have duplicate names: {sorted(dup_nets)}")

        for r in rs:
            for nic in r.spec.nics:
                if nic.network not in net_names:
                    raise ValueError(
                        f"VM {r.name!r} references unknown network {nic.network!r}; "
                        f"declared networks: {sorted(net_names)}"
                    )
            if r.spec.os_drive.pool not in pool_names:
                raise ValueError(
                    f"VM {r.name!r} OSDrive references unknown pool "
                    f"{r.spec.os_drive.pool!r}; declared pools: {sorted(pool_names)}"
                )
            for d in r.spec.data_drives:
                if d.pool not in pool_names:
                    raise ValueError(
                        f"VM {r.name!r} HardDrive references unknown pool "
                        f"{d.pool!r}; declared pools: {sorted(pool_names)}"
                    )

        # Plan-wide addressing validation (subnet membership, reserved-slot
        # collision, DHCP-pool collision, duplicates, dhcp=False + no static).
        # Accumulates every problem into one ValueError.
        validate_addressing(switches, rs)

        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "networks", switches)
        object.__setattr__(self, "pools", ps)
        object.__setattr__(self, "vms", rs)
        object.__setattr__(self, "install_uplink", install_uplink)

    @property
    def all_networks(self) -> tuple[Network, ...]:
        return tuple(n for s in self.networks for n in s.networks)

    @property
    def all_switches(self) -> tuple[Switch, ...]:
        return self.networks


def _import_libvirt() -> Any:
    """Lazy import. Raises DriverError with a useful hint if libvirt-python is missing."""
    try:
        import libvirt
    except ImportError as e:
        raise DriverError(
            "libvirt-python is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return libvirt


def _import_pyroute2() -> Any:
    """Lazy import of ``pyroute2`` (netlink for bridge management).

    Only needed by ``LibvirtDriver.create_bridge`` / ``destroy_bridge``;
    plans that never set ``Switch.uplink`` and have no ``mgmt`` switches
    never reach this.
    """
    try:
        import pyroute2
    except ImportError as e:
        raise DriverError(
            "pyroute2 is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return pyroute2


def _import_libvirt_qemu() -> Any:
    """Lazy import of ``libvirt_qemu`` (the QEMU-specific libvirt module).

    ``libvirt_qemu`` ships inside the ``libvirt-python`` package, so a
    missing import is the same dependency gap as :func:`_import_libvirt`.
    """
    try:
        import libvirt_qemu
    except ImportError as e:
        raise DriverError(
            "libvirt-python is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return libvirt_qemu


def _short(s: str, n: int) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def _default_pool_root(uri: str) -> Path:
    """Pick a pool dir libvirtd + libvirt-qemu can both reach.

    System mode runs libvirt-qemu under its own uid and can't traverse into
    a user's home, so dir-type pools have to live in a shared location. The
    reliable signal across local and remote (``qemu+ssh://``, ``qemu+tcp://``)
    URIs is the trailing ``/system`` vs ``/session``.
    """
    if uri.rstrip("/").endswith("/system"):
        return Path("/var/lib/libvirt/images/testrange")
    return Path.home() / ".local" / "share" / "testrange" / "pools"


class LibvirtDriver(HypervisorDriver):
    """libvirt-backed HypervisorDriver.

    Methods that touch the backend lazy-import ``libvirt`` so the rest of
    the package is usable on hosts without libvirt-dev.
    """

    DRIVER_NAME = "LibvirtDriver"

    def __init__(self, *, uri: str, pool_root: Path | None = None) -> None:
        self.uri = uri
        self._conn: Any | None = None
        self.pool_root = pool_root or _default_pool_root(uri)

    def connect(self) -> None:
        if self._conn is not None:
            return
        libvirt = _import_libvirt()
        _log.info("libvirt.open(%r)", self.uri)
        self._conn = libvirt.open(self.uri)
        if self._conn is None:
            raise DriverError(f"libvirt.open({self.uri!r}) returned None")

    def disconnect(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception as e:
            _log.warning("libvirt close failed: %s", e)
        self._conn = None

    @property
    def conn(self) -> Any:
        if self._conn is None:
            raise DriverError("LibvirtDriver not connected; call connect() first")
        return self._conn

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        """Deterministic libvirt-safe name.

        libvirt requires resource names to match ``[A-Za-z0-9_.+-]+``.
        We use ``tr_<kind>_<run_id8>_<name>`` and sanitize.
        """
        run_short = _short(run_id, 8)
        safe_name = "".join(c if c.isalnum() or c in "_.+-" else "_" for c in name)
        return f"tr_{kind}_{run_short}_{safe_name}"

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        """Stable MAC from (plan_name, vm_name, nic_idx) under the KVM OUI."""
        seed = f"{plan_name}/{vm_name}/{nic_idx}"
        h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return f"{_KVM_OUI}:{h[0:2]}:{h[2:4]}:{h[4:6]}"

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        """A libvirt VolumeRef is the volume's full filesystem path on libvirtd's host."""
        return VolumeRef(str(self._pool_dir(pool_backend_name) / vol_name))

    def preflight(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager,
        install_switch: Switch,
    ) -> PreflightReport:
        findings: list[PreflightFinding] = []

        hyp = plan.hypervisor
        if not isinstance(hyp, LibvirtHypervisor):
            findings.append(
                PreflightFinding(
                    severity="error",
                    code="bad_hypervisor",
                    message=(
                        f"LibvirtDriver expects a LibvirtHypervisor, got {type(hyp).__name__}"
                    ),
                )
            )
            return PreflightReport(findings=tuple(findings))

        findings.extend(_collect_subnet_findings(hyp, install_switch))
        findings.extend(_collect_cache_findings(hyp, cache_manager))
        findings.extend(_collect_sidecar_findings(hyp, cache_manager))
        findings.extend(self._collect_pool_root_findings())
        findings.extend(self._collect_uplink_findings(hyp, install_switch))

        return PreflightReport(findings=tuple(findings))

    def _collect_uplink_findings(
        self, hyp: LibvirtHypervisor, install_switch: Switch
    ) -> list[PreflightFinding]:
        """Verify each Switch with a bridge resolves to a usable physical NIC.

        Triggered by any Switch with `needs_bridge` (uplink or mgmt) plus the
        install Switch (which always has an uplink when install_uplink is set).

        Checks, in order:
        - **Remote URI** — pyroute2 talks LOCAL netlink only.
        - **NIC exists** in /sys/class/net.
        - **NIC is free** (not already enslaved).
        - **install_uplink unset** when at least one VM needs install (cache miss).
        """
        findings: list[PreflightFinding] = []
        uplinked = [s for s in hyp.networks if s.uplink is not None]
        if install_switch.uplink is not None:
            uplinked = [*uplinked, install_switch]

        is_remote = "://" in self.uri and not self.uri.startswith("qemu:///")
        if is_remote and (uplinked or any(s.needs_bridge for s in hyp.networks)):
            for sw in uplinked or [s for s in hyp.networks if s.needs_bridge]:
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="remote_uplink_unsupported",
                        message=(
                            f"switch {sw.name!r}: testrange's bridge management "
                            f"uses pyroute2 (LOCAL netlink only); cannot apply on "
                            f"remote URI {self.uri!r}"
                        ),
                        fix_hint=(
                            "use a local libvirt URI (qemu:///system / qemu:///session) "
                            "or manage bridges out-of-band on the remote host"
                        ),
                    )
                )
            return findings

        for sw in uplinked:
            assert sw.uplink is not None
            sysfs = Path("/sys/class/net") / sw.uplink
            if not sysfs.exists():
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="uplink_nic_not_found",
                        message=(
                            f"switch {sw.name!r}: uplink NIC {sw.uplink!r} not "
                            f"found ({sysfs} missing)"
                        ),
                        fix_hint=f"check that {sw.uplink!r} is the right interface name",
                    )
                )
                continue
            master = sysfs / "master"
            if master.is_symlink():
                master_name = master.resolve().name
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="uplink_nic_enslaved",
                        message=(
                            f"switch {sw.name!r}: uplink {sw.uplink!r} is already "
                            f"enslaved to bridge {master_name!r}"
                        ),
                        fix_hint=(
                            f"release with `ip link set {sw.uplink} nomaster`, "
                            f"or pick a different uplink"
                        ),
                    )
                )
        return findings

    def _collect_pool_root_findings(self) -> list[PreflightFinding]:
        # System mode: libvirt-qemu owns /var/lib/libvirt/images and creates the
        # per-pool subdir via sp.build(0). The Python process (user UID) usually
        # can't write here, so don't try to mkdir from preflight — leave that to
        # pool build, which will surface a real failure with libvirtd context.
        if self.uri.rstrip("/").endswith("/system"):
            return []
        try:
            self.pool_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return [
                PreflightFinding(
                    severity="error",
                    code="pool_root_unwritable",
                    message=f"cannot create pool root {self.pool_root}: {e}",
                    fix_hint=f"chmod/chown {self.pool_root.parent}, or pass pool_root= to LibvirtDriver",
                )
            ]
        return []

    def create_network(
        self,
        network: Network,
        switch: Switch,
        backend_name: str,
        *,
        bridge_name: str | None = None,
    ) -> Any:
        _import_libvirt()
        xml = _render_network_xml(network, switch, backend_name, bridge_name=bridge_name)
        _log.info("define network %s (%s)", backend_name, switch.cidr)
        net = self.conn.networkDefineXML(xml)
        net.create()
        return net

    def compose_bridge_name(self, run_id: str, switch_name: str) -> str:
        """Deterministic bridge name: ``tr-<10-hex-sha256>`` (15-char IFNAMSIZ)."""
        return f"tr-{_short(f'{run_id}/{switch_name}', 10)}"

    def create_bridge(
        self,
        uplink: str,
        bridge_name: str,
        *,
        mgmt_cidr: str | None = None,
    ) -> None:
        """Create the host bridge, enslave the named NIC, optionally set mgmt IP.

        Idempotent. Requires CAP_NET_ADMIN (typically root). Local-machine
        only — pyroute2 manipulates LOCAL netlink. Preflight catches the
        remote-URI case before we get here.
        """
        pyroute2 = _import_pyroute2()
        with pyroute2.IPRoute() as ipr:
            try:
                uplink_idxs = ipr.link_lookup(ifname=uplink)
            except OSError as e:
                raise DriverError(f"uplink {uplink!r}: lookup failed: {e}") from e
            if not uplink_idxs:
                raise DriverError(f"uplink {uplink!r}: no such interface on this host")
            uplink_idx = uplink_idxs[0]

            existing = ipr.link_lookup(ifname=bridge_name)
            if existing:
                bridge_idx = existing[0]
                _log.info("bridge %s already present (reusing)", bridge_name)
            else:
                _log.info("create bridge %s, enslave %s", bridge_name, uplink)
                try:
                    ipr.link("add", ifname=bridge_name, kind="bridge")
                except pyroute2.NetlinkError as e:
                    _wrap_netlink_error(e, f"create bridge {bridge_name!r}")
                bridge_idx = ipr.link_lookup(ifname=bridge_name)[0]

            try:
                ipr.link("set", index=uplink_idx, state="up")
                ipr.link("set", index=uplink_idx, master=bridge_idx)
                ipr.link("set", index=bridge_idx, state="up")
            except pyroute2.NetlinkError as e:
                _wrap_netlink_error(e, f"bring up bridge {bridge_name!r}")

            if mgmt_cidr:
                address, _, prefix_str = mgmt_cidr.partition("/")
                if not prefix_str:
                    raise DriverError(
                        f"mgmt_cidr {mgmt_cidr!r} must be in CIDR form (e.g. 10.0.0.2/24)"
                    )
                try:
                    ipr.addr(
                        "add", index=bridge_idx, address=address, prefixlen=int(prefix_str)
                    )
                except pyroute2.NetlinkError as e:
                    if "File exists" not in str(e):
                        _wrap_netlink_error(
                            e, f"assign {mgmt_cidr!r} to bridge {bridge_name!r}"
                        )

    def create_isolated_bridge(self, bridge_name: str, *, mgmt_cidr: str | None = None) -> None:
        """Create an isolated host bridge with no enslaved NIC.

        Used for switches that need testrange-managed bridge semantics
        (mgmt-IP, or NAT topology's switch-side bridge) but no physical
        uplink. Idempotent.
        """
        pyroute2 = _import_pyroute2()
        with pyroute2.IPRoute() as ipr:
            existing = ipr.link_lookup(ifname=bridge_name)
            if existing:
                bridge_idx = existing[0]
                _log.info("bridge %s already present (reusing)", bridge_name)
            else:
                _log.info("create isolated bridge %s", bridge_name)
                try:
                    ipr.link("add", ifname=bridge_name, kind="bridge")
                except pyroute2.NetlinkError as e:
                    _wrap_netlink_error(e, f"create bridge {bridge_name!r}")
                bridge_idx = ipr.link_lookup(ifname=bridge_name)[0]
            try:
                ipr.link("set", index=bridge_idx, state="up")
            except pyroute2.NetlinkError as e:
                _wrap_netlink_error(e, f"bring up bridge {bridge_name!r}")
            if mgmt_cidr:
                address, _, prefix_str = mgmt_cidr.partition("/")
                try:
                    ipr.addr(
                        "add", index=bridge_idx, address=address, prefixlen=int(prefix_str)
                    )
                except pyroute2.NetlinkError as e:
                    if "File exists" not in str(e):
                        _wrap_netlink_error(
                            e, f"assign {mgmt_cidr!r} to bridge {bridge_name!r}"
                        )

    def destroy_bridge(self, bridge_name: str) -> None:
        """Remove the host bridge. Idempotent.

        Kernel auto-releases enslaved interfaces and any assigned
        addresses when the bridge is removed.
        """
        pyroute2 = _import_pyroute2()
        with pyroute2.IPRoute() as ipr:
            idxs = ipr.link_lookup(ifname=bridge_name)
            if not idxs:
                _log.info("bridge %s already absent", bridge_name)
                return
            try:
                ipr.link("del", index=idxs[0])
            except pyroute2.NetlinkError as e:
                _wrap_netlink_error(e, f"delete bridge {bridge_name!r}")

    def destroy_network(self, backend_name: str) -> None:
        libvirt = _import_libvirt()
        try:
            net = self.conn.networkLookupByName(backend_name)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower() or "no network" in str(e).lower():
                _log.info("network %s already absent", backend_name)
                return
            raise
        try:
            if net.isActive():
                net.destroy()
        except Exception as e:
            _log.warning("network %s destroy failed (will still undefine): %s", backend_name, e)
        try:
            net.undefine()
        except Exception as e:
            _log.warning("network %s undefine failed: %s", backend_name, e)

    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        path = self.pool_root / backend_name
        xml = _render_pool_xml(backend_name, path)
        _log.info("define pool %s (%s)", backend_name, path)
        sp = self.conn.storagePoolDefineXML(xml)
        sp.setAutostart(True)
        # build() creates the target dir under libvirtd's ownership (libvirt-qemu
        # in system mode). Doing it from the Python process would fail when
        # pool_root lives somewhere only libvirtd can write (e.g. /var/lib/libvirt).
        sp.build(0)
        sp.create()
        return sp

    def destroy_pool(self, backend_name: str) -> None:
        libvirt = _import_libvirt()
        try:
            sp = self.conn.storagePoolLookupByName(backend_name)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower() or "no storage pool" in str(e).lower():
                _log.info("pool %s already absent", backend_name)
                return
            raise
        # Sweep any leftover volumes before the dir-rmdir. The LIFO state
        # walker deletes orchestrator-tracked volumes already, but external
        # snapshots and similar can drop files into the pool dir that
        # libvirt's volume index doesn't know about. refresh() re-scans the
        # dir so listVolumes() picks them up.
        try:
            sp.refresh(0)
        except Exception as e:
            _log.debug("pool %s refresh (non-fatal): %s", backend_name, e)
        try:
            for vol_name in sp.listVolumes():
                try:
                    sp.storageVolLookupByName(vol_name).delete(0)
                except Exception as e:
                    _log.warning(
                        "pool %s: leftover vol %s delete failed: %s", backend_name, vol_name, e
                    )
        except Exception as e:
            _log.warning("pool %s: vol enumeration failed: %s", backend_name, e)
        try:
            if sp.isActive():
                sp.destroy()
        except Exception as e:
            _log.warning("pool %s stop failed: %s", backend_name, e)
        try:
            sp.delete(0)
        except Exception as e:
            _log.warning("pool %s delete (dir removal) failed: %s", backend_name, e)
        try:
            sp.undefine()
        except Exception as e:
            _log.warning("pool %s undefine failed: %s", backend_name, e)

    # qcow2 for disks (overlay support); raw for the cloud-init seed ISO.
    _VOLUME_SUFFIX: ClassVar[dict[str, str]] = {
        "install_disk": ".qcow2",
        "run_disk": ".qcow2",
        "base_image": ".qcow2",
        "install_seed": ".iso",
        "sidecar_disk": ".qcow2",
        "sidecar_config": ".iso",
    }

    def volume_suffix(self, kind: str) -> str:
        try:
            return self._VOLUME_SUFFIX[kind]
        except KeyError as e:
            raise DriverError(f"LibvirtDriver: unknown volume kind {kind!r}") from e

    def _pool_dir(self, pool_backend_name: str) -> Path:
        return self.pool_root / pool_backend_name

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        """Upload raw bytes as a new volume in the pool via libvirt's stream API.

        Replace-if-exists: any pre-existing volume at ``target_ref`` is
        deleted first, matching the old ``os.replace`` semantics so a stale
        blob from a partial prior run can't bleed through.
        """
        libvirt = _import_libvirt()
        ref_path = Path(target_ref)
        pool_backend_name = ref_path.parent.name
        filename = ref_path.name
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        try:
            old_vol = sp.storageVolLookupByName(filename)
        except libvirt.libvirtError:
            old_vol = None
        if old_vol is not None:
            try:
                old_vol.delete(0)
            except libvirt.libvirtError as e:
                _log.warning("write_to_pool: delete-old %s failed: %s", filename, e)
        self._stream_upload_to_vol(
            sp,
            vol_name=filename,
            size=len(data),
            fmt="raw",
            reader_factory=lambda: io.BytesIO(data),
        )
        try:
            sp.refresh(0)
        except Exception as e:
            _log.debug("pool refresh after write_to_pool failed (non-fatal): %s", e)
        return target_ref

    def _stream_upload_to_vol(
        self,
        sp: Any,
        *,
        vol_name: str,
        size: int,
        fmt: str,
        reader_factory: Any,
    ) -> None:
        """Create a volume of ``size`` bytes and stream from ``reader_factory()``.

        Used by both ``upload_to_pool`` (file source, qcow2) and
        ``write_to_pool`` (in-memory bytes, raw). On any failure, aborts the
        stream and deletes the partial volume.
        """
        xml = _render_uploaded_volume_xml(vol_name, size, fmt)
        vol = sp.createXML(xml, 0)
        stream = self.conn.newStream(0)
        try:
            vol.upload(stream, 0, size, 0)

            def _send(_stream: Any, nbytes: int, f: Any) -> bytes:
                buf: bytes = f.read(nbytes)
                return buf

            f = reader_factory()
            try:
                stream.sendAll(_send, f)
            finally:
                try:
                    f.close()
                except Exception:
                    pass
            stream.finish()
        except Exception:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                vol.delete(0)
            except Exception:
                pass
            raise

    def create_disk_from_base(
        self,
        target_ref: VolumeRef,
        source_ref: VolumeRef,
    ) -> VolumeRef:
        """Create a self-contained qcow2 by full-copy of an in-pool source volume.

        Under the dir-pool driver, libvirt's ``createXMLFrom`` invokes
        ``qemu-img convert``, so the new volume contains all data from the
        source and has no backing reference.
        """
        target_path = Path(target_ref)
        source_path = Path(source_ref)
        # The libvirt VolumeRef is a full filesystem path; the parent dir
        # IS the pool directory, and pool & source live in the same pool.
        pool_backend_name = target_path.parent.name
        target_name = target_path.name
        source_name = source_path.name
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        source_vol = sp.storageVolLookupByName(source_name)
        capacity = int(source_vol.info()[1])
        xml = _render_uploaded_volume_xml(target_name, capacity)
        _log.info("clone %s → %s in pool %s", source_name, target_name, pool_backend_name)
        sp.createXMLFrom(xml, source_vol, 0)
        return target_ref

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        """Stream ``source_path`` into the pool as a new qcow2 volume at ``target_ref``.

        Idempotent: if the volume already exists at the target, returns the
        ref without re-uploading.
        """
        libvirt = _import_libvirt()
        target_path = Path(target_ref)
        pool_backend_name = target_path.parent.name
        vol_name = target_path.name
        sp = self.conn.storagePoolLookupByName(pool_backend_name)

        try:
            sp.storageVolLookupByName(vol_name)
            _log.info("upload skipped (volume exists): %s", vol_name)
            return target_ref
        except libvirt.libvirtError:
            pass

        size = source_path.stat().st_size
        _log.info("upload %s (%d bytes) → pool %s", vol_name, size, pool_backend_name)
        self._stream_upload_to_vol(
            sp,
            vol_name=vol_name,
            size=size,
            fmt="qcow2",
            reader_factory=lambda: source_path.open("rb"),
        )
        try:
            sp.refresh(0)
        except Exception as e:
            _log.debug("pool refresh after upload failed (non-fatal): %s", e)
        return target_ref

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        """Stream a pool volume's bytes back to the orchestrator filesystem.

        Source volume must be self-contained (no backing chain). The
        orchestrator always uses ``create_disk_from_base`` for in-pool
        clones, which produces a flat qcow2, so this invariant holds.
        """
        # libvirt VolumeRef = full filesystem path; parent is the pool dir,
        # filename is the in-pool volume name.
        ref_path = Path(vol_ref)
        pool_backend_name = ref_path.parent.name
        vol_name = ref_path.name
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        vol = sp.storageVolLookupByName(vol_name)
        _log.info("download %s ← pool %s → %s", vol_name, pool_backend_name, dest_path)
        stream = self.conn.newStream(0)
        try:
            # length=0 → libvirt streams until end of volume.
            vol.download(stream, 0, 0, 0)

            def _recv(_stream: Any, data: bytes, f: Any) -> int:
                f.write(data)
                return len(data)

            with dest_path.open("wb") as f:
                stream.recvAll(_recv, f)
            stream.finish()
        except Exception:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                dest_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return dest_path

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        libvirt = _import_libvirt()
        ref_path = Path(vol_ref)
        pool_backend_name = ref_path.parent.name
        vol_name = ref_path.name
        try:
            sp = self.conn.storagePoolLookupByName(pool_backend_name)
            vol = sp.storageVolLookupByName(vol_name)
            vol.delete(0)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower():
                return
            raise

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
        # For libvirt, a VolumeRef is a filesystem path on libvirtd's host;
        # convert to Path for the domain-XML renderer which embeds it as
        # <source file='...'/>.
        os_disk_path = Path(os_disk_ref)
        seed_iso_path = Path(seed_iso_ref) if seed_iso_ref is not None else None
        macs = [self.compose_mac(plan_name, spec.name, i) for i in range(len(spec.nics))]
        xml = _render_domain_xml(
            backend_name,
            spec,
            os_disk_path=os_disk_path,
            seed_iso_path=seed_iso_path,
            network_refs=network_refs,
            macs=macs,
        )
        _log.info("define vm %s", backend_name)
        return self.conn.defineXML(xml)

    def start_vm(self, backend_name: str) -> None:
        _log.info("start vm %s", backend_name)
        dom = self.conn.lookupByName(backend_name)
        dom.create()

    def get_lease_ip(self, network_backend_name: str, mac: str) -> str | None:
        """Look up an IP leased to ``mac`` on a libvirt network's dnsmasq."""
        try:
            net = self.conn.networkLookupByName(network_backend_name)
        except Exception as e:
            _log.warning("lease lookup: network %s not found: %s", network_backend_name, e)
            return None
        try:
            leases = net.DHCPLeases()
        except Exception as e:
            _log.debug("DHCPLeases failed: %s", e)
            return None
        mac_lc = mac.lower()
        for lease in leases:
            if lease.get("mac", "").lower() == mac_lc:
                ip = lease.get("ipaddr")
                return str(ip) if ip else None
        return None

    def get_vm_power_state(self, backend_name: str) -> str:
        libvirt = _import_libvirt()
        dom = self.conn.lookupByName(backend_name)
        state, _ = dom.state()
        names = {
            libvirt.VIR_DOMAIN_NOSTATE: "nostate",
            libvirt.VIR_DOMAIN_RUNNING: "running",
            libvirt.VIR_DOMAIN_BLOCKED: "blocked",
            libvirt.VIR_DOMAIN_PAUSED: "paused",
            libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
            libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
            libvirt.VIR_DOMAIN_CRASHED: "crashed",
        }
        return names.get(state, f"unknown-{state}")

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        libvirt = _import_libvirt()
        try:
            dom = self.conn.lookupByName(backend_name)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower() or "no domain" in str(e).lower():
                _log.info("vm %s already absent", backend_name)
                return
            raise
        try:
            dom.shutdown()
        except Exception as e:
            _log.warning("ACPI shutdown failed for %s: %s", backend_name, e)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_vm_power_state(backend_name) == "shutoff":
                return
            time.sleep(2.0)
        # Force destroy
        _log.warning("shutdown timeout for %s, forcing destroy", backend_name)
        try:
            dom.destroy()
        except Exception as e:
            _log.warning("force destroy failed: %s", e)

    def destroy_vm(self, backend_name: str) -> None:
        libvirt = _import_libvirt()
        try:
            dom = self.conn.lookupByName(backend_name)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower() or "no domain" in str(e).lower():
                _log.info("vm %s already absent", backend_name)
                return
            raise
        # libvirt refuses to undefine a domain that still has snapshots.
        # Use METADATA_ONLY: we're tearing down the whole pool, so we
        # don't need the disk-chain to be merged back. (Default-flag
        # delete tries a live block-commit, which on a forced-off or
        # mid-revert VM fails with "Permission denied".) The pool sweep
        # in destroy_pool below handles any stray snapshot disk files.
        try:
            for snap_name in dom.snapshotListNames():
                try:
                    dom.snapshotLookupByName(snap_name).delete(
                        libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY
                    )
                except Exception as e:
                    _log.warning("vm %s: snapshot %s delete failed: %s", backend_name, snap_name, e)
        except Exception as e:
            _log.warning("vm %s: snapshot enumeration failed: %s", backend_name, e)
        try:
            if dom.isActive():
                dom.destroy()
        except Exception as e:
            _log.warning("vm %s destroy failed: %s", backend_name, e)
        try:
            dom.undefine()
        except Exception as e:
            _log.warning("vm %s undefine failed: %s", backend_name, e)

    def create_snapshot(
        self,
        vm_backend_name: str,
        name: str,
        description: str = "",
        *,
        mem: bool = False,
    ) -> None:
        libvirt = _import_libvirt()
        dom = self.conn.lookupByName(vm_backend_name)
        # Reject duplicates up-front for a clear error; otherwise libvirt
        # would emit a less-readable internal-error message.
        try:
            dom.snapshotLookupByName(name)
        except libvirt.libvirtError as e:
            msg = str(e).lower()
            if "not found" not in msg and "no domain snapshot" not in msg:
                raise
        else:
            raise DriverError(f"snapshot {name!r} already exists on vm {vm_backend_name!r}")
        # mem=False → DISK_ONLY flag (no RAM capture, VM can be running or
        # shut off); mem=True → default (memory included when running, must
        # be running to mean anything).
        flags = 0 if mem else libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
        xml = _render_snapshot_xml(name, description)
        _log.info("snapshot %s on vm %s (mem=%s)", name, vm_backend_name, mem)
        dom.snapshotCreateXML(xml, flags)

    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        dom = self.conn.lookupByName(vm_backend_name)
        names: list[str] = list(dom.snapshotListNames())
        return names

    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        libvirt = _import_libvirt()
        dom = self.conn.lookupByName(vm_backend_name)
        try:
            snap = dom.snapshotLookupByName(name)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower() or "no domain snapshot" in str(e).lower():
                _log.info("snapshot %s on %s already absent", name, vm_backend_name)
                return
            raise
        _log.info("delete snapshot %s on vm %s", name, vm_backend_name)
        snap.delete(0)

    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        libvirt = _import_libvirt()
        dom = self.conn.lookupByName(vm_backend_name)
        try:
            snap = dom.snapshotLookupByName(name)
        except libvirt.libvirtError as e:
            msg = str(e).lower()
            if "not found" in msg or "no domain snapshot" in msg:
                raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}") from e
            raise
        _log.info("revert vm %s to snapshot %s", vm_backend_name, name)
        dom.revertToSnapshot(snap, 0)

    # --- Native guest agent (QEMU Guest Agent) ------------------------

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        return _LibvirtGuestAgent(self, backend_name).execute

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        return _LibvirtGuestAgent(self, backend_name).read_file

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        return _LibvirtGuestAgent(self, backend_name).write_file


def _qga_agent_not_ready(e: Exception) -> bool:
    """True when a libvirtError reads like the guest agent is not up yet."""
    msg = str(e).lower()
    return "guest agent is not" in msg or "not connected" in msg or "not responding" in msg


def _qga_argv(argv: Sequence[str], cwd: str | None) -> tuple[str, list[str]]:
    """Split argv into QGA's ``(path, arg-list)``.

    QGA's ``guest-exec`` has no native ``cwd``; when one is requested it
    is shimmed through ``sh -c 'cd -- <dir> && exec <argv>'``.
    """
    if not argv:
        raise GuestAgentError("guest-exec: empty argv")
    if cwd is not None:
        inner = " ".join(shlex.quote(a) for a in argv)
        return "sh", ["-c", f"cd -- {shlex.quote(cwd)} && exec {inner}"]
    return argv[0], list(argv[1:])


class _LibvirtGuestAgent:
    """VM-bound QEMU Guest Agent executor for one libvirt domain.

    Speaks the QGA JSON protocol over ``libvirt_qemu.qemuAgentCommand``.
    The domain is re-resolved on every call — a cached ``virDomain`` goes
    stale across a libvirt reconnect, so this matches the rest of the
    driver's lookup discipline. Operations tolerate a guest agent that
    has not finished coming up, retrying for a bounded deadline — the
    same shape as the SSH communicator's connect-retry.
    """

    def __init__(self, driver: LibvirtDriver, backend_name: str) -> None:
        self._driver = driver
        self._backend_name = backend_name

    def _send(self, command: str, arguments: dict[str, Any] | None, *, deadline: float) -> Any:
        """Send one QGA command; return its decoded ``return`` payload.

        Retries a not-yet-responding agent until ``deadline``. Wraps
        libvirt-side errors and QGA ``{"error": ...}`` responses into
        :class:`GuestAgentError`.
        """
        libvirt = _import_libvirt()
        libvirt_qemu = _import_libvirt_qemu()
        payload: dict[str, Any] = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        wire = json.dumps(payload)
        while True:
            dom = self._driver.conn.lookupByName(self._backend_name)
            try:
                # 10s per-RPC: QGA control commands answer fast; the only
                # slow thing (a guest process) is polled, not awaited here.
                raw = libvirt_qemu.qemuAgentCommand(dom, wire, 10, 0)
                break
            except libvirt.libvirtError as e:
                if time.monotonic() < deadline and _qga_agent_not_ready(e):
                    time.sleep(0.5)
                    continue
                raise GuestAgentError(
                    f"QGA {command!r} on {self._backend_name!r} failed: {e}"
                ) from e
        try:
            resp = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise GuestAgentError(
                f"QGA {command!r} on {self._backend_name!r} returned non-JSON: {raw!r}"
            ) from e
        if "error" in resp:
            raise GuestAgentError(f"QGA {command!r} on {self._backend_name!r}: {resp['error']}")
        return resp.get("return")

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        """Run a command via ``guest-exec``; poll ``guest-exec-status``."""
        started = time.monotonic()
        deadline = started + timeout
        path, args = _qga_argv(argv, cwd)
        ret = self._send(
            "guest-exec",
            {"path": path, "arg": args, "capture-output": True},
            deadline=deadline,
        )
        pid = ret["pid"]
        while True:
            status = self._send("guest-exec-status", {"pid": pid}, deadline=deadline)
            if status.get("exited"):
                break
            if time.monotonic() >= deadline:
                raise GuestAgentError(
                    f"QGA guest-exec on {self._backend_name!r} did not finish within {timeout:.0f}s"
                )
            time.sleep(0.5)
        return ExecResult(
            exit_code=int(status.get("exitcode", -1)),
            stdout=base64.b64decode(status.get("out-data", "") or ""),
            stderr=base64.b64decode(status.get("err-data", "") or ""),
            duration=time.monotonic() - started,
        )

    def read_file(self, path: str) -> bytes:
        """Read a guest file via ``guest-file-open``/``-read``/``-close``."""
        deadline = time.monotonic() + 60.0
        ret = self._send("guest-file-open", {"path": path, "mode": "r"}, deadline=deadline)
        handle = ret if isinstance(ret, int) else ret["handle"]
        chunks: list[bytes] = []
        try:
            while True:
                r = self._send("guest-file-read", {"handle": handle}, deadline=deadline)
                buf = r.get("buf-b64")
                if buf:
                    chunks.append(base64.b64decode(buf))
                if r.get("eof"):
                    break
        finally:
            with contextlib.suppress(GuestAgentError):
                self._send("guest-file-close", {"handle": handle}, deadline=deadline)
        return b"".join(chunks)

    def write_file(self, path: str, data: bytes) -> None:
        """Write a guest file via ``guest-file-open``/``-write``/``-close``."""
        deadline = time.monotonic() + 60.0
        ret = self._send("guest-file-open", {"path": path, "mode": "w"}, deadline=deadline)
        handle = ret if isinstance(ret, int) else ret["handle"]
        try:
            self._send(
                "guest-file-write",
                {
                    "handle": handle,
                    "buf-b64": base64.b64encode(data).decode("ascii"),
                    "count": len(data),
                },
                deadline=deadline,
            )
        finally:
            with contextlib.suppress(GuestAgentError):
                self._send("guest-file-close", {"handle": handle}, deadline=deadline)


def _collect_subnet_findings(
    hyp: LibvirtHypervisor, install_switch: Switch
) -> list[PreflightFinding]:
    """Pairwise CIDR-overlap check across every user Switch and the install Switch.

    The install Switch is rendered on the same libvirtd as user Switches; a
    CIDR collision would surface as a confusing libvirt error at install
    time. Catching it here gives the user a `fix_hint` they can act on.
    """
    findings: list[PreflightFinding] = []
    parsed: list[tuple[Switch, ipaddress.IPv4Network]] = []
    for s in (*hyp.networks, install_switch):
        try:
            net = ipaddress.ip_network(s.cidr, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                parsed.append((s, net))
        except ValueError as e:
            findings.append(
                PreflightFinding(
                    severity="error",
                    code="invalid_cidr",
                    message=f"switch {s.name!r} has invalid CIDR {s.cidr!r}: {e}",
                )
            )

    for i, (a, an) in enumerate(parsed):
        for b, bn in parsed[i + 1 :]:
            if an.overlaps(bn):
                hint = None
                if a is install_switch or b is install_switch:
                    user_sw = b if a is install_switch else a
                    hint = (
                        f"switch {user_sw.name!r} overlaps the install "
                        f"switch's CIDR ({install_switch.cidr}); pick a "
                        f"different cidr= for {user_sw.name!r}"
                    )
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="subnet_overlap",
                        message=f"switches {a.name!r} and {b.name!r} overlap ({an} vs {bn})",
                        fix_hint=hint,
                    )
                )
    return findings


def _collect_cache_findings(
    hyp: LibvirtHypervisor,
    cache_manager: CacheManager,
) -> list[PreflightFinding]:
    from testrange.cache.entry import CacheEntry
    from testrange.exceptions import CacheMissError

    findings: list[PreflightFinding] = []
    for vm in hyp.vms:
        base = getattr(vm.builder, "base", None)
        if not isinstance(base, CacheEntry):
            continue
        try:
            # Preflight existence check — don't pull a multi-GB base
            # over HTTP just to satisfy a check. The install-phase
            # resolve (fetch=True) will materialize it for real.
            cache_manager.resolve(base, fetch=False)
        except CacheMissError as e:
            findings.append(
                PreflightFinding(
                    severity="error",
                    code="cache_miss",
                    message=str(e),
                    fix_hint=f"testrange cache add <path-or-url> --name {base.identifier}",
                )
            )
    return findings


def _render_network_xml(
    network: Network,
    switch: Switch,
    backend_name: str,
    *,
    bridge_name: str | None = None,
) -> str:
    """Render a libvirt ``<network>`` XML doc for one Network on a Switch.

    DHCP/DNS/NAT are never libvirt's job — the per-Switch sidecar VM owns
    them. ``<dhcp>``, ``<domain>``, and ``<forward mode='nat'/>`` are
    never emitted. Three topology cases:

    - ``uplink`` set, ``nat=False`` — the switch bridge IS the uplink
      bridge (testrange-created via pyroute2, NIC enslaved). XML is
      ``<forward mode='bridge'/><bridge name='<bridge_name>'/>``.
    - ``uplink`` set, ``nat=True`` — the switch bridge stays isolated;
      the sidecar straddles to a separate uplink bridge. XML references
      the testrange-created isolated switch bridge by name.
    - ``uplink`` unset — passive Linux bridge managed by libvirt (or by
      testrange when ``mgmt=True`` so the host can hold ``.2``).
    """
    del network
    if switch.uplink is not None:
        if not bridge_name:
            raise DriverError(
                f"network {backend_name!r}: switch {switch.name!r} has uplink set "
                "but bridge_name was not provided to the renderer"
            )
        return (
            f"<network>"
            f"<name>{backend_name}</name>"
            f"<forward mode='bridge'/>"
            f"<bridge name='{bridge_name}'/>"
            f"</network>"
        )
    if switch.mgmt or switch.needs_sidecar:
        if not bridge_name:
            raise DriverError(
                f"network {backend_name!r}: switch {switch.name!r} needs an isolated "
                "testrange bridge but bridge_name was not provided"
            )
        return (
            f"<network>"
            f"<name>{backend_name}</name>"
            f"<forward mode='bridge'/>"
            f"<bridge name='{bridge_name}'/>"
            f"</network>"
        )
    return f"<network><name>{backend_name}</name><bridge stp='on' delay='0'/></network>"


def _collect_sidecar_findings(
    hyp: LibvirtHypervisor,
    cache_manager: CacheManager,
) -> list[PreflightFinding]:
    """Sidecar-shaped checks: cache image present, at least one pool exists."""
    from testrange.cache.entry import CacheEntry
    from testrange.exceptions import CacheMissError

    findings: list[PreflightFinding] = []
    sidecar_switches = [s for s in hyp.networks if s.needs_sidecar]
    install_needs_sidecar = hyp.install_uplink is not None
    if not (sidecar_switches or install_needs_sidecar):
        return findings
    if not hyp.pools:
        findings.append(
            PreflightFinding(
                severity="error",
                code="sidecar_no_pool",
                message=(
                    "sidecar VM needs a pool for its disk but the plan declares none"
                ),
                fix_hint="declare at least one StoragePool in the plan",
            )
        )
    try:
        cache_manager.resolve(CacheEntry(SIDECAR_CACHE_NAME), fetch=False)
    except CacheMissError as e:
        findings.append(
            PreflightFinding(
                severity="error",
                code="cache_miss",
                message=f"sidecar image: {e}",
                fix_hint=f"testrange cache add <path-or-url> --name {SIDECAR_CACHE_NAME}",
            )
        )
    return findings


def _wrap_netlink_error(e: Exception, what: str) -> None:
    msg = str(e)
    if "Operation not permitted" in msg or "EPERM" in msg:
        raise DriverError(
            f"{what}: needs CAP_NET_ADMIN (run as root, or grant the cap)"
        ) from e
    raise DriverError(f"{what}: {e}") from e


def _render_pool_xml(backend_name: str, path: Path) -> str:
    """Render a libvirt `<pool type='dir'>` XML doc."""
    return (
        f"<pool type='dir'><name>{backend_name}</name><target><path>{path}</path></target></pool>"
    )


def _render_uploaded_volume_xml(vol_name: str, capacity_bytes: int, fmt: str = "qcow2") -> str:
    """Render a libvirt volume XML for an uploaded file (no backing).

    ``fmt`` selects the on-disk format: ``qcow2`` for base/overlay disk images,
    ``raw`` for opaque blobs like cloud-init seed ISOs.
    """
    return (
        f"<volume>"
        f"<name>{vol_name}</name>"
        f"<capacity unit='bytes'>{capacity_bytes}</capacity>"
        f"<target><format type='{fmt}'/></target>"
        f"</volume>"
    )


def _render_snapshot_xml(name: str, description: str) -> str:
    """Render a libvirt domain-snapshot XML doc."""
    desc_block = f"<description>{description}</description>" if description else ""
    return f"<domainsnapshot><name>{name}</name>{desc_block}</domainsnapshot>"


def _render_domain_xml(
    backend_name: str,
    spec: VMSpec,
    *,
    os_disk_path: Path,
    seed_iso_path: Path | None,
    network_refs: dict[str, str],
    macs: list[str],
) -> str:
    """Render a libvirt domain XML for ``defineXML``."""
    nic_xmls = []
    for idx, nic in enumerate(spec.nics):
        if nic.network not in network_refs:
            raise DriverError(
                f"create_vm: no backend network for {nic.network!r}; known: {sorted(network_refs)}"
            )
        net_backend = network_refs[nic.network]
        mac = macs[idx]
        model = getattr(nic, "driver", "virtio")
        nic_xmls.append(
            f"<interface type='network'>"
            f"<source network='{net_backend}'/>"
            f"<mac address='{mac}'/>"
            f"<model type='{model}'/>"
            f"</interface>"
        )

    seed_xml = ""
    if seed_iso_path is not None:
        seed_xml = (
            f"<disk type='file' device='cdrom'>"
            f"<source file='{seed_iso_path}'/>"
            f"<target dev='sdc' bus='sata'/>"
            f"<readonly/>"
            f"<driver name='qemu' type='raw'/>"
            f"</disk>"
        )

    return (
        f"<domain type='kvm'>"
        f"<name>{backend_name}</name>"
        f"<memory unit='MiB'>{spec.memory.size_mb}</memory>"
        f"<vcpu>{spec.cpu.count}</vcpu>"
        f"<os>"
        f"<type arch='x86_64' machine='pc'>hvm</type>"
        f"<boot dev='hd'/>"
        f"</os>"
        f"<features><acpi/><apic/></features>"
        f"<cpu mode='host-passthrough'/>"
        f"<devices>"
        f"<disk type='file' device='disk'>"
        f"<source file='{os_disk_path}'/>"
        f"<target dev='vda' bus='virtio'/>"
        f"<driver name='qemu' type='qcow2'/>"
        f"</disk>"
        f"{seed_xml}"
        f"{''.join(nic_xmls)}"
        # QEMU Guest Agent virtio channel — rendered unconditionally.
        # Inert on guests without qemu-guest-agent installed; lets a
        # QGACommunicator reach the guest via libvirt_qemu.qemuAgentCommand
        # without the driver having to inspect the communicator type.
        f"<channel type='unix'>"
        f"<target type='virtio' name='org.qemu.guest_agent.0'/>"
        f"</channel>"
        f"<serial type='pty'><target port='0'/></serial>"
        f"<console type='pty'><target type='serial' port='0'/></console>"
        # VNC + virtio-gpu so `virt-viewer <domain>` works out of the box.
        # VNC is universally compiled into qemu; SPICE and QXL are commonly
        # stripped from distro builds. listen='127.0.0.1' keeps the display
        # local to libvirtd's host.
        f"<graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>"
        f"<video><model type='virtio'/></video>"
        f"</devices>"
        f"</domain>"
    )


from testrange.drivers._registry import register as _register  # noqa: E402

_register(
    hypervisor_cls=LibvirtHypervisor,
    driver_name=LibvirtDriver.DRIVER_NAME,
    from_hypervisor=lambda hyp: LibvirtDriver(uri=hyp.connection),
    from_uri=lambda uri: LibvirtDriver(uri=uri),
)
