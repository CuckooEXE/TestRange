"""ProxMox VE unattended install :class:`~testrange.vms.builders.base.Builder`.

The install phase boots the prepared ProxMox installer ISO with a
``PROXMOX-AIS``-labeled seed ISO attached as a second CD-ROM.  The
installer reads ``answer.toml`` off the seed, runs unattended, and —
because the install domain is configured with
``<on_reboot>destroy</on_reboot>`` via
:class:`~testrange.vms.builders.base.InstallDomain.reboot_is_install_done`
— the first reboot out of the installer (into the freshly-installed
system) is treated as install completion.  The orchestrator then
snapshots the disk into the cache.

The run phase boots the cached disk normally, with no seed ISO —
``answer.toml`` baked in the root password and SSH keys during
install, so the VM comes up reachable without further state injection.

The base ProxMox ISO must first be *prepared* (initrd patched to enter
unattended mode).  That's handled by
:mod:`testrange.vms.builders._proxmox_prepare`, a pure-Python
replacement for ``proxmox-auto-install-assistant prepare-iso``;
:meth:`~testrange.cache.CacheManager.get_proxmox_prepared_iso` caches
the prepared copy so each ISO version is prepared only once.
"""

from __future__ import annotations

import io
import ipaddress
import re
from typing import TYPE_CHECKING, Any

from pycdlib import PyCdlib  # type: ignore[attr-defined]

from testrange._logging import get_logger
from testrange.cache import vm_config_hash
from testrange.devices import vNIC
from testrange.exceptions import CloudInitError
from testrange.packages.apt import Apt
from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.images import resolve_image

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM as VM

_log = get_logger(__name__)


_DEFAULT_PARTITION_LABEL = "PROXMOX-AIS"
"""Stock label the PVE installer searches for in ``--fetch-from partition`` mode."""


class ProxmoxAnswerBuilder(Builder):
    """ProxMox VE auto-installer strategy.

    Stateless: per-VM fields (hostname, root password, SSH keys) are
    read from the VM argument on every call.  Installer-wide knobs
    (country, keyboard, timezone, filesystem, default disk) live on
    the builder.

    :param country: Two-letter country code used in
        ``answer.toml [global] country``.  Defaults to ``"us"``.
    :param keyboard: Keyboard layout for the installer and installed
        system.  Defaults to ``"en-us"``.
    :param timezone: Installed-system timezone.  Defaults to ``"UTC"``.
    :param fqdn_domain: Domain portion used to build each VM's FQDN as
        ``<vm.name>.<fqdn_domain>``.  Defaults to
        ``"testrange.local"``.
    :param mailto: Address for PVE root mail.  Defaults to
        ``"root@testrange.local"``.
    :param filesystem: Filesystem for the root install.  Defaults to
        ``"ext4"``.  Multi-disk (ZFS / btrfs-raid) layouts are out of
        scope for v0 — override the builder and extend
        :meth:`build_answer_toml` if you need them.
    :param disk_device: Guest-visible device name the installer
        partitions.  Defaults to ``"vda"`` — virtio-blk attachments
        (the cross-backend default) show up as ``/dev/vda`` inside
        the guest.  Override for backends that attach the install
        disk on a different bus.
    :param partition_label: Volume label the prepared installer
        searches for at install time to read ``answer.toml`` off a
        seed ISO.  Defaults to the stock ProxMox value,
        ``"PROXMOX-AIS"``.
    :param uefi: If ``True`` (default), boot the install + run
        domains under OVMF (UEFI).  PVE installer media is hybrid
        BIOS + UEFI, but BIOS-mode GRUB (``i386-pc``) triple-faults
        under SeaBIOS + q35 + SATA-CD — a combination some backends
        generate by default for CD-ROMs.  UEFI sidesteps the
        problem by running through OVMF + ``x86_64-efi`` GRUB, which
        reads the ISO via ``EFI_BLOCK_IO_PROTOCOL`` instead of
        BIOS INT 13h.  Flip to ``False`` only if you're chasing a
        firmware-specific bug or provisioning a VM that must match
        a BIOS-only production layout — and be prepared for the
        install to reboot-loop if your backend still attaches the
        CD on SATA.
    :param network_cidr_prefix: Prefix length to assume for a VM's
        static IP when emitting the ``[network]`` block.  Defaults
        to ``24`` — matches TestRange's default ``VirtualNetwork``
        subnet widths.  Override when using a non-/24 network.
    :param network_gateway: Explicit gateway IP.  Defaults to
        ``None`` → auto-derived as the first host of the VM's
        static-IP subnet (``.1``), which is the convention
        TestRange's default networks ship with.
    :param network_dns: Explicit DNS server IP.  Defaults to
        ``None`` → falls back to *network_gateway* (the network's
        DHCP/DNS service typically answers on the gateway).
    :param network_interface: Interface name the PVE installer's
        ``[network] filter.ID_NET_NAME`` matches against.  Defaults
        to ``"enp1s0"`` — systemd-udev's predictable name for
        virtio-net on q35 pcie-root-port slot 0x1, the topology
        TestRange's default-bus backends assign to the single NIC
        in both install and run phases.  Filter must match a NIC
        *present during install* (PVE's installer validates this
        before writing config), and the written config references
        the NIC by name — so interface-name filtering is stable
        across the install-to-run MAC change.  Override for
        multi-NIC layouts or backends with different PCI topology.

    .. note::

       PVE's ``[network] source = "from-dhcp"`` does **not** mean
       "DHCP on every boot" — it means "take the current DHCP lease
       and freeze it as static config in the installed system."
       Under TestRange that freezes the *install-phase* lease (from
       a throwaway 192.168.24x subnet), which the run-phase NIC on
       the user-facing test network can't use.  To avoid this, this
       builder emits ``source = "from-answer"`` whenever the VM
       declares a static IP on its primary
       :class:`~testrange.devices.vNIC` — derivable
       deterministically from the VM spec without touching the
       install-phase network.  If no static IP is declared, it
       falls back to ``from-dhcp`` and logs a warning; the VM
       will install, but will likely be unreachable in the run
       phase for the reason above.
    """

    country: str
    keyboard: str
    timezone: str
    fqdn_domain: str
    mailto: str
    filesystem: str
    disk_device: str
    partition_label: str
    uefi: bool
    network_cidr_prefix: int
    network_gateway: str | None
    network_dns: str | None
    network_interface: str

    def __init__(
        self,
        country: str = "us",
        keyboard: str = "en-us",
        timezone: str = "UTC",
        fqdn_domain: str = "testrange.local",
        mailto: str = "root@testrange.local",
        filesystem: str = "ext4",
        disk_device: str = "vda",
        partition_label: str = _DEFAULT_PARTITION_LABEL,
        uefi: bool = True,
        network_cidr_prefix: int = 24,
        network_gateway: str | None = None,
        network_dns: str | None = None,
        network_interface: str = "enp1s0",
    ) -> None:
        self.country = country
        self.keyboard = keyboard
        self.timezone = timezone
        self.fqdn_domain = fqdn_domain
        self.mailto = mailto
        self.filesystem = filesystem
        self.disk_device = disk_device
        self.partition_label = partition_label
        self.uefi = uefi
        self.network_cidr_prefix = network_cidr_prefix
        self.network_gateway = network_gateway
        self.network_dns = network_dns
        self.network_interface = network_interface

    def default_communicator(self) -> str:
        """PVE installs ship OpenSSH on by default; the host-side
        guest-agent socket isn't wired by the base install, so SSH is
        the reliable channel."""
        return "ssh"

    def needs_boot_keypress(self) -> bool:
        """PVE GRUB auto-enters the boot entry after a countdown — no
        keypress spam needed."""
        return False

    def cache_key(self, vm: VM) -> str:
        """Fold the ProxMox-specific install state into the cache hash.

        Includes the same fields other install-phase builders use
        (iso, users, packages, post-install, disk size) plus the
        ``[network]`` block of ``answer.toml``.  The network block
        ends up baked into the installed system's
        ``/etc/network/interfaces``, so two VMs with different
        static IPs produce different installed systems and MUST NOT
        share a cache entry.  SSH keys are intentionally excluded
        so key rotation does not invalidate cached builds.
        """
        return vm_config_hash(
            iso=vm.iso,
            usernames_passwords_sudo=[
                (c.username, c.password, c.sudo) for c in vm.users
            ],
            package_reprs=[repr(p) for p in vm.pkgs],
            post_install_cmds=[*vm.post_install_cmds, *self._network_block(vm)],
            disk_size=vm._primary_disk_size(),
        )

    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> InstallDomain:
        # 1. Resolve the vanilla installer ISO (download if needed),
        #    then produce a prepared copy whose initrd drops into
        #    auto-install / fetch-from-partition mode.  The first-
        #    boot script (if vm.pkgs / vm.post_install_cmds asked
        #    for one) lands on the *prepared installer ISO* at
        #    ``/proxmox-first-boot`` — the literal path PVE's
        #    ``proxmox-fetch-answer`` reads.  An earlier version put
        #    it on the answer seed ISO; PVE's installer aborts with
        #    "Failed loading first-boot executable from iso (was iso
        #    prepared with --on-first-boot)" because it only checks
        #    the installer ISO for that file.  Cache key on the
        #    prepared-ISO side incorporates the script hash so VMs
        #    with different first-boot payloads get distinct cached
        #    images.
        vanilla = resolve_image(vm.iso, cache)
        prepared_local = cache.get_proxmox_prepared_iso(
            vanilla,
            first_boot_script=_first_boot_script(vm),
        )
        prepared_ref = cache.stage_source(prepared_local, run.storage)

        # 2. Blank OS disk — the PVE installer partitions it itself
        #    (single-disk ext4 layout by default).
        work_disk_ref = run.create_blank_disk(
            vm.name, vm._primary_disk_size()
        )

        # 3. Seed ISO carrying answer.toml only — the [first-boot]
        #    section in the TOML points the installer at the
        #    ``/proxmox-first-boot`` file we already embedded in the
        #    prepared installer ISO above.  The filename convention
        #    is builder-specific (the PVE installer looks for the
        #    ``PROXMOX-AIS`` label rather than reading the filename),
        #    so compose via the generic :meth:`RunDir.path_for`
        #    helper rather than the cloud-init-specific seed helpers.
        seed_ref = run.path_for(f"{vm.name}-proxmox-answer.iso")
        run.storage.transport.write_bytes(
            seed_ref,
            build_proxmox_seed_iso_bytes(
                self.build_answer_toml(vm),
                volume_label=self.partition_label,
            ),
        )

        return InstallDomain(
            work_disk=work_disk_ref,
            seed_iso=seed_ref,
            extra_cdroms=(prepared_ref,),
            uefi=self.uefi,
            windows=False,
            boot_cdrom=True,
        )

    def install_manifest(
        self,
        vm: VM,
        config_hash: str,
    ) -> dict[str, Any]:
        return {
            "name": vm.name,
            "iso": vm.iso,
            "users": [
                {"username": c.username, "sudo": c.sudo} for c in vm.users
            ],
            "packages": [repr(p) for p in vm.pkgs],
            "post_install_cmds": vm.post_install_cmds,
            "disk_size": vm._primary_disk_size(),
            "config_hash": config_hash,
            "proxmox": True,
        }

    def prepare_run_domain(
        self,
        vm: VM,
        run: RunDir,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> RunDomain:
        # The cached disk already has root password, SSH keys,
        # hostname, AND static network config written in by the
        # installer (see :meth:`_network_block`) — no phase-2 seed
        # needed.  The static config uses a MAC-filtered
        # ``[network]`` block in answer.toml, so the PVE installer's
        # generated ``/etc/network/interfaces`` matches whichever
        # NIC the backend assigns at run time.
        # Firmware family MUST match what install used; mismatched
        # OVMF vs SeaBIOS produces a disk that panics on boot.
        return RunDomain(seed_iso=None, uefi=self.uefi, windows=False)

    # ------------------------------------------------------------------
    # answer.toml generation — kept public so tests / debuggers can
    # inspect the exact payload without booting anything.
    # ------------------------------------------------------------------

    def build_answer_toml(self, vm: VM) -> str:
        """Produce the ``answer.toml`` for *vm*.

        :raises CloudInitError: If no root credential is present (the
            PVE installer requires a root password).
        """
        root = _root_credential(vm.users)
        ssh_keys = [c.ssh_key for c in vm.users if c.ssh_key]

        # PVE 9.x answer.toml uses kebab-case field names for
        # multi-word keys (`root-password`, `root-ssh-keys`,
        # `disk-list`, `reboot-mode`) — verified against
        # proxmox-auto-installer/src/answer.rs and the upstream
        # minimal.toml test fixture.  Single-word fields stay as-is.
        #
        # reboot-mode = "power-off" is critical: it tells the
        # installer to POWER OFF (not reboot) after a successful
        # install, so the install-phase wait loop sees the SHUTOFF
        # edge it keys on.  Without it, the default ``reboot`` mode
        # would kick the VM back into the installer ISO and loop
        # forever until the build timeout — and a reboot-as-poweroff
        # backend hook isn't a workaround because early-boot failures
        # also issue sysrq reboots, which would silently cache a
        # blank disk.
        global_block = [
            f"country = {_toml_str(self.country)}",
            f"keyboard = {_toml_str(self.keyboard)}",
            f"timezone = {_toml_str(self.timezone)}",
            f"fqdn = {_toml_str(f'{vm.name}.{self.fqdn_domain}')}",
            f"mailto = {_toml_str(self.mailto)}",
            f"root-password = {_toml_str(root.password)}",
            'reboot-mode = "power-off"',
        ]
        if ssh_keys:
            global_block.append(
                "root-ssh-keys = ["
                + ", ".join(_toml_str(k) for k in ssh_keys)
                + "]"
            )

        network_block = self._network_block(vm)

        disk_block = [
            f"filesystem = {_toml_str(self.filesystem)}",
            f"disk-list = [{_toml_str(self.disk_device)}]",
        ]

        lines: list[str] = ["[global]", *global_block, "", "[network]",
                            *network_block, "", "[disk-setup]", *disk_block]

        # Only emit ``[first-boot]`` when something actually wants to
        # run there — a stray empty section trips PVE's installer
        # validator.  When ``vm.pkgs`` (apt installs) or
        # ``vm.post_install_cmds`` (free-form shell) are non-empty,
        # ``_first_boot_script`` renders the bash that the embedded
        # ``/first-boot`` script on the seed ISO will execute, and
        # the ``[first-boot]`` block here points the installer at it.
        if _first_boot_script(vm) is not None:
            lines += ["", "[first-boot]",
                      'source = "from-iso"',
                      'ordering = "fully-up"']
        return "\n".join(lines) + "\n"

    def _network_block(self, vm: VM) -> list[str]:
        """Return the ``[network]`` section body as a list of TOML lines.

        Picks between ``source = "from-answer"`` (explicit static
        config derived from the VM's primary
        :class:`vNIC`) and ``source = "from-dhcp"``
        (the PVE default, which freezes the install-phase lease as
        static — unusable under TestRange's install/test-network
        split).  See the builder docstring ``.. note::`` for why
        static is strongly preferred.
        """
        ref = _primary_network_ref(vm)
        if ref is None or not ref.ip:
            _log.warning(
                "VM %r has no static IP on its primary vNIC; "
                "falling back to answer.toml source = \"from-dhcp\".  PVE "
                "will bake the install-phase DHCP lease as static config, "
                "leaving the VM unreachable in the run phase.  Set "
                "``vNIC(..., ip=\"...\")`` to emit a "
                "``from-answer`` block instead.",
                vm.name,
            )
            return ['source = "from-dhcp"']

        # Derive the full CIDR / gateway / DNS from the static IP +
        # the builder's prefix default.  ``ipaddress.ip_network(...,
        # strict=False)`` coerces the host IP into its network, so
        # we can compute the first-host (.1) as the gateway.
        net = ipaddress.ip_network(
            f"{ref.ip}/{self.network_cidr_prefix}", strict=False,
        )
        gateway = self.network_gateway or str(net.network_address + 1)
        dns = self.network_dns or gateway
        cidr = f"{ref.ip}/{self.network_cidr_prefix}"

        # Interface-name filter, not MAC.  PVE's installer REQUIRES
        # the filter to match a NIC currently present during install
        # (it fails loud with "filter did not match any device" if
        # not), and the install-phase NIC has a different MAC than
        # the run-phase NIC under TestRange (backends rotate the MAC
        # to keep the install vs. test networks isolated).
        # Interface-name filtering sidesteps the mismatch:
        # systemd-udev names virtio-net on the standard q35
        # pcie-root-port 0x1 as ``enp1s0`` deterministically from
        # the PCI path, so the install-phase NIC and the run-phase
        # NIC share the name.  The written
        # ``/etc/network/interfaces`` references the NIC by name
        # too, so the config applies cleanly at run time regardless
        # of the MAC change.
        return [
            'source = "from-answer"',
            f"cidr = {_toml_str(cidr)}",
            f"gateway = {_toml_str(gateway)}",
            f"dns = {_toml_str(dns)}",
            f"filter.ID_NET_NAME = {_toml_str(self.network_interface)}",
        ]


# ----------------------------------------------------------------------
# Module-level helpers.  Stateless — kept out of the builder class so
# tests can import them directly.
# ----------------------------------------------------------------------


def _primary_network_ref(vm: VM) -> vNIC | None:
    """Return the VM's first :class:`vNIC` device, or ``None``.

    The PVE installer configures exactly one NIC at install time
    (no equivalent of cloud-init's network-config v2), so the
    "primary" NIC is whichever :class:`vNIC` appears
    first in ``vm.devices``.  Multi-NIC PVE installs would need a
    post-install hook to bring up secondary interfaces; out of
    scope for v0.
    """
    for device in vm.devices:
        if isinstance(device, vNIC):
            return device
    return None


def _first_boot_script(vm: VM) -> str | None:
    """Compose a ``/first-boot`` bash script for *vm* or return ``None``.

    The PVE auto-installer's ``[first-boot] source = "from-iso"`` mode
    runs an executable named ``first-boot`` from the answer ISO at
    first boot of the freshly-installed system.  We use it as the
    one-and-only seam between TestRange's per-VM ``vm.pkgs`` /
    ``vm.post_install_cmds`` declarations and the PVE installer
    (which otherwise has no notion of "extra packages" — its design
    is "full PVE node, nothing else").

    Two ingredients land in the script when present:

    * Apt packages from ``vm.pkgs`` — collected into a single
      ``apt-get install -y <pkg> <pkg>...`` line so the network round
      trip happens once.  Non-Apt :class:`AbstractPackage` subclasses
      (``Pip``, ``Dnf``, …) on a PVE host don't make sense and are
      skipped with a warning rather than rendered as broken commands.

    * Free-form shell from ``vm.post_install_cmds`` — appended verbatim
      after the package install runs, so a hook that depends on a just-
      installed package finds it on ``$PATH``.

    Returns ``None`` when neither is set so callers can omit the
    ``[first-boot]`` section entirely (an empty section is a hard
    error from the installer's TOML validator).

    The rendered script:

    * runs under ``set -euo pipefail`` so any failed step aborts the
      first boot — better to surface "dnsmasq install failed" than to
      let the orchestrator's downstream preflight raise a misleading
      "dnsmasq missing" error;
    * sets ``DEBIAN_FRONTEND=noninteractive`` + ``apt-get update``
      before any install so we don't need a separate package-cache
      refresh hook in the spec.
    """
    apt_pkgs = [p.name for p in vm.pkgs if isinstance(p, Apt)]
    skipped = [p for p in vm.pkgs if not isinstance(p, Apt)]
    for pkg in skipped:
        _log.warning(
            "VM %r: package %r is not an Apt package and will be "
            "skipped on the PVE first-boot hook (PVE is Debian-based "
            "and the answer-builder only knows ``Apt``).",
            vm.name, pkg,
        )
    cmds = list(vm.post_install_cmds)
    if not apt_pkgs and not cmds:
        return None

    lines: list[str] = [
        "#!/bin/bash",
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
    ]
    if apt_pkgs:
        lines.append("apt-get update -y")
        lines.append("apt-get install -y " + " ".join(apt_pkgs))
    lines.extend(cmds)
    return "\n".join(lines) + "\n"


def _root_credential(users: list[Credential]) -> Credential:
    """Return the root :class:`Credential` or raise.

    PVE's answer.toml requires ``root_password`` — there's no way to
    tell the installer "skip root setup" — so the VM spec must carry
    a root credential.
    """
    root = next((u for u in users if u.is_root()), None)
    if root is None:
        raise CloudInitError(
            "ProxMox VMs require a Credential(username='root', ...) "
            "entry — answer.toml needs a root_password"
        )
    return root


# Basic TOML string escape — enough for the values answer.toml takes
# (country codes, keyboard layouts, FQDNs, timezone names, SSH key
# strings, passwords).  Covers backslash and double-quote; other
# control chars would already be rejected by the installer.  We avoid
# a full TOML library dependency because this is the only TOML we
# emit in the whole project.
_TOML_ESCAPE_RE = re.compile(r'([\\"])')


def _toml_str(value: str) -> str:
    """Return *value* as a TOML basic string literal."""
    escaped = _TOML_ESCAPE_RE.sub(r"\\\1", value)
    # Disallow embedded newlines — answer.toml has no use for them in
    # the fields we populate, and a stray \n would break parsing.
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def build_proxmox_seed_iso_bytes(
    answer_toml: str,
    *,
    volume_label: str = _DEFAULT_PARTITION_LABEL,
) -> bytes:
    """Return the raw bytes of a ``PROXMOX-AIS``-labeled seed ISO.

    Mirrors
    :func:`testrange.vms.builders.cloud_init.build_seed_iso_bytes` in
    style: pure-Python ISO creation via :mod:`pycdlib`, no external
    tools.  The PVE installer's partition fetch-from mode searches
    attached filesystems for one with *volume_label* and reads
    ``answer.toml`` from its root.

    The first-boot script (when the answer.toml asks for one via
    ``[first-boot] source = "from-iso"``) does **not** live here —
    PVE's ``proxmox-fetch-answer`` reads it from
    ``/proxmox-first-boot`` on the *prepared installer ISO* instead.
    See :func:`testrange.vms.builders._proxmox_prepare.prepare_iso_bytes`
    and :meth:`testrange.cache.CacheManager.get_proxmox_prepared_iso`
    for the embed + cache path.

    :param answer_toml: The rendered TOML content (see
        :meth:`ProxmoxAnswerBuilder.build_answer_toml`).
    :param volume_label: ISO volume label — must match the
        ``partition_label`` baked into the prepared installer ISO's
        initrd.  Defaults to the stock ``PROXMOX-AIS``.
    """
    iso = PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident=volume_label)

    data = answer_toml.encode("utf-8")
    buf = io.BytesIO()
    try:
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            iso_path="/ANSWER.TOM;1",
            joliet_path="/answer.toml",
        )
        iso.write_fp(buf)
        return buf.getvalue()
    except Exception as exc:
        raise CloudInitError(
            f"Failed to build Proxmox seed ISO: {exc}"
        ) from exc
    finally:
        iso.close()


__all__ = [
    "ProxmoxAnswerBuilder",
    "build_proxmox_seed_iso_bytes",
]
