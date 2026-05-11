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
import ipaddress
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


class LibvirtDriver(HypervisorDriver):
    """libvirt-backed HypervisorDriver.

    Methods that touch the backend lazy-import ``libvirt`` so the rest of
    the package is usable on hosts without libvirt-dev.
    """

    DRIVER_NAME = "LibvirtDriver"

    def __init__(self, *, uri: str, pool_root: Path | None = None) -> None:
        self.uri = uri
        self._conn: Any | None = None
        # Storage pool directory root (libvirt "dir"-type pools).
        # Default: under XDG state dir so tests don't clobber the home dir.
        self.pool_root = pool_root or (Path.home() / ".local" / "share" / "testrange" / "pools")

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
        # Try-touch (read-only invariant intact: only mkdir, no probe file)
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
        path.mkdir(parents=True, exist_ok=True)
        xml = _render_pool_xml(backend_name, path)
        _log.info("define pool %s (%s)", backend_name, path)
        sp = self.conn.storagePoolDefineXML(xml)
        sp.setAutostart(True)
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
        try:
            sp.undefine()
        except Exception as e:
            _log.warning("pool %s undefine failed: %s", backend_name, e)


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
