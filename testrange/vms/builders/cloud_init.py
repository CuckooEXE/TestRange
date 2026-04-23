"""Cloud-init :class:`~testrange.vms.builders.base.Builder` for Linux
cloud images.

The install phase boots a NoCloud seed ISO on top of an overlay of the
resolved ``.qcow2`` / ``.img`` base, lets cloud-init create users,
install packages, run post-install commands, and power the VM off.
The run phase rotates the ``instance-id`` via a phase-2 seed ISO so
cloud-init treats each test run as a new instance (without
reinstalling packages).
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

import yaml
from passlib.hash import sha512_crypt
from pycdlib import PyCdlib  # type: ignore[attr-defined]

from testrange.cache import vm_config_hash
from testrange.exceptions import CloudInitError
from testrange.packages import Homebrew
from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.images import resolve_image

# Sentinel file written at the end of the install runcmd script iff every
# step succeeded.  ``power_state.condition`` only fires a poweroff when
# this file exists, so any install failure (apt, dpkg verify, pip, user
# post-install cmd) leaves the VM up until the backend hits its build
# timeout — which surfaces as :class:`~testrange.exceptions.VMBuildError`
# instead of silently caching a broken image.
_INSTALL_OK_SENTINEL = "/var/lib/testrange/install_ok"

# Dropped in via ``write_files:`` whenever any ``Apt(..., insecure=True)``
# is in the package list.  Disables TLS peer/host verification for every
# APT operation on the install boot — covers cloud-init's native
# ``packages:`` install when the mirror's CA isn't on the VM yet.
_APT_INSECURE_CONF = (
    'Acquire::https::Verify-Peer "false";\n'
    'Acquire::https::Verify-Host "false";\n'
)

# Same idea for DNF, appended to ``/etc/dnf/dnf.conf``.  Default
# ``dnf.conf`` files ship with just a ``[main]`` section, so appending
# lands inside ``[main]`` — which is where ``sslverify`` belongs.  We
# use ``append: true`` (not an overwrite) to preserve whatever distro
# defaults already live in the file.
_DNF_INSECURE_CONF = "\nsslverify=False\n"

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM as VM


def _hash_password(plaintext: str) -> str:
    """Return a Linux-compatible SHA-512 crypt hash for *plaintext*.

    :param plaintext: The plaintext password to hash.
    :returns: A ``$6$...`` hash string suitable for the cloud-init
        ``hashed_passwd`` field.
    """
    hashed: str = sha512_crypt.using(rounds=5000).hash(plaintext)
    return hashed


class CloudInitBuilder(Builder):
    """Cloud-init install + run strategy.

    One builder instance can serve any number of Linux VMs — VM-specific
    state (users, packages, post-install cmds) is read from each VM
    argument.  Session-wide knobs (TLS trust for APT/DNF mirrors) live
    here on the builder because APT and DNF config is process-wide for
    the install boot: a "per-package" switch would be a lie.

    :param apt_insecure: If ``True``, the install boot drops an APT
        config snippet that disables HTTPS peer/host verification.
        Useful when the Debian/Ubuntu VM's package mirror is a private
        server whose CA isn't in the VM's default trust store.
        Defaults to ``False``.
    :param dnf_insecure: If ``True``, the install boot appends
        ``sslverify=False`` to ``/etc/dnf/dnf.conf`` for the same
        reason on RHEL-family VMs.  Defaults to ``False``.
    """

    apt_insecure: bool
    """If ``True``, APT is configured to skip TLS verification during install."""

    dnf_insecure: bool
    """If ``True``, DNF is configured with ``sslverify=False`` during install."""

    def __init__(
        self,
        apt_insecure: bool = False,
        dnf_insecure: bool = False,
    ) -> None:
        self.apt_insecure = apt_insecure
        self.dnf_insecure = dnf_insecure

    def default_communicator(self) -> str:
        """Linux images default to the QEMU guest agent channel.

        Cloud-init always installs ``qemu-guest-agent`` as part of
        :meth:`install_user_data`, so this is always available.
        """
        return "guest-agent"

    def cache_key(self, vm: VM) -> str:
        """Hash folding in the iso, users, packages, post-install
        commands, and disk size.  SSH keys are intentionally excluded
        so key rotation does not invalidate cached builds.
        """
        return vm_config_hash(
            iso=vm.iso,
            usernames_passwords_sudo=[
                (c.username, c.password, c.sudo) for c in vm.users
            ],
            package_reprs=[repr(p) for p in vm.pkgs],
            post_install_cmds=vm.post_install_cmds,
            disk_size=vm._primary_disk_size(),
        )

    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> InstallDomain:
        # 1. Resolve source (URL → local outer-host path); 2. stage to
        # backend (no-op for local backends, SFTP / REST upload for
        # remote ones); 3. create the install overlay on the backend.
        local_base = resolve_image(vm.iso, cache)
        base_ref = cache.stage_source(local_base, run.storage)
        work_disk = run.create_install_disk(
            vm.name, base_ref, vm._primary_disk_size()
        )
        seed_ref = run.seed_iso_path(vm.name, install=True)
        run.storage.write_bytes(
            seed_ref,
            build_seed_iso_bytes(
                meta_data=self.install_meta_data(vm, self.cache_key(vm)),
                user_data=self.install_user_data(vm),
            ),
        )
        return InstallDomain(work_disk=work_disk, seed_iso=seed_ref)

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
        }

    def prepare_run_domain(
        self,
        vm: VM,
        run: RunDir,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> RunDomain:
        seed_ref = run.seed_iso_path(vm.name, install=False)
        run.storage.write_bytes(
            seed_ref,
            build_seed_iso_bytes(
                meta_data=self.run_meta_data(vm, run.run_id),
                user_data=self.run_user_data(vm),
                network_config=self.run_network_config(mac_ip_pairs),
            ),
        )
        return RunDomain(seed_iso=seed_ref)

    # ------------------------------------------------------------------
    # YAML generation — kept public so tests and debuggers can inspect
    # the exact payload without booting anything.  All methods are
    # stateless: they read everything they need from ``vm``.
    # ------------------------------------------------------------------

    def install_user_data(self, vm: VM) -> str:
        """Return the phase-1 ``user-data`` YAML string.

        The VM only powers off when every install step succeeded — see
        :data:`_INSTALL_OK_SENTINEL`.  Any failure (apt, dpkg, pip, user
        cmd) leaves the VM up and the install-phase timeout surfaces
        the error instead of silently caching a broken image.
        """
        native_pkgs = _native_packages(vm.pkgs)
        doc: dict[str, Any] = {
            "hostname": vm.name,
            "fqdn": f"{vm.name}.local",
            "manage_etc_hosts": True,
            "users": [_user_entry(c) for c in vm.users],
            "ssh_pwauth": True,
            "packages": native_pkgs,
            "package_update": True,
            "package_upgrade": False,
            "runcmd": _runcmd_entries(
                vm.pkgs, vm.users, vm.post_install_cmds, native_pkgs
            ),
            "datasource_list": ["NoCloud", "None"],
            "power_state": {
                "mode": "poweroff",
                "message": "TestRange install phase complete",
                "timeout": 30,
                # Shell-form condition — only power off when runcmd wrote
                # the sentinel.  List syntax avoids quoting pitfalls.
                "condition": ["test", "-f", _INSTALL_OK_SENTINEL],
            },
        }
        write_files = _insecure_write_files(
            apt_insecure=self.apt_insecure,
            dnf_insecure=self.dnf_insecure,
        )
        if write_files:
            doc["write_files"] = write_files
        return "#cloud-config\n" + yaml.dump(doc, default_flow_style=False)

    def install_meta_data(self, vm: VM, config_hash: str) -> str:
        """Return the phase-1 ``meta-data`` YAML string.

        The ``instance-id`` folds in the config hash so cloud-init
        always considers this a fresh instance even if the base image
        carries leftover first-boot state.
        """
        doc = {
            "instance-id": f"install-{config_hash}",
            "local-hostname": vm.name,
        }
        return yaml.dump(doc, default_flow_style=False)

    def run_user_data(self, vm: VM) -> str:
        """Return the phase-2 ``user-data`` YAML string.

        Re-asserts per-user auth (password hash + unlock state + SSH
        keys); does not reinstall packages.  The password hash has to
        be re-sent every run because cloud-init's ``users`` module
        otherwise re-locks the account — the hash stays in
        ``/etc/shadow`` but gets prefixed with ``!``, blocking password
        auth over SSH.
        """
        users: list[dict[str, Any]] = []
        for cred in vm.users:
            entry: dict[str, Any] = {
                "name": cred.username,
                "lock_passwd": False,
                "hashed_passwd": _hash_password(cred.password),
            }
            if cred.ssh_key:
                entry["ssh_authorized_keys"] = [cred.ssh_key]
            users.append(entry)

        doc: dict[str, Any] = {
            "datasource_list": ["NoCloud", "None"],
            "users": users,
        }
        return "#cloud-config\n" + yaml.dump(doc, default_flow_style=False)

    def run_meta_data(self, vm: VM, run_id: str) -> str:
        """Return the phase-2 ``meta-data`` YAML string.

        The run ID is used as ``instance-id`` so cloud-init treats
        every test run as a new instance.
        """
        doc = {
            "instance-id": f"run-{run_id}",
            "local-hostname": vm.name,
        }
        return yaml.dump(doc, default_flow_style=False)

    def run_network_config(
        self,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> str | None:
        """Return a cloud-init network-config v2 YAML string, or ``None``.

        Only generated when at least one NIC has a static IP address.
        """
        has_static = any(ip for _, ip, _, _ in mac_ip_pairs)
        if not has_static:
            return None

        ethernets: dict[str, Any] = {}
        for idx, (mac, ip_prefix, gateway, nameserver) in enumerate(
            mac_ip_pairs
        ):
            key = f"id{idx}"
            if ip_prefix:
                entry: dict[str, Any] = {
                    "match": {"macaddress": mac},
                    "set-name": f"eth{idx}",
                    "addresses": [ip_prefix],
                }
                if gateway:
                    entry["gateway4"] = gateway
                if nameserver:
                    entry["nameservers"] = {"addresses": [nameserver]}
                ethernets[key] = entry
            else:
                ethernets[key] = {
                    "match": {"macaddress": mac},
                    "set-name": f"eth{idx}",
                    "dhcp4": True,
                }

        doc = {"version": 2, "ethernets": ethernets}
        return yaml.dump(doc, default_flow_style=False)


# ----------------------------------------------------------------------
# Module-level helpers.  These used to be CloudInitBuilder methods in
# the old stateful API; broken out now so the builder itself stays
# stateless.
# ----------------------------------------------------------------------


def _user_entry(cred: Credential) -> dict[str, Any]:
    """Build a cloud-init user dict for a single credential."""
    entry: dict[str, Any] = {
        "name": cred.username,
        "lock_passwd": False,
        "hashed_passwd": _hash_password(cred.password),
    }
    if cred.ssh_key:
        entry["ssh_authorized_keys"] = [cred.ssh_key]
    if not cred.is_root():
        entry["shell"] = "/bin/bash"
        if cred.sudo:
            entry["groups"] = ["sudo", "wheel", "users"]
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
    return entry


def _native_packages(packages: list[AbstractPackage]) -> list[str]:
    """Return packages cloud-init can install via its ``packages:`` module."""
    pkgs: list[str] = []
    for p in packages:
        entry = p.native_package_name()
        if entry is not None:
            pkgs.append(entry)
    if "qemu-guest-agent" not in pkgs:
        pkgs.append("qemu-guest-agent")
    return sorted(pkgs)


def _runcmd_entries(
    packages: list[AbstractPackage],
    users: list[Credential],
    post_install_cmds: list[str],
    native_packages: list[str],
) -> list[str]:
    """Return shell commands to append to cloud-init ``runcmd:``.

    Runs under ``#!/bin/sh`` with ``set -e`` so any failure aborts the
    script, the sentinel is never written, and
    :attr:`CloudInitBuilder.install_user_data`'s ``power_state.condition``
    skips the poweroff — leaving the VM up until the build-phase timeout
    fires.  Caller-supplied ``post_install_cmds`` are subject to the same
    fail-fast discipline.
    """
    cmds: list[str] = []

    # Fail-fast discipline for the rest of the script.  cloud-init
    # concatenates every runcmd entry into a single ``/bin/sh`` script
    # (dash on Debian, bash-in-POSIX on RHEL), so we stick to POSIX:
    # ``set -e`` to bail on the first failure and explicit ``echo`` +
    # ``exit 1`` on the checks that have something useful to log.
    # ``trap ... ERR`` would be nicer but isn't POSIX — dash rejects it.
    cmds.append("set -e")

    # Verify cloud-init actually installed every native package.  If apt
    # (or dnf) hit a cert/mirror error, the ``packages:`` module logs
    # and moves on — without this check we'd cache a broken image and
    # then hang in the run phase waiting on qemu-guest-agent.
    if native_packages:
        pkgs_sh = " ".join(_sh_quote(p) for p in native_packages)
        cmds.append(
            "if command -v dpkg >/dev/null 2>&1; then _tr_check=\"dpkg -s\"; "
            'elif command -v rpm >/dev/null 2>&1; then _tr_check="rpm -q"; '
            'else echo "TESTRANGE: no dpkg or rpm — cannot verify packages" >&2; '
            "exit 1; fi; "
            f"for _tr_pkg in {pkgs_sh}; do "
            '$_tr_check "$_tr_pkg" >/dev/null 2>&1 || { '
            'echo "TESTRANGE: package \'$_tr_pkg\' failed to install" >&2; '
            "exit 1; }; done"
        )

    cmds.append(
        "systemctl enable --now qemu-guest-agent || { "
        'echo "TESTRANGE: failed to enable qemu-guest-agent" >&2; exit 1; }'
    )

    brew_pkgs = [p for p in packages if isinstance(p, Homebrew)]
    if brew_pkgs:
        brew_user = next(
            (u.username for u in users if not u.is_root()), None
        )
        if brew_user is None:
            raise CloudInitError(
                "Homebrew packages require at least one non-root user credential. "
                "Add a Credential(username='...') entry."
            )
        cmds.append(Homebrew.install_homebrew_command().format(user=brew_user))
        for brew_pkg in brew_pkgs:
            for cmd in brew_pkg.install_commands():
                cmds.append(cmd.format(brew_user=brew_user))

    for pkg in packages:
        if pkg.package_manager not in ("apt", "dnf", "brew"):
            cmds.extend(pkg.install_commands())

    cmds.extend(post_install_cmds)

    # Everything above succeeded — drop the sentinel so the
    # power_state.condition fires and the VM shuts down cleanly.
    cmds.append(f"mkdir -p {_sh_quote(_INSTALL_OK_SENTINEL.rsplit('/', 1)[0])}")
    cmds.append(f"touch {_sh_quote(_INSTALL_OK_SENTINEL)}")
    cmds.append("sync")
    return cmds


def _sh_quote(value: str) -> str:
    """Return *value* single-quoted for safe embedding in a /bin/sh script."""
    return "'" + value.replace("'", "'\\''") + "'"


def _insecure_write_files(
    apt_insecure: bool, dnf_insecure: bool
) -> list[dict[str, Any]]:
    """Return cloud-init ``write_files:`` entries for mirrors whose CA
    isn't in the VM's trust store.

    Both entries land before cloud-init's ``packages:`` module runs, so
    the native install itself picks up the relaxed TLS config.
    """
    entries: list[dict[str, Any]] = []
    if apt_insecure:
        entries.append(
            {
                "path": "/etc/apt/apt.conf.d/99testrange-insecure",
                "permissions": "0644",
                "owner": "root:root",
                "content": _APT_INSECURE_CONF,
            }
        )
    if dnf_insecure:
        entries.append(
            {
                "path": "/etc/dnf/dnf.conf",
                "permissions": "0644",
                "owner": "root:root",
                "content": _DNF_INSECURE_CONF,
                "append": True,
            }
        )
    return entries


def build_seed_iso_bytes(
    meta_data: str,
    user_data: str,
    network_config: str | None = None,
) -> bytes:
    """Return the raw bytes of a cloud-init NoCloud seed ISO.

    Uses :mod:`pycdlib` for pure-Python ISO 9660 creation (no external
    ``genisoimage`` / ``xorriso`` dependency).  The volume ID is
    ``cidata``, as required by the cloud-init NoCloud datasource.

    Returning bytes rather than writing to a path keeps seed ISO
    generation backend-agnostic — the caller hands the bytes to
    whichever :class:`~testrange.storage.AbstractStorageBackend` is in
    play (local filesystem, SFTP to a remote host, REST upload into an
    API-driven storage pool, …).
    """
    iso = PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="cidata")

    def _add(content: str, iso9660_name: str, joliet_name: str) -> None:
        data = content.encode("utf-8")
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            iso_path=f"/{iso9660_name};1",
            joliet_path=f"/{joliet_name}",
        )

    buf = io.BytesIO()
    try:
        _add(meta_data, "META_DATA", "meta-data")
        _add(user_data, "USER_DATA", "user-data")
        if network_config:
            _add(network_config, "NETWORK_CONFIG", "network-config")
        iso.write_fp(buf)
        return buf.getvalue()
    except Exception as exc:
        raise CloudInitError(f"Failed to write seed ISO: {exc}") from exc
    finally:
        iso.close()


__all__ = ["CloudInitBuilder", "build_seed_iso_bytes"]
