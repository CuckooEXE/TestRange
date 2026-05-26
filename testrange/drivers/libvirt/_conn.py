"""Connection plumbing for the libvirt driver.

The control plane is **libvirt-python** against a local hypervisor
(``qemu:///system`` by default). :class:`LibvirtConn` is the connection config
(round-trips through the teardown URI persisted in ``state.json``);
:class:`LibvirtClient` wraps a live ``virConnect``. The driver holds exactly one
``LibvirtClient``; the concern modules (``_net``, ``_storage``, ``_vm``,
``_guest``) take it as their first argument.

Remote URIs (``qemu+ssh://…``) connect fine for the control plane, but host-local
L2 (the pyroute2 bridges) can't reach a remote host — so remote L2 is out of
scope here and tracked separately (BACKEND-5).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from testrange._log import get_logger
from testrange.exceptions import DriverError

_log = get_logger(__name__)

# The wrapper scheme TestRange persists into state.json. The libvirt connect URI
# is itself a URI (with its own scheme/query), so it is url-quoted *inside* this
# wrapper rather than used directly — keeps the two URI grammars from colliding.
_TEARDOWN_SCHEME = "tr-libvirt"


def _import_libvirt() -> Any:
    """Lazy import. Raises :class:`DriverError` with an install hint if missing."""
    try:
        import libvirt
    except ImportError as e:
        raise DriverError(
            "libvirt-python is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return libvirt


def _import_pyroute2() -> Any:
    """Lazy import. ``pyroute2`` builds the isolated host bridges for L2."""
    try:
        import pyroute2
    except ImportError as e:
        raise DriverError(
            "pyroute2 is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return pyroute2


@dataclass(frozen=True)
class LibvirtConn:
    """Everything needed to reach a libvirt hypervisor.

    ``libvirt_uri`` is the connect URI (default ``qemu:///system`` — the
    system-wide QEMU instance, which needs root). ``backing_pool`` is the name of
    a pre-existing libvirt **dir** storage pool the per-run pools carve into; it
    is static driver config (not provisioned here) and must survive into the
    teardown URI so cleanup rebuilds the same context.
    """

    libvirt_uri: str = "qemu:///system"
    backing_pool: str = "default"

    def to_uri(self) -> str:
        """Round-trip to the URI persisted in state.json (cleanup entry point)."""
        query = urllib.parse.urlencode({"conn": self.libvirt_uri, "pool": self.backing_pool})
        return f"{_TEARDOWN_SCHEME}://?{query}"

    @classmethod
    def from_uri(cls, uri: str) -> LibvirtConn:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != _TEARDOWN_SCHEME:
            raise DriverError(f"expected a {_TEARDOWN_SCHEME}:// teardown URI, got {uri!r}")
        q = urllib.parse.parse_qs(parsed.query)
        return cls(
            libvirt_uri=q.get("conn", ["qemu:///system"])[0],
            backing_pool=q.get("pool", ["default"])[0],
        )


class LibvirtClient:
    """A live libvirt connection wrapping one ``virConnect``.

    The driver builds one in ``connect()``; unit tests inject a duck-typed
    stand-in (exposing ``raw`` and the handful of libvirt calls the concern
    modules use) so no real hypervisor is touched and ``libvirt`` is never
    imported.
    """

    def __init__(self, conn: LibvirtConn) -> None:
        self._conn = conn
        self._lv: Any | None = None

    def connect(self) -> None:
        libvirt = _import_libvirt()
        self._lv = libvirt.open(self._conn.libvirt_uri)
        if self._lv is None:  # libvirt.open returns None on failure in some bindings
            raise DriverError(f"libvirt.open({self._conn.libvirt_uri!r}) returned no connection")
        _log.info("connected to libvirt at %s", self._conn.libvirt_uri)

    def close(self) -> None:
        if self._lv is not None:
            self._lv.close()
            self._lv = None

    @property
    def raw(self) -> Any:
        """The live ``virConnect``. Raises if accessed before :meth:`connect`."""
        if self._lv is None:
            raise DriverError("LibvirtClient used before connect()")
        return self._lv

    @property
    def backing_pool(self) -> str:
        return self._conn.backing_pool
