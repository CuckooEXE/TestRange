"""Connection plumbing for the libvirt driver.

The control plane is **libvirt-python** against a local hypervisor
(``qemu:///system`` by default) — the sole libvirt dependency. L2 is realized
through the libvirt *network* API (``networkDefineXML``/``networkCreate``), so
the daemon builds the bridge + dnsmasq and the driver needs no ``pyroute2`` and
no ``CAP_NET_ADMIN`` (ADR-0016, BACKEND-1). Membership in the ``libvirt`` group
is the only host requirement; no root, no pre-install.

:class:`LibvirtConn` is the connection config (round-trips through the teardown
URI persisted in ``state.json``); :class:`LibvirtClient` wraps a live
``virConnect``. The driver holds exactly one ``LibvirtClient``; the concern
modules (``_net``, ``_storage``, ``_vm``, ``_guest``, ``_serial``) take it as
their first argument.

Remote URIs (``qemu+ssh://…``) are first-class: because L2 is realized by the
*daemon*, even the bridge/dnsmasq are built on the remote host, and the
build-result sink rides this same connection (``virDomainOpenConsole`` of the
guest's pty serial — no host-local socket path in the domain XML, BACKEND-5).
A remote connection still needs its named uplink bridge to pre-exist remotely.
"""

from __future__ import annotations

import threading
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
    _route_libvirt_errors_to_log(libvirt)
    return libvirt


_error_handler_registered = False

# Process-wide libvirt event loop, started once before the first connection.
# Nonblocking virStream I/O (the BACKEND-5 console sink) only makes progress
# when something pumps the connection's socket: blocking RPCs pump it as a side
# effect of their own wait, but a sink that only ever polls `recv` on an idle
# connection would see would-block forever — stream data (and the EOF when the
# build VM powers off) arrives exclusively through the event loop. Live-found
# on the nested-cert sweep: four parallel remote builds sat deaf for the whole
# build-timeout. The loop must be registered BEFORE any virConnect is opened.
_event_loop_lock = threading.Lock()
_event_loop_running = False


def _ensure_event_loop(libvirt: Any) -> None:
    global _event_loop_running
    with _event_loop_lock:
        if _event_loop_running:
            return
        libvirt.virEventRegisterDefaultImpl()

        def _pump() -> None:
            while True:
                try:
                    libvirt.virEventRunDefaultImpl()
                except Exception as e:  # pragma: no cover - never seen; keep pumping
                    _log.warning("libvirt event loop iteration failed: %s", e)

        threading.Thread(target=_pump, name="libvirt-events", daemon=True).start()
        _event_loop_running = True


def _route_libvirt_errors_to_log(libvirt: Any) -> None:
    """Stop libvirt printing errors to stderr; route them through Python logging.

    libvirt's C layer prints every error to fd 2 by default (e.g.
    ``libvirt: QEMU Driver error : …``). That bypasses Python ``logging`` — and
    therefore the rich handler and the live dashboard's ``Live`` region — and
    writes straight to the terminal, corrupting the display (the flicker /
    ``libvirt: …`` glimpses). Registering any handler replaces that default
    stderr printer. The message is already carried by the raised
    ``libvirtError`` the driver surfaces explicitly, and many of these fire on
    benign, expected conditions during polling/teardown, so we log at DEBUG
    (visible under ``--log-level debug``) and keep them off the raw terminal.

    Registered once, process-globally (libvirt's handler registry is global);
    re-imports are no-ops. ``registerErrorHandler`` writes to fd 2 from the C
    layer, so this cannot be done from Python's ``sys.stderr`` redirection.
    """
    global _error_handler_registered
    if _error_handler_registered:
        return

    def _handler(_ctx: object, error: tuple[Any, ...]) -> None:
        # Called as f(ctx, error); error = (code, domain, message, level, …).
        _log.debug("libvirt: %s", error[2] if len(error) > 2 else error)

    # registerErrorHandler(f, ctx) — the callback is the FIRST arg.
    libvirt.registerErrorHandler(_handler, None)
    _error_handler_registered = True


def _import_libvirt_qemu() -> Any:
    """Lazy import of the ``libvirt_qemu`` helper module.

    The QGA native transport (``_guest``) drives ``qemuAgentCommand``, which
    lives in ``libvirt_qemu`` — a separate module shipped by the same
    ``libvirt-python`` wheel, not the top-level ``libvirt``.
    """
    try:
        import libvirt_qemu
    except ImportError as e:
        raise DriverError(
            "libvirt-python is not installed; install with `pip install -e .[libvirt]`"
        ) from e
    return libvirt_qemu


@dataclass(frozen=True)
class LibvirtConn:
    """Everything needed to reach a libvirt hypervisor.

    ``libvirt_uri`` is the connect URI (default ``qemu:///system`` — the
    system-wide QEMU instance, reachable non-root by a ``libvirt``-group member).
    There is no ``backing_pool`` knob: per-run dir pools are driver-created under
    ``/var/lib/libvirt/images`` and torn down with the run (BACKEND-1), so the
    only connection state is the URI itself.
    """

    libvirt_uri: str = "qemu:///system"

    def to_uri(self) -> str:
        """Round-trip to the URI persisted in state.json (cleanup entry point)."""
        query = urllib.parse.urlencode({"conn": self.libvirt_uri})
        return f"{_TEARDOWN_SCHEME}://?{query}"

    @classmethod
    def from_uri(cls, uri: str) -> LibvirtConn:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != _TEARDOWN_SCHEME:
            raise DriverError(f"expected a {_TEARDOWN_SCHEME}:// teardown URI, got {uri!r}")
        q = urllib.parse.parse_qs(parsed.query)
        return cls(libvirt_uri=q.get("conn", ["qemu:///system"])[0])


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
        # Serializes QGA agent commands on this one shared connection when the
        # I/O phases drive it from several worker threads (ADR-0023), so
        # concurrent readiness polls don't interleave on the channel. Held only
        # for the quick op — one command — never across a readiness-loop sleep,
        # so the waits still overlap. Re-entrant because a guest op
        # (exec/read/write) issues several agent commands in sequence.
        self.call_lock = threading.RLock()

    def connect(self) -> None:
        libvirt = _import_libvirt()
        _ensure_event_loop(libvirt)
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

    # The libvirt-specific "does this object exist?" translation lives here so
    # the concern modules never touch libvirt error codes: a hit returns the
    # object, a clean absence returns None, and any *other* libvirt error
    # propagates (a permission/transport failure must not read as "gone").

    def lookup_pool(self, name: str) -> Any | None:
        libvirt = _import_libvirt()
        try:
            return self.raw.storagePoolLookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                return None
            raise

    def lookup_volume(self, pool_name: str, vol_name: str) -> Any | None:
        libvirt = _import_libvirt()
        pool = self.lookup_pool(pool_name)
        if pool is None:
            return None
        try:
            return pool.storageVolLookupByName(vol_name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return None
            raise

    def lookup_domain(self, name: str) -> Any | None:
        libvirt = _import_libvirt()
        try:
            return self.raw.lookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise

    def lookup_network(self, name: str) -> Any | None:
        libvirt = _import_libvirt()
        try:
            return self.raw.networkLookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_NETWORK:
                return None
            raise
