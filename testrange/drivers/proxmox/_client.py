"""Connection plumbing for the Proxmox driver.

Two transports live here because PVE needs both:

- the **REST API** (proxmoxer ``https`` backend) for the control plane, and
- a **paramiko SSH/SFTP** channel for the one thing the REST API cannot do —
  stream a volume's bytes back to the orchestrator host (``download_from_pool``).

``ProxmoxConn`` is the connection config (round-trips through the teardown URI);
``ProxmoxClient`` wraps a live ``ProxmoxAPI`` plus the lazy SSH channel and the
UPID task-poller. The driver holds exactly one ``ProxmoxClient``; the concern
modules (`_sdn`, `_storage`, `_vm`, `_guest`) take it as their first argument.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testrange._log import get_logger
from testrange.exceptions import DriverError

_log = get_logger(__name__)

# PVE tasks are quick (create/clone/snapshot) but disk import can run minutes on
# a multi-GB image. Size the default generously; callers pass a tighter value
# where they want one.
_TASK_TIMEOUT_S = 600.0


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


@dataclass(frozen=True)
class ProxmoxConn:
    """Everything needed to reach a PVE node over REST + SSH.

    ``backing_storage`` and ``sdn_zone`` are not strictly "connection" but they
    must survive into the teardown URI (``compose_volume_ref`` purity and SDN
    vnet cleanup both need them), so they ride along here.
    """

    host: str
    node: str
    user: str = "root@pam"
    password: str = ""
    verify_ssl: bool = False
    port: int = 8006
    backing_storage: str = "local"
    sdn_zone: str = "trzone"
    ssh_user: str = "root"
    ssh_password: str = ""
    ssh_port: int = 22

    def to_uri(self) -> str:
        """Round-trip to the URI persisted in state.json (cleanup entry point).

        Credentials are embedded (lab posture, per the approved plan); a
        production backend would carry only non-secret routing here.
        """
        userinfo = urllib.parse.quote(self.user, safe="")
        if self.password:
            userinfo += ":" + urllib.parse.quote(self.password, safe="")
        query = urllib.parse.urlencode(
            {
                "storage": self.backing_storage,
                "zone": self.sdn_zone,
                "verify": "1" if self.verify_ssl else "0",
                "ssh_user": self.ssh_user,
                "ssh_port": str(self.ssh_port),
                **({"ssh_password": self.ssh_password} if self.ssh_password else {}),
            }
        )
        return f"pve://{userinfo}@{self.host}:{self.port}/{self.node}?{query}"

    @classmethod
    def from_uri(cls, uri: str) -> ProxmoxConn:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "pve":
            raise DriverError(f"ProxmoxConn.from_uri: expected pve:// scheme, got {uri!r}")
        if not parsed.hostname or not parsed.path.strip("/"):
            raise DriverError(f"ProxmoxConn.from_uri: missing host or node in {uri!r}")
        q = urllib.parse.parse_qs(parsed.query)
        return cls(
            host=parsed.hostname,
            node=parsed.path.strip("/"),
            user=urllib.parse.unquote(parsed.username or "root@pam"),
            password=urllib.parse.unquote(parsed.password or ""),
            verify_ssl=q.get("verify", ["0"])[0] == "1",
            port=parsed.port or 8006,
            backing_storage=q.get("storage", ["local"])[0],
            sdn_zone=q.get("zone", ["trzone"])[0],
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

    @property
    def node(self) -> str:
        return self._conn.node

    @property
    def storage(self) -> str:
        return self._conn.backing_storage

    @property
    def zone(self) -> str:
        return self._conn.sdn_zone

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
        )
        # Cheap authenticated read so bad creds / unreachable host fail loud here
        # rather than on the first real operation.
        self._api.nodes(self._conn.node).status.get()
        _log.info("connected to PVE %s node %s", self._conn.host, self._conn.node)

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

    # -- SSH/SFTP (download_from_pool only) --------------------------------

    def _ensure_ssh(self) -> Any:
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
        """SFTP a file off the PVE host into a local path (overwrites)."""
        client = self._ensure_ssh()
        sftp = client.open_sftp()
        try:
            sftp.get(remote_path, str(dest_path))
        finally:
            sftp.close()
