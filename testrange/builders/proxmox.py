"""ProxmoxAnswerBuilder — Proxmox VE auto-installer (answer.toml) builder.

Installs a Proxmox VE node *as a guest* (nested-virt labs) via the PVE 9.x
auto-installer. The build boots the prepared installer ISO (bootable CDROM) with
a ``PROXMOX-AIS``-labelled seed ISO (the data CDROM) carrying ``answer.toml``;
the installer partitions a blank OS disk unattended (installer-origin, BUILD-1).

Build-result contract (ADR-0012)
--------------------------------
PVE's ``answer.toml`` has no ``packages``/``runcmd`` equivalent, so all
provisioning runs in the ``/proxmox-first-boot`` script PVE copies into the
installed system and runs as a oneshot on first boot. That script also carries
the build-result contract: it runs **fail-fast** (``set -eE`` + an ``ERR``
trap), emits the framed ``TESTRANGE-RESULT:`` record to ``/dev/ttyS0``, and
powers off — replacing the prior power-off-edge keying. The orchestrator's
serial sink (every backend) reads it back.

Network (the install/run split)
-------------------------------
The static run-phase address is baked into the installed
``/etc/network/interfaces`` by ``answer.toml`` ``[network] source =
"from-answer"``, with ``vmbr0`` bridging ``network_interface``. That name is a
PCI-slot-derived predictable name, and the build NIC and run NIC sit at
different slots, so the first-boot script also bakes a systemd ``.link`` that
renames the run NIC to ``network_interface`` (see :func:`_pin_mgmt_nic`,
CORE-67) — otherwise ``vmbr0``'s port is missing at run and the static is
unreachable. The first-boot script flushes that static off ``vmbr0`` and DHCPs
off the build switch's sidecar so ``apt`` reaches the mirror during build; the
on-disk config is untouched, so the run boot comes up on the static.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.builders._proxmox_prepare import PREPARE_ISO_RECIPE, prepare_iso
from testrange.builders.base import Builder, NativeAgentProvision
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.devices.network import StaticAddr
from testrange.exceptions import BuilderError, BuildNotReadyError
from testrange.packages.apt import Apt
from testrange.packages.base import Package
from testrange.packages.pip import Pip

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec
    from testrange.networks.base import BuildNic, NetworkAddressing
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


def _import_pycdlib() -> Any:
    """Lazy import. Raises BuilderError with a useful hint if pycdlib is missing."""
    try:
        import pycdlib
    except ImportError as e:
        raise BuilderError(
            "pycdlib is not installed; install with `pip install -e .[proxmox]`"
        ) from e
    return pycdlib


_DEFAULT_PARTITION_LABEL = "PROXMOX-AIS"
_SERIAL_DEVICE = "/dev/ttyS0"
_FIRST_BOOT_LOG = "/var/log/testrange-first-boot.log"
_FAIL_LOG_TAIL_BYTES = 16384


class ProxmoxAnswerBuilder(Builder):
    """Proxmox VE auto-installer strategy (installer-origin).

    Installer-wide knobs (country, keyboard, timezone, filesystem) and the
    provisioning (packages, post-install commands) live on the builder; the
    per-VM hostname, root password, SSH keys, and static network come from the
    VM spec/credentials at render time.

    Args:
      installer_iso: The **vanilla** PVE installer ISO (a CacheEntry). The
        builder prepares it (xorriso, ADR-0022) into an auto-install ISO during
        staging; this entry's content sha keys the cache.
      credentials: Baked into the install. MUST include a ``root`` credential —
        ``answer.toml`` requires a root password (fail loud otherwise).
      packages / post_install_commands: provisioning threaded into the
        first-boot script (PVE has no answer.toml equivalent), run fail-fast.
      country / keyboard / timezone / fqdn_domain / mailto / filesystem:
        ``answer.toml`` installer settings.
      disk_device: guest device the installer partitions. Default ``vda`` —
        virtio-blk, what the libvirt reference backend attaches (the certified
        installer-origin path). PVE-as-host (scsi0) would use ``sda``.
      network_interface: ``[network] filter.ID_NET_NAME`` match — the udev
        predictable name of the NIC present during install (default ``enp1s0``,
        virtio-net on q35 pcie-root-port 0x1).
      apt_insecure: drop an apt.conf.d snippet disabling TLS verification for an
        internal mirror with an untrusted CA.
    """

    def __init__(
        self,
        *,
        installer_iso: CacheEntry,
        credentials: Sequence[Credential] = (),
        packages: Sequence[Package] = (),
        post_install_commands: Sequence[str] = (),
        country: str = "us",
        keyboard: str = "en-us",
        timezone: str = "UTC",
        fqdn_domain: str = "testrange.local",
        mailto: str = "root@testrange.local",
        filesystem: str = "ext4",
        disk_device: str = "vda",
        partition_label: str = _DEFAULT_PARTITION_LABEL,
        network_interface: str = "enp1s0",
        apt_insecure: bool = False,
    ) -> None:
        creds, pkgs, cmds = self._validate_init_params(
            credentials=credentials,
            packages=packages,
            post_install_commands=post_install_commands,
        )
        self.installer_iso = installer_iso
        self._credentials = creds
        self.packages = pkgs
        self.post_install_commands = cmds
        self.country = country
        self.keyboard = keyboard
        self.timezone = timezone
        self.fqdn_domain = fqdn_domain
        self.mailto = mailto
        self.filesystem = filesystem
        self.disk_device = disk_device
        self.partition_label = partition_label
        self.network_interface = network_interface
        self.apt_insecure = apt_insecure

    @staticmethod
    def _validate_init_params(
        *,
        credentials: Sequence[Credential],
        packages: Sequence[Package],
        post_install_commands: Sequence[str],
    ) -> tuple[tuple[Credential, ...], tuple[Package, ...], tuple[str, ...]]:
        creds = tuple(credentials)
        pkgs = tuple(packages)
        cmds = tuple(post_install_commands)
        # answer.toml requires a root password; the root credential must be a
        # PosixCred carrying one. Fail loud at construction.
        root = next((c for c in creds if c.username == "root"), None)
        if root is None:
            raise ValueError(
                "ProxmoxAnswerBuilder requires a root Credential "
                "(answer.toml root-password is mandatory)"
            )
        if not isinstance(root, PosixCred) or not root.password:
            raise ValueError(
                "ProxmoxAnswerBuilder's root Credential must be a PosixCred with a password "
                "(answer.toml root-password is mandatory)"
            )
        for p in pkgs:
            if not isinstance(p, Apt | Pip):
                raise ValueError(
                    f"ProxmoxAnswerBuilder.packages entries must be Apt or Pip; "
                    f"got {type(p).__name__}"
                )
        for cmd in cmds:
            if not cmd:
                raise ValueError(
                    "ProxmoxAnswerBuilder.post_install_commands entries must be non-empty strings"
                )
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"ProxmoxAnswerBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        return creds, pkgs, cmds

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self._credentials

    def os_disk_base(self) -> None:
        """Installer-origin: no base image. The OS disk is materialized blank
        and the installer partitions it (BUILD-1)."""

    def boot_media(self) -> CacheEntry:
        """The vanilla PVE installer ISO; prepared into an auto-install ISO by
        :meth:`prepare_boot_media` during staging."""
        return self.installer_iso

    def prepare_boot_media(self, media_path: Path) -> Path:
        """Bake the auto-installer activation file + first-boot script into the
        ISO (xorriso, ADR-0022), caching the prepared copy beside the vanilla.

        The first-boot script depends only on builder config (packages,
        commands, apt_insecure) — not per-VM network — so the prepared ISO keys
        on its digest. Booted ``answer.toml`` (the per-VM seed) is delivered
        separately by :meth:`render_seed`.
        """
        script = self._first_boot_script()
        # Key the prepared ISO on its baked-in inputs: the first-boot script and
        # the partition_label (which lands in /auto-installer-mode.toml), plus the
        # prep-recipe version so a behavior change in prepare_iso (e.g. the grub
        # serial-console rewrite) busts a stale cached copy made by an older
        # recipe. The *installed* disk is unaffected, so config_hash omits this.
        digest = hashlib.sha256(
            (
                self._first_boot_digest()
                + "\x00"
                + self.partition_label
                + "\x00"
                + PREPARE_ISO_RECIPE
            ).encode("utf-8")
        ).hexdigest()[:16]
        prepared = media_path.parent / f"{media_path.stem}-prepared-{digest}.iso"
        if not prepared.exists():
            prepare_iso(
                media_path,
                prepared,
                partition_label=self.partition_label,
                first_boot_script=script,
            )
        return prepared

    def config_hash(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        base_sha: str = "",
        sidecar_sha: str = "",
        macs: Sequence[str] = (),
        build_nic: BuildNic,
        native_agent: NativeAgentProvision | None = None,
    ) -> str:
        """Deterministic 16-char hex hash keying the installed PVE disk.

        Folds: the ``[network]`` block (baked into ``/etc/network/interfaces`` —
        a different static IP is a different installed system), the installer
        settings + disk layout, the first-boot script digest (which folds the
        threaded packages/post-install/apt_insecure), the baked SSH public keys
        (the answer file's ``root-ssh-keys`` — CORE-64), and ``base_sha`` (the
        vanilla installer ISO's content sha). Pure: no clocks/run_id/I/O (ADR-0007).

        SSH keys are folded by value: run VMs boot the cached disk with no re-seed
        (``seed_iso_ref=None``), so the keys baked at install are the only
        ``authorized_keys`` there is — excluding them would let a plan with a
        different key cache-hit a disk it cannot log into.
        """
        del macs, build_nic, native_agent  # installer builds the PVE node, not a Linux guest
        root = self._root_credential()
        network = "\n".join(self._network_block(spec, addressing))
        disks = f"{self.filesystem}:{self.disk_device}:{spec.os_drive.size_gb}"
        settings = "|".join(
            [self.country, self.keyboard, self.timezone, self.fqdn_domain, self.mailto]
        )
        ssh_keys = "|".join(
            c.ssh_key.auth_line
            for c in self._credentials
            if isinstance(c, PosixCred) and c.ssh_key is not None
        )
        first_boot_digest = self._first_boot_digest()[:24]
        combined = (
            f"settings:{settings}\n---\nfqdn:{recipe.name}.{self.fqdn_domain}\n---\n"
            f"root-password:{root.password}\n---\nssh-keys:{ssh_keys}\n---\n"
            f"network:\n{network}\n---\n"
            f"disks:{disks}\n---\nfirst-boot:{first_boot_digest}\n---\n"
            f"base:{base_sha}\n---\nsidecar:{sidecar_sha}"
        )
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
        build_nic: BuildNic,
        native_agent: NativeAgentProvision | None = None,
    ) -> bytes:
        """Build the ``PROXMOX-AIS``-labelled seed ISO carrying ``answer.toml``."""
        del macs, build_nic, native_agent  # installer builds the PVE node, not a Linux guest
        answer = self.build_answer_toml(spec, recipe, addressing=addressing)
        return self._build_seed_iso_bytes(answer)

    def wait_ready(self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec) -> None:
        """Confirm the installed node answers over SSH (PVE ships OpenSSH; there
        is no host QGA socket on a base install, so SSH is the channel).

        The build already ran provisioning, so this is just a liveness gate: a
        trivial command must succeed once the run boot is up.
        """
        del spec, recipe
        r = execute(("true",), timeout=300.0)
        if r.exit_code != 0:
            raise BuildNotReadyError(
                f"PVE node not reachable over SSH (exit {r.exit_code}); stderr={r.stderr!r}"
            )

    def build_answer_toml(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
    ) -> str:
        """Render ``answer.toml`` for *spec* (public so tests/debuggers inspect it).

        PVE 9.x kebab-case multi-word keys (``root-password``, ``root-ssh-keys``,
        ``disk-list``) verified against ``proxmox-auto-installer/src/answer.rs``.
        ``reboot-mode`` is omitted (default ``reboot``) so PVE reboots into the
        installed system where ``[first-boot]`` runs and powers off.
        """
        root = self._root_credential()
        assert root.password is not None  # construction guarantees a non-empty root password
        ssh_keys = [
            c.ssh_key.auth_line
            for c in self._credentials
            if isinstance(c, PosixCred) and c.ssh_key is not None
        ]
        global_block = [
            f"country = {_toml_str(self.country)}",
            f"keyboard = {_toml_str(self.keyboard)}",
            f"timezone = {_toml_str(self.timezone)}",
            f"fqdn = {_toml_str(f'{recipe.name}.{self.fqdn_domain}')}",
            f"mailto = {_toml_str(self.mailto)}",
            f"root-password = {_toml_str(root.password)}",
        ]
        if ssh_keys:
            global_block.append(
                "root-ssh-keys = [" + ", ".join(_toml_str(k) for k in ssh_keys) + "]"
            )
        disk_block = [
            f"filesystem = {_toml_str(self.filesystem)}",
            f"disk-list = [{_toml_str(self.disk_device)}]",
        ]
        lines = [
            "[global]",
            *global_block,
            "",
            "[network]",
            *self._network_block(spec, addressing),
            "",
            "[disk-setup]",
            *disk_block,
            "",
            "[first-boot]",
            'source = "from-iso"',
            # network-online: the script runs after the OS applies the answer.toml
            # static, which it then flushes and re-DHCPs for apt.
            'ordering = "network-online"',
        ]
        return "\n".join(lines) + "\n"

    def _root_credential(self) -> PosixCred:
        for c in self._credentials:
            if c.username == "root" and isinstance(c, PosixCred):
                return c
        # Unreachable: __init__ validates a root PosixCred with a password.
        raise ValueError("no root credential")  # pragma: no cover

    def _network_block(
        self, spec: VMSpec, addressing: Mapping[str, NetworkAddressing]
    ) -> list[str]:
        """The ``[network]`` body. Static ``from-answer`` from the primary NIC's
        :class:`StaticAddr` (the only mode usable across the install/run split);
        falls back to ``from-dhcp`` when no static is declared."""
        nic = spec.nics[0] if spec.nics else None
        if nic is None or not isinstance(nic.addr, StaticAddr):
            # from-dhcp freezes the install-phase lease as static — unusable
            # under TestRange's build/run network split, but the only honest
            # fallback when the plan declares no static.
            return ['source = "from-dhcp"']
        static = nic.addr
        sub = addressing.get(nic.network)
        cidr = static.cidr(sub.prefix_len if sub is not None else None)
        gw = static.gw if static.gw is not None else (sub.gateway if sub is not None else None)
        dns = (
            static.dns[0]
            if static.dns
            else (sub.dns_server if sub is not None and sub.dns_server is not None else None)
        )
        lines = ['source = "from-answer"', f"cidr = {_toml_str(cidr)}"]
        if gw is not None:
            lines.append(f"gateway = {_toml_str(gw)}")
        if dns is not None:
            lines.append(f"dns = {_toml_str(dns)}")
        lines.append(f"filter.ID_NET_NAME = {_toml_str(self.network_interface)}")
        return lines

    def _first_boot_script(self) -> str:
        """Render ``/proxmox-first-boot``: network-flip → repo-swap → provisioning
        → framed serial result → poweroff, all fail-fast.

        Depends only on builder config (network_interface/packages/commands/
        apt_insecure), so the prepared ISO keys on this script's digest
        (:meth:`prepare_boot_media`).
        """
        apt_pkgs = [p.name for p in self.packages if isinstance(p, Apt)]
        pips = [p for p in self.packages if isinstance(p, Pip)]
        lines = [
            _FIRST_BOOT_PROLOGUE,
            _network_flip(self.network_interface),
            _pin_mgmt_nic(self.network_interface),
            _REPO_SWAP,
        ]
        if self.apt_insecure:
            lines.append(_APT_INSECURE)
        lines.append("apt-get update")
        if apt_pkgs:
            lines.append(f"apt-get install -y {' '.join(apt_pkgs)}")
        lines.extend(_pip_install_lines(pips))
        lines.extend(self.post_install_commands)
        if self.apt_insecure:
            # Drop the build-time TLS-skip before capture so it does not survive
            # into the installed (run-phase) system.
            lines.append(f"rm -f {_APT_INSECURE_CONF}")
        lines.append(_FIRST_BOOT_FOOTER)
        return "\n".join(lines) + "\n"

    def _first_boot_digest(self) -> str:
        """Full SHA-256 hex of the first-boot script; callers slice as needed.

        The single source for hashing the script — :meth:`config_hash` (cache
        key) and :meth:`prepare_boot_media` (prepared-ISO filename) both derive
        from this, so they can never drift on what "the script's digest" means.
        """
        return hashlib.sha256(self._first_boot_script().encode("utf-8")).hexdigest()

    def _build_seed_iso_bytes(self, answer_toml: str) -> bytes:
        """The ``PROXMOX-AIS``-labelled ISO carrying ``/answer.toml`` (pycdlib)."""
        pycdlib = _import_pycdlib()
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, vol_ident=self.partition_label)
        data = answer_toml.encode("utf-8")
        try:
            iso.add_fp(
                io.BytesIO(data),
                len(data),
                "/ANSWER.;1",
                joliet_path="/answer.toml",
            )
            buf = io.BytesIO()
            iso.write_fp(buf)
            return buf.getvalue()
        finally:
            iso.close()


# The fail-fast preamble: define the ERR trap that frames the failing command
# (rc + $BASH_COMMAND) and a base64 log tail onto the serial console, then arm
# it with `set -eE`. Mirrors CloudInitBuilder's build-result framing (ADR-0012),
# reading from the first-boot log instead of cloud-init's.
_FIRST_BOOT_PROLOGUE = f"""\
#!/bin/bash
exec > >(tee -a {_FIRST_BOOT_LOG}) 2>&1
__tr_serial={_SERIAL_DEVICE}
__tr_emit_fail() {{
    __tr_rc=$?
    __tr_cmd=$BASH_COMMAND
    {{
        printf 'TESTRANGE-RESULT: fail rc=%s cmd="%s"\\n' "$__tr_rc" "$__tr_cmd"
        printf 'TESTRANGE-LOG-BEGIN\\n'
        tail -c {_FAIL_LOG_TAIL_BYTES} {_FIRST_BOOT_LOG} 2>/dev/null | base64
        printf 'TESTRANGE-LOG-END\\n'
    }} > "$__tr_serial" 2>/dev/null
    poweroff -f
}}
trap __tr_emit_fail ERR
set -eE"""


# Network-flip: the answer.toml-installed static lives on vmbr0 (the L3 bridge,
# with the physical NIC enslaved). Flush it + drop the default route, then DHCP
# off the build switch's sidecar so apt reaches the mirror. /etc/network/
# interfaces on disk is untouched, so the run boot comes up on the static. The
# NIC name is the same one answer.toml's filter.ID_NET_NAME applied the static
# to (``network_interface``), so the two must agree — hence it is threaded in
# rather than hard-coded.
def _network_flip(nic: str) -> str:
    return f"""\
NIC="{nic}"
BR="vmbr0"
ip addr flush dev "$BR" 2>/dev/null || true
ip addr flush dev "$NIC" 2>/dev/null || true
ip route flush dev "$BR" 2>/dev/null || true
while ip route show default 2>/dev/null | grep -q .; do
  ip route del default 2>/dev/null || break
done
ip link set "$NIC" up
ip link set "$BR" up
dhclient -1 -v "$BR\""""


# Pin the run-phase management NIC name. answer.toml binds vmbr0's bridge-port to
# a kernel-predictable name (``network_interface``), but predictable names are
# derived from the PCI slot: the build phase installs over a dedicated build NIC
# and the run phase attaches the declared NIC at a *different* slot, so the run
# NIC comes up under a different name, vmbr0's port is missing, and the static is
# unreachable (CORE-67 — the same build→run NIC class as ESXI-18). A systemd
# ``.link`` baked into the installed system renames the management NIC to
# ``network_interface`` by driver match (slot-independent), so vmbr0 finds its
# port at run. This lives in the installed system, NOT answer.toml — the PVE
# auto-installer rejects unknown answer keys, so network-naming fixes can't go
# there. Single managed NIC, matching what ``from-answer`` configures (nics[0]);
# ``.link`` files are applied by udev, so systemd-networkd need not be enabled.
def _pin_mgmt_nic(nic: str) -> str:
    return f"""\
mkdir -p /etc/systemd/network
cat >/etc/systemd/network/10-testrange-mgmt.link <<'EOF'
[Match]
Driver=virtio_net

[Link]
Name={nic}
EOF"""


# Swap the enterprise repo (401s without a subscription, tanks apt under set -e)
# for the public no-subscription mirror. Both .list and .sources variants removed
# so we don't depend on which format the installer chose.
_REPO_SWAP = """\
rm -f /etc/apt/sources.list.d/pve-enterprise.list \\
      /etc/apt/sources.list.d/pve-enterprise.sources \\
      /etc/apt/sources.list.d/ceph.list \\
      /etc/apt/sources.list.d/ceph.sources
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb http://download.proxmox.com/debian/pve $codename pve-no-subscription" \\
  > /etc/apt/sources.list.d/pve-no-subscription.list
export DEBIAN_FRONTEND=noninteractive"""

# apt_insecure: skip TLS peer/host verification for HTTPS apt (internal mirror
# with a CA outside the default trust store). Build-time only — _first_boot_script
# removes the conf before capture so it does NOT survive into the run image.
_APT_INSECURE_CONF = "/etc/apt/apt.conf.d/99-testrange-insecure"
_APT_INSECURE = f"""\
mkdir -p /etc/apt/apt.conf.d
cat >{_APT_INSECURE_CONF} <<'EOF'
Acquire::https::Verify-Peer "false";
Acquire::https::Verify-Host "false";
EOF"""

# PVE gates its first-boot hook on this sentinel: the service runs only while it
# exists (ConditionPathExists) and removes it in ExecStartPost *after* our script
# (its ExecStart) returns. But our poweroff below pre-empts ExecStartPost, so the
# sentinel survives into the captured image and PVE re-runs first-boot on the RUN
# boot — which powers the node off ~10s in, before SSH (CORE-67, live-confirmed by
# booting the captured disk). Remove it ourselves so first-boot runs exactly once,
# at build. Path is PVE's, from proxmox-first-boot-*.service.
_PVE_FIRSTBOOT_SENTINEL = "/var/lib/proxmox-first-boot/pending-first-boot-setup"

# Success footer: drop PVE's first-boot sentinel so the hook doesn't re-run at
# run, sync provisioning writes to disk before announcing ok (the orchestrator
# captures the disk the moment it reads the token), then power off.
_FIRST_BOOT_FOOTER = f"""\
rm -f {_PVE_FIRSTBOOT_SENTINEL}
sync
printf 'TESTRANGE-RESULT: ok\\n' > {_SERIAL_DEVICE} 2>/dev/null
systemctl poweroff"""


def _pip_install_lines(pips: Sequence[Pip]) -> list[str]:
    """``pip3 install`` lines; insecure pips pass ``--trusted-host`` (mirrors
    CloudInitBuilder)."""
    if not pips:
        return []
    secure = [p.name for p in pips if not p.insecure]
    insecure = [p.name for p in pips if p.insecure]
    lines: list[str] = []
    base_cmd = "pip3 install --break-system-packages"
    if secure:
        lines.append(f"{base_cmd} {' '.join(secure)}")
    if insecure:
        lines.append(
            f"{base_cmd} --trusted-host pypi.org --trusted-host files.pythonhosted.org "
            f"{' '.join(insecure)}"
        )
    return lines


def _toml_str(value: str) -> str:
    """Render *value* as a TOML basic string literal (escapes ``\\`` and ``"``).

    Enough for the values answer.toml takes (country codes, FQDNs, SSH keys,
    passwords); avoids a TOML library for the only TOML the project emits.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


__all__ = ["ProxmoxAnswerBuilder"]
