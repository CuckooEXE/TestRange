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

Remote URIs (``qemu+ssh://…``) connect fine — and because L2 is realized by the
*daemon*, even the bridge/dnsmasq are built on the remote host — but a remote
connection still needs its named uplink bridge to pre-exist remotely and its
serial unix-socket path is on the remote host; that surface is tracked
separately (BACKEND-5).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pwd
import socket
import struct
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testrange._log import get_logger
from testrange.exceptions import DriverError

_log = get_logger(__name__)


def _peer_uid(conn: socket.socket) -> int:
    """The uid of the process on the other end of a connected AF_UNIX socket."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


def _allowed_serial_peer_uids() -> set[int]:
    """The uids permitted to connect to a build-result serial socket.

    The socket sits 0o777 in a world-traversable ``/tmp`` because the
    orchestrator runs unprivileged and cannot chown it to the qemu service
    account, so an SO_PEERCRED filter — not the filesystem mode — is what stops a
    local co-tenant connecting ahead of QEMU and spoofing a ``TESTRANGE-RESULT:
    ok`` to poison the build cache (CORE-91). Allowed: ourselves
    (``qemu:///session`` runs QEMU as us), root, and the system qemu service
    account (``qemu:///system`` — ``libvirt-qemu`` on Debian, ``qemu`` on RHEL).
    """
    uids = {os.getuid(), 0}
    for name in ("libvirt-qemu", "qemu"):
        with contextlib.suppress(KeyError):
            uids.add(pwd.getpwnam(name).pw_uid)
    return uids


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
        # Serial build-result sink plumbing. A guest's <serial type='unix'> is
        # mode='connect' (QEMU connects to a socket WE listen on) — the inverse
        # of mode='bind', which fails non-root because the qemu-owned socket is
        # not connect-able by uid 1000. We must be listening *before* the domain
        # starts (libvirt's security driver stats the path at start), so the
        # listener is opened in create_vm and accept()ed later by the sink.
        self._serial_dir: Path | None = None
        self._serial_listeners: dict[str, tuple[Any, str]] = {}
        # Serializes mutation of this one shared connection's in-memory state
        # when the I/O phases drive it from several worker threads (ADR-0023):
        # QGA agent commands (so concurrent readiness polls don't interleave on
        # the channel) and the lazy serial-listener plumbing below (the
        # ``_serial_dir`` create + the ``_serial_listeners`` map, which a
        # parallel build mutates per build VM). Held only for the quick op — a
        # command, or a dict/dir mutation — never across a readiness-loop sleep,
        # so the waits still overlap. Re-entrant because a guest op
        # (exec/read/write) issues several agent commands in sequence.
        self.call_lock = threading.RLock()

    def connect(self) -> None:
        libvirt = _import_libvirt()
        self._lv = libvirt.open(self._conn.libvirt_uri)
        if self._lv is None:  # libvirt.open returns None on failure in some bindings
            raise DriverError(f"libvirt.open({self._conn.libvirt_uri!r}) returned no connection")
        _log.info("connected to libvirt at %s", self._conn.libvirt_uri)

    def close(self) -> None:
        for backend_name in list(self._serial_listeners):
            self.close_serial_listener(backend_name)
        if self._serial_dir is not None:
            with contextlib.suppress(OSError):
                self._serial_dir.rmdir()
            self._serial_dir = None
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

    def _ensure_serial_dir(self) -> Path:
        """A driver-owned dir under ``/tmp`` to hold serial sockets.

        Two constraints fix the location:

        - **Traversable by the daemon.** QEMU (``libvirt-qemu``) must connect to
          our ``mode='connect'`` socket, so every parent dir needs ``o+x``. We
          create directly under ``/tmp`` (``1777``) rather than honoring
          ``$TMPDIR`` (often a private ``0700`` dir the daemon can't enter), and
          chmod the dir ``0755``.
        - **Cleanable.** libvirt's security driver relabels each socket to
          ``libvirt-qemu`` at domain start; because we *own* this (non-sticky)
          dir, we can still unlink the relabeled socket and rmdir afterward.

        (A host whose libvirtd runs with systemd ``PrivateTmp=yes`` would not
        share this ``/tmp``; that is a remote/hardened-host concern tracked under
        BACKEND-5, not the local-cert path.)
        """
        # Double-checked under call_lock: a parallel build (ADR-0023) calls
        # open_serial_listener for several build VMs at once, and an unguarded
        # check-then-set would mkdtemp twice and leak the loser's dir.
        with self.call_lock:
            if self._serial_dir is None:
                d = Path(tempfile.mkdtemp(prefix="tr-lv-serial-", dir="/tmp"))
                # 0o711, not 0o755: the daemon connects to a known exact socket
                # path (from the domain XML) and only needs to *traverse* the dir,
                # not *list* it. Dropping o+r denies a local co-tenant the cheap
                # enumeration of our socket filenames (CORE-91).
                d.chmod(0o711)
                self._serial_dir = d
            return self._serial_dir

    def open_serial_listener(self, backend_name: str) -> str:
        """Bind+listen a unix socket for ``backend_name``'s serial console.

        Returns the socket path to embed in the domain XML (mode='connect'). Must
        be called before ``start_vm`` so the socket exists when QEMU connects;
        the connection waits in the listen backlog until :meth:`accept_serial`.
        """
        sock_dir = self._ensure_serial_dir()
        token = hashlib.sha256(backend_name.encode()).hexdigest()[:12]
        path = str(sock_dir / f"{token}.sock")
        with contextlib.suppress(FileNotFoundError):
            Path(path).unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        Path(path).chmod(0o777)  # qemu (libvirt-qemu) must be able to connect
        srv.listen(1)
        with self.call_lock:  # concurrent build VMs each register their listener
            self._serial_listeners[backend_name] = (srv, path)
        return path

    def accept_serial(self, backend_name: str, *, timeout: float) -> Any:
        """Accept QEMU's connection to ``backend_name``'s serial listener.

        Raises :class:`DriverError` if no listener was opened for this VM.
        """
        with self.call_lock:  # guard the shared map; release before the blocking accept
            entry = self._serial_listeners.get(backend_name)
        if entry is None:
            raise DriverError(f"no serial listener open for {backend_name!r}")
        srv, _path = entry
        # Filter on the peer's uid: a local co-tenant can connect to the 0o777
        # socket ahead of QEMU and sit in the listen backlog, then write a forged
        # `TESTRANGE-RESULT: ok` the build phase would trust. Drop any connection
        # whose peer isn't QEMU (or us/root) and keep waiting within the deadline
        # so the spoof can't poison the cache (CORE-91).
        allowed = _allowed_serial_peer_uids()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no serial connection from an allowed uid for {backend_name!r} "
                    f"within {timeout:.0f}s"
                )
            srv.settimeout(remaining)
            conn, _ = srv.accept()
            uid = _peer_uid(conn)
            if uid in allowed:
                return conn
            _log.warning(
                "serial %r: rejecting connection from unexpected uid %d (allowed: %s)",
                backend_name,
                uid,
                sorted(allowed),
            )
            conn.close()

    def close_serial_listener(self, backend_name: str) -> None:
        """Close + unlink ``backend_name``'s serial listener. Tolerant of absence."""
        with self.call_lock:
            entry = self._serial_listeners.pop(backend_name, None)
        if entry is None:
            return
        srv, path = entry
        with contextlib.suppress(OSError):
            srv.close()
        with contextlib.suppress(OSError):
            Path(path).unlink()
