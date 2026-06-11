"""Connection plumbing for the Proxmox driver.

The driver is **proxmoxer-only by policy** for the control plane. Two transports
live here:

- the **REST API** (proxmoxer ``https`` backend) for everything PVE can do over
  REST — the entire control plane; and
- a **paramiko SSH/SFTP** channel for **volume byte I/O in both directions**
  (``sftp_get`` for ``download_from_pool``; ``sftp_put`` for ``upload_to_pool``).
  PVE's REST has no volume byte-*egress* at all, and its ``upload`` endpoint
  rejects large ``import`` disk images server-side (501 "for data too large" —
  PVE-23), so volume bytes do not go over REST. SFTP writes/reads the file
  directly under the storage's content dir, where ``dir``/``nfs`` storage
  discovers it by scan (ADR-0008 §6).

``ProxmoxConn`` is the connection config (round-trips through the teardown URI);
``ProxmoxClient`` wraps a live ``ProxmoxAPI`` plus the lazy SSH channel and the
UPID task-poller. The driver holds exactly one ``ProxmoxClient``; the concern
modules (`_sdn`, `_storage`, `_vm`, `_guest`) take it as their first argument.
"""

from __future__ import annotations

import ssl
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from testrange._log import get_logger
from testrange._progress import ProgressReporter
from testrange.exceptions import DriverError

_log = get_logger(__name__)

# PVE tasks are quick (create/clone/snapshot) but disk import can run minutes on
# a multi-GB image. Size the default generously; callers pass a tighter value
# where they want one.
_TASK_TIMEOUT_S = 600.0

# proxmoxer's 5s default per-request timeout aborts large image uploads; the
# control plane is unaffected by a higher ceiling (calls return in ms).
_HTTP_TIMEOUT_S = 600.0

# urllib3's HTTPAdapter pools 10 connections by default; the I/O phases issue
# concurrent control-plane calls on the one shared session (ADR-0023), so a high
# ``--jobs`` would overflow that pool ("Connection pool is full") and churn
# connections. Size the pool with headroom over the default worker cap so the
# session is not the bottleneck; an extreme ``--jobs`` against a single PVE host
# is out of scope (you're hammering one node at that point).
_REST_POOL_MAXSIZE = 32


def _import_proxmoxer() -> Any:
    """Lazy import. Raises DriverError with an install hint if proxmoxer is missing."""
    try:
        import proxmoxer
        import urllib3
    except ImportError as e:
        raise DriverError(
            "proxmoxer is not installed; install with `pip install -e .[proxmox]`"
        ) from e
    # PVE ships a self-signed cert by default and we connect with verify_ssl off;
    # silence the per-request warning (the lab posture is documented).
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return proxmoxer


def _import_paramiko() -> Any:
    try:
        import paramiko
    except ImportError as e:
        raise DriverError(
            "paramiko is not installed; install with `pip install -e .[proxmox]`"
        ) from e
    return paramiko


def _import_websocket() -> Any:
    """Lazy import. ``websocket-client`` reads the build-result serial console."""
    try:
        import websocket
    except ImportError as e:
        raise DriverError(
            "websocket-client is not installed; install with `pip install -e .[proxmox]`"
        ) from e
    return websocket


# Handshake budget for the serial websocket: TCP connect + the termproxy auth
# round-trip. The per-read timeout during streaming is the *caller's* (it sets
# the heartbeat cadence), so this only bounds connection setup.
_WS_CONNECT_TIMEOUT_S = 15.0


def _sftp_makedirs(sftp: Any, remote_dir: str) -> None:
    """``mkdir -p`` over SFTP (paramiko has no recursive mkdir).

    ``remote_dir`` is absolute (a storage content dir under ``storage_path``).
    The dirs usually already exist on a configured storage; this is the safety
    net for a content type (e.g. ``import``) whose dir hasn't been created yet.
    """
    cur = PurePosixPath("/")
    for part in PurePosixPath(remote_dir).parts[1:]:  # skip the leading "/"
        cur = cur / part
        try:
            sftp.stat(str(cur))
        except FileNotFoundError:
            try:
                sftp.mkdir(str(cur))
            except OSError as e:
                # Tolerate a concurrent/already-created dir, but NOT a genuine
                # failure (e.g. permission): re-check and only swallow if the
                # dir now exists, else re-raise so the real cause surfaces here
                # instead of as a confusing later "put failed".
                try:
                    sftp.stat(str(cur))
                except FileNotFoundError:
                    raise e from None


def parse_connection(uri: str) -> tuple[str, int, str, str]:
    """Parse a ``proxmox://user:pass@host[:port]`` connection URI.

    Returns ``(host, port, user, password)``; ``user``/``password`` are ``""``
    when the URI omits them (the caller fills defaults/overrides). The SDN zone
    is **not** part of the connection — TestRange mints a per-run zone (see
    ``_sdn``) — and operational knobs (node, storage, ssh) ride on
    :class:`ProxmoxConn`, not the author URI.
    """
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "proxmox":
        raise DriverError(f"expected a proxmox:// connection URI, got {uri!r}")
    if not parsed.hostname:
        raise DriverError(f"connection URI is missing a host: {uri!r}")
    return (
        parsed.hostname,
        parsed.port or 8006,
        urllib.parse.unquote(parsed.username or ""),
        urllib.parse.unquote(parsed.password or ""),
    )


def normalize_realm(user: str) -> str:
    """Give a bare PVE username the default ``pam`` realm; keep an explicit one.

    PVE authenticates against a realm, so ``user="root"`` must become
    ``root@pam``; an already-qualified ``root@pam`` / ``user@pve`` / ``user@ldap``
    is preserved. Sole caller is :meth:`ProxmoxProfile.build_driver`.
    """
    return user if "@" in user else f"{user}@pam"


@dataclass(frozen=True)
class ProxmoxConn:
    """Everything needed to reach a PVE node over REST + SSH.

    ``node`` may be ``""`` — :meth:`ProxmoxClient.connect` then auto-detects the
    single node on the host. ``backing_storage`` is not strictly "connection"
    but must survive into the teardown URI (``compose_volume_ref`` purity needs
    it), so it rides along. The SDN zone does *not*: it is minted per-run on the
    driver and self-discovered at teardown.
    """

    host: str
    node: str = ""  # "" => auto-detect the single node at connect()
    user: str = "root@pam"
    password: str = ""
    verify_ssl: bool = False
    port: int = 8006
    backing_storage: str = "local"
    ssh_user: str = "root"
    ssh_password: str = ""
    ssh_port: int = 22

    def to_uri(self) -> str:
        """Round-trip to the URI persisted in state.json (cleanup entry point).

        Credentials are embedded (lab posture, per the approved plan); a
        production backend would carry only non-secret routing here. ``node``
        may be empty (auto-detected) — cleanup re-resolves it the same way.
        """
        userinfo = urllib.parse.quote(self.user, safe="")
        if self.password:
            userinfo += ":" + urllib.parse.quote(self.password, safe="")
        query = urllib.parse.urlencode(
            {
                "storage": self.backing_storage,
                "verify": "1" if self.verify_ssl else "0",
                "ssh_user": self.ssh_user,
                "ssh_port": str(self.ssh_port),
                **({"ssh_password": self.ssh_password} if self.ssh_password else {}),
            }
        )
        return f"proxmox://{userinfo}@{self.host}:{self.port}/{self.node}?{query}"

    @classmethod
    def from_uri(cls, uri: str) -> ProxmoxConn:
        host, port, user, password = parse_connection(uri)
        parsed = urllib.parse.urlparse(uri)
        q = urllib.parse.parse_qs(parsed.query)
        return cls(
            host=host,
            node=parsed.path.strip("/"),  # "" => auto-detect
            user=user or "root@pam",
            password=password,
            verify_ssl=q.get("verify", ["0"])[0] == "1",
            port=port,
            backing_storage=q.get("storage", ["local"])[0],
            ssh_user=q.get("ssh_user", ["root"])[0],
            ssh_password=q.get("ssh_password", [""])[0],
            ssh_port=int(q.get("ssh_port", ["22"])[0]),
        )


class ProxmoxClient:
    """A live PVE connection: proxmoxer REST handle + lazy SSH/SFTP channel.

    The driver builds one of these in ``connect()``; unit tests inject a
    duck-typed stand-in (anything exposing ``api``, ``node``, ``wait_task``,
    ``sftp_get``) so no real network is touched.
    """

    def __init__(self, conn: ProxmoxConn) -> None:
        self._conn = conn
        self._api: Any | None = None
        self._ssh: Any | None = None
        # Resolved at connect(): the configured node, or the sole node on the
        # host when conn.node is "" (auto-detect).
        self._node: str | None = conn.node or None
        # Serializes mutation of this connection's shared in-memory state when the
        # I/O phases drive it from several worker threads (ADR-0023): QGA agent
        # REST calls (so concurrent readiness polls don't race the session's
        # cookie/CSRF state during a ticket refresh) and the lazy SSH-client
        # connect (``_ensure_ssh``). Held per-op (a call, or the one-time SSH
        # connect) — never across the exec poll loop's sleep, so the polls
        # overlap. Re-entrant for symmetry with the libvirt client; current
        # callers acquire per-op without nesting.
        self.call_lock = threading.RLock()

    @property
    def node(self) -> str:
        if self._node is None:
            raise DriverError("ProxmoxClient.node read before connect() resolved it")
        return self._node

    @property
    def storage(self) -> str:
        return self._conn.backing_storage

    @property
    def api(self) -> Any:
        if self._api is None:
            raise DriverError("ProxmoxClient used before connect()")
        return self._api

    def connect(self) -> None:
        proxmoxer = _import_proxmoxer()
        self._api = proxmoxer.ProxmoxAPI(
            self._conn.host,
            user=self._conn.user,
            password=self._conn.password,
            port=self._conn.port,
            verify_ssl=self._conn.verify_ssl,
            service="PVE",
            # proxmoxer defaults to a 5s per-request timeout — fine for snappy
            # control-plane calls, but a slow node can take longer to answer a
            # task-spawning request (clone/import/resize) on a busy host. Raise
            # the session ceiling; ordinary calls still return in milliseconds.
            # (Volume bytes no longer ride this session at all — they go over
            # SFTP, PVE-23.)
            timeout=_HTTP_TIMEOUT_S,
        )
        self._node = self._resolve_node()
        self._widen_session_pool()
        # Cheap authenticated read so bad creds / unreachable host fail loud here
        # rather than on the first real operation.
        self._api.nodes(self._node).status.get()
        _log.info("connected to PVE %s node %s", self._conn.host, self._node)

    def _widen_session_pool(self) -> None:
        """Resize the proxmoxer session's connection pool for ADR-0023 concurrency.

        proxmoxer stores its live ``requests.Session`` at ``_api._store["session"]``
        (verified against proxmoxer 2.3.0); mount an ``HTTPAdapter`` sized to
        :data:`_REST_POOL_MAXSIZE` so concurrent control-plane calls reuse pooled
        connections instead of overflowing urllib3's 10-slot default. Defensive:
        a proxmoxer internal-layout change must not break ``connect`` — the pool
        size is an optimization, not a correctness requirement.
        """
        import requests.adapters

        try:
            session = self.api._store["session"]
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=_REST_POOL_MAXSIZE, pool_maxsize=_REST_POOL_MAXSIZE
            )
            session.mount("https://", adapter)
        except (AttributeError, KeyError, TypeError) as e:  # pragma: no cover - defensive
            _log.debug("could not widen proxmoxer session pool (continuing): %s", e)

    def _resolve_node(self) -> str:
        """The configured node, or the host's sole node when unspecified.

        "Test authors don't care where" — a single-node host (the common lab
        case) needs no ``node``. A multi-node cluster is ambiguous, so we fail
        loud asking for one rather than guessing.
        """
        if self._conn.node:
            return self._conn.node
        nodes = [n["node"] for n in self.api.nodes.get()]
        if len(nodes) == 1:
            return str(nodes[0])
        raise DriverError(
            f"no node specified and host {self._conn.host!r} has {len(nodes)} nodes "
            f"({sorted(nodes)}); set node= or proxmox://…/<node>"
        )

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception as e:  # pragma: no cover - best-effort teardown
                _log.warning("ssh close failed: %s", e)
            self._ssh = None
        # proxmoxer is stateless HTTP; nothing to close on the REST side.
        self._api = None

    def wait_task(self, upid: str, *, timeout: float = _TASK_TIMEOUT_S) -> None:
        """Block until a PVE task UPID finishes; raise on failure or timeout."""
        from proxmoxer.tools import Tasks

        status = Tasks.blocking_status(self.api, upid, timeout=int(timeout))
        if status is None:
            raise DriverError(f"PVE task {upid!r} did not finish within {timeout:.0f}s")
        exit_status = status.get("exitstatus")
        if exit_status != "OK":
            raise DriverError(f"PVE task {upid!r} failed: exitstatus={exit_status!r}")

    def _ensure_ssh(self) -> Any:
        # Double-checked under call_lock: a parallel build (ADR-0023) runs
        # download_from_pool concurrently across build VMs, so two workers could
        # both see ``_ssh is None`` and open two connections, leaking one (close()
        # only closes a single handle). The lock makes the lazy connect once-only;
        # the per-transfer ``open_sftp()`` opens its own channel and needs no lock.
        if self._ssh is not None:
            return self._ssh
        with self.call_lock:
            if self._ssh is not None:
                return self._ssh
            paramiko = _import_paramiko()
            client = paramiko.SSHClient()
            # The PVE host is a fixed, operator-owned box (not an ephemeral guest),
            # but we still can't assume a known_hosts entry on the runner; trust on
            # first use. Acceptable for the lab posture; document and move on.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self._conn.host,
                port=self._conn.ssh_port,
                username=self._conn.ssh_user,
                password=self._conn.ssh_password or None,
                look_for_keys=False,
                allow_agent=False,
                timeout=15.0,
            )
            self._ssh = client
            return client

    def sftp_get(self, remote_path: str, dest_path: Path) -> None:
        """SFTP a file off the PVE host into a local path (overwrites).

        paramiko reports ``(transferred, total)`` per chunk; we feed that to a
        :class:`ProgressReporter` so a large disk capture shows progress instead
        of going silent (PVE-15).
        """
        client = self._ensure_ssh()
        sftp = client.open_sftp()
        reporter: ProgressReporter | None = None

        def _cb(done: int, total: int) -> None:
            nonlocal reporter
            if reporter is None:
                reporter = ProgressReporter(total, f"download {dest_path.name}", log=_log)
            reporter.update(done)

        try:
            sftp.get(remote_path, str(dest_path), callback=_cb)
        finally:
            # finish() in the finally so a mid-stream sftp.get failure (after the
            # first progress callback created the reporter) still releases the rich
            # Live instead of leaving the terminal in live mode (CORE-94).
            if reporter is not None:
                reporter.finish()
            sftp.close()

    def sftp_put(self, source_path: Path, remote_path: str) -> None:
        """SFTP a local file onto the PVE host (creates parent dirs, overwrites).

        Volume bytes go *up* the same SSH channel they come *down*: PVE's REST
        ``upload`` endpoint 501s on large ``import`` disk images, so we write the
        file straight into the storage's content dir, where ``dir``/``nfs``
        storage discovers it by scan (PVE-23, ADR-0008 §6). Reports progress and
        re-raises a transport failure as a :class:`DriverError` naming the
        observed average rate (the slow/degraded-host signal from PVE-15).
        """
        total = source_path.stat().st_size
        reporter = ProgressReporter(total, f"upload {Path(remote_path).name}", log=_log)
        client = self._ensure_ssh()
        sftp = client.open_sftp()
        try:
            _sftp_makedirs(sftp, str(PurePosixPath(remote_path).parent))
            # paramiko's put callback is (transferred, total); we already know
            # total, so drop it and feed the running count to the reporter.
            sftp.put(
                str(source_path),
                remote_path,
                callback=lambda done, _total: reporter.update(done),
            )
        except Exception as e:
            raise DriverError(
                f"upload of {Path(remote_path).name} ({total / (1024 * 1024):.0f} MiB) to PVE "
                f"failed after {reporter.elapsed():.0f}s ({reporter.avg_rate_mib():.2f} MiB/s "
                f"avg); the host or uplink may be degraded: {e}"
            ) from e
        finally:
            reporter.finish()
            sftp.close()

    def storage_path(self) -> str:
        """Absolute on-host path of the backing storage (e.g. ``/var/lib/vz``).

        Needed to turn a volid into a filesystem path for the SFTP transfers
        (the REST API exposes no byte-level read/write for volumes).
        """
        path = self.api.storage(self._conn.backing_storage).get().get("path")
        if not path:
            raise DriverError(
                f"PVE storage {self._conn.backing_storage!r} has no on-host path "
                "(only 'dir'/'nfs'-style storages are supported)"
            )
        return str(path)

    def open_serial_websocket(self, vmid: int) -> Any:
        """Open a vm's ``serial0`` console as a connected, authenticated websocket.

        This is the **second sanctioned non-proxmoxer transport** (after the
        SFTP download). PVE exposes serial output only via the web UI's
        two-step — there is no REST GET (RESEARCH.md "PVE-16 spike"):

        1. ``POST …/qemu/{vmid}/termproxy`` (proxmoxer) → one-shot
           ``{port, ticket (the vncticket), user}``;
        2. a websocket to ``…/vncwebsocket?port=&vncticket=`` carrying the raw
           PTY byte stream — which proxmoxer can't speak, hence
           ``websocket-client``.

        termproxy requires **password-ticket** auth (it rejects API tokens), so
        the session ticket proxmoxer holds is reused as the ``PVEAuthCookie``
        cookie. After connect we send the ``"{user}:{vncticket}\\n"`` auth frame
        and expect ``OK``. Returns a websocket positioned at the start of the
        serial stream; the caller reads frames and must close it.
        """
        websocket = _import_websocket()
        ticket, _csrf = self.api.get_tokens()
        if not ticket:
            raise DriverError(
                "PVE serial console needs password-ticket auth (termproxy rejects API "
                "tokens); set user+password on the ProxmoxHypervisor"
            )
        node = self.node  # the *resolved* node (conn.node may be "" under auto-detect)
        resp = self.api.nodes(node).qemu(vmid).termproxy.post()
        # PVE session tickets live ~2h; a long build can cross that, and the
        # PVEAuthCookie below must be valid *at connect time*. The termproxy POST
        # above is an authenticated request, so proxmoxer transparently
        # re-authenticates if the ticket had expired — re-read it here to pick up
        # the refreshed cookie rather than send the stale one (PVE-41).
        ticket, _csrf = self.api.get_tokens()
        query = urllib.parse.urlencode({"port": resp["port"], "vncticket": resp["ticket"]})
        url = (
            f"wss://{self._conn.host}:{self._conn.port}"
            f"/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket?{query}"
        )
        # Origin must match the API base (PVE checks it); TLS verification off
        # mirrors the proxmoxer session's lab posture.
        sslopt = None if self._conn.verify_ssl else {"cert_reqs": ssl.CERT_NONE}
        ws = websocket.create_connection(
            url,
            header=[f"Cookie: PVEAuthCookie={ticket}"],
            origin=f"https://{self._conn.host}:{self._conn.port}",
            sslopt=sslopt,
            timeout=_WS_CONNECT_TIMEOUT_S,
        )
        try:
            ws.send(f"{resp['user']}:{resp['ticket']}\n")
            reply = ws.recv()
            reply_bytes = reply.encode() if isinstance(reply, str) else reply
            if not reply_bytes.startswith(b"OK"):
                raise DriverError(
                    f"PVE termproxy auth rejected for vmid {vmid} (reply {reply_bytes!r})"
                )
        except Exception:
            ws.close()
            raise
        return ws
