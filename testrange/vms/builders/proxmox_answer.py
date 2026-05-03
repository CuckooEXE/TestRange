"""ProxMox VE unattended install :class:`~testrange.vms.builders.base.Builder`.

The install phase boots the prepared ProxMox installer ISO with a
``PROXMOX-AIS``-labeled seed ISO attached as a second CD-ROM.  The
installer reads ``answer.toml`` off the seed and runs unattended.
``reboot-mode = "power-off"`` in the global block tells PVE's
installer to POWER OFF (rather than reboot) on success, so the
orchestrator's install-phase wait sees a clean SHUTOFF edge to key
on for "install complete".  The orchestrator then snapshots the
disk into the cache.

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

import hashlib
import io
import ipaddress
import re
from typing import TYPE_CHECKING, Any

from pycdlib import PyCdlib  # type: ignore[attr-defined]

from testrange._logging import get_logger
from testrange.cache import vm_config_hash
from testrange.devices import vNIC
from testrange.exceptions import CloudInitError
from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.images import resolve_image

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.communication.base import AbstractCommunicator
    from testrange.credentials import Credential
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM as VM

_log = get_logger(__name__)


_DEFAULT_PARTITION_LABEL = "PROXMOX-AIS"
"""Stock label the PVE installer searches for in ``--fetch-from partition`` mode."""


_PVE_BOOTSTRAP_SCRIPT = """\
set -euo pipefail
exec > >(tee -a /var/log/testrange-pve-bootstrap.log) 2>&1
echo "=== testrange PVE bootstrap starting at $(date -Is) ==="

# Swap PVE enterprise repos for the public no-subscription mirror —
# enterprise.proxmox.com 401s without a paid subscription, and
# apt-get update under set -e tanks the rest of the script.  Both
# .list (legacy) and .sources (PVE 9 deb822) variants removed so
# we don't depend on which format the installer chose.
rm -f /etc/apt/sources.list.d/pve-enterprise.list \\
      /etc/apt/sources.list.d/pve-enterprise.sources \\
      /etc/apt/sources.list.d/ceph.list \\
      /etc/apt/sources.list.d/ceph.sources
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb http://download.proxmox.com/debian/pve $codename pve-no-subscription" \\
  > /etc/apt/sources.list.d/pve-no-subscription.list

# Install dnsmasq for the SDN per-vnet DHCP+DNS integration; disable
# the default systemd unit because PVE's SDN spawns its own dnsmasq
# instances per-vnet via ifupdown hooks and the systemd unit's
# 0.0.0.0:53/67 binds would conflict.
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y dnsmasq
systemctl disable --now dnsmasq

echo "=== testrange PVE bootstrap completed at $(date -Is) ==="
"""
"""Bash script run inside the freshly-installed PVE node, baked into
the cached install artifact via :meth:`ProxmoxAnswerBuilder.post_install_hook`.

Runs over SSH on the install network (always ``internet=True``) before
the install VM is templated, so the cached PVE template carries
dnsmasq + the no-subscription repo regardless of whatever run-phase
network the orchestrator later swaps the clone onto.  This is what
makes airgapped run topologies work — without it the run VM would
need internet to apt-get install dnsmasq at first inner-orchestrator
entry.

Idempotency: every step (``rm -f``, ``apt install``,
``systemctl disable --now``) is idempotent, so re-running against an
already-bootstrapped node is a no-op.  Output goes to
``/var/log/testrange-pve-bootstrap.log`` inside the guest for
post-mortem when something does go wrong.
"""


_PVE_BOOTSTRAP_TIMEOUT_S = 300
"""Maximum seconds for the bootstrap to complete.

Apt-get update + install on a cold PVE node typically takes 30-90s.
The cap here is generous to absorb slow public-mirror tails without
masking a true hang."""


_PVE_APT_INSECURE_PROLOGUE = """\
# apt_insecure=True: skip TLS peer/host verification for every APT
# HTTPS operation on this node, including the dnsmasq install below
# AND any subsequent apt invocations on the installed system.
# Useful when the PVE mirror is internal and presents a CA that
# isn't in the default trust store; harmless otherwise.  The
# drop-in survives in the cached template so user apt commands
# after a cache hit also skip verification.  Filename matches the
# cloud-init builder's drop-in
# (``/etc/apt/apt.conf.d/99testrange-insecure``) for symmetry.
mkdir -p /etc/apt/apt.conf.d
cat >/etc/apt/apt.conf.d/99testrange-insecure <<'EOF'
Acquire::https::Verify-Peer "false";
Acquire::https::Verify-Host "false";
EOF

"""
"""Prolog prepended to :data:`_PVE_BOOTSTRAP_SCRIPT` when
``apt_insecure=True``.  Lands on disk before any apt command runs,
so the dnsmasq install and any future apt operations both pick up
the relaxed TLS config."""


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
    :param apt_insecure: If ``True``, the post-install bootstrap
        drops an apt.conf.d snippet that disables HTTPS peer/host
        verification for every APT operation on the installed PVE
        node.  Useful when the chosen mirror is internal and
        presents a CA that isn't in the default trust store.
        Defaults to ``False``.  Mirrors the flag of the same name
        on :class:`~testrange.vms.builders.CloudInitBuilder` — APT
        TLS trust is process-wide, so a per-:class:`Apt`-package
        switch would be a lie.  The drop-in is written to
        ``/etc/apt/apt.conf.d/99testrange-insecure`` (same path the
        cloud-init builder uses) and survives in the cached PVE
        template, so user apt commands inside the VM after a cache
        hit also skip verification.

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
    apt_insecure: bool
    """If ``True``, post-install bootstrap configures APT to skip TLS verification."""

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
        apt_insecure: bool = False,
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
        self.apt_insecure = apt_insecure

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

        ``vm.pkgs`` and ``vm.post_install_cmds`` are hashed for
        the cache-key contract but **not actually applied** by the
        PVE installer — its answer.toml schema has no equivalent of
        cloud-init's ``packages`` / ``runcmd``.  For nested
        ``Hypervisor(orchestrator=ProxmoxOrchestrator)``, the
        dnsmasq + repo-swap bootstrap runs over SSH from
        :meth:`post_install_hook` during the install phase (re-boot
        between SHUTOFF and snapshot, on the bare-metal install
        network) so it's baked into the cached PVE template.
        User-supplied ``vm.pkgs`` / ``vm.post_install_cmds`` on a
        PVE Hypervisor are silently ignored today.  Documented here
        so a future slice deliberately decides whether to plumb
        them (e.g. by extending :meth:`post_install_hook`) instead
        of re-introducing the prior ``[first-boot]`` rendering
        machinery.
        """
        return vm_config_hash(
            iso=vm.iso,
            usernames_passwords_sudo=[
                (c.username, c.password, c.sudo) for c in vm.users
            ],
            package_reprs=[repr(p) for p in vm.pkgs],
            post_install_cmds=[
                *vm.post_install_cmds,
                *self._network_block(vm),
                # Fold the bootstrap script digest into the cache key
                # so any edit to ``_PVE_BOOTSTRAP_SCRIPT`` invalidates
                # every cached PVE template — the bootstrap is baked
                # into the installed system by ``post_install_hook``,
                # so a stale template would silently survive the fix.
                f"post_install_hook={self.post_install_cache_key_extra(vm)}",
            ],
            disk_size=vm._primary_disk_size(),
        )

    def has_post_install_hook(self) -> bool:
        """Always ``True`` — :meth:`post_install_hook` runs the PVE
        bootstrap (apt install dnsmasq, repo swap) and is required for
        the cached install artifact to work on airgapped run-phase
        networks.
        """
        return True

    def post_install_hook(
        self,
        vm: VM,
        communicator: AbstractCommunicator,
    ) -> None:
        """Run :meth:`_build_bootstrap_script` on the freshly-installed
        PVE node so the cached install artifact carries dnsmasq +
        the no-subscription repo (and, when :attr:`apt_insecure` is
        ``True``, an apt.conf.d drop-in that disables TLS verification).

        The orchestrator re-starts the install VMID on the install
        network (always ``internet=True``) before calling this; the
        script reaches ``download.proxmox.com`` and ``deb.debian.org``
        through the install-network gateway regardless of whatever
        run-phase network the clone will eventually land on.

        :raises CloudInitError: If the bootstrap exits non-zero.  The
            exception message includes the tail of stderr to make the
            cause obvious without requiring an SSH login.  The full
            log is at ``/var/log/testrange-pve-bootstrap.log`` inside
            the guest.
        """
        del vm  # unused — bootstrap is target-agnostic
        _log.info(
            "running PVE bootstrap (apt install dnsmasq + repo swap%s) "
            "over %s; baking into cached install artifact",
            ", apt_insecure=True" if self.apt_insecure else "",
            type(communicator).__name__,
        )
        result = communicator.exec(
            ["bash", "-c", self._build_bootstrap_script()],
            timeout=_PVE_BOOTSTRAP_TIMEOUT_S,
        )
        if result.exit_code != 0:
            stderr_tail = (result.stderr or b"").decode("utf-8", "replace")[-500:]
            raise CloudInitError(
                f"PVE bootstrap exited {result.exit_code}.  See "
                "``/var/log/testrange-pve-bootstrap.log`` inside the "
                "install VM for full output.  stderr tail:\n"
                f"{stderr_tail}"
            )

    def post_install_cache_key_extra(self, vm: VM) -> str:
        """Return a deterministic 24-hex-char digest of the rendered
        bootstrap script for this builder's config.

        Matches the 24-char width of :func:`vm_config_hash`'s output so
        debug logs that print partial hashes line up.  Folded into
        :meth:`cache_key` to invalidate cached templates whenever any
        script-affecting state (the script body itself OR the
        :attr:`apt_insecure` toggle that prepends an apt.conf.d
        prologue) changes.
        """
        del vm
        return hashlib.sha256(
            self._build_bootstrap_script().encode("utf-8"),
        ).hexdigest()[:24]

    def _build_bootstrap_script(self) -> str:
        """Return the bootstrap script body for this builder's config.

        Default returns :data:`_PVE_BOOTSTRAP_SCRIPT` unchanged.  When
        :attr:`apt_insecure` is ``True``, prepends
        :data:`_PVE_APT_INSECURE_PROLOGUE` so the apt.conf.d drop-in
        lands before any apt command runs.
        """
        if self.apt_insecure:
            return _PVE_APT_INSECURE_PROLOGUE + _PVE_BOOTSTRAP_SCRIPT
        return _PVE_BOOTSTRAP_SCRIPT

    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> InstallDomain:
        # 1. Resolve the vanilla installer ISO (download if needed),
        #    then produce a prepared copy whose initrd drops into
        #    auto-install / fetch-from-partition mode.
        vanilla = resolve_image(vm.iso, cache)
        prepared_local = cache.get_proxmox_prepared_iso(vanilla)
        prepared_ref = cache.stage_source(prepared_local, run.storage)

        # 2. Blank OS disk — the PVE installer partitions it itself
        #    (single-disk ext4 layout by default).
        work_disk_ref = run.create_blank_disk(
            vm.name, vm._primary_disk_size()
        )

        # 3. Seed ISO carrying answer.toml.  The PVE installer reads
        #    it off the partition labelled ``PROXMOX-AIS`` (or
        #    whatever ``self.partition_label`` is set to); compose
        #    via the generic :meth:`RunDir.path_for` helper rather
        #    than the cloud-init-specific seed helpers.
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

# ``_PVE_BOOTSTRAP_SCRIPT`` and ``_PVE_BOOTSTRAP_TIMEOUT_S`` are
# intentionally underscore-prefixed (module-private constants) but
# imported by the proxmox backend's VM build path and by tests, so
# they live above the ``__all__`` list rather than in it.
