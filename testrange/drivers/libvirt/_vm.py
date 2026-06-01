"""VM lifecycle for the libvirt backend (BACKEND-1.B); snapshots (BACKEND-1.D).

A testrange VM is a libvirt **domain** named by its deterministic backend name
(``compose_resource_name`` → ``tr-vm-<run8>-web``), so the name *is* the recovery
anchor: every later call resolves the domain via ``lookupByName`` — no external
map, crash-safe teardown.

``create_vm`` renders domain XML:

- **disks** — qcow2, virtio-blk: OS at ``vda``, data disks at ``vdb`` … (the
  ``fileserver`` capability depends on this device ordering). The seed (cloud-init
  / sidecar config) is a read-only ``sata`` CD-ROM so the guest reads ``cidata``;
  boot is pinned to ``hd``.
- **NICs** — one ``<interface type='network'>`` per ``spec.nics[i]`` on the
  libvirt network named by ``network_refs`` (every L2 segment, including the
  resolved uplink, is a libvirt network here), with the stable MAC
  ``compose_mac(plan, vm, i)`` so DHCP hands out a predictable lease (ADR-0006).
  At build (``build_nic`` set, ADR-0017) the declared NICs are replaced by a
  single build-NIC interface on the build network.
- **serial build-result sink** — a **build VM** (the one with a ``build_nic``,
  ADR-0017) gets a ``<serial type='unix' mode='connect'>`` pointing at a socket
  the driver already listens on (see ``_conn``); its serial is tailed by
  ``read_build_result_sink``. Sidecars and run VMs get a throwaway ``pty`` console
  (no back-pressure, nothing to drain) — a sidecar carries a seed but is monitored
  via QGA, and binding it a unix socket breaks against a remote daemon (the socket
  is on the orchestrator host, not the daemon's — ADR-0021's nested inner run).
- **QGA channel** — an unconditional ``org.qemu.guest_agent.0`` virtio channel
  (``mode='bind'``, the daemon owns that socket) so ``_guest`` can drive the QEMU
  Guest Agent.

Lifecycle maps to ``create()`` / graceful ``shutdown()`` (hard ``destroy()`` after
the timeout) / ``destroy()`` + ``undefine()`` / ``state()``. Functions take the
live :class:`LibvirtClient`; unit tests inject a duck-typed fake.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape, quoteattr

from testrange._log import get_logger
from testrange.drivers.libvirt._conn import _import_libvirt
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.drivers.base import VolumeRef
    from testrange.drivers.libvirt._conn import LibvirtClient
    from testrange.networks.base import BuildNic
    from testrange.vms.spec import VMSpec

_log = get_logger(__name__)

# Power-state poll cadence for the graceful-shutdown wait.
_POLL_INTERVAL_S = 1.0


def _resolve_domain(client: LibvirtClient, backend_name: str) -> Any:
    """The live domain stamped ``backend_name``. Raises if absent (drift)."""
    dom = client.lookup_domain(backend_name)
    if dom is None:
        raise DriverError(f"no libvirt domain named {backend_name!r} (resolution found none)")
    return dom


def _vol_path(client: LibvirtClient, ref: VolumeRef) -> str:
    """Absolute host path of the volume a ``VolumeRef`` names. Raises if absent."""
    pool_name, _, vol_name = str(ref).partition("/")
    vol = client.lookup_volume(pool_name, vol_name)
    if vol is None:
        raise DriverError(f"create_vm: no volume at {ref!r}")
    return str(vol.path())


def _disk_xml(path: str, dev: str) -> str:
    return (
        "<disk type='file' device='disk'>"
        "<driver name='qemu' type='qcow2'/>"
        f"<source file={quoteattr(path)}/>"
        f"<target dev='{dev}' bus='virtio'/>"
        "</disk>"
    )


def _cdrom_xml(path: str) -> str:
    return (
        "<disk type='file' device='cdrom'>"
        "<driver name='qemu' type='raw'/>"
        f"<source file={quoteattr(path)}/>"
        "<target dev='sda' bus='sata'/>"
        "<readonly/>"
        "</disk>"
    )


def _interface_xml(mac: str, network: str) -> str:
    return (
        "<interface type='network'>"
        f"<source network={quoteattr(network)}/>"
        f"<mac address={quoteattr(mac)}/>"
        "<model type='virtio'/>"
        "</interface>"
    )


def _serial_xml(serial_sock: str | None) -> str:
    """A unix-socket serial (seed VMs, read by the sink) or a throwaway pty.

    The unix variant is ``mode='connect'``: QEMU connects to the socket the
    driver already listens on (``mode='bind'`` is not connectable non-root). A run
    VM gets a ``pty`` so cloud images still find a console, with nothing to drain.
    """
    if serial_sock is None:
        return "<serial type='pty'><target port='0'/></serial>"
    return (
        "<serial type='unix'>"
        f"<source mode='connect' path={quoteattr(serial_sock)}/>"
        "<target port='0'/>"
        "</serial>"
    )


_QGA_CHANNEL = (
    "<channel type='unix'>"
    "<source mode='bind'/>"
    "<target type='virtio' name='org.qemu.guest_agent.0'/>"
    "</channel>"
)


def _domain_xml(
    backend_name: str,
    spec: VMSpec,
    *,
    os_path: str,
    data_paths: Sequence[str],
    seed_path: str | None,
    nics: Sequence[tuple[str, str]],
    serial_sock: str | None,
) -> str:
    devices = [_disk_xml(os_path, "vda")]
    if len(data_paths) > 25:
        # vdb..vdz is 25 slots; past 'z' the chr() arithmetic silently produces
        # "vd{" and beyond. Fail loud rather than emit a bogus target dev.
        raise DriverError(
            f"libvirt backend supports at most 25 data disks (vdb..vdz); got {len(data_paths)}"
        )
    for i, path in enumerate(data_paths):
        devices.append(_disk_xml(path, f"vd{chr(ord('b') + i)}"))
    if seed_path is not None:
        devices.append(_cdrom_xml(seed_path))
    devices.extend(_interface_xml(mac, net) for mac, net in nics)
    devices.append(_serial_xml(serial_sock))
    devices.append(_QGA_CHANNEL)
    # A VGA adapter is REQUIRED even though the VM is headless: libvirt emits
    # ``-nodefaults`` (no implicit devices), and the Debian cloud image's GRUB
    # uses ``gfxterm``, which loops forever redrawing the menu when no video
    # device exists — the guest never reaches the kernel. A bare ``<video>`` (no
    # ``<graphics>`` backend) gives GRUB the adapter it needs; we still drive the
    # console over the serial sink. (Proven on the dev host: no-video boots loop
    # at "Booting `Debian GNU/Linux'"; +VGA boots to a kernel.)
    devices.append("<video><model type='vga'/></video>")
    return (
        "<domain type='kvm'>"
        f"<name>{backend_name}</name>"
        f"<memory unit='MiB'>{spec.memory.size_mb}</memory>"
        f"<currentMemory unit='MiB'>{spec.memory.size_mb}</currentMemory>"
        f"<vcpu>{spec.cpu.count}</vcpu>"
        "<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>"
        # ACPI is required for the daemon's graceful shutdown() to reach the guest.
        "<features><acpi/><apic/></features>"
        "<cpu mode='host-passthrough'/>"
        "<clock offset='utc'/>"
        "<on_poweroff>destroy</on_poweroff>"
        "<on_reboot>restart</on_reboot>"
        "<on_crash>destroy</on_crash>"
        f"<devices>{''.join(devices)}</devices>"
        "</domain>"
    )


def create_vm(
    client: LibvirtClient,
    backend_name: str,
    spec: VMSpec,
    plan_name: str,
    *,
    os_disk_ref: VolumeRef,
    seed_iso_ref: VolumeRef | None,
    network_refs: dict[str, str],
    data_disk_refs: Sequence[VolumeRef] = (),
    build_nic: BuildNic | None = None,
) -> str:
    """Define a libvirt domain from the orchestrator's staged disks.

    The OS disk is already full-size (the orchestrator ``resize_volume``\\ d it
    before this call), so create_vm only attaches; cloud-init's ``growpart``
    expands the rootfs on first boot. Only a **build VM** (``build_nic`` set) gets
    the unix-socket serial sink, its listener opened here so the socket exists when
    ``start_vm`` boots QEMU; sidecars and run VMs get a throwaway pty (see the
    module docstring for why a sidecar must not get the unix socket).

    When ``build_nic`` is set (build phase, ADR-0017) the domain gets a *single*
    ``<interface>`` for the build NIC and the declared ``spec.nics`` are not
    attached; otherwise one ``<interface>`` per ``spec.nics[i]`` with its stable
    MAC ``compose_mac(plan, vm, i)`` (ADR-0006).
    """
    os_path = _vol_path(client, os_disk_ref)
    data_paths = [_vol_path(client, ref) for ref in data_disk_refs]
    seed_path = _vol_path(client, seed_iso_ref) if seed_iso_ref is not None else None
    if build_nic is not None:
        nics = [(build_nic.mac, network_refs[build_nic.network])]
    else:
        nics = [
            (_compose_mac(plan_name, spec.name, idx), network_refs[nic.network])
            for idx, nic in enumerate(spec.nics)
        ]
    # The unix-socket serial sink is opened ONLY for build VMs — the orchestrator
    # reads a build VM's TESTRANGE-RESULT off it (build_one_vm). A build VM is the
    # one with the dedicated build NIC (ADR-0017), so build_nic is the signal.
    # Sidecars carry a seed too but are monitored via QGA, never the serial sink;
    # giving them one is dead weight locally and, on a *remote* daemon (the inner
    # qemu+ssh connection of a nested run, ADR-0021), outright broken — the socket
    # we bind lives on the orchestrator host, not the remote daemon's, so its
    # security driver can't stat it. Seed-carrying non-build VMs get a throwaway
    # pty like run VMs.
    serial_sock = client.open_serial_listener(backend_name) if build_nic is not None else None
    xml = _domain_xml(
        backend_name,
        spec,
        os_path=os_path,
        data_paths=data_paths,
        seed_path=seed_path,
        nics=nics,
        serial_sock=serial_sock,
    )
    client.raw.defineXML(xml)
    _log.info("defined libvirt domain %s", backend_name)
    return f"vm:{backend_name}"


def _compose_mac(plan_name: str, vm_name: str, nic_idx: int) -> str:
    from testrange.drivers.libvirt import _naming

    return _naming.compose_mac(plan_name, vm_name, nic_idx)


def start_vm(client: LibvirtClient, backend_name: str) -> None:
    dom = _resolve_domain(client, backend_name)
    if not dom.isActive():
        dom.create()
    _log.info("started libvirt domain %s", backend_name)


def shutdown_vm(client: LibvirtClient, backend_name: str, *, timeout: float = 120.0) -> None:
    """Graceful ACPI shutdown, hard-stopping after ``timeout`` (``destroy``)."""
    libvirt = _import_libvirt()
    dom = _resolve_domain(client, backend_name)
    if not dom.isActive():
        return
    dom.shutdown()
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if dom.state()[0] == libvirt.VIR_DOMAIN_SHUTOFF:
            _log.info("domain %s shut down gracefully", backend_name)
            return
        time.sleep(_POLL_INTERVAL_S)
    if dom.isActive():
        _log.warning("domain %s did not shut down in %.0fs; destroying", backend_name, timeout)
        dom.destroy()


def destroy_vm(client: LibvirtClient, backend_name: str) -> None:
    """Stop (if running) then undefine the domain. Tolerant of absence.

    Releases the serial listener too. Undefine clears snapshot/checkpoint
    metadata and any NVRAM so a snapshotted VM (e.g. ``keybox``) tears down clean.
    """
    libvirt = _import_libvirt()
    dom = client.lookup_domain(backend_name)
    if dom is None:
        client.close_serial_listener(backend_name)
        _log.debug("destroy_vm(%s): not present (already gone)", backend_name)
        return
    if dom.isActive():
        dom.destroy()
    flags = (
        libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
        | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
        | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA
        | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    )
    dom.undefineFlags(flags)
    client.close_serial_listener(backend_name)
    _log.info("destroyed libvirt domain %s", backend_name)


def get_vm_power_state(client: LibvirtClient, backend_name: str) -> str:
    """Power state in the orchestrator's vocabulary (``running`` / ``shutoff``)."""
    libvirt = _import_libvirt()
    dom = _resolve_domain(client, backend_name)
    state = dom.state()[0]
    if state == libvirt.VIR_DOMAIN_RUNNING:
        return "running"
    if state == libvirt.VIR_DOMAIN_SHUTOFF:
        return "shutoff"
    return f"libvirt-state-{state}"


def _snapshot_xml(name: str, description: str) -> str:
    parts = [f"<name>{escape(name)}</name>"]
    if description:
        parts.append(f"<description>{escape(description)}</description>")
    return f"<domainsnapshot>{''.join(parts)}</domainsnapshot>"


def _creation_time(snap: Any) -> int:
    """The snapshot's ``<creationTime>`` epoch seconds (for oldest-first order)."""
    m = re.search(r"<creationTime>(\d+)</creationTime>", snap.getXMLDesc(0))
    return int(m.group(1)) if m else 0


def _lookup_snapshot(client: LibvirtClient, dom: Any, name: str) -> Any | None:
    libvirt = _import_libvirt()
    try:
        return dom.snapshotLookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
            return None
        raise


def create_snapshot(
    client: LibvirtClient,
    vm_backend_name: str,
    name: str,
    description: str = "",
    *,
    mem: bool = False,
) -> None:
    """Snapshot the VM under ``name`` (internal qcow2 snapshot).

    libvirt's internal snapshots are full system checkpoints: a snapshot of a
    *running* domain always captures RAM (a revert resumes the running state),
    and a snapshot of a *shut-off* domain is disk-only. libvirt **rejects** an
    internal disk-only snapshot of a running domain outright ("internal snapshot
    of a running VM must include the memory state"), so ``mem`` cannot toggle that
    on this backend — it is accepted for ABC parity, and a ``mem=True`` request is
    always satisfiable here (memory snapshots are supported, so we never raise on
    it). Either way the disk state is captured and reverts correctly. Raises if
    ``name`` already exists.
    """
    del mem  # libvirt decides memory-vs-disk-only by the domain's run state
    dom = _resolve_domain(client, vm_backend_name)
    if _lookup_snapshot(client, dom, name) is not None:
        raise DriverError(f"snapshot {name!r} already exists on vm {vm_backend_name!r}")
    dom.snapshotCreateXML(_snapshot_xml(name, description), 0)
    _log.info("created snapshot %s on vm %s", name, vm_backend_name)


def list_snapshots(client: LibvirtClient, vm_backend_name: str) -> list[str]:
    dom = _resolve_domain(client, vm_backend_name)
    snaps = sorted(dom.listAllSnapshots(0), key=_creation_time)
    return [s.getName() for s in snaps]


def delete_snapshot(client: LibvirtClient, vm_backend_name: str, name: str) -> None:
    """Delete the named snapshot. No-op if it doesn't exist."""
    dom = _resolve_domain(client, vm_backend_name)
    snap = _lookup_snapshot(client, dom, name)
    if snap is None:
        return
    snap.delete(0)
    _log.info("deleted snapshot %s on vm %s", name, vm_backend_name)


def restore_snapshot(client: LibvirtClient, vm_backend_name: str, name: str) -> None:
    """Revert the VM to ``name``. Raises if the snapshot doesn't exist."""
    dom = _resolve_domain(client, vm_backend_name)
    snap = _lookup_snapshot(client, dom, name)
    if snap is None:
        raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}")
    dom.revertToSnapshot(snap, 0)
    _log.info("reverted vm %s to snapshot %s", vm_backend_name, name)
