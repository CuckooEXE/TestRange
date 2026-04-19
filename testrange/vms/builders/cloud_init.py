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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from passlib.hash import sha512_crypt
from pycdlib import PyCdlib  # type: ignore[attr-defined]

from testrange.cache import vm_config_hash
from testrange.exceptions import CloudInitError
from testrange.packages import Homebrew
from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.images import resolve_image

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.backends.libvirt.vm import VM
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.packages import AbstractPackage


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

    Stateless: every method takes the :class:`~testrange.backends.libvirt.VM`
    as an argument.  One builder instance can serve any number of
    Linux VMs.
    """

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
        base_image = resolve_image(vm.iso, cache)
        work_disk = run.create_install_disk(
            vm.name, base_image, vm._primary_disk_size()
        )
        seed_iso = run.seed_iso_path(vm.name, install=True)
        write_seed_iso(
            seed_iso,
            meta_data=self.install_meta_data(vm, self.cache_key(vm)),
            user_data=self.install_user_data(vm),
        )
        return InstallDomain(work_disk=work_disk, seed_iso=seed_iso)

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
        seed_iso = run.seed_iso_path(vm.name, install=False)
        write_seed_iso(
            seed_iso,
            meta_data=self.run_meta_data(vm, run.run_id),
            user_data=self.run_user_data(vm),
            network_config=self.run_network_config(mac_ip_pairs),
        )
        return RunDomain(seed_iso=seed_iso)

    # ------------------------------------------------------------------
    # YAML generation — kept public so tests and debuggers can inspect
    # the exact payload without booting anything.  All methods are
    # stateless: they read everything they need from ``vm``.
    # ------------------------------------------------------------------

    def install_user_data(self, vm: VM) -> str:
        """Return the phase-1 ``user-data`` YAML string.

        The VM powers off after cloud-init completes so the installed
        disk can be snapshotted and cached.
        """
        doc: dict[str, Any] = {
            "hostname": vm.name,
            "fqdn": f"{vm.name}.local",
            "manage_etc_hosts": True,
            "users": [_user_entry(c) for c in vm.users],
            "ssh_pwauth": True,
            "packages": _native_packages(vm.pkgs),
            "package_update": True,
            "package_upgrade": False,
            "runcmd": _runcmd_entries(vm.pkgs, vm.users, vm.post_install_cmds),
            "datasource_list": ["NoCloud", "None"],
            "power_state": {
                "mode": "poweroff",
                "message": "TestRange install phase complete",
                "timeout": 30,
                "condition": True,
            },
        }
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
) -> list[str]:
    """Return shell commands to append to cloud-init ``runcmd:``.

    Handles Homebrew and other non-native package managers, then
    appends the caller's ``post_install_cmds``.
    """
    cmds: list[str] = []

    cmds.append("systemctl enable --now qemu-guest-agent || true")

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
    return cmds


def write_seed_iso(
    output_path: Path,
    meta_data: str,
    user_data: str,
    network_config: str | None = None,
) -> None:
    """Write a cloud-init NoCloud seed ISO to *output_path*.

    Uses :mod:`pycdlib` for pure-Python ISO 9660 creation (no external
    ``genisoimage`` / ``xorriso`` dependency).  The volume ID is
    ``cidata``, as required by the cloud-init NoCloud datasource.
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

    try:
        _add(meta_data, "META_DATA", "meta-data")
        _add(user_data, "USER_DATA", "user-data")
        if network_config:
            _add(network_config, "NETWORK_CONFIG", "network-config")
        iso.write(str(output_path))
    except Exception as exc:
        raise CloudInitError(f"Failed to write seed ISO: {exc}") from exc
    finally:
        iso.close()


__all__ = ["CloudInitBuilder", "write_seed_iso"]
