"""CloudInitBuilder — cloud-init seed renderer for Linux guests.

The seed is an ISO9660 image labeled ``cidata`` containing ``user-data``,
``meta-data``, and ``network-config``. The install VM mounts it on first
boot and applies it.

Build-result contract (ADR-0012)
--------------------------------
All provisioning — apt, pip, and ``post_install_commands`` — runs inside a
single **fail-fast** ``bash`` script with an ``ERR`` trap. On success the
script emits ``TESTRANGE-RESULT: ok`` to ``/dev/ttyS0`` then powers off; on
the first failing command the trap emits ``TESTRANGE-RESULT: fail rc=… cmd=…``
plus a base64'd tail of ``/var/log/cloud-init-output.log``, then powers off.
The orchestrator treats the ``ok`` token as the *only* success signal. Package
installs live in the trapped script (not cloud-init's ``packages:`` directive)
so a package failure is caught fail-fast under the ``ERR`` trap.

Network rendering uses **interface-name matching** (``match: name: ...``)
so the cached disk doesn't bake in the install VM's MAC.

Static IPs
----------
When any NIC declares a static ``ipv4``, the install seed still uses DHCP
(install runs on a transient internet-attached subnet; static IPs from the
user's real subnets have no route there). To make the run-phase clone come
up on the user's networks with the right static address, the builder stages
two cloud-init ``write_files`` entries:

* ``/etc/netplan/50-cloud-init.yaml`` — the *real* run-phase netplan, mode
  ``0600``. Cloud-init writes the install-time DHCP netplan at this path in
  its ``init`` stage; our ``write_files`` (``config`` stage) overwrites it
  later in the same boot, so the cached post-install disk already contains
  the static-aware netplan.
* ``/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg`` —
  ``network: {config: disabled}`` so cloud-init never re-renders the
  netplan on later boots.

No ``netplan apply`` during install. The new file is consumed at the
run-phase boot, when the VM is attached to the user's real networks.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import yaml

from testrange.builders.base import Builder
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.devices.network import DHCPAddr
from testrange.exceptions import BuilderError, BuildNotReadyError
from testrange.networks.base import NetworkAddressing
from testrange.packages.apt import Apt
from testrange.packages.base import Package
from testrange.packages.pip import Pip

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


def _import_pycdlib() -> Any:
    """Lazy import. Raises BuilderError with a useful hint if pycdlib is missing."""
    try:
        import pycdlib
    except ImportError as e:
        raise BuilderError(
            "pycdlib is not installed; install with `pip install -e .[cloudinit]`"
        ) from e
    return pycdlib


class CloudInitBuilder(Builder):
    """Cloud-init seed builder.

    Credentials, packages, and post-install commands all live on the
    Builder. The Communicator does not see them directly — at bind time
    the orchestrator looks up ``builder.credentials`` by username.
    """

    def __init__(
        self,
        *,
        base: CacheEntry,
        credentials: Sequence[Credential] = (),
        packages: Sequence[Package] = (),
        post_install_commands: Sequence[str] = (),
        insecure_apt: bool = False,
        insecure_dnf: bool = False,
    ) -> None:
        creds, pkgs, cmds = self._validate_init_params(
            base=base,
            credentials=credentials,
            packages=packages,
            post_install_commands=post_install_commands,
            insecure_apt=insecure_apt,
            insecure_dnf=insecure_dnf,
        )
        self.base = base
        self._credentials = creds
        self.packages = pkgs
        self.post_install_commands = cmds
        self.insecure_apt = insecure_apt
        self.insecure_dnf = insecure_dnf

    @staticmethod
    def _validate_init_params(
        *,
        base: CacheEntry,
        credentials: Sequence[Credential],
        packages: Sequence[Package],
        post_install_commands: Sequence[str],
        insecure_apt: bool,
        insecure_dnf: bool,
    ) -> tuple[tuple[Credential, ...], tuple[Package, ...], tuple[str, ...]]:
        del insecure_apt, insecure_dnf  # accepted to keep the kwarg surface stable
        creds = tuple(credentials)
        pkgs = tuple(packages)
        cmds = tuple(post_install_commands)
        for cmd in cmds:
            if not cmd:
                raise ValueError(
                    "CloudInitBuilder.post_install_commands entries must be non-empty strings"
                )
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"CloudInitBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        return creds, pkgs, cmds

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self._credentials

    def find_credential(self, username: str) -> Credential | None:
        """Look up a credential by username. Returns None if not found."""
        for c in self._credentials:
            if c.username == username:
                return c
        return None

    def render_user_data(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
    ) -> str:
        """Render cloud-init ``user-data`` (YAML, ``#cloud-config`` header).

        When any NIC has a static ``ipv4``, the run-phase netplan and a
        cloud-init disable drop-in are spliced into ``write_files`` so the
        cached post-install disk boots on the user's real networks with the
        right addressing. See module docstring.

        ``macs`` (when provided, one entry per NIC in spec order) switches
        the run-phase netplan to MAC-based matching, which is the only
        reliable way to address NICs positionally on guests with
        predictable interface names (``enp1s0``/``enp2s0``/...). When
        empty, falls back to name-pattern matching for backwards compat
        with callers that don't have stable MACs available.
        """
        del recipe  # not used yet; reserved for future per-recipe hooks
        # NOTE: any plaintext password below is baked into the cloud-init seed
        # ISO, which lives in the backend storage pool in cleartext — anyone
        # with read access to the pool can recover it. Acceptable only for
        # ephemeral, isolated test guests; prefer ssh-key-only PosixCreds for
        # anything sensitive.
        users: list[dict[str, Any]] = []
        chpasswd_users: list[dict[str, str]] = []
        for c in self._credentials:
            if not isinstance(c, PosixCred):
                continue
            user_entry: dict[str, Any] = {"name": c.username, "lock_passwd": False}
            if c.username != "root":
                user_entry["shell"] = "/bin/bash"
            if c.ssh_key:
                user_entry["ssh_authorized_keys"] = [c.ssh_key.auth_line]
            if c.sudo or c.admin:
                user_entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
                user_entry["groups"] = list(c.groups) or ["sudo"]
            elif c.groups:
                user_entry["groups"] = list(c.groups)
            users.append(user_entry)
            if c.password:
                # Modern cloud-init chpasswd form (`type: text` = plaintext).
                # The top-level `list:` string form is deprecated.
                chpasswd_users.append({"name": c.username, "type": "text", "password": c.password})

        apt_pkgs = [p.name for p in self.packages if isinstance(p, Apt)]
        pips = [p for p in self.packages if isinstance(p, Pip)]

        body: dict[str, Any] = {
            "ssh_pwauth": True,
            "users": users or [{"name": "root", "lock_passwd": False}],
        }
        write_files = _render_insecure_write_files(
            insecure_apt=self.insecure_apt, insecure_dnf=self.insecure_dnf
        )
        write_files.extend(_render_run_netplan_write_files(spec, addressing, macs))
        if write_files:
            body["write_files"] = write_files
        if chpasswd_users:
            body["chpasswd"] = {
                "users": chpasswd_users,
                "expire": False,
            }
        # All provisioning runs inside one fail-fast bash script that emits the
        # build-result record to the serial console and powers off (see module
        # docstring). apt lives here — not in cloud-init's `packages:` directive
        # — so a failed install aborts the script and reports `fail` instead of
        # powering off "successfully" with a half-provisioned disk.
        body["runcmd"] = [
            ["bash", "-c", _render_provision_script(apt_pkgs, pips, self.post_install_commands)]
        ]

        yaml_text = yaml.safe_dump(
            body,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        )
        return "#cloud-config\n" + yaml_text

    def render_meta_data(self, spec: VMSpec, recipe: VMRecipe) -> str:
        """Render cloud-init ``meta-data``."""
        del recipe
        # instance-id is deterministic from the spec's name so cloud-init
        # treats reboots as the same instance and doesn't re-run.
        body = {
            "instance-id": f"iid-{spec.name}",
            "local-hostname": spec.name,
        }
        return yaml.safe_dump(body, default_flow_style=False, sort_keys=True)

    def render_network_config(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
    ) -> str:
        """Render cloud-init ``network-config`` (netplan v2) for **install**.

        Always DHCP-only — the install VM runs on the transient install
        network, and any static address from the user's real subnets would
        have no route there. Static IPs are honored at run-phase via the
        netplan staged into ``write_files`` (see module docstring).

        Matches interfaces by **kernel name** (``match: name: ...``), not
        MAC, so the cached disk works regardless of MAC stability (the
        stable-MAC TODO is belt-and-suspenders).
        """
        del recipe, addressing
        # Match NICs by kernel interface name pattern. Index 0 takes any en*
        # (the first PCI-attached NIC); subsequent NICs use a per-index prefix
        # so order is stable regardless of how the backend numbers slots.
        ethernets: dict[str, Any] = {}
        for idx, _nic in enumerate(spec.nics):
            iface_name = f"id{idx}"
            ethernets[iface_name] = {
                "match": {"name": "en*"} if idx == 0 else {"name": f"en{idx}*"},
                "dhcp4": True,
                "dhcp6": False,
            }
        body = {
            "version": 2,
            "ethernets": ethernets,
        }
        return yaml.safe_dump(body, default_flow_style=False, sort_keys=True)

    def config_hash(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        base_sha: str = "",
        sidecar_sha: str = "",
        macs: Sequence[str] = (),
    ) -> str:
        """Deterministic 16-char hex hash keying the built **disk set**.

        Inputs: rendered seed text (which folds in the staged run-phase
        netplan for static-IP VMs) + the base disk's content sha + the
        sidecar image's content sha + the writable-disk declarations
        (OS-drive ``size_gb`` and each ``HardDrive``'s ``size_gb``, in spec
        order). Pure: no clocks, no run_id, no I/O. Static-IP changes flow
        into the hash via ``write_files`` so different addresses get different
        cache entries. ``macs`` flows in via the rendered run-phase netplan:
        stable MACs for the same plan/VM produce a stable hash.

        ``sidecar_sha`` is the content sha of the ``testrange-sidecar`` image
        (CI-1). Every build boots on the build switch's sidecar for DHCP/DNS/
        NAT, so the sidecar is part of the build environment: a drifted
        sidecar can produce byte-different disks under an otherwise-identical
        key. Folding its content sha in means a drifted sidecar invalidates
        the build cache instead of silently reusing a stale artifact.

        Per ADR-0010 §4 the hash keys the whole artifact set, not one disk:
        because a build boots with every writable disk attached and captures
        each, anything that changes the *set* the build produces (a data
        disk's size, the data-disk count, the OS-disk size) must move the
        hash — otherwise a resized disk would silently reuse a stale artifact.
        """
        u = self.render_user_data(spec, recipe, addressing=addressing, macs=macs)
        m = self.render_meta_data(spec, recipe)
        n = self.render_network_config(spec, recipe, addressing=addressing)
        disks = "|".join(
            [f"os:{spec.os_drive.size_gb}"]
            + [f"data{i}:{d.size_gb}" for i, d in enumerate(spec.data_drives)]
        )
        combined = (
            f"user-data:\n{u}\n---\nmeta-data:\n{m}\n---\n"
            f"network-config:\n{n}\n---\ndisks:{disks}\n---\n"
            f"base:{base_sha}\n---\nsidecar:{sidecar_sha}"
        )
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def wait_ready(self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec) -> None:
        """Run ``cloud-init status --wait`` until cloud-init reaches done.

        At run-phase boot, cloud-init re-walks its stage machine on the
        cloned disk; SSH binds after ``cloud-init.target`` (the second
        stage), but ``cloud-config`` and ``cloud-final`` keep running
        after SSH accepts connections. Blocking on the cloud-init final
        state hands test code a guest whose finalizers have all unwound.
        ``cloud-init status --wait`` can take minutes on a cold boot,
        hence the explicit timeout.
        """
        del spec, recipe
        r = execute(("cloud-init", "status", "--wait"), timeout=300.0)
        if r.exit_code != 0:
            raise BuildNotReadyError(
                f"cloud-init status --wait exited {r.exit_code}; stderr={r.stderr!r}"
            )

    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
    ) -> bytes:
        """Build the ISO9660 ``cidata`` seed image as bytes."""
        pycdlib = _import_pycdlib()

        user_data = self.render_user_data(spec, recipe, addressing=addressing, macs=macs).encode(
            "utf-8"
        )
        meta_data = self.render_meta_data(spec, recipe).encode("utf-8")
        network_config = self.render_network_config(spec, recipe, addressing=addressing).encode(
            "utf-8"
        )

        iso = pycdlib.PyCdlib()
        # interchange_level=3 (relaxed ISO9660 names), joliet=3 (Windows-style
        # long names), rock_ridge="1.09" (the Rock Ridge version Linux mounts
        # cleanly, for POSIX names). vol_ident="cidata" is REQUIRED — cloud-init
        # discovers the NoCloud datasource by that exact volume label.
        iso.new(
            interchange_level=3,
            joliet=3,
            rock_ridge="1.09",
            vol_ident="cidata",
        )
        for path, data, rr_name in (
            ("/USERDATA.;1", user_data, "user-data"),
            ("/METADATA.;1", meta_data, "meta-data"),
            ("/NETWORKC.;1", network_config, "network-config"),
        ):
            iso.add_fp(
                io.BytesIO(data),
                len(data),
                path,
                rr_name=rr_name,
                joliet_path=_joliet_name_for(path),
            )

        buf = io.BytesIO()
        iso.write_fp(buf)
        iso.close()
        return buf.getvalue()


def _joliet_name_for(path: str) -> str:
    """Map our ISO path to a Joliet (long-name) path."""
    return {
        "/USERDATA.;1": "/user-data",
        "/METADATA.;1": "/meta-data",
        "/NETWORKC.;1": "/network-config",
    }[path]


# Apt config drop-in that disables signature verification. Written into
# /etc/apt/apt.conf.d/ as the last-loaded file so it wins against distro
# defaults.
_INSECURE_APT_CONFIG = (
    'Acquire::AllowInsecureRepositories "true";\n'
    'Acquire::AllowDowngradeToInsecureRepositories "true";\n'
    'APT::Get::AllowUnauthenticated "true";\n'
)

# dnf has no /etc/dnf/dnf.conf.d/ drop-in; append into the [main] section of
# /etc/dnf/dnf.conf instead (cloud-init's write_files supports append=true).
_INSECURE_DNF_CONFIG = "sslverify=False\ngpgcheck=0\n"


def _render_insecure_write_files(*, insecure_apt: bool, insecure_dnf: bool) -> list[dict[str, Any]]:
    """Build cloud-init ``write_files`` entries for the insecure apt/dnf flags."""
    entries: list[dict[str, Any]] = []
    if insecure_apt:
        entries.append(
            {
                "path": "/etc/apt/apt.conf.d/99-testrange-insecure",
                "content": _INSECURE_APT_CONFIG,
                "owner": "root:root",
                "permissions": "0644",
            }
        )
    if insecure_dnf:
        entries.append(
            {
                "path": "/etc/dnf/dnf.conf",
                "content": _INSECURE_DNF_CONFIG,
                "owner": "root:root",
                "permissions": "0644",
                "append": True,
            }
        )
    return entries


# The guest serial device the build-result record is written to. Linux exposes
# the first 16550 UART here; the BSDs/Windows use com0/COM1, handled by their
# own builders. The build VM's virtual hardware must carry this UART — a driver
# concern (the build-result sink reads the host side of the same port).
_SERIAL_DEVICE = "/dev/ttyS0"

# How much of the cloud-init output log to ship back on failure. A bounded tail
# keeps a runaway log from flooding the serial console; it is base64'd so a
# binary payload survives the channel intact.
_FAIL_LOG_TAIL_BYTES = 16384

# The fail-fast preamble: define the ERR trap that frames the failing command
# (rc + $BASH_COMMAND) and a base64 log tail onto the serial console, arm it,
# then `set -eE` so any nonzero exit fires it. `-E` propagates the trap into
# shell functions/subshells.
_PROVISION_PREAMBLE = f"""\
__tr_serial={_SERIAL_DEVICE}
__tr_emit_fail() {{
    __tr_rc=$?
    __tr_cmd=$BASH_COMMAND
    {{
        printf 'TESTRANGE-RESULT: fail rc=%s cmd="%s"\\n' "$__tr_rc" "$__tr_cmd"
        printf 'TESTRANGE-LOG-BEGIN\\n'
        tail -c {_FAIL_LOG_TAIL_BYTES} /var/log/cloud-init-output.log 2>/dev/null | base64
        printf 'TESTRANGE-LOG-END\\n'
    }} > "$__tr_serial" 2>/dev/null
    poweroff -f
}}
trap __tr_emit_fail ERR
set -eE
export DEBIAN_FRONTEND=noninteractive"""

# The success footer: `sync` flushes provisioning writes to the virtual disk
# *before* announcing `ok`, because the orchestrator captures the disk the
# moment it reads the token.
_PROVISION_FOOTER = f"""\
sync
printf 'TESTRANGE-RESULT: ok\\n' > "{_SERIAL_DEVICE}" 2>/dev/null
poweroff"""


def _render_provision_script(
    apt_pkgs: Sequence[str], pips: Sequence[Pip], post_install_commands: Sequence[str]
) -> str:
    """Render the fail-fast bash provisioning script (build-result contract).

    Runs apt (update + install), pip installs, then ``post_install_commands``
    in order under ``set -eE`` + an ``ERR`` trap, and frames the build-result
    record onto the serial console (success footer or trap). Returned as a
    single string to hand to ``bash -c`` via a cloud-init ``runcmd`` list entry
    (exec'd directly, so the whole script runs under bash — needed for the
    ``ERR`` trap and ``$BASH_COMMAND``, which POSIX ``sh`` lacks).
    """
    lines = [_PROVISION_PREAMBLE]
    if apt_pkgs:
        lines.append("apt-get update")
        lines.append(f"apt-get install -y {' '.join(apt_pkgs)}")
    lines.extend(_render_pip_install_lines(pips))
    lines.extend(post_install_commands)
    lines.append(_PROVISION_FOOTER)
    return "\n".join(lines) + "\n"


def _render_pip_install_lines(pips: Sequence[Pip]) -> list[str]:
    """Render ``pip3 install`` runcmd lines.

    Secure pips batch into one install; insecure pips batch into a second
    install that passes ``--trusted-host`` for pypi.org + files.pythonhosted.org
    so they can install from a misconfigured / proxied / air-gapped index.
    """
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


# ----------------------------------------------------------------------------
# Run-phase netplan staging.
#
# Why this exists: the install VM runs on a transient DHCP-only "install"
# network so apt etc. work. The user's real subnets only attach at run-phase.
# Baking a static address into the install-time netplan would have no route
# during install and break apt mid-boot. Instead we write the *real* netplan
# at install time via cloud-init's `write_files` (config stage runs AFTER
# init-stage netplan rendering, so our file wins) and disable cloud-init's
# network module on subsequent boots so it doesn't undo our work.
# ----------------------------------------------------------------------------


def _render_run_netplan_yaml(
    spec: VMSpec,
    addressing: Mapping[str, NetworkAddressing],
    macs: Sequence[str] = (),
) -> str:
    """Render the netplan the guest should use at run-phase.

    Per-NIC, keyed on ``nic.addr``:

    - ``None`` => ``dhcp4: false, dhcp6: false, optional: true`` — the NIC is
      left unconfigured (no address, and crucially no DHCP wait).
    - :class:`DHCPAddr` => ``dhcp4: true``.
    - :class:`StaticAddr` => static address + (first-static-only) default
      route + nameservers; prefix/gateway/DNS are taken from the ``StaticAddr``
      when listed, else derived from the Switch's :class:`NetworkAddressing`.

    Wraps under a top-level ``network:`` because this file goes directly
    into ``/etc/netplan/`` (cloud-init's ``network-config`` is unwrapped
    because cloud-init wraps it before writing).

    When ``macs`` is provided (one per NIC in spec order), matches by
    MAC — the only reliable way to address NICs positionally on guests
    with predictable interface names. When empty, falls back to a name
    pattern; this fallback is only safe for single-NIC VMs.
    """
    if macs and len(macs) != len(spec.nics):
        raise ValueError(f"macs has {len(macs)} entries but spec.nics has {len(spec.nics)}")
    first_static_seen = False
    ethernets: dict[str, Any] = {}
    for idx, nic in enumerate(spec.nics):
        iface_name = f"id{idx}"
        if macs:
            match: dict[str, Any] = {"macaddress": macs[idx].lower()}
        else:
            match = {"name": "en*"} if idx == 0 else {"name": f"en{idx}*"}
        cfg: dict[str, Any] = {"match": match}
        a = nic.addr
        if a is None:
            # NIC present but unconfigured: leave it to the OS. Must NOT emit
            # dhcp4: true — that hangs boot waiting for a lease nothing serves.
            cfg["dhcp4"] = False
            cfg["dhcp6"] = False
            cfg["optional"] = True
        elif isinstance(a, DHCPAddr):
            cfg["dhcp4"] = True
            cfg["dhcp6"] = False
        else:  # StaticAddr — explicit wins, else derive from the Switch.
            sub = addressing.get(nic.network)
            cfg["addresses"] = [a.cidr(sub.prefix_len if sub is not None else None)]
            dns = (
                list(a.dns)
                if a.dns
                else ([sub.dns_server] if sub is not None and sub.dns_server is not None else [])
            )
            if dns:
                cfg["nameservers"] = {"addresses": dns}
            gw = a.gw if a.gw is not None else (sub.gateway if sub is not None else None)
            if gw is not None and not first_static_seen:
                cfg["routes"] = [{"to": "default", "via": gw}]
                first_static_seen = True
        ethernets[iface_name] = cfg
    body = {"network": {"version": 2, "ethernets": ethernets}}
    return yaml.safe_dump(body, default_flow_style=False, sort_keys=True)


def _render_run_netplan_write_files(
    spec: VMSpec,
    addressing: Mapping[str, NetworkAddressing],
    macs: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Cloud-init ``write_files`` entries that stage the run-phase netplan.

    Returns an empty list for single-NIC all-DHCP VMs — the install-time
    DHCP netplan already matches the single NIC by name pattern and works
    at run-phase too. Anything else needs the run-phase netplan: a static
    address must be baked in, an unconfigured (``addr=None``) NIC must render
    ``dhcp4: false`` (which the install netplan does *not* do), and multi-NIC
    matching by interface name is unreliable on guests with predictable names
    (only MAC matching disambiguates positionally).
    """
    all_dhcp = all(isinstance(nic.addr, DHCPAddr) for nic in spec.nics)
    if len(spec.nics) <= 1 and all_dhcp:
        return []
    staged = _render_run_netplan_yaml(spec, addressing, macs)
    return [
        {
            "path": "/etc/netplan/50-cloud-init.yaml",
            "content": staged,
            "owner": "root:root",
            # netplan 0.106+ warns/errors on world-readable netplan files.
            "permissions": "0600",
        },
        {
            "path": "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg",
            "content": "network: {config: disabled}\n",
            "owner": "root:root",
            "permissions": "0644",
        },
    ]
