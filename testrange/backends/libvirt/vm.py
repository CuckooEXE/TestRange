"""libvirt-backed virtual machine implementation.

This module provides the concrete :class:`VM` class which:

- Stores the VM specification (name, image, users, packages, devices)
- Holds a :class:`~testrange.vms.builders.base.Builder` that encodes
  the install-phase strategy (cloud-init, Windows unattended, or
  no-op)
- Generates libvirt domain XML
- Drives the install phase (power-off wait → cache) for builders that
  need one
- Creates per-run qcow2 overlays
- Exposes runtime methods via the configured communicator
  (QEMU guest agent, SSH, or WinRM)
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import libvirt

from testrange._concurrency import vm_build_lock
from testrange._logging import get_logger, log_duration
from testrange.backends.libvirt.guest_agent import GuestAgentCommunicator
from testrange.cache import CacheManager
from testrange.communication.base import AbstractCommunicator
from testrange.devices import HardDrive, Memory, VirtualNetworkRef, vCPU
from testrange.exceptions import VMBuildError
from testrange.vms.base import AbstractVM
from testrange.vms.builders import Builder, auto_select_builder

_log = get_logger(__name__)

_COMMUNICATOR_KINDS = ("guest-agent", "ssh", "winrm")
"""Legal values for the ``communicator=`` kwarg on :class:`VM`."""

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.storage.transport.base import AbstractFileTransport


def _libvirt_conn(context: AbstractOrchestrator) -> libvirt.virConnect:
    """Extract the libvirt ``virConnect`` handle from the orchestrator.

    The libvirt :class:`VM` implementation is only callable against
    :class:`~testrange.backends.libvirt.Orchestrator`; the abstract method
    accepts the generic :class:`AbstractOrchestrator` type and this
    helper does the backend cast.
    """
    return context._conn  # type: ignore[attr-defined]


_POLL_INTERVAL = 5
"""Seconds between domain-state polls while waiting for the install phase to complete."""

_BUILD_TIMEOUT = 1800
"""Maximum seconds to wait for the VM install phase (cloud-init + power-off)."""

_GA_READY_TIMEOUT = 300
"""Maximum seconds to wait for the QEMU guest agent to become ready after test boot."""

_OVMF_CODE_PATH = "/usr/share/OVMF/OVMF_CODE_4M.fd"
"""Host path to the OVMF firmware code (read-only) used for UEFI domains."""

_OVMF_VARS_TEMPLATE = "/usr/share/OVMF/OVMF_VARS_4M.fd"
"""Host path to the OVMF variables template.  Per-domain NVRAM files
are seeded from this when the caller doesn't provide one — either by
libvirt via the ``<nvram template="...">`` attribute or by us
pre-creating the file (see :func:`_preseed_nvram`).
"""

_BOOT_KEYPRESS_WINDOW_S = 30
"""How long to spam spacebars at the start of an install-phase boot.

Windows install ISOs under UEFI show a ~5 second "Press any key to
boot from CD or DVD..." prompt.  30 seconds gives a wide margin for
slow VMs / slow OVMF startup without meaningfully delaying installs
that don't need any keypress (Setup ignores stray keystrokes once it
has started its own input loop)."""

_BOOT_KEYPRESS_INTERVAL_S = 1.0
"""Gap between spacebars during the boot-keypress window."""

_MAX_CONSECUTIVE_STATE_ERRORS = 5
"""Give up polling ``domain.state()`` after this many consecutive
libvirt errors.  At :data:`_POLL_INTERVAL` = 5s that's ~25 seconds of
lost connection before we bail — fast enough to surface a dead
libvirtd within the user's attention span, slow enough to tolerate a
hiccup during a heavy install."""

_LINUX_KEY_SPACE = 57
"""Linux keycode for SPACE — consumes the 'Press any key' prompt without
risking a menu selection the way ENTER or arrow keys might."""


def _preseed_nvram(nvram_ref: str, transport: AbstractFileTransport) -> None:
    """Pre-create the per-domain NVRAM file from the OVMF_VARS template.

    When libvirt creates the NVRAM file itself (by copying the
    ``<nvram template="...">`` path on first domain start), the DAC
    security driver records no ``remember_owner`` xattr — so on
    domain stop the file stays owned by ``libvirt-qemu`` at mode
    ``0600``, and our Python (running as the invoking user) can't
    read it.  Pre-seeding the file ourselves makes *us* the original
    owner; the DAC driver stores that in an xattr when it chowns
    to ``libvirt-qemu`` on start, and restores it when the domain
    stops.  After shutdown the file is readable again and we can
    snapshot it into the cache.

    The seeded bytes match exactly what libvirt would have copied
    (it's the same template), so runtime behaviour is identical.
    """
    from pathlib import Path as _Path
    transport.write_bytes(
        nvram_ref, _Path(_OVMF_VARS_TEMPLATE).read_bytes(), mode=0o644,
    )


def _destroy_and_undefine(domain: libvirt.virDomain) -> None:
    """Best-effort destroy + undefine of a libvirt domain.

    Swallows every libvirt error so callers can use this in ``finally``
    blocks and teardown paths without worrying about masking the real
    exception.  ``VIR_DOMAIN_UNDEFINE_NVRAM`` covers UEFI domains;
    libvirt ignores the flag for BIOS ones.
    """
    try:
        if domain.isActive():
            domain.destroy()
    except libvirt.libvirtError:
        pass
    try:
        domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
    except (libvirt.libvirtError, AttributeError):
        try:
            domain.undefine()
        except libvirt.libvirtError:
            pass


def _press_any_key_loop(
    domain: libvirt.virDomain,
    stop: threading.Event,
    *,
    duration_s: float = _BOOT_KEYPRESS_WINDOW_S,
    interval_s: float = _BOOT_KEYPRESS_INTERVAL_S,
) -> None:
    """Send spacebar keypresses to *domain* until *stop* is set or
    *duration_s* elapses.

    Runs in a daemon thread during the first seconds of install boot to
    consume UEFI "Press any key to boot from CD or DVD..." prompts.
    Every libvirt error is swallowed — the keypress is a best-effort
    hint, not a hard requirement, and a noisy exception here would
    mask the real install failure.
    """
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline and not stop.is_set():
        try:
            domain.sendKey(
                libvirt.VIR_KEYCODE_SET_LINUX,
                50,                    # hold 50ms
                [_LINUX_KEY_SPACE],
                1,
                0,
            )
        except libvirt.libvirtError:
            pass
        stop.wait(interval_s)


class VM(AbstractVM):
    """A KVM/QEMU virtual machine managed by libvirt.

    .. code-block:: python

        VM(
            name="MyVM",
            iso="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
            users=[Credential("root", "Password123!")],
            pkgs=[Apt("nginx")],
            post_install_cmds=["systemctl enable --now nginx"],
            devices=[vCPU(2), Memory(4), VirtualNetworkRef("NetA"), HardDrive(20)],
        )

    :param name: Unique name for this VM within a test run.
    :param iso: OS image reference: an absolute path to a local
        ``.qcow2`` / ``.img`` / ``.iso`` file, or an ``https://`` URL
        pointing at an upstream cloud image.  When ``builder=`` is a
        :class:`~testrange.vms.builders.NoOpBuilder` this must be a
        local path to a fully provisioned qcow2.
    :param users: List of user credentials.  For install-phase builders
        these are *created* during provisioning; for
        :class:`~testrange.vms.builders.NoOpBuilder` they are treated
        as *already existing* in the image and passed through to the
        communicator.
    :param pkgs: Packages to install during the build phase.  Honoured
        by :class:`~testrange.vms.builders.CloudInitBuilder` (apt/dnf/pip/brew)
        and :class:`~testrange.vms.builders.WindowsUnattendedBuilder`
        (winget only); ignored by
        :class:`~testrange.vms.builders.NoOpBuilder`.
    :param post_install_cmds: Commands run at the end of the install
        phase.  Shell on Linux (cloud-init ``runcmd``), PowerShell on
        Windows (autounattend ``FirstLogonCommands``).
    :param devices: List of :class:`~testrange.devices.AbstractDevice`
        objects defining vCPUs, memory, storage, and network attachments.
    :param builder: Explicit
        :class:`~testrange.vms.builders.base.Builder` strategy.  When
        ``None`` (the default) a builder is auto-selected from ``iso``:
        Windows install ISOs → :class:`WindowsUnattendedBuilder`,
        everything else → :class:`CloudInitBuilder`.  Pass
        :class:`~testrange.vms.builders.NoOpBuilder` explicitly for
        prebuilt qcow2 images.
    :param communicator: Which communication backend to attach on run.
        One of ``"guest-agent"``, ``"ssh"``, ``"winrm"``.  When
        ``None`` (the default) the builder's
        :meth:`~testrange.vms.builders.base.Builder.default_communicator`
        is used.
    """

    _name: str
    """Internal VM name set at construction; exposed via the :attr:`name` property."""

    iso: str
    """OS image reference (absolute local path or ``https://`` URL)."""

    users: list[Credential]
    """User accounts to create during the install phase."""

    pkgs: list[AbstractPackage]
    """Packages to install during the build phase."""

    post_install_cmds: list[str]
    """Commands run after package installation during the build phase."""

    devices: list[AbstractDevice]
    """Device specifications (vCPU, memory, storage, NICs) for this VM."""

    builder: Builder
    """Provisioning strategy — determines how :meth:`build` produces a cached disk."""

    communicator: str
    """Selected communicator backend name (``"guest-agent"``, ``"ssh"``, or ``"winrm"``)."""

    _communicator: AbstractCommunicator | None
    """Communicator instance; ``None`` until :meth:`start_run` completes."""

    _domain: libvirt.virDomain | None
    """Active libvirt domain object; ``None`` before :meth:`start_run` is called."""

    _install_domain: libvirt.virDomain | None
    """Install-phase libvirt domain; ``None`` except during :meth:`_run_install_phase`.

    Stashed on the instance so :meth:`shutdown` can clean it up as a
    safety net if the install phase's own ``finally`` block fails to
    destroy+undefine (e.g. libvirt connection dropped mid-cleanup).
    """

    _run_id: str | None
    """UUID of the current test run; ``None`` outside an active run."""

    def __init__(
        self,
        name: str,
        iso: str,
        users: list[Credential],
        pkgs: list[AbstractPackage] | None = None,
        post_install_cmds: list[str] | None = None,
        devices: list[AbstractDevice] | None = None,
        builder: Builder | None = None,
        communicator: str | None = None,
    ) -> None:
        self._name = name
        self.iso = iso
        self.users = users
        self.pkgs: list[AbstractPackage] = pkgs or []
        self.post_install_cmds: list[str] = post_install_cmds or []
        self.devices: list[AbstractDevice] = devices or []

        # Auto-select a builder from iso= when the caller doesn't pass
        # one.  The registry (testrange.vms.builders.BUILDER_REGISTRY)
        # walks front-to-back; the default entry matches Windows
        # install ISOs and everything else falls through to
        # CloudInitBuilder.  Third-party builders register themselves
        # via testrange.vms.builders.register_builder().
        if builder is None:
            builder = auto_select_builder(iso)
        self.builder = builder

        # Communicator default comes from the builder so each
        # provisioning strategy can pick what it knows works — e.g.
        # the Windows builder installs qemu-guest-agent via
        # FirstLogonCommands but defaults to WinRM because that's
        # reachable earlier in the boot.
        if communicator is None:
            communicator = self.builder.default_communicator()
        self.communicator = communicator

        if communicator not in _COMMUNICATOR_KINDS:
            raise VMBuildError(
                f"VM {name!r}: communicator={communicator!r} is not one of "
                f"{_COMMUNICATOR_KINDS}"
            )

        # Runtime state — set by Orchestrator
        self._communicator = None
        self._domain = None
        self._install_domain = None
        self._run_id = None

    @property
    def name(self) -> str:
        """Return the VM's configured name.

        :returns: Name string.
        """
        return self._name

    def _vcpu_count(self) -> int:
        vcpus = [d for d in self.devices if isinstance(d, vCPU)]
        return vcpus[0].count if vcpus else 2

    def _memory_kib(self) -> int:
        mems = [d for d in self.devices if isinstance(d, Memory)]
        return mems[0].kib if mems else 2 * 1024 * 1024

    def _hard_drives(self) -> list[HardDrive]:
        return [d for d in self.devices if isinstance(d, HardDrive)]

    def _network_refs(self) -> list[VirtualNetworkRef]:
        return [d for d in self.devices if isinstance(d, VirtualNetworkRef)]

    def _primary_disk_size(self) -> str:
        drives = self._hard_drives()
        return drives[0].qemu_size if drives else "20G"

    def _base_domain_xml(
        self,
        domain_name: str,
        disk_path: str | Path,
        seed_iso_path: str | Path | None,
        network_entries: list[tuple[str, str]],  # (libvirt_net_name, mac)
        run_id: str,
        extra_cdroms: list[str] | list[Path] | None = None,
        boot_cdrom: bool = False,
        uefi: bool = False,
        nvram_path: str | Path | None = None,
        windows: bool = False,
    ) -> str:
        """Build a libvirt domain XML string.

        All disk / ISO / NVRAM arguments are **backend-local refs** —
        strings valid on whichever host this domain's libvirtd is
        running on.  For the default local backend those are outer-host
        absolute paths; for ``qemu+ssh://`` they are paths on the
        remote host.

        :param domain_name: The libvirt domain name.
        :param disk_path: Backend-local ref to the primary disk qcow2.
        :param seed_iso_path: Backend-local ref to the cloud-init /
            autounattend seed ISO, or ``None`` to omit the CD-ROM
            entirely (used by the BYOI / prebuilt flow where
            cloud-init is not involved).
        :param network_entries: List of ``(libvirt_network_name, mac_address)``
            pairs, one per NIC.
        :param run_id: Run UUID (for uniqueness in domain name).
        :param extra_cdroms: Additional read-only CD-ROM refs — used
            during Windows install to surface the Windows ISO and the
            virtio-win driver ISO alongside the unattend seed ISO.
        :param boot_cdrom: If ``True``, boot from CD-ROM before disk.  The
            Windows install phase needs this to load the installer from
            the Windows ISO; the run phase boots from the installed disk.
        :param uefi: If ``True``, emit OVMF (UEFI) firmware references.
            Required for Windows 10+ GPT installs.
        :param nvram_path: Backend-local ref to the per-domain NVRAM
            variables file (OVMF_VARS copy).  Required when
            ``uefi=True``.
        :param windows: If ``True``, use device models Windows has native
            drivers for: SATA for the primary disk, e1000e for NICs.
            Keeps the install phase working without the virtio-win
            driver ISO needing to be threaded through ``DriverPaths``.
        :returns: libvirt domain XML string.
        """
        # Tolerate ``pathlib.Path`` as well as backend-local strings —
        # the tests pass ``Path`` objects directly and the builders
        # still pass strings.  Both are valid refs for a local backend.
        disk_path = str(disk_path)
        if seed_iso_path is not None:
            seed_iso_path = str(seed_iso_path)
        if nvram_path is not None:
            nvram_path = str(nvram_path)
        extra_cdroms = [str(c) for c in (extra_cdroms or [])]

        domain = ET.Element("domain", type="kvm")

        ET.SubElement(domain, "name").text = domain_name
        ET.SubElement(domain, "uuid").text = str(uuid.uuid5(
            uuid.NAMESPACE_DNS, domain_name
        ))
        ET.SubElement(domain, "memory", unit="KiB").text = str(self._memory_kib())
        ET.SubElement(domain, "currentMemory", unit="KiB").text = str(
            self._memory_kib()
        )
        ET.SubElement(domain, "vcpu", placement="static").text = str(
            self._vcpu_count()
        )

        os_el = ET.SubElement(domain, "os")
        ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
        if uefi:
            # OVMF split firmware: read-only code + per-VM variables.
            # Writing NVRAM to the run scratch dir keeps each run
            # independent.  Windows Setup flips secure-boot variables
            # during install, so we deliberately pick the non-secureboot
            # code firmware — otherwise the install ISO's own bootloader
            # is rejected on machines without signed shim entries.
            assert nvram_path is not None, "uefi=True requires nvram_path"
            loader = ET.SubElement(
                os_el, "loader",
                readonly="yes", type="pflash",
                secure="no",
            )
            loader.text = _OVMF_CODE_PATH
            nvram = ET.SubElement(os_el, "nvram", template=_OVMF_VARS_TEMPLATE)
            nvram.text = nvram_path
        # NOTE: no <cmdline> — qemu rejects -append without -kernel for disk
        # boots. NoCloud is auto-detected from the "cidata" volume label on
        # the seed ISO, so the ds= kernel arg isn't needed here.
        if boot_cdrom:
            # CDROM first, then HD — Windows installer boots off the ISO
            # and writes to the empty qcow2.
            ET.SubElement(os_el, "boot", dev="cdrom")
            ET.SubElement(os_el, "boot", dev="hd")
        else:
            ET.SubElement(os_el, "boot", dev="hd")

        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "apic")

        ET.SubElement(domain, "cpu", mode="host-passthrough", check="none")

        clock = ET.SubElement(domain, "clock", offset="utc")
        ET.SubElement(clock, "timer", name="rtc", tickpolicy="catchup")
        ET.SubElement(clock, "timer", name="pit", tickpolicy="delay")
        ET.SubElement(clock, "timer", name="hpet", present="no")

        ET.SubElement(domain, "on_poweroff").text = "destroy"
        ET.SubElement(domain, "on_reboot").text = "restart"
        ET.SubElement(domain, "on_crash").text = "destroy"

        devices = ET.SubElement(domain, "devices")
        ET.SubElement(devices, "emulator").text = "/usr/bin/qemu-system-x86_64"

        # Primary disk
        drives = self._hard_drives()
        primary_drive = drives[0] if drives else HardDrive()
        disk_el = ET.SubElement(devices, "disk", type="file", device="disk")
        ET.SubElement(disk_el, "driver", name="qemu", type="qcow2", discard="unmap")
        ET.SubElement(disk_el, "source", file=disk_path)
        if primary_drive.nvme:
            ET.SubElement(disk_el, "target", dev="nvme0n1", bus="nvme")
        elif windows:
            # Windows Setup has native AHCI/SATA drivers but not virtio-blk;
            # using SATA sidesteps the DriverPaths dance with virtio-win.
            ET.SubElement(disk_el, "target", dev="sda", bus="sata")
        else:
            ET.SubElement(disk_el, "target", dev="vda", bus="virtio")

        # Additional data drives (index 1+).  They sit alongside the
        # primary disk in the run dir, so derive the path by string
        # manipulation on the primary ref (sibling-of semantics work
        # identically for local absolute paths and remote paths).
        primary_parent = (
            disk_path.rsplit("/", 1)[0] if "/" in disk_path else ""
        )
        for idx, drive in enumerate(drives[1:], start=1):
            d = ET.SubElement(devices, "disk", type="file", device="disk")
            ET.SubElement(d, "driver", name="qemu", type="qcow2")
            data_name = f"{self._name}-data{idx}.qcow2"
            data_ref = (
                f"{primary_parent}/{data_name}" if primary_parent else data_name
            )
            ET.SubElement(d, "source", file=data_ref)
            if drive.nvme:
                ET.SubElement(d, "target", dev=f"nvme{idx}n1", bus="nvme")
            else:
                dev_names = "bcdefghijklmnop"
                ET.SubElement(d, "target", dev=f"vd{dev_names[idx - 1]}", bus="virtio")

        # CD-ROM chain.  SATA target letters are assigned starting *after*
        # the primary disk on Windows (which lives at sda) so we don't
        # collide; on Linux the primary disk is on virtio-blk (vda),
        # leaving sda/sdb/... free for CD-ROMs.
        #
        # Order matters when boot_cdrom=True: libvirt expands
        # <boot dev='cdrom'/> by assigning bootindex=1 to the first CDROM
        # in the device list.  For Windows installs the intended boot
        # media is the Windows ISO (first of extra_cdroms); the seed ISO
        # only needs to be *attached* so Setup picks up autounattend.xml
        # from any mounted volume.  Put extras first in that case.
        cdrom_sources: list[str] = []
        if boot_cdrom and extra_cdroms:
            cdrom_sources.extend(extra_cdroms)
            if seed_iso_path is not None:
                cdrom_sources.append(seed_iso_path)
        else:
            if seed_iso_path is not None:
                cdrom_sources.append(seed_iso_path)
            if extra_cdroms:
                cdrom_sources.extend(extra_cdroms)

        _sd_letters = "abcdefghijklmnop"
        cdrom_letter_offset = 1 if (windows and not primary_drive.nvme) else 0
        for cd_idx, cdrom_path in enumerate(cdrom_sources):
            cdrom = ET.SubElement(devices, "disk", type="file", device="cdrom")
            ET.SubElement(cdrom, "driver", name="qemu", type="raw")
            ET.SubElement(cdrom, "source", file=cdrom_path)
            ET.SubElement(
                cdrom, "target",
                dev=f"sd{_sd_letters[cd_idx + cdrom_letter_offset]}",
                bus="sata",
            )
            ET.SubElement(cdrom, "readonly")

        # Network interfaces — Windows Setup has e1000e drivers built in
        # but not virtio-net, so use e1000e for Windows VMs.  Linux
        # guests stay on virtio for performance.
        nic_model = "e1000e" if windows else "virtio"
        for net_name, mac in network_entries:
            iface = ET.SubElement(devices, "interface", type="network")
            ET.SubElement(iface, "source", network=net_name)
            ET.SubElement(iface, "mac", address=mac)
            ET.SubElement(iface, "model", type=nic_model)

        # QEMU Guest Agent virtio-serial channel
        channel = ET.SubElement(devices, "channel", type="unix")
        ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")

        # Serial console (for debugging)
        serial = ET.SubElement(devices, "serial", type="pty")
        ET.SubElement(serial, "target", type="isa-serial", port="0")
        console = ET.SubElement(devices, "console", type="pty")
        ET.SubElement(console, "target", type="serial", port="0")

        # RNG for entropy
        rng = ET.SubElement(devices, "rng", model="virtio")
        ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"

        # Memory balloon
        ET.SubElement(devices, "memballoon", model="virtio")

        # Optional VNC graphics for debugging.  Off by default — headless
        # runs stay headless.  Set TESTRANGE_VNC=1 to attach a localhost
        # VNC server (auto-assigned port) plus a QXL video device so
        # `virt-viewer <domain>` can show the installer screen.
        if os.environ.get("TESTRANGE_VNC") == "1":
            graphics = ET.SubElement(devices, "graphics", type="vnc")
            graphics.set("port", "-1")
            graphics.set("autoport", "yes")
            graphics.set("listen", "127.0.0.1")
            ET.SubElement(graphics, "listen", type="address", address="127.0.0.1")
            video = ET.SubElement(devices, "video")
            ET.SubElement(video, "model", type="qxl", vram="16384")

        ET.indent(domain)
        return ET.tostring(domain, encoding="unicode", xml_declaration=False)

    def build(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
    ) -> str:
        """Produce a runnable disk image for this VM.

        Delegates to the VM's :attr:`builder`:

        - Builders whose :meth:`~testrange.vms.builders.base.Builder.needs_install_phase`
          returns ``False`` (e.g.
          :class:`~testrange.vms.builders.NoOpBuilder`) return a staged
          copy of a pre-existing qcow2 via
          :meth:`~testrange.vms.builders.base.Builder.ready_image`.
        - Other builders compute a cache key, check the post-install
          cache, and either return the hit or run the install phase
          described by their
          :meth:`~testrange.vms.builders.base.Builder.prepare_install_domain`
          output.

        :param context: The libvirt orchestrator; the ``virConnect``
            handle is pulled from it internally.
        :param cache: Active :class:`~testrange.cache.CacheManager`.
        :param run: Scratch dir for this test run.
        :param install_network_name: libvirt name of the NAT install
            network (ignored by builders that don't need one).
        :param install_network_mac: MAC address for the install NIC
            (ignored by builders that don't need one).
        :returns: Backend-local ref to the runnable disk image.  For a
            local libvirt this is an outer-host path; for
            ``qemu+ssh://`` it's the path on the remote where the
            image now lives.
        :raises VMBuildError: If the install phase fails or times out.
        """
        if not self.builder.needs_install_phase():
            return self.builder.ready_image(self, cache, run)

        h = self.builder.cache_key(self)

        # Concurrency: two tests with identical VM specs produce the
        # same cache key and would otherwise both run the install
        # phase AND race to qemu-img convert into the same cached
        # path.  Serialise per-hash so the first arrival installs and
        # subsequent arrivals hit the cache.
        with vm_build_lock(h):
            cached = cache.get_vm(h, run.storage)
            if cached is not None:
                _log.info(
                    "VM %r install cache hit (%s) — skipping install phase",
                    self._name,
                    h[:12],
                )
                return cached
            _log.info(
                "VM %r install cache miss (%s) — running install phase",
                self._name,
                h[:12],
            )
            return self._run_install_phase(
                conn=_libvirt_conn(context),
                cache=cache,
                run=run,
                install_network_name=install_network_name,
                install_network_mac=install_network_mac,
                h=h,
            )

    def _run_install_phase(
        self,
        conn: libvirt.virConnect,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
        h: str,
    ) -> str:
        """Boot the builder's install domain and snapshot the result.

        Factored out of :meth:`build` so the build lock only wraps the
        check-install-store sequence.
        """
        domain_spec = self.builder.prepare_install_domain(self, run, cache)
        nvram_ref = (
            run.nvram_path(self._name) if domain_spec.uefi else None
        )
        if nvram_ref is not None:
            # Seed the per-run NVRAM ourselves so DAC's remember_owner
            # xattr records us as the original owner — otherwise the
            # file ends up ``libvirt-qemu:0600`` after shutdown and
            # :meth:`store_vm_nvram` below can't read it.  Safe to
            # skip if the file already exists (e.g., re-using a run
            # dir), but within ``_run_install_phase`` the run dir is
            # always fresh, so we unconditionally seed.
            _preseed_nvram(nvram_ref, run.storage.transport)

        domain_name = f"tr-build-{self._name[:10]}-{run.run_id[:8]}"
        xml = self._base_domain_xml(
            domain_name=domain_name,
            disk_path=domain_spec.work_disk,
            seed_iso_path=domain_spec.seed_iso,
            network_entries=[(install_network_name, install_network_mac)],
            run_id=run.run_id,
            extra_cdroms=list(domain_spec.extra_cdroms),
            boot_cdrom=domain_spec.boot_cdrom,
            uefi=domain_spec.uefi,
            nvram_path=nvram_ref,
            windows=domain_spec.windows,
        )

        try:
            domain = conn.defineXML(xml)
        except libvirt.libvirtError as exc:
            raise VMBuildError(
                f"Failed to define install domain for {self._name!r}: {exc}"
            ) from exc

        # Stash on the instance before ``create()`` so the outer
        # orchestrator's teardown has a second chance to clean up if
        # the ``finally`` below never runs (e.g. ``create()`` raises
        # — ``defineXML`` has already persisted a domain entry in
        # libvirt, and losing track of it leaks a defined-but-never-
        # started domain that confuses the next run).
        self._install_domain = domain
        try:
            domain.create()
        except libvirt.libvirtError as exc:
            _destroy_and_undefine(domain)
            self._install_domain = None
            raise VMBuildError(
                f"Failed to start install domain for {self._name!r}: {exc}"
            ) from exc
        _log.info(
            "install domain %r running; waiting for %s builder to finish "
            "and power off (timeout %ds)",
            domain_name,
            type(self.builder).__name__,
            _BUILD_TIMEOUT,
        )

        # Start the boot-keypress thread (Windows UEFI 'Press any key'
        # prompt).  No-op for builders that don't need it.  The stop
        # event is set in the finally block below to release the thread
        # before we destroy the domain.
        keypress_stop = threading.Event()
        keypress_thread: threading.Thread | None = None
        if self.builder.needs_boot_keypress():
            keypress_thread = threading.Thread(
                target=_press_any_key_loop,
                args=(domain, keypress_stop),
                name=f"tr-keypress-{self._name}",
                daemon=True,
            )
            keypress_thread.start()
            _log.debug(
                "boot-keypress thread started for %r (%ds window)",
                domain_name, _BOOT_KEYPRESS_WINDOW_S,
            )

        try:
            with log_duration(
                _log, f"install phase for VM {self._name!r}"
            ):
                deadline = time.monotonic() + _BUILD_TIMEOUT
                consecutive_errors = 0
                while time.monotonic() < deadline:
                    try:
                        state, _ = domain.state()
                        consecutive_errors = 0
                        if state == libvirt.VIR_DOMAIN_SHUTOFF:
                            break
                    except libvirt.libvirtError as exc:
                        # A single transient error is fine (libvirtd
                        # restarted, momentary RPC blip).  Persistent
                        # errors mean the connection is dead and we'd
                        # otherwise silently wait out the full
                        # ``_BUILD_TIMEOUT`` before reporting a useless
                        # timeout — surface the real reason instead.
                        consecutive_errors += 1
                        if consecutive_errors >= _MAX_CONSECUTIVE_STATE_ERRORS:
                            raise VMBuildError(
                                f"Lost libvirt connection while waiting "
                                f"for VM {self._name!r} install phase "
                                f"({consecutive_errors} consecutive "
                                f"state() errors): {exc}"
                            ) from exc
                    time.sleep(_POLL_INTERVAL)
                else:
                    raise VMBuildError(
                        f"Install phase timed out after {_BUILD_TIMEOUT}s "
                        f"for VM {self._name!r}"
                    )

            manifest = self.builder.install_manifest(self, h)
            snapshot_ref = cache.store_vm(
                h, domain_spec.work_disk, manifest, run.storage,
            )
            # Preserve UEFI boot entries the installer just wrote.
            # Libvirt's ``VIR_DOMAIN_UNDEFINE_NVRAM`` (passed by
            # :func:`_destroy_and_undefine` in the ``finally`` below)
            # deletes the per-run NVRAM at teardown; without this
            # snapshot the run phase would spin up with an empty
            # OVMF ``BootOrder`` and UEFI would sit at a shell on
            # any distro (ProxMox VE included) that doesn't write
            # the ``/EFI/BOOT/BOOTX64.EFI`` removable fallback.
            if domain_spec.uefi and nvram_ref is not None:
                cache.store_vm_nvram(h, nvram_ref, run.storage)
            return snapshot_ref
        finally:
            # Stop the keypress thread before destroying the domain —
            # sendKey on a destroyed domain raises (swallowed) but
            # joining keeps thread counts honest in tests/logs.
            keypress_stop.set()
            if keypress_thread is not None:
                keypress_thread.join(timeout=5)

            # Always destroy + undefine, whether we hit shutoff, a timeout,
            # a cache-write error, or a KeyboardInterrupt mid-wait.
            # Leaving the domain live wedges the next run: qcow2/NVRAM
            # files stay open and the VM orphans itself under
            # qemu:///system with no Python process to tidy it.
            _destroy_and_undefine(domain)
            self._install_domain = None

    def _make_guest_agent_communicator(self) -> AbstractCommunicator:
        """Construct the libvirt-backed QEMU guest-agent communicator.

        Reached via
        :meth:`~testrange.vms.base.AbstractVM._make_communicator` when
        ``communicator="guest-agent"`` — the SSH and WinRM branches
        live in the ABC and work against every backend unchanged.
        """
        assert self._domain is not None
        return GuestAgentCommunicator(self._domain)

    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],  # (lv_net_name, mac)
        mac_ip_pairs: list[tuple[str, str, str, str]],  # (mac, ip/prefix, gateway, nameserver)
    ) -> None:
        """Create an overlay, write the run seed ISO, define and start the domain.

        After this method returns the domain is running and the configured
        communicator is responding.

        :param context: The libvirt orchestrator; the ``virConnect``
            handle is pulled from it internally.
        :param run: Scratch dir for this test run.
        :param installed_disk: Backend-local ref to the cached installed
            (or prebuilt) disk image.
        :param network_entries: ``(lv_network_name, mac)`` pairs for domain
            XML generation.
        :param mac_ip_pairs: ``(mac, ip_with_cidr, gateway, nameserver)`` for
            cloud-init network-config.  Pass empty ``ip_with_cidr`` for DHCP.
            Empty ``gateway``/``nameserver`` skips those fields per-NIC.
        :raises VMBuildError: If the domain cannot be defined or started.
        :raises VMTimeoutError: If the communicator does not respond in time.
        """
        conn = _libvirt_conn(context)
        self._run_id = run.run_id

        # Create overlay on the cached installed (or prebuilt) disk —
        # happens on the backend (local qemu-img locally, remote
        # qemu-img via SSH for qemu+ssh:// connections).
        overlay_ref = run.create_overlay(self._name, installed_disk)

        # Hand the run-phase domain shape off to the builder.  The
        # builder decides whether to write a phase-2 seed ISO (cloud-init
        # rotates instance-id here; Windows + NoOp don't) and which
        # firmware / device models the run domain needs.
        run_spec = self.builder.prepare_run_domain(self, run, mac_ip_pairs)
        nvram_ref = run.nvram_path(self._name) if run_spec.uefi else None

        # Seed the per-run NVRAM from the install-phase snapshot when
        # we have one.  Libvirt uses the file as-is if it exists; it
        # only copies from the ``template=`` attribute when the file
        # is absent.  Derived from ``installed_disk`` rather than
        # asking ``cache`` directly so we don't have to thread a
        # CacheManager through :meth:`AbstractVM.start_run`.  Relies
        # on the cache layout convention
        # ``<vms_dir>/<hash>.qcow2`` ↔ ``<vms_dir>/<hash>.nvram.fd``
        # (see :meth:`~testrange.cache.CacheManager.vm_nvram_ref`).
        if nvram_ref is not None and installed_disk.endswith(".qcow2"):
            transport = run.storage.transport
            cached_nvram = installed_disk.removesuffix(".qcow2") + ".nvram.fd"
            if transport.exists(cached_nvram):
                _log.debug(
                    "seeding run-phase NVRAM from cached %s",
                    cached_nvram,
                )
                transport.write_bytes(
                    nvram_ref, transport.read_bytes(cached_nvram),
                )

        domain_name = f"tr-{self._name[:10]}-{run.run_id[:8]}"
        xml = self._base_domain_xml(
            domain_name=domain_name,
            disk_path=overlay_ref,
            seed_iso_path=run_spec.seed_iso,
            network_entries=network_entries,
            run_id=run.run_id,
            uefi=run_spec.uefi,
            nvram_path=nvram_ref,
            windows=run_spec.windows,
        )

        try:
            self._domain = conn.defineXML(xml)
            self._domain.create()
        except libvirt.libvirtError as exc:
            raise VMBuildError(
                f"Failed to start run domain for {self._name!r}: {exc}"
            ) from exc
        _log.debug("run domain %r defined and started", domain_name)

        self._communicator = self._make_communicator(mac_ip_pairs)
        with log_duration(
            _log, f"wait for {self.communicator} on {self._name!r}"
        ):
            self._communicator.wait_ready(timeout=_GA_READY_TIMEOUT)

    def shutdown(self) -> None:
        """Gracefully destroy any running domain for this VM.

        Cleans up both the run-phase domain (set by :meth:`start_run`)
        and — as a safety net — the install-phase domain (set by
        :meth:`_run_install_phase`).  The install-phase domain is
        normally undefined by the ``finally`` block inside that method
        before it returns; this handles the case where that cleanup
        itself failed.

        :raises VMNotRunningError: If neither domain has been set.
        """
        if self._domain is None and self._install_domain is None:
            from testrange.exceptions import VMNotRunningError
            raise VMNotRunningError(f"VM {self._name!r} is not running.")
        if self._install_domain is not None:
            _destroy_and_undefine(self._install_domain)
            self._install_domain = None
        if self._domain is not None:
            _destroy_and_undefine(self._domain)
            self._domain = None
        self._communicator = None

    def __repr__(self) -> str:
        return f"VM(name={self._name!r}, iso={self.iso!r})"
