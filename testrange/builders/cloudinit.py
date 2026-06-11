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

Network rendering (ADR-0017)
----------------------------
The cloud-init ``network-config`` is the **single, final netplan** — there is no
install-vs-run split. It matches every interface by **MAC** (the sole strategy;
the old ``match: name: en*`` fallback is gone) and contains:

* the **dedicated build NIC** — a transient NIC on the build switch the build
  phase attaches in place of the declared NICs (ADR-0017 §1), statically
  addressed from the build switch's ``.3`` infra slot;
* every **declared** NIC, with its real run-phase address.

The same baked file serves both phases because an absent-MAC stanza is inert:
during build only the build NIC is physically present (the declared stanzas
match nothing, so ``apt`` egresses via the build NIC with no carrier-wait), and
at run only the declared NICs are present (the build NIC's stanza is inert).

One ``write_files`` entry is retained,
``/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg``
(``network: {config: disabled}``): it pins the build-boot-rendered netplan
across the seed-less run boot so cloud-init never re-renders it. It is
unconditional.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from testrange.builders.base import Builder, NativeAgentProvision
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.devices.network import DHCPAddr, StaticAddr
from testrange.exceptions import BuilderError, BuildNotReadyError
from testrange.networks.base import BuildNic, NetworkAddressing
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


def _import_yaml() -> Any:
    """Lazy import. Raises BuilderError with a useful hint if pyyaml is missing."""
    try:
        import yaml
    except ImportError as e:
        raise BuilderError(
            "pyyaml is not installed; install with `pip install -e .[cloudinit]`"
        ) from e
    return yaml


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
        insecure_pkg_manager: bool = False,
    ) -> None:
        creds, pkgs, cmds = self._validate_init_params(
            base=base,
            credentials=credentials,
            packages=packages,
            post_install_commands=post_install_commands,
        )
        self.base = base
        self._credentials = creds
        self.packages = pkgs
        self.post_install_commands = cmds
        self.insecure_pkg_manager = insecure_pkg_manager

    @staticmethod
    def _validate_init_params(
        *,
        base: CacheEntry,
        credentials: Sequence[Credential],
        packages: Sequence[Package],
        post_install_commands: Sequence[str],
    ) -> tuple[tuple[Credential, ...], tuple[Package, ...], tuple[str, ...]]:
        creds = tuple(credentials)
        pkgs = tuple(packages)
        cmds = tuple(post_install_commands)
        for p in pkgs:
            if not isinstance(p, Apt | Pip):
                raise ValueError(
                    f"CloudInitBuilder.packages entries must be Apt or Pip; got {type(p).__name__}"
                )
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

    def os_disk_base(self) -> CacheEntry:
        """The base image bytes are uploaded onto the OS disk and grown."""
        return self.base

    def render_user_data(
        self, spec: VMSpec, recipe: VMRecipe, *, native_agent: NativeAgentProvision | None = None
    ) -> str:
        """Render cloud-init ``user-data`` (YAML, ``#cloud-config`` header).

        Covers users/credentials, package + post-install provisioning (the
        fail-fast build-result script), and ``write_files``. Network addressing
        is *not* here — it lives entirely in ``network-config`` now (ADR-0017);
        the only network-related ``write_files`` entry is the unconditional
        ``99-testrange-disable-network.cfg`` guard (see module docstring).

        ``native_agent`` (CORE-90) is the backend's native-agent install recipe,
        brokered in by the orchestrator for a ``NativeCommunicator`` VM. Its
        package(s) install and enable command(s) run *first* — ahead of the
        plan's own packages/commands — because a plan post-install step may
        depend on the agent being up, never the reverse. ``None`` injects nothing.
        """
        del spec, recipe  # not used; reserved for future per-recipe hooks
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
            if c.admin:
                user_entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
                user_entry["groups"] = list(c.groups) or ["sudo"]
            elif c.groups:
                user_entry["groups"] = list(c.groups)
            users.append(user_entry)
            if c.password:
                # Modern cloud-init chpasswd form (`type: text` = plaintext).
                # The top-level `list:` string form is deprecated.
                chpasswd_users.append({"name": c.username, "type": "text", "password": c.password})

        # CORE-90: the backend's native agent (qemu-guest-agent / open-vm-tools)
        # installs + enables ahead of the plan's own packages/commands.
        agent_apt = [p.name for p in native_agent.packages] if native_agent else []
        agent_cmds = native_agent.enable_commands if native_agent else ()
        apt_pkgs = agent_apt + [p.name for p in self.packages if isinstance(p, Apt)]
        pips = [p for p in self.packages if isinstance(p, Pip)]

        body: dict[str, Any] = {
            "ssh_pwauth": True,
            "users": users or [{"name": "root", "lock_passwd": False}],
        }
        write_files = _render_insecure_write_files(insecure=self.insecure_pkg_manager)
        write_files.append(_DISABLE_NETWORK_WRITE_FILE)
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
            [
                "bash",
                "-c",
                _render_provision_script(
                    apt_pkgs, pips, (*agent_cmds, *self.post_install_commands)
                ),
            ]
        ]

        yaml_text: str = _import_yaml().safe_dump(
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
        meta: str = _import_yaml().safe_dump(body, default_flow_style=False, sort_keys=True)
        return meta

    def render_network_config(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        build_nic: BuildNic,
        macs: Sequence[str] = (),
    ) -> str:
        """Render the single, final cloud-init ``network-config`` (netplan v2).

        One match-by-MAC netplan applied live on the build boot and persisted
        unchanged into the cached image (ADR-0017 §3). It carries the dedicated
        ``build_nic`` (present only during build) plus every declared NIC
        (present only at run); an absent-MAC stanza is inert, so the same file
        serves both phases. Returned **unwrapped** (no top-level ``network:``) —
        cloud-init wraps ``network-config`` before writing it.

        Per-NIC addressing follows the :class:`StaticAddr` resolution rule
        (explicit wins, else derive from the NIC's ``NetworkAddressing``):

        - ``None`` => ``dhcp4: false, dhcp6: false, optional: true`` (no DHCP wait);
        - :class:`DHCPAddr` => ``dhcp4: true``;
        - :class:`StaticAddr` => address + (first-static-only) default route + DNS.

        The build NIC computes its default route independently of the declared
        NICs: it is the only present interface during build (so it needs the
        route to egress), and it is inert at run, so it never competes with a
        declared static NIC's route.

        ``macs`` (one per declared NIC, in spec order) is required when the VM
        has NICs — match-by-MAC is the sole strategy (ADR-0006/0017); a mismatch
        raises.
        """
        del recipe
        if len(macs) != len(spec.nics):
            raise ValueError(
                f"macs has {len(macs)} entries but spec.nics has {len(spec.nics)}; "
                "match-by-MAC requires one MAC per declared NIC (ADR-0017)"
            )
        ethernets: dict[str, Any] = {
            "build0": _nic_netplan_entry(
                build_nic.addr, build_nic.mac, build_nic.addressing, emit_default_route=True
            )
        }
        first_static_seen = False
        for idx, nic in enumerate(spec.nics):
            is_static = isinstance(nic.addr, StaticAddr)
            ethernets[f"id{idx}"] = _nic_netplan_entry(
                nic.addr,
                macs[idx],
                addressing.get(nic.network),
                emit_default_route=is_static and not first_static_seen,
            )
            first_static_seen = first_static_seen or is_static
        body = {"version": 2, "ethernets": ethernets}
        netcfg: str = _import_yaml().safe_dump(body, default_flow_style=False, sort_keys=True)
        return netcfg

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
        """Deterministic 16-char hex hash keying the built **disk set**.

        Inputs: rendered seed text (which folds in the single match-by-MAC
        netplan — build NIC + declared NICs) + the base disk's content sha +
        the sidecar image's content sha + the writable-disk declarations
        (OS-drive ``size_gb`` and each ``HardDrive``'s ``size_gb``, in spec
        order). Pure: no clocks, no run_id, no I/O. Static-IP changes flow
        into the hash via the rendered netplan so different addresses get
        different cache entries. ``macs`` and ``build_nic`` flow in the same
        way: stable MACs and a stable build-NIC address for the same plan/VM
        produce a stable hash.

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
        u = self.render_user_data(spec, recipe, native_agent=native_agent)
        m = self.render_meta_data(spec, recipe)
        n = self.render_network_config(
            spec, recipe, addressing=addressing, build_nic=build_nic, macs=macs
        )
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
        build_nic: BuildNic,
        native_agent: NativeAgentProvision | None = None,
    ) -> bytes:
        """Build the ISO9660 ``cidata`` seed image as bytes."""
        pycdlib = _import_pycdlib()

        user_data = self.render_user_data(spec, recipe, native_agent=native_agent).encode("utf-8")
        meta_data = self.render_meta_data(spec, recipe).encode("utf-8")
        network_config = self.render_network_config(
            spec, recipe, addressing=addressing, build_nic=build_nic, macs=macs
        ).encode("utf-8")

        iso = pycdlib.PyCdlib()
        # NB: the produced ISO bytes are NOT reproducible — pycdlib bakes the
        # wall-clock into the volume descriptor, so identical inputs yield
        # byte-different ISOs. Harmless because the build cache keys on the
        # rendered seed *text* (config_hash, sort_keys=True), never on ISO bytes.
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
        # (ISO9660 8.3 path, bytes, Rock Ridge name, Joliet long-name path).
        for path, data, rr_name, joliet_path in (
            ("/USERDATA.;1", user_data, "user-data", "/user-data"),
            ("/METADATA.;1", meta_data, "meta-data", "/meta-data"),
            ("/NETWORKC.;1", network_config, "network-config", "/network-config"),
        ):
            iso.add_fp(
                io.BytesIO(data),
                len(data),
                path,
                rr_name=rr_name,
                joliet_path=joliet_path,
            )

        buf = io.BytesIO()
        iso.write_fp(buf)
        iso.close()
        return buf.getvalue()


# Apt config drop-in that disables TLS certificate verification, for an
# internal HTTPS mirror fronted by an untrusted CA. Written into
# /etc/apt/apt.conf.d/ as the last-loaded file so it wins against distro
# defaults.
_INSECURE_APT_CONFIG = """\
Acquire::https::Verify-Peer "false";
Acquire::https::Verify-Host "false";
"""


def _render_insecure_write_files(*, insecure: bool) -> list[dict[str, Any]]:
    """Cloud-init ``write_files`` entry disabling apt TLS verification.

    Apt is the only package manager the builder's typed ``packages`` cover, so
    the insecure drop-in is apt-only; a guest provisioned via ``dnf`` in
    ``post_install_commands`` manages its own config.
    """
    if not insecure:
        return []
    return [
        {
            "path": "/etc/apt/apt.conf.d/99-testrange-insecure",
            "content": _INSECURE_APT_CONFIG,
            "owner": "root:root",
            "permissions": "0644",
        }
    ]


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
set -eE"""

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
        # DEBIAN_FRONTEND is apt-specific; scope it to the apt commands rather
        # than exporting it for the whole (distro-agnostic) provisioning script.
        lines.append("export DEBIAN_FRONTEND=noninteractive")
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


# The cloud-init disable-network drop-in (ADR-0017 §4). cloud-init renders the
# combined netplan live on the build boot; this pins it across the seed-less run
# boot so cloud-init's network module can't re-render and undo our MAC matching.
# Unconditional — every cached disk carries it.
_DISABLE_NETWORK_WRITE_FILE = {
    "path": "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg",
    "content": "network: {config: disabled}\n",
    "owner": "root:root",
    "permissions": "0644",
}


def _nic_netplan_entry(
    addr: DHCPAddr | StaticAddr | None,
    mac: str,
    sub: NetworkAddressing | None,
    *,
    emit_default_route: bool,
) -> dict[str, Any]:
    """One netplan ``ethernets`` stanza, matched by MAC (ADR-0017).

    Keyed on the address mode, identical for the build NIC and every declared
    NIC (``sub`` is the NIC's network addressing — the build switch's for the
    build NIC, ``addressing[nic.network]`` for a declared one):

    - ``None`` => ``dhcp4: false, dhcp6: false, optional: true`` (no DHCP wait);
    - :class:`DHCPAddr` => ``dhcp4: true``;
    - :class:`StaticAddr` => address + DNS + (when ``emit_default_route`` and a
      gateway resolves) a default route; prefix/gateway/DNS are the explicit
      ``StaticAddr`` values, else derived from ``sub``.
    """
    cfg: dict[str, Any] = {"match": {"macaddress": mac.lower()}}
    if addr is None:
        cfg["dhcp4"] = False
        cfg["dhcp6"] = False
        cfg["optional"] = True
    elif isinstance(addr, DHCPAddr):
        cfg["dhcp4"] = True
        cfg["dhcp6"] = False
    else:  # StaticAddr — explicit wins, else derive from the network addressing.
        cfg["addresses"] = [addr.cidr(sub.prefix_len if sub is not None else None)]
        dns = (
            list(addr.dns)
            if addr.dns
            else ([sub.dns_server] if sub is not None and sub.dns_server is not None else [])
        )
        if dns:
            cfg["nameservers"] = {"addresses": dns}
        gw = addr.gw if addr.gw is not None else (sub.gateway if sub is not None else None)
        if gw is not None and emit_default_route:
            cfg["routes"] = [{"to": "default", "via": gw}]
    return cfg
