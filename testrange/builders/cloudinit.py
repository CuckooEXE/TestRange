"""CloudInitBuilder — cloud-init seed renderer for Linux guests.

The seed is an ISO9660 image labeled ``cidata`` containing
``user-data``, ``meta-data``, and ``network-config``. The install VM
mounts it on first boot and applies it. Our seeds end with
``poweroff`` in ``runcmd`` so the install VM self-terminates and the
orchestrator (Phase 4) snapshots the disk as the cached post-install
artifact.

Network rendering uses **interface-name matching** (``match: name: ...``)
to sidestep MAC-based matches in the cached disk — see PLAN.md TODO.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import yaml

from testrange.builders.base import Builder
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.exceptions import BuilderError
from testrange.packages.apt import Apt
from testrange.packages.base import Package
from testrange.packages.pip import Pip

if TYPE_CHECKING:  # pragma: no cover
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


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
    ) -> None:
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
        self.base = base
        self._credentials = creds
        self.packages = pkgs
        self.post_install_commands = cmds

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self._credentials

    def find_credential(self, username: str) -> Credential | None:
        """Look up a credential by username. Returns None if not found."""
        for c in self._credentials:
            if c.username == username:
                return c
        return None

    # ---- rendering -----------------------------------------------------

    def render_user_data(self, spec: VMSpec, recipe: VMRecipe) -> str:
        """Render cloud-init ``user-data`` (YAML, ``#cloud-config`` header)."""
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
        pip_pkgs = [p.name for p in self.packages if isinstance(p, Pip)]

        runcmd: list[str] = []
        if pip_pkgs:
            runcmd.append("pip3 install --break-system-packages " + " ".join(pip_pkgs))
        runcmd.extend(self.post_install_commands)
        runcmd.append("poweroff")  # self-terminating install

        body: dict[str, Any] = {
            "ssh_pwauth": True,
            "users": users or [{"name": "root", "lock_passwd": False}],
        }
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

    def render_network_config(self, spec: VMSpec, recipe: VMRecipe) -> str:
        """Render cloud-init ``network-config`` (netplan v2).

        Matches interfaces by **kernel name** (``match: name: ...``), not
        MAC, so the cached disk works regardless of MAC stability (the
        stable-MAC TODO is belt-and-suspenders).
        """
        del recipe
        # NIC ordering on libvirt: PCI slot order = attach order. Map to
        # predictable interface names (en* in kernel 3.x+ are pcie-based).
        # In practice with virtio, names are ens3, ens4, ... Use a glob.
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

    def config_hash(self, spec: VMSpec, recipe: VMRecipe, *, base_sha: str = "") -> str:
        """Deterministic 16-char hex hash of the post-install disk contents.

        Inputs are the rendered seed text + the base disk's content sha
        (passed in by the orchestrator after resolving the CacheEntry).
        Pure: no clocks, no run_id, no I/O.
        """
        u = self.render_user_data(spec, recipe)
        m = self.render_meta_data(spec, recipe)
        n = self.render_network_config(spec, recipe)
        combined = (
            f"user-data:\n{u}\n---\nmeta-data:\n{m}\n---\n"
            f"network-config:\n{n}\n---\nbase:{base_sha}"
        )
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def render_seed(self, spec: VMSpec, recipe: VMRecipe) -> bytes:
        """Build the ISO9660 ``cidata`` seed image as bytes."""
        try:
            import pycdlib
        except ImportError as e:
            raise BuilderError(
                "pycdlib is not installed; install with `pip install -e .[cloudinit]`"
            ) from e

        user_data = self.render_user_data(spec, recipe).encode("utf-8")
        meta_data = self.render_meta_data(spec, recipe).encode("utf-8")
        network_config = self.render_network_config(spec, recipe).encode("utf-8")

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
