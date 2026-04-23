"""Unit tests for :mod:`testrange.storage`.

The storage layer is now a two-axis composition:

- ``transport`` — file + subprocess primitives against some filesystem
  (local, SSH, …).
- ``disk`` — disk-format operations (qcow2, VHDX-in-the-future, …)
  routed through a transport's ``run_tool``.

These tests exercise both axes independently, then the convenience
pairings (:class:`LocalStorageBackend` / :class:`SSHStorageBackend`)
and the orchestrator's URI → backend auto-selection.  SSH tests mock
paramiko — end-to-end integration against a real SSH server belongs
in the manual-QA examples, not the unit suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from testrange.exceptions import CacheError
from testrange.storage import (
    AbstractFileTransport,
    LocalFileTransport,
    LocalStorageBackend,
    Qcow2DiskFormat,
    SSHFileTransport,
    SSHStorageBackend,
    StorageBackend,
)

# ===========================================================================
# LocalFileTransport — transport axis
# ===========================================================================


class TestLocalFileTransportPrimitives:
    """File + path ops on the outer host's filesystem."""

    def test_cache_root_is_resolved(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        assert t.cache_root == str(tmp_path.resolve())

    def test_images_and_vms_dir_paths(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        assert t.images_dir() == f"{tmp_path.resolve()}/images"
        assert t.vms_dir() == f"{tmp_path.resolve()}/vms"

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        target = f"{t.cache_root}/subdir/hello.bin"
        t.write_bytes(target, b"payload", mode=0o600)
        assert t.exists(target)
        assert t.read_bytes(target) == b"payload"
        assert Path(target).parent.is_dir()

    def test_size_matches_disk(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        target = f"{t.cache_root}/s.bin"
        t.write_bytes(target, b"x" * 1024)
        assert t.size(target) == 1024

    def test_remove_is_idempotent_on_missing(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        t.remove(f"{t.cache_root}/never-existed.bin")  # must not raise

    def test_makedirs_sets_mode(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        target = f"{t.cache_root}/deep/nested/dir"
        t.makedirs(target, mode=0o700)
        assert Path(target).is_dir()

    def test_upload_and_download_round_trip(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        ref = f"{t.cache_root}/dest/sub/cp.bin"
        t.upload(src, ref)
        assert Path(ref).read_bytes() == b"contents"

        out = tmp_path / "out.bin"
        t.download(ref, out)
        assert out.read_bytes() == b"contents"


class TestLocalFileTransportRuns:
    """Per-run scratch-dir lifecycle."""

    def test_make_and_cleanup_run(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        ref = t.make_run_dir("run-42")
        assert Path(ref).is_dir()
        assert ref == f"{t.cache_root}/runs/run-42"
        # World-readable so the hypervisor process can open disks inside.
        assert Path(ref).stat().st_mode & 0o005 == 0o005
        t.cleanup_run("run-42")
        assert not Path(ref).exists()

    def test_cleanup_run_missing_is_no_op(self, tmp_path: Path) -> None:
        LocalFileTransport(tmp_path).cleanup_run("never-existed")

    def test_make_run_dir_idempotent(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        assert t.make_run_dir("run-x") == t.make_run_dir("run-x")


class TestLocalFileTransportRunTool:
    """``run_tool`` is the execution primitive disk formats build on."""

    def test_run_tool_returns_exit_stdout_stderr(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        code, out, err = t.run_tool(["sh", "-c", "echo hello; echo err >&2"])
        assert code == 0
        assert b"hello" in out
        assert b"err" in err

    def test_run_tool_nonzero_exit_is_returned_not_raised(
        self, tmp_path: Path
    ) -> None:
        t = LocalFileTransport(tmp_path)
        code, _, _ = t.run_tool(["sh", "-c", "exit 3"])
        assert code == 3

    def test_run_tool_missing_binary_wraps_in_cache_error(
        self, tmp_path: Path
    ) -> None:
        t = LocalFileTransport(tmp_path)
        with pytest.raises(CacheError, match="not installed"):
            t.run_tool(["this-binary-does-not-exist-xyzzy"])


# ===========================================================================
# Qcow2DiskFormat — disk-format axis
# ===========================================================================


class _FakeTransport:
    """Minimal AbstractFileTransport stand-in: records run_tool calls,
    returns whatever was queued."""

    def __init__(self, exit_code: int = 0, stderr: bytes = b"") -> None:
        self.calls: list[list[str]] = []
        self.exit_code = exit_code
        self.stderr = stderr

    def run_tool(self, argv, timeout=60.0):
        self.calls.append(list(argv))
        return self.exit_code, b"", self.stderr


class TestQcow2DiskFormat:
    """Disk-format ops must route through the transport's run_tool with
    the canonical ``qemu-img`` argv.  Drift here silently regresses
    every backend built on Qcow2DiskFormat."""

    def test_create_overlay_invokes_qemu_img_create(self) -> None:
        t = _FakeTransport()
        Qcow2DiskFormat(t).create_overlay("/t/base.qcow2", "/t/ov.qcow2")
        assert t.calls == [[
            "qemu-img", "create",
            "-f", "qcow2",
            "-b", "/t/base.qcow2",
            "-F", "qcow2",
            "/t/ov.qcow2",
        ]]

    def test_create_blank_sends_expected_argv(self) -> None:
        t = _FakeTransport()
        Qcow2DiskFormat(t).create_blank("/t/blank.qcow2", "40G")
        assert t.calls == [
            ["qemu-img", "create", "-f", "qcow2", "/t/blank.qcow2", "40G"]
        ]

    def test_resize_sends_expected_argv(self) -> None:
        t = _FakeTransport()
        Qcow2DiskFormat(t).resize("/t/d.qcow2", "64G")
        assert t.calls == [["qemu-img", "resize", "/t/d.qcow2", "64G"]]

    def test_compress_sends_expected_argv(self) -> None:
        t = _FakeTransport()
        Qcow2DiskFormat(t).compress("/t/s.qcow2", "/t/d.qcow2")
        assert t.calls == [[
            "qemu-img", "convert",
            "-f", "qcow2",
            "-O", "qcow2",
            "-c",
            "/t/s.qcow2",
            "/t/d.qcow2",
        ]]

    def test_nonzero_exit_wrapped_as_cache_error(self) -> None:
        """The disk format layer translates tool-exit failures into
        :class:`CacheError` with stderr context — callers should never
        see a raw nonzero exit surfaced as a plain tuple."""
        t = _FakeTransport(exit_code=1, stderr=b"disk full")
        with pytest.raises(CacheError, match="disk full"):
            Qcow2DiskFormat(t).resize("/t/d.qcow2", "1P")


# ===========================================================================
# StorageBackend composition + convenience pairings
# ===========================================================================


class TestStorageBackendComposition:
    """The decomposition is the whole point — callers should be able
    to see, construct, and swap the two axes independently."""

    def test_manual_composition(self, tmp_path: Path) -> None:
        t = LocalFileTransport(tmp_path)
        b = StorageBackend(transport=t, disk=Qcow2DiskFormat(t))
        assert b.transport is t
        assert isinstance(b.disk, Qcow2DiskFormat)

    def test_local_convenience_preloads_qcow2(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        assert isinstance(b.transport, LocalFileTransport)
        assert isinstance(b.disk, Qcow2DiskFormat)
        # The disk format shares the same transport instance — no
        # accidental doubling.
        assert b.disk._transport is b.transport

    def test_ssh_convenience_preloads_qcow2(self) -> None:
        b = SSHStorageBackend(host="x", username="y")
        assert isinstance(b.transport, SSHFileTransport)
        assert isinstance(b.disk, Qcow2DiskFormat)
        assert b.disk._transport is b.transport

    def test_close_delegates_to_transport(self) -> None:
        """Calling close() on a backend tears down the underlying
        transport (SSH connection, etc.).  Local transport has no
        close; the backend's close must still be safe to call."""
        transport = MagicMock(spec=AbstractFileTransport)
        transport.close = MagicMock()
        b = StorageBackend(transport=transport, disk=MagicMock())
        b.close()
        transport.close.assert_called_once()

    def test_close_is_safe_when_transport_has_none(self) -> None:
        b = LocalStorageBackend(Path("/tmp"))
        b.close()  # LocalFileTransport has no close — must not raise


# ===========================================================================
# SSHFileTransport — transport axis, remote
# ===========================================================================


class TestSSHFileTransportConstruction:
    def test_defaults_cache_root_to_user_subdir(self) -> None:
        t = SSHFileTransport(host="kvm.example.com", username="alice")
        assert t.cache_root == "/var/tmp/testrange/alice"

    def test_explicit_cache_root_wins(self) -> None:
        t = SSHFileTransport(host="x", username="y", cache_root="/opt/tr")
        assert t.cache_root == "/opt/tr"

    def test_username_defaults_to_env_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("USER", "cfg")
        assert SSHFileTransport(host="x")._user == "cfg"

    def test_cache_root_interpolates_user_token(self) -> None:
        t = SSHFileTransport(host="x", username="impersonated")
        assert t.cache_root == "/var/tmp/testrange/impersonated"


class TestSSHFileTransportConnectFailure:
    """Connection errors surface as :class:`CacheError` — never a raw
    paramiko exception.  Prevents ``with Orchestrator(host=…)`` from
    dumping a paramiko stacktrace at the user."""

    def test_connect_error_wrapped_in_cache_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import paramiko
        client = MagicMock()
        client.connect.side_effect = paramiko.SSHException("bad creds")
        monkeypatch.setattr(
            "testrange.storage.transport.ssh.paramiko.SSHClient",
            lambda: client,
        )
        with pytest.raises(CacheError, match="SSH connect"):
            SSHFileTransport(host="x", username="y")._connect()

    def test_oserror_wrapped_in_cache_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.connect.side_effect = OSError("network unreachable")
        monkeypatch.setattr(
            "testrange.storage.transport.ssh.paramiko.SSHClient",
            lambda: client,
        )
        with pytest.raises(CacheError, match="SSH connect"):
            SSHFileTransport(host="x", username="y")._connect()


class TestSSHFileTransportExec:
    """``_exec`` returns (code, stdout, stderr) as bytes; ``_exec_check``
    raises on non-zero exit."""

    def _mocked(self) -> tuple[SSHFileTransport, MagicMock]:
        client = MagicMock()
        t = SSHFileTransport(host="x", username="y")
        t._client = client
        return t, client

    def test_exec_returns_exit_code_and_byte_streams(self) -> None:
        t, client = self._mocked()
        out, err = MagicMock(), MagicMock()
        out.channel.recv_exit_status.return_value = 0
        out.read.return_value = b"hello\n"
        err.read.return_value = b""
        client.exec_command.return_value = (MagicMock(), out, err)

        code, stdout, stderr = t._exec(["echo", "hello"])
        assert code == 0
        assert stdout == b"hello\n"
        assert stderr == b""
        client.exec_command.assert_called_once_with("echo hello")

    def test_exec_check_raises_on_nonzero(self) -> None:
        t, client = self._mocked()
        out, err = MagicMock(), MagicMock()
        out.channel.recv_exit_status.return_value = 1
        out.read.return_value = b""
        err.read.return_value = b"permission denied"
        client.exec_command.return_value = (MagicMock(), out, err)

        with pytest.raises(CacheError, match="permission denied"):
            t._exec_check(["false"])


class TestSSHFileTransportRunTool:
    """``run_tool`` is the plain exec primitive that disk formats use.
    Unlike ``_exec_check`` it does NOT raise on non-zero — the disk
    format layer decides how to handle failures."""

    def test_run_tool_returns_exit_and_streams(self) -> None:
        client = MagicMock()
        t = SSHFileTransport(host="x", username="y")
        t._client = client
        out, err = MagicMock(), MagicMock()
        out.channel.recv_exit_status.return_value = 0
        out.read.return_value = b"ok"
        err.read.return_value = b""
        client.exec_command.return_value = (MagicMock(), out, err)

        code, stdout, stderr = t.run_tool(["qemu-img", "info", "/x"])
        assert code == 0
        assert stdout == b"ok"
        client.exec_command.assert_called_once_with(
            "qemu-img info /x", timeout=60.0,
        )


class TestSSHFileTransportFileOps:
    """SFTP primitives route through paramiko.SFTPClient."""

    def _mocked(self) -> tuple[SSHFileTransport, MagicMock]:
        sftp = MagicMock()
        t = SSHFileTransport(host="x", username="y")
        t._sftp = sftp
        t._client = MagicMock()  # prevent lazy connect
        return t, sftp

    def test_write_bytes_parents_and_chmod(self) -> None:
        t, sftp = self._mocked()
        fh = MagicMock()
        fh.__enter__.return_value = fh
        sftp.file.return_value = fh
        with patch.object(t, "_ensure_parent") as ep:
            t.write_bytes("/remote/x.bin", b"data", mode=0o600)
        ep.assert_called_once_with("/remote/x.bin")
        fh.write.assert_called_once_with(b"data")
        sftp.chmod.assert_called_once_with("/remote/x.bin", 0o600)

    def test_exists_true_on_stat_ok(self) -> None:
        t, sftp = self._mocked()
        sftp.stat.return_value = MagicMock()
        assert t.exists("/any") is True

    def test_exists_false_on_missing(self) -> None:
        t, sftp = self._mocked()
        sftp.stat.side_effect = FileNotFoundError
        assert t.exists("/any") is False

    def test_remove_swallows_missing(self) -> None:
        t, sftp = self._mocked()
        sftp.remove.side_effect = FileNotFoundError
        t.remove("/any")

    def test_upload_creates_parents(self) -> None:
        t, sftp = self._mocked()
        with patch.object(t, "_ensure_parent") as ep:
            t.upload(Path("/local/src.qcow2"), "/remote/dest.qcow2")
        ep.assert_called_once_with("/remote/dest.qcow2")
        sftp.put.assert_called_once_with("/local/src.qcow2", "/remote/dest.qcow2")


class TestSSHFileTransportClose:
    def test_close_closes_both_channels(self) -> None:
        sftp, client = MagicMock(), MagicMock()
        t = SSHFileTransport(host="x", username="y")
        t._sftp = sftp
        t._client = client
        t.close()
        sftp.close.assert_called_once()
        client.close.assert_called_once()
        assert t._sftp is None
        assert t._client is None

    def test_close_is_idempotent(self) -> None:
        SSHFileTransport(host="x", username="y").close()

    def test_close_swallows_underlying_errors(self) -> None:
        sftp = MagicMock()
        sftp.close.side_effect = RuntimeError("boom")
        client = MagicMock()
        client.close.side_effect = RuntimeError("boom")
        t = SSHFileTransport(host="x", username="y")
        t._sftp = sftp
        t._client = client
        t.close()  # must not raise


# ===========================================================================
# Orchestrator backend selection — end-to-end URI → backend dispatch
# ===========================================================================


class TestOrchestratorBackendSelection:
    """``Orchestrator(host=…)`` picks the right storage backend without
    extra kwargs, and respects an explicit ``storage_backend=`` override."""

    def test_localhost_picks_local_backend(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator
        orch = Orchestrator()  # host="localhost" default
        b = orch._select_storage_backend()
        assert isinstance(b, LocalStorageBackend)
        assert isinstance(b.transport, LocalFileTransport)

    def test_bare_hostname_picks_ssh_backend(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator
        orch = Orchestrator(host="kvm.example.com")
        b = orch._select_storage_backend()
        assert isinstance(b, SSHStorageBackend)
        assert isinstance(b.transport, SSHFileTransport)
        assert b.transport._host == "kvm.example.com"

    def test_qemu_ssh_uri_parses_user_host_port(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator
        orch = Orchestrator(host="qemu+ssh://alice@kvm.example.com:2222/system")
        b = orch._select_storage_backend()
        assert isinstance(b.transport, SSHFileTransport)
        assert b.transport._user == "alice"
        assert b.transport._host == "kvm.example.com"
        assert b.transport._port == 2222

    def test_qemu_ssh_uri_without_user_uses_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator
        monkeypatch.setenv("USER", "local")
        orch = Orchestrator(host="qemu+ssh://kvm.example.com/system")
        b = orch._select_storage_backend()
        assert b.transport._user == "local"

    def test_explicit_storage_backend_wins(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator
        sentinel = MagicMock(spec=StorageBackend)
        orch = Orchestrator(
            host="qemu+ssh://kvm.example.com/system",
            storage_backend=sentinel,
        )
        assert orch._select_storage_backend() is sentinel
