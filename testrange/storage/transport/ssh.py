"""SSH file transport — SFTP + SSH exec to a remote host.

Used by any orchestrator whose hypervisor is reachable over SSH: disk
images get uploaded over SFTP and tools are executed over SSH on the
remote side.  The remote hypervisor sees files on its own filesystem
at paths this transport manages under ``<cache_root>``.

Authentication follows paramiko's default discovery — ``~/.ssh/config``,
``ssh-agent``, default key files.  Callers who need non-default auth
pass explicit kwargs at construction.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import paramiko

from testrange.exceptions import CacheError
from testrange.storage.transport.base import AbstractFileTransport

_DEFAULT_REMOTE_CACHE = "/var/tmp/testrange/{user}"
"""Default remote cache root.  ``{user}`` substituted with the SSH login."""


class SSHFileTransport(AbstractFileTransport):
    """File + exec primitives over an SSH connection.

    :param host: Remote hostname or IP.
    :param username: SSH username.  Defaults to ``$USER``.
    :param port: SSH port.  Defaults to 22.
    :param key_filename: Explicit private-key path.  When ``None``,
        paramiko walks standard locations and tries ssh-agent.
    :param cache_root: Remote cache root.  Defaults to
        ``/var/tmp/testrange/<ssh_user>``.
    :param connect_timeout: Seconds to wait for the TCP handshake.
    """

    _client: paramiko.SSHClient | None
    _sftp: paramiko.SFTPClient | None
    _host: str
    _user: str
    _port: int
    _key_filename: str | None
    _connect_timeout: float
    _cache_root: str

    def __init__(
        self,
        host: str,
        username: str | None = None,
        port: int = 22,
        key_filename: str | None = None,
        cache_root: str | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._user = username or os.environ.get("USER") or "root"
        self._port = port
        self._key_filename = key_filename
        self._connect_timeout = connect_timeout
        self._cache_root = cache_root or _DEFAULT_REMOTE_CACHE.format(
            user=self._user
        )
        self._client = None
        self._sftp = None

    # ------------------------------------------------------------------
    # Connection lifecycle — lazy connect, explicit close.
    # ------------------------------------------------------------------

    def _connect(self) -> paramiko.SSHClient:
        if self._client is not None:
            return self._client
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self._host,
                port=self._port,
                username=self._user,
                key_filename=self._key_filename,
                timeout=self._connect_timeout,
                allow_agent=True,
                look_for_keys=True,
            )
        except (paramiko.SSHException, OSError) as exc:
            raise CacheError(
                f"SSH connect to {self._user}@{self._host}:{self._port} "
                f"failed: {exc}"
            ) from exc
        self._client = client
        return client

    def _get_sftp(self) -> paramiko.SFTPClient:
        if self._sftp is not None:
            return self._sftp
        self._sftp = self._connect().open_sftp()
        return self._sftp

    def close(self) -> None:
        """Close the SFTP and SSH connections.  Idempotent, never raises."""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Internal exec helpers — used by run_tool + cache_root ops.
    # ------------------------------------------------------------------

    def _exec(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        """Run *argv* remotely via ``exec_command`` and return
        ``(exit_code, stdout_bytes, stderr_bytes)``.

        Args are joined with :func:`shlex.join` so user-supplied paths
        with spaces survive.
        """
        client = self._connect()
        cmd = shlex.join(argv)
        _, out, err = client.exec_command(cmd)
        exit_code = out.channel.recv_exit_status()
        return exit_code, out.read(), err.read()

    def _exec_check(self, argv: list[str]) -> str:
        """``_exec`` + raise :class:`CacheError` on non-zero exit.
        Returns stdout as a decoded string for convenience."""
        code, stdout, stderr = self._exec(argv)
        if code != 0:
            raise CacheError(
                f"remote command {argv[0]!r} failed "
                f"(exit {code}): "
                f"{stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()}"
            )
        return stdout.decode(errors="replace")

    # ------------------------------------------------------------------
    # Cache root + per-run scratch
    # ------------------------------------------------------------------

    @property
    def cache_root(self) -> str:
        return self._cache_root

    def make_run_dir(self, run_id: str) -> str:
        run_path = self.run_dir(run_id)
        self._exec_check(["mkdir", "-p", run_path])
        self._exec_check(["chmod", "0755", run_path])
        return run_path

    def cleanup_run(self, run_id: str) -> None:
        run_path = self.run_dir(run_id)
        # ``rm -rf`` is fine: run dirs are fully owned by us and always
        # under our cache root, never a user path.  Silence errors so
        # teardown stays exception-free.
        try:
            self._exec(["rm", "-rf", run_path])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # File primitives
    # ------------------------------------------------------------------

    def exists(self, ref: str) -> bool:
        sftp = self._get_sftp()
        try:
            sftp.stat(ref)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def size(self, ref: str) -> int:
        sftp = self._get_sftp()
        attrs = sftp.stat(ref)
        return int(attrs.st_size or 0)

    def write_bytes(self, ref: str, data: bytes, mode: int = 0o644) -> None:
        self._ensure_parent(ref)
        sftp = self._get_sftp()
        with sftp.file(ref, "wb") as fh:
            fh.write(data)
        sftp.chmod(ref, mode)

    def read_bytes(self, ref: str) -> bytes:
        sftp = self._get_sftp()
        with sftp.file(ref, "rb") as fh:
            return fh.read()

    def remove(self, ref: str) -> None:
        sftp = self._get_sftp()
        try:
            sftp.remove(ref)
        except (OSError, FileNotFoundError):
            pass

    def rename(self, src_ref: str, dst_ref: str) -> None:
        # ``posix_rename`` is the SFTP v3 extension that mirrors
        # POSIX ``rename(2)`` — atomic, replaces the destination.
        # Plain ``sftp.rename`` fails if the dest exists, which would
        # turn a retry-after-crash into a second bug.
        sftp = self._get_sftp()
        sftp.posix_rename(src_ref, dst_ref)

    def makedirs(self, ref: str, mode: int = 0o755) -> None:
        # paramiko's SFTPClient has no ``makedirs``; fall back to the
        # remote shell where ``mkdir -p`` is a one-liner.
        self._exec_check(["mkdir", "-p", ref])
        try:
            self._exec_check(["chmod", oct(mode)[2:], ref])
        except CacheError:
            pass

    def _ensure_parent(self, ref: str) -> None:
        """``mkdir -p`` on *ref*'s parent before a write."""
        parent = ref.rsplit("/", 1)[0] if "/" in ref else ""
        if parent:
            self._exec_check(["mkdir", "-p", parent])

    # ------------------------------------------------------------------
    # Bulk transfer — SFTP put / get with streaming.
    # ------------------------------------------------------------------

    def upload(self, local_path: Path, ref: str) -> None:
        self._ensure_parent(ref)
        sftp = self._get_sftp()
        sftp.put(str(local_path), ref)
        try:
            sftp.chmod(ref, 0o644)
        except OSError:
            pass

    def download(self, ref: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        sftp = self._get_sftp()
        sftp.get(ref, str(local_path))

    # ------------------------------------------------------------------
    # Tool execution — runs on the remote host.
    # ------------------------------------------------------------------

    def run_tool(
        self,
        argv: list[str],
        timeout: float = 60.0,
    ) -> tuple[int, bytes, bytes]:
        # paramiko's exec_command has its own per-channel timeout
        # semantics that differ from subprocess; for v1 we let the
        # caller's timeout guard via channel settings.  Parity with
        # LocalFileTransport is close enough for tool execution.
        client = self._connect()
        cmd = shlex.join(argv)
        _, out, err = client.exec_command(cmd, timeout=timeout)
        exit_code = out.channel.recv_exit_status()
        return exit_code, out.read(), err.read()
