"""Connection profile for the ESXi driver (CORE-18).

:class:`ESXiProfile` is the concrete :class:`~testrange.connect.BackendProfile`
the ``--profile`` path dispatches to when the TOML names ``driver = "esxi"``. It
declares the ESXi-specific connection keys (host/user/password/datastore/port/
verify_ssl) plus the common ``[uplinks]`` map, and builds an :class:`ESXiDriver`
against them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from testrange.connect import BackendProfile, register_profile
from testrange.devices.network import StaticAddr
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver


@dataclass(frozen=True)
class ESXiProfile(BackendProfile):
    """Connection profile for the standalone ESXi backend.

    ``datastore`` is the VMFS store volume byte I/O targets (default
    ``datastore1``, the single-datastore lab default). ``verify_ssl`` defaults
    off — ESXi ships a self-signed cert.
    """

    scheme: ClassVar[str] = "esxi"
    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"host", "user", "password", "port", "verify_ssl", "datastore"}
    )

    host: str = ""
    user: str = "root"
    password: str = ""
    port: int = 443
    verify_ssl: bool = False
    datastore: str = "datastore1"
    uplinks: Mapping[str, str] = field(default_factory=dict)
    uplink_addrs: Mapping[str, StaticAddr] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("ESXiProfile.host must be a non-empty string (the ESXi host/IP)")

    @classmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        cls._validate_keys(table, cls._FIELDS, path)
        uplinks, uplink_addrs = cls._parse_uplinks(table, path)
        return cls(
            host=str(table.get("host", "")),
            user=str(table.get("user", "root")),
            password=str(table.get("password", "")),
            port=int(table.get("port", 443)),
            verify_ssl=bool(table.get("verify_ssl", False)),
            datastore=str(table.get("datastore", "datastore1")),
            uplinks=uplinks,
            uplink_addrs=uplink_addrs,
        )

    def build_driver(self) -> ESXiDriver:
        return ESXiDriver(
            EsxiConn(
                host=self.host,
                user=self.user,
                password=self.password,
                datastore=self.datastore,
                verify_ssl=self.verify_ssl,
                port=self.port,
            ),
            uplinks=self.uplinks,
        )

    def describe_fields(self) -> Iterable[tuple[str, str]]:
        yield ("host", self.host)
        yield ("port", str(self.port))
        yield ("user", self.user)
        yield ("datastore", self.datastore)
        yield ("password", self._mask_password(self.password))


register_profile(ESXiProfile)


__all__ = ["ESXiProfile"]
