"""Libvirt driver: ``LibvirtHypervisor`` Plan-time entry + ``LibvirtDriver`` runtime.

Phase 2 scope:
  - connect / disconnect
  - preflight (read-only checks)
  - deterministic backend names + stable MACs
  - libvirt network CRUD + storage pool CRUD

Phase 3+ adds VM CRUD and disk operations.

``libvirt-python`` is imported lazily inside methods so the rest of the
package is importable on hosts without libvirt-dev installed.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.devices.pool.base import StoragePool
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError
from testrange.networks.base import Network, Switch
from testrange.preflight import PreflightFinding, PreflightReport
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.plan import Plan

_log = get_logger(__name__)

# Locally-administered OUI for KVM/QEMU guests.
_KVM_OUI = "52:54:00"


@dataclass(frozen=True)
class LibvirtHypervisor:
    """Top-level Plan entry: a libvirt host with its declared topology.

    Driver class is inferred at orchestrator-construction time:
    ``LibvirtHypervisor -> LibvirtDriver(uri=connection)``.
    """

    connection: str
    networks: tuple[Switch, ...]
    pools: tuple[StoragePool, ...]
    vms: tuple[VMRecipe, ...]

    def __init__(
        self,
        *,
        connection: str,
        networks: Sequence[Switch] = (),
        pools: Sequence[StoragePool] = (),
        vms: Sequence[VMRecipe] = (),
    ) -> None:
        if not isinstance(connection, str) or not connection:
            raise ValueError("LibvirtHypervisor.connection must be a non-empty string")
        switches = tuple(networks)
        for s in switches:
            if not isinstance(s, Switch):
                raise TypeError(
                    f"LibvirtHypervisor.networks must contain Switch, got {type(s).__name__}"
                )
        ps = tuple(pools)
        for p in ps:
            if not isinstance(p, StoragePool):
                raise TypeError(
                    f"LibvirtHypervisor.pools must contain StoragePool, got {type(p).__name__}"
                )
        rs = tuple(vms)
        for r in rs:
            if not isinstance(r, VMRecipe):
                raise TypeError(
                    f"LibvirtHypervisor.vms must contain VMRecipe, got {type(r).__name__}"
                )

        net_names = {n.name for s in switches for n in s.networks}
        pool_names = {p.name for p in ps}
        vm_names = [r.name for r in rs]
        dup_vms = {n for n in vm_names if vm_names.count(n) > 1}
        if dup_vms:
            raise ValueError(f"LibvirtHypervisor.vms has duplicate names: {sorted(dup_vms)}")
        all_nets = [n.name for s in switches for n in s.networks]
        dup_nets = {n for n in all_nets if all_nets.count(n) > 1}
        if dup_nets:
            raise ValueError(
                f"LibvirtHypervisor networks have duplicate names: {sorted(dup_nets)}"
            )

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

        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "networks", switches)
        object.__setattr__(self, "pools", ps)
        object.__setattr__(self, "vms", rs)

    @property
    def all_networks(self) -> tuple[Network, ...]:
        return tuple(n for s in self.networks for n in s.networks)


# ---- driver runtime ---------------------------------------------------


def _import_libvirt() -> Any:
    """Lazy import. Raises DriverError with a useful hint if libvirt-python is missing."""
    try:
        import libvirt
    except ImportError as e:
        raise DriverError(
            "libvirt-python is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return libvirt


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

    # ---- connection ---------------------------------------------------

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

    # ---- naming + mac --------------------------------------------------

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

    # ---- preflight (READ-ONLY) ----------------------------------------

    def preflight(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager,
    ) -> PreflightReport:
        findings: list[PreflightFinding] = []

        hyp = plan.hypervisor
        if not isinstance(hyp, LibvirtHypervisor):
            findings.append(
                PreflightFinding(
                    severity="error",
                    code="bad_hypervisor",
                    message=(
                        f"LibvirtDriver expects a LibvirtHypervisor, "
                        f"got {type(hyp).__name__}"
                    ),
                )
            )
            return PreflightReport(findings=tuple(findings))

        findings.extend(_collect_subnet_findings(hyp))
        findings.extend(_collect_cache_findings(hyp, cache_manager))
        findings.extend(self._collect_pool_root_findings())

        return PreflightReport(findings=tuple(findings))

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

    # ---- network CRUD --------------------------------------------------

    def create_network(self, network: Network, switch: Switch, backend_name: str) -> Any:
        _import_libvirt()
        xml = _render_network_xml(network, switch, backend_name)
        _log.info("define network %s (%s)", backend_name, network.cidr)
        net = self.conn.networkDefineXML(xml)
        net.create()  # start it
        return net

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

    # ---- pool CRUD -----------------------------------------------------

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
        try:
            if sp.isActive():
                sp.destroy()
        except Exception as e:
            _log.warning("pool %s stop failed: %s", backend_name, e)
        # delete() removes the on-disk target directory. Pool must be inactive
        # (destroy() above handles that) and the directory must be empty —
        # the LIFO state walker deletes contained volumes before reaching the
        # pool, so by this point it is.
        try:
            sp.delete(0)
        except Exception as e:
            _log.warning("pool %s delete (dir removal) failed: %s", backend_name, e)
        try:
            sp.undefine()
        except Exception as e:
            _log.warning("pool %s undefine failed: %s", backend_name, e)

    # ---- volume operations --------------------------------------------

    def _pool_dir(self, pool_backend_name: str) -> Path:
        return self.pool_root / pool_backend_name

    def write_to_pool(self, pool_backend_name: str, filename: str, data: bytes) -> Path:
        """Upload raw bytes as a new volume in the pool via libvirt's stream API.

        Replace-if-exists: any pre-existing volume with the same name is deleted
        first, matching the old ``os.replace`` semantics so a stale blob from a
        partial prior run can't bleed through.
        """
        libvirt = _import_libvirt()
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        in_pool_path = self._pool_dir(pool_backend_name) / filename
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
        return in_pool_path

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
                return f.read(nbytes)

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

    def create_overlay_disk(
        self,
        pool_backend_name: str,
        vol_name: str,
        source_path: Path,
    ) -> Path:
        """Create a qcow2 overlay volume backed by ``source_path``."""
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        xml = _render_overlay_volume_xml(vol_name, source_path)
        _log.info("create overlay %s backed by %s", vol_name, source_path)
        sp.createXML(xml, 0)
        return self._pool_dir(pool_backend_name) / vol_name

    def upload_to_pool(
        self,
        pool_backend_name: str,
        vol_name: str,
        source_path: Path,
    ) -> Path:
        """Stream ``source_path`` into the pool as a new qcow2 volume.

        Idempotent: if the named volume already exists, returns its path
        without re-uploading.
        """
        libvirt = _import_libvirt()
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        in_pool_path = self._pool_dir(pool_backend_name) / vol_name

        try:
            sp.storageVolLookupByName(vol_name)
            _log.info("upload skipped (volume exists): %s", vol_name)
            return in_pool_path
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
        return in_pool_path

    def download_from_pool(
        self,
        pool_backend_name: str,
        vol_name: str,
        dest_path: Path,
    ) -> Path:
        """Stream a pool volume's bytes back to the orchestrator filesystem.

        The source volume may be a qcow2 overlay with a baked-in
        ``backing_file`` reference in its header. Returning those bytes
        directly would yield a file that's only usable while the original
        backing chain is still resolvable at that path. To avoid that, we
        first flatten the volume into a no-backing-store qcow2 clone inside
        the pool (libvirt's ``createXMLFrom`` calls ``qemu-img convert``
        under the dir-pool driver, which reads through the backing chain),
        then stream the clone back, then delete the clone.
        """
        libvirt = _import_libvirt()
        sp = self.conn.storagePoolLookupByName(pool_backend_name)
        source_vol = sp.storageVolLookupByName(vol_name)
        # vol.info() -> [type, capacity, allocation]
        capacity = int(source_vol.info()[1])

        clone_name = f"{vol_name}.flat.tmp"
        # Drop any stale clone left behind by a prior failed download.
        try:
            stale = sp.storageVolLookupByName(clone_name)
            try:
                stale.delete(0)
            except Exception as e:
                _log.warning(
                    "download: failed to delete stale clone %s: %s", clone_name, e
                )
        except libvirt.libvirtError:
            pass

        clone_xml = _render_uploaded_volume_xml(clone_name, capacity)
        _log.info(
            "flatten %s → %s in pool %s (qcow2, no backing chain)",
            vol_name, clone_name, pool_backend_name,
        )
        clone_vol = sp.createXMLFrom(clone_xml, source_vol, 0)
        try:
            _log.info(
                "download %s ← pool %s → %s",
                vol_name, pool_backend_name, dest_path,
            )
            stream = self.conn.newStream(0)
            try:
                # length=0 → libvirt streams until end of volume.
                clone_vol.download(stream, 0, 0, 0)

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
        finally:
            try:
                clone_vol.delete(0)
            except Exception as e:
                _log.warning(
                    "download: failed to delete flat clone %s: %s", clone_name, e
                )
        return dest_path

    def delete_volume(self, pool_backend_name: str, vol_name: str) -> None:
        libvirt = _import_libvirt()
        try:
            sp = self.conn.storagePoolLookupByName(pool_backend_name)
            vol = sp.storageVolLookupByName(vol_name)
            vol.delete(0)
        except libvirt.libvirtError as e:
            if "not found" in str(e).lower():
                return
            raise

    # ---- VM CRUD -------------------------------------------------------

    def create_vm(
        self,
        backend_name: str,
        spec: VMSpec,
        plan_name: str,
        *,
        os_disk_path: Path,
        seed_iso_path: Path | None,
        network_refs: dict[str, str],
    ) -> Any:
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
        try:
            if dom.isActive():
                dom.destroy()
        except Exception as e:
            _log.warning("vm %s destroy failed: %s", backend_name, e)
        try:
            dom.undefine()
        except Exception as e:
            _log.warning("vm %s undefine failed: %s", backend_name, e)


# ---- helpers (preflight + XML rendering) ------------------------------


def _collect_subnet_findings(hyp: LibvirtHypervisor) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    nets = list(hyp.all_networks)
    parsed = []
    for n in nets:
        try:
            parsed.append((n, ipaddress.ip_network(n.cidr, strict=False)))
        except ValueError as e:
            findings.append(
                PreflightFinding(
                    severity="error",
                    code="invalid_cidr",
                    message=f"network {n.name!r} has invalid CIDR {n.cidr!r}: {e}",
                )
            )

    for i, (a, an) in enumerate(parsed):
        for _b, bn in parsed[i + 1 :]:
            if an.overlaps(bn):
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="subnet_overlap",
                        message=f"networks {a.name!r} and {_b.name!r} overlap "
                        f"({an} vs {bn})",
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
            cache_manager.resolve(base)
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


def _render_network_xml(network: Network, switch: Switch, backend_name: str) -> str:
    """Render a libvirt `<network>` XML doc for one Network on a Switch.

    v0: per-network Linux bridge (libvirt's default). The Switch-level
    grouping is captured in `compose_resource_name` (resources from the
    same Switch share a prefix) but actual L2 bridging across networks
    in a Switch is a TODO (would require OVS).
    """
    net = network.network
    if not isinstance(net, ipaddress.IPv4Network):
        raise DriverError(f"only IPv4 networks supported in v0, got {network.cidr!r}")
    gateway = str(net.network_address + 1)
    netmask = str(net.netmask)
    forward = "<forward mode='nat'/>" if switch.internet else ""

    dhcp_block = ""
    if network.dhcp:
        # Conservative range: .100 - .200 within the subnet.
        start = str(net.network_address + 100)
        end = str(net.network_address + 200)
        dhcp_block = f"<dhcp><range start='{start}' end='{end}'/></dhcp>"

    domain = ""
    if network.dns:
        # Use the network name as the DNS zone for VMs on this network.
        domain = f"<domain name='{network.name}.testrange' localOnly='yes'/>"

    return (
        f"<network>"
        f"<name>{backend_name}</name>"
        f"{forward}"
        f"<bridge stp='on' delay='0'/>"
        f"{domain}"
        f"<ip address='{gateway}' netmask='{netmask}'>"
        f"{dhcp_block}"
        f"</ip>"
        f"</network>"
    )


def _render_pool_xml(backend_name: str, path: Path) -> str:
    """Render a libvirt `<pool type='dir'>` XML doc."""
    return (
        f"<pool type='dir'>"
        f"<name>{backend_name}</name>"
        f"<target><path>{path}</path></target>"
        f"</pool>"
    )


def _render_overlay_volume_xml(vol_name: str, source_path: Path) -> str:
    """Render a libvirt volume XML for a qcow2 overlay over ``source_path``."""
    return (
        f"<volume>"
        f"<name>{vol_name}</name>"
        f"<capacity unit='G'>0</capacity>"
        f"<target><format type='qcow2'/></target>"
        f"<backingStore>"
        f"<path>{source_path}</path>"
        f"<format type='qcow2'/>"
        f"</backingStore>"
        f"</volume>"
    )


def _render_uploaded_volume_xml(
    vol_name: str, capacity_bytes: int, fmt: str = "qcow2"
) -> str:
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
                f"create_vm: no backend network for {nic.network!r}; "
                f"known: {sorted(network_refs)}"
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
