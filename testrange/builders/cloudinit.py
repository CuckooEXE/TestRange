"""CloudInitBuilder — cloud-init seed renderer for Linux guests.

The seed is an ISO9660 image labeled ``cidata`` containing ``user-data``,
``meta-data``, and ``network-config``. The install VM mounts it on first
boot and applies it. Seeds end with ``poweroff`` in ``runcmd`` so the
install VM self-terminates and the orchestrator snapshots the disk as the
cached post-install artifact.

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
        if not isinstance(base, CacheEntry):
            raise TypeError(
                f"CloudInitBuilder.base must be a CacheEntry, got {type(base).__name__}"
            )
        creds = tuple(credentials)
        pkgs = tuple(packages)
        cmds = tuple(post_install_commands)
        for c in creds:
            if not isinstance(c, Credential):
                raise TypeError(
                    f"CloudInitBuilder.credentials must contain Credential, got {type(c).__name__}"
                )
        for p in pkgs:
            if not isinstance(p, Package):
                raise TypeError(
                    f"CloudInitBuilder.packages must contain Package, got {type(p).__name__}"
                )
        for cmd in cmds:
            if not isinstance(cmd, str) or not cmd:
                raise ValueError(
                    "CloudInitBuilder.post_install_commands entries must be non-empty strings"
                )
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"CloudInitBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        if not isinstance(insecure_apt, bool):
            raise TypeError(
                f"CloudInitBuilder.insecure_apt must be bool, got {type(insecure_apt).__name__}"
            )
        if not isinstance(insecure_dnf, bool):
            raise TypeError(
                f"CloudInitBuilder.insecure_dnf must be bool, got {type(insecure_dnf).__name__}"
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
        users: list[dict[str, Any]] = []
        chpasswd_lines: list[str] = []
        for c in self._credentials:
            if not isinstance(c, PosixCred):
                continue
            user_entry: dict[str, Any] = {"name": c.username, "lock_passwd": False}
            if c.username != "root":
                user_entry["shell"] = "/bin/bash"
            if c.pubkey:
                user_entry["ssh_authorized_keys"] = [c.pubkey]
            if c.sudo or c.admin:
                user_entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
                user_entry["groups"] = list(c.extra_groups) or ["sudo"]
            elif c.extra_groups:
                user_entry["groups"] = list(c.extra_groups)
            users.append(user_entry)
            if c.password:
                chpasswd_lines.append(f"{c.username}:{c.password}")

        apt_pkgs = [p.name for p in self.packages if isinstance(p, Apt)]
        pips = [p for p in self.packages if isinstance(p, Pip)]

        runcmd: list[str] = []
        runcmd.extend(_render_pip_install_lines(pips))
        runcmd.extend(self.post_install_commands)
        runcmd.append("poweroff")  # self-terminating install

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
        if chpasswd_lines:
            body["chpasswd"] = {
                "list": "\n".join(chpasswd_lines),
                "expire": False,
            }
        if apt_pkgs:
            body["package_update"] = True
            body["packages"] = apt_pkgs
        body["runcmd"] = runcmd

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
        macs: Sequence[str] = (),
    ) -> str:
        """Deterministic 16-char hex hash of the post-install disk contents.

        Inputs: rendered seed text (which folds in the staged run-phase
        netplan for static-IP VMs) + the base disk's content sha. Pure: no
        clocks, no run_id, no I/O. Static-IP changes flow into the hash via
        ``write_files`` so different addresses get different cache entries.
        ``macs`` flows in via the rendered run-phase netplan: stable MACs
        for the same plan/VM produce a stable hash.
        """
        u = self.render_user_data(spec, recipe, addressing=addressing, macs=macs)
        m = self.render_meta_data(spec, recipe)
        n = self.render_network_config(spec, recipe, addressing=addressing)
        combined = (
            f"user-data:\n{u}\n---\nmeta-data:\n{m}\n---\n"
            f"network-config:\n{n}\n---\nbase:{base_sha}"
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

_RUN_NETPLAN_TARGET = "/etc/netplan/50-cloud-init.yaml"
_DISABLE_NETWORK_PATH = "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg"
_DISABLE_NETWORK_BODY = "network: {config: disabled}\n"


def _render_run_netplan_yaml(
    spec: VMSpec,
    addressing: Mapping[str, NetworkAddressing],
    macs: Sequence[str] = (),
) -> str:
    """Render the netplan the guest should use at run-phase.

    Per-NIC: ``ipv4`` set => static address + (first-static-only) default
    route + nameservers pointing at the gateway; ``ipv4`` unset => dhcp4.
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
        if nic.ipv4 is not None:
            addr = addressing[nic.network]
            cfg["addresses"] = [f"{nic.ipv4}/{addr.prefix_len}"]
            cfg["nameservers"] = {"addresses": [addr.gateway]}
            if not first_static_seen:
                cfg["routes"] = [{"to": "default", "via": addr.gateway}]
                first_static_seen = True
        else:
            cfg["dhcp4"] = True
            cfg["dhcp6"] = False
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
    at run-phase too. Any static IP, or any multi-NIC topology, needs the
    run-phase netplan: static addresses must be baked in, and multi-NIC
    matching by interface name is unreliable on guests with predictable
    names (only MAC matching disambiguates positionally).
    """
    no_statics = not any(nic.ipv4 is not None for nic in spec.nics)
    if len(spec.nics) <= 1 and no_statics:
        return []
    staged = _render_run_netplan_yaml(spec, addressing, macs)
    return [
        {
            "path": _RUN_NETPLAN_TARGET,
            "content": staged,
            "owner": "root:root",
            # netplan 0.106+ warns/errors on world-readable netplan files.
            "permissions": "0600",
        },
        {
            "path": _DISABLE_NETWORK_PATH,
            "content": _DISABLE_NETWORK_BODY,
            "owner": "root:root",
            "permissions": "0644",
        },
    ]
