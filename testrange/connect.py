"""Connection profiles — the local TOML file a dev points ``--connect`` at (CORE-9).

A connection profile supplies the *backend* a portable plan runs against: the
driver scheme, the connection (host/user/password/port/...), and the
environment knobs (build egress, backing storage, node) that are a binding
concern rather than portable topology. Keeping these in a local file — not the
committed plan — lets one plan run unmodified against any backend, and keeps
backend addresses out of the test.

Secrets policy (deliberately simple): passwords live **inline** in the TOML as
plain ``password`` / ``ssh_password`` strings. TestRange backends are
firewalled lab environments, so a credential in a local file is acceptable;
``.gitignore`` (CORE-12) keeps a real profile out of git. There is no
env/file-indirection layer.

Format (parsed with stdlib :mod:`tomllib`, no new dependency)::

    driver = "proxmox"
    host = "10.0.0.5"
    user = "root@pam"          # optional; a bare user takes the @pam realm
    password = "Target123!"
    port = 8006                # optional
    verify_ssl = false         # optional
    node = ""                  # optional; "" auto-detects the single node
    backing_storage = "local"  # optional
    ssh_user = "root"          # optional; defaults to the API user's local part
    ssh_password = "..."       # optional; defaults to the API password
    ssh_port = 22              # optional

    [build_switch]             # optional: managed build-internet egress
    uplink = "vmbr9"           # host interface to SNAT the build network out of
    cidr = "10.10.10.0/24"     # optional internal build subnet

The ``[build_switch]`` table maps to a
:class:`~testrange.networks.base.ManagedBuildSwitch` — the managed-egress
automation (ADR-0014). A bring-your-own plain ``Switch`` egress path is not
expressible here by design; declare it by *pinning* the plan to a concrete
``*Hypervisor`` with a ``build_switch=Switch(...)`` instead.

Shape validation only: this module parses and validates structure. The driver
*scheme* is not checked against the registry here — that is
``driver_for_profile``'s job (CORE-8) — so this stays backend-agnostic.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testrange.exceptions import ProfileError
from testrange.networks.base import ManagedBuildSwitch

# Connection fields fed to a driver's ``from_profile`` (CORE-8). ``build_switch``
# is intentionally absent: it is read off the BackendProfile directly by the
# binding resolver (CORE-10), not passed to driver construction.
_CONNECTION_KEYS = (
    "host",
    "user",
    "port",
    "verify_ssl",
    "node",
    "backing_storage",
    "ssh_user",
    "ssh_password",
    "ssh_port",
    "pool_root",
    "backing_capacity_gb",
)
_ALLOWED_TOP = {"driver", "password", "build_switch", *_CONNECTION_KEYS}
_ALLOWED_BUILD_SWITCH = {"uplink", "cidr"}


@dataclass(frozen=True)
class BackendProfile:
    """A parsed connection profile: driver scheme + connection + build egress.

    ``driver`` is the only required field. The rest default to ``None`` (driver
    defaults apply) except ``password`` (empty string). ``build_switch`` carries
    the optional managed build-egress intent.
    """

    driver: str
    host: str | None = None
    user: str | None = None
    password: str = ""
    port: int | None = None
    verify_ssl: bool | None = None
    node: str | None = None
    backing_storage: str | None = None
    ssh_user: str | None = None
    ssh_password: str | None = None
    ssh_port: int | None = None
    pool_root: str | None = None
    backing_capacity_gb: int | None = None
    build_switch: ManagedBuildSwitch | None = None

    def __post_init__(self) -> None:
        if not self.driver:
            raise ProfileError("connection profile requires a non-empty 'driver' scheme")

    def to_mapping(self) -> dict[str, Any]:
        """Connection fields for the registry's ``driver_for_profile`` (CORE-8).

        Emits ``driver`` and ``password`` always, and every other connection
        field that was set (``None`` omitted so the driver's own defaults
        apply). ``build_switch`` is excluded — the resolver reads it off the
        profile directly.
        """
        mapping: dict[str, Any] = {"driver": self.driver, "password": self.password}
        for key in _CONNECTION_KEYS:
            value = getattr(self, key)
            if value is not None:
                mapping[key] = value
        return mapping


def load_profile(path: Path) -> BackendProfile:
    """Read, parse, and validate a connection profile at ``path``.

    Raises :class:`ProfileError` for a missing/unreadable file, invalid TOML, an
    unknown key (typo protection — the offending key is named), a missing
    ``driver``, or a malformed ``[build_switch]`` table.
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ProfileError(f"connection profile not found: {path}") from e
    except OSError as e:
        raise ProfileError(f"cannot read connection profile {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"connection profile {path} is not valid TOML: {e}") from e
    return _from_toml(data, path)


def _from_toml(data: dict[str, Any], path: Path) -> BackendProfile:
    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise ProfileError(
            f"connection profile {path} has unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_TOP)}"
        )
    driver = data.get("driver")
    if not isinstance(driver, str) or not driver:
        raise ProfileError(f"connection profile {path} requires a non-empty 'driver' scheme")

    build_switch = None
    if "build_switch" in data:
        build_switch = _parse_build_switch(data["build_switch"], path)

    return BackendProfile(
        driver=driver,
        host=data.get("host"),
        user=data.get("user"),
        password=data.get("password", ""),
        port=data.get("port"),
        verify_ssl=data.get("verify_ssl"),
        node=data.get("node"),
        backing_storage=data.get("backing_storage"),
        ssh_user=data.get("ssh_user"),
        ssh_password=data.get("ssh_password"),
        ssh_port=data.get("ssh_port"),
        pool_root=data.get("pool_root"),
        backing_capacity_gb=data.get("backing_capacity_gb"),
        build_switch=build_switch,
    )


def _parse_build_switch(table: Any, path: Path) -> ManagedBuildSwitch:
    if not isinstance(table, dict):
        raise ProfileError(f"connection profile {path}: [build_switch] must be a table")
    unknown = set(table) - _ALLOWED_BUILD_SWITCH
    if unknown:
        raise ProfileError(
            f"connection profile {path}: [build_switch] has unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_BUILD_SWITCH)}"
        )
    uplink = table.get("uplink")
    if not isinstance(uplink, str) or not uplink:
        raise ProfileError(
            f"connection profile {path}: [build_switch] requires a non-empty 'uplink'"
        )
    try:
        return ManagedBuildSwitch(uplink=uplink, cidr=table.get("cidr"))
    except ValueError as e:
        raise ProfileError(f"connection profile {path}: invalid [build_switch]: {e}") from e


__all__ = ["BackendProfile", "load_profile"]
