"""Connection profile for the libvirt driver (CORE-9 / CORE-18).

:class:`LibvirtProfile` is the concrete :class:`~testrange.connect.BackendProfile`
that the ``--profile`` path dispatches to when the TOML names ``driver =
"libvirt"``. It declares the two libvirt-specific connection keys (``uri``,
``backing_pool``), the named-uplink map, self-registers, and builds a
:class:`LibvirtDriver` against that connection.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from testrange.connect import BackendProfile, register_profile
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.drivers.libvirt.driver import LibvirtDriver


@dataclass(frozen=True)
class LibvirtProfile(BackendProfile):
    """Connection profile for the libvirt backend (BACKEND-1).

    ``uri`` is the libvirt connect URI; ``backing_pool`` is the name of a
    pre-existing libvirt **dir** storage pool the per-run pools carve into.
    Both default to the same values :class:`LibvirtConn` uses (``qemu:///system``
    / ``default``), keeping a no-knobs ``driver = "libvirt"`` profile workable
    on a stock host.
    """

    scheme: ClassVar[str] = "libvirt"
    _FIELDS: ClassVar[frozenset[str]] = frozenset({"uri", "backing_pool"})

    uri: str = "qemu:///system"
    backing_pool: str = "default"
    uplinks: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        cls._validate_keys(table, cls._FIELDS, path)
        return cls(
            uri=str(table.get("uri", "qemu:///system")),
            backing_pool=str(table.get("backing_pool", "default")),
            uplinks=cls._parse_uplinks(table, path),
        )

    def build_driver(self) -> LibvirtDriver:
        return LibvirtDriver(
            LibvirtConn(libvirt_uri=self.uri, backing_pool=self.backing_pool),
            uplinks=self.uplinks,
        )

    def describe_fields(self) -> Iterable[tuple[str, str]]:
        yield ("uri", self.uri)
        yield ("backing_pool", self.backing_pool)


register_profile(LibvirtProfile)


__all__ = ["LibvirtProfile"]
