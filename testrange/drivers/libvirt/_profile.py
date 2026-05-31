"""Connection profile for the libvirt driver (CORE-9 / CORE-18).

:class:`LibvirtProfile` is the concrete :class:`~testrange.connect.BackendProfile`
that the ``--profile`` path dispatches to when the TOML names ``driver =
"libvirt"``. It declares the one libvirt connection key (``uri``) plus the
named-uplink map, self-registers, and builds a :class:`LibvirtDriver` against
that connection. There is no ``backing_pool`` knob — per-run dir pools are
driver-created and torn down with the run (BACKEND-1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from testrange.connect import BackendProfile, register_profile
from testrange.devices.network import StaticAddr
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.drivers.libvirt.driver import LibvirtDriver


@dataclass(frozen=True)
class LibvirtProfile(BackendProfile):
    """Connection profile for the libvirt backend (BACKEND-1).

    ``uri`` is the libvirt connect URI; it defaults to the same value
    :class:`LibvirtConn` uses (``qemu:///system``), keeping a no-knobs ``driver =
    "libvirt"`` profile workable on a stock host where the user is in the
    ``libvirt`` group. ``uplinks`` maps a plan's logical ``Switch.uplink`` names
    to host bridges (ADR-0016) — e.g. ``egress`` to the out-of-band ``tr-egress``
    NAT bridge a sidecar routes out of.
    """

    scheme: ClassVar[str] = "libvirt"
    _FIELDS: ClassVar[frozenset[str]] = frozenset({"uri"})

    uri: str = "qemu:///system"
    uplinks: Mapping[str, str] = field(default_factory=dict)
    uplink_addrs: Mapping[str, StaticAddr] = field(default_factory=dict)

    @classmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        cls._validate_keys(table, cls._FIELDS, path)
        uplinks, uplink_addrs = cls._parse_uplinks(table, path)
        return cls(
            uri=str(table.get("uri", "qemu:///system")),
            uplinks=uplinks,
            uplink_addrs=uplink_addrs,
        )

    def build_driver(self) -> LibvirtDriver:
        return LibvirtDriver(LibvirtConn(libvirt_uri=self.uri), uplinks=self.uplinks)

    def describe_fields(self) -> Iterable[tuple[str, str]]:
        yield ("uri", self.uri)


register_profile(LibvirtProfile)


__all__ = ["LibvirtProfile"]
