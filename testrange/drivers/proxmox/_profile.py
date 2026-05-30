"""Connection profile for the Proxmox VE driver (CORE-9 / CORE-18).

:class:`ProxmoxProfile` is the concrete :class:`~testrange.connect.BackendProfile`
the ``--profile`` path dispatches to when the TOML names ``driver = "proxmox"``.
It declares the PVE-specific connection keys (REST + SSH), applies the same
defaulting :class:`ProxmoxHypervisor` does on the Plan-time path (bare ``user``
takes ``@pam``; SSH user/password reuse the API user's local part and the API
password unless overridden) so a profile-supplied connection resolves
identically to an in-Plan one, and builds a :class:`ProxmoxDriver` against it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from testrange.connect import BackendProfile, register_profile
from testrange.drivers.proxmox._client import ProxmoxConn, normalize_realm
from testrange.drivers.proxmox.driver import ProxmoxDriver


@dataclass(frozen=True)
class ProxmoxProfile(BackendProfile):
    """Connection profile for the Proxmox VE backend.

    ``ssh_user`` / ``ssh_password`` are ``None`` when unset — :meth:`build_driver`
    derives them from the API ``user`` / ``password`` so a single set of creds
    Just Works. ``node = ""`` auto-detects the single node on the host at
    connect time (matches :class:`ProxmoxConn`).
    """

    scheme: ClassVar[str] = "proxmox"
    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "host",
            "user",
            "password",
            "port",
            "verify_ssl",
            "node",
            "backing_storage",
            "ssh_user",
            "ssh_password",
            "ssh_port",
        }
    )

    host: str = ""
    user: str = "root@pam"
    password: str = ""
    port: int = 8006
    verify_ssl: bool = False
    node: str = ""
    backing_storage: str = "local"
    ssh_user: str | None = None
    ssh_password: str | None = None
    ssh_port: int = 22
    uplinks: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Early-fail authoring check that used to live on ProxmoxHypervisor
        # (CORE-19 moved it here when the hypervisor became topology-only).
        if not self.host:
            raise ValueError("ProxmoxProfile.host must be a non-empty string (the PVE host/IP)")

    @classmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        cls._validate_keys(table, cls._FIELDS, path)
        ssh_password = table.get("ssh_password")
        return cls(
            host=str(table.get("host", "")),
            user=str(table.get("user", "root@pam")),
            password=str(table.get("password", "")),
            port=int(table.get("port", 8006)),
            verify_ssl=bool(table.get("verify_ssl", False)),
            node=str(table.get("node", "")),
            backing_storage=str(table.get("backing_storage", "local")),
            ssh_user=str(table["ssh_user"]) if "ssh_user" in table else None,
            ssh_password=str(ssh_password) if ssh_password is not None else None,
            ssh_port=int(table.get("ssh_port", 22)),
            uplinks=cls._parse_uplinks(table, path),
        )

    def build_driver(self) -> ProxmoxDriver:
        # Defaulting mirrors ProxmoxHypervisor.conn(): bare user takes @pam; SSH
        # reuses the API user's local part and the API password unless overridden.
        # The in-Plan path and the --profile path must resolve identically here so
        # a pinned-vs-portable plan can't drift.
        user = normalize_realm(self.user)
        ssh_user = self.ssh_user or user.split("@", 1)[0]
        ssh_password = self.ssh_password if self.ssh_password is not None else self.password
        return ProxmoxDriver(
            ProxmoxConn(
                host=self.host,
                node=self.node,
                user=user,
                password=self.password,
                verify_ssl=self.verify_ssl,
                port=self.port,
                backing_storage=self.backing_storage,
                ssh_user=ssh_user,
                ssh_password=ssh_password,
                ssh_port=self.ssh_port,
            ),
            uplinks=self.uplinks,
        )

    def describe_fields(self) -> Iterable[tuple[str, str]]:
        yield ("host", self.host)
        yield ("port", str(self.port))
        if self.node:
            yield ("node", self.node)
        yield ("user", self.user)
        yield ("password", self._mask_password(self.password))


register_profile(ProxmoxProfile)


__all__ = ["ProxmoxProfile"]
