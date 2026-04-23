"""Unit tests for :mod:`testrange.storage`.

Covers the primitives each backend owes the orchestrator: file + bulk
ops round-trip cleanly, per-run scratch dir lifecycle works, and the
``qemu-img`` helpers delegate to the expected subprocess / SSH calls.

The SSH tests mock paramiko so we exercise the backend's *logic*
without needing a real SSH server.  End-to-end integration — booting a
VM against ``qemu+ssh://`` — is covered by the examples/manual QA, not
the unit suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from testrange.exceptions import CacheError
from testrange.storage import (
    AbstractStorageBackend,
    LocalStorageBackend,
    SSHStorageBackend,
)

# ---------------------------------------------------------------------------
# LocalStorageBackend
# ---------------------------------------------------------------------------


class TestLocalStorageBackendPrimitives:
    """The local backend is the baseline — every primitive must match
    the direct filesystem op it replaces.  Any drift here breaks every
    existing orchestrator test by regressing behaviour."""

    def test_cache_root_is_resolved(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        assert b.cache_root == str(tmp_path.resolve())

    def test_images_and_vms_dir_paths(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        assert b.images_dir() == f"{tmp_path.resolve()}/images"
        assert b.vms_dir() == f"{tmp_path.resolve()}/vms"

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        target = f"{b.cache_root}/subdir/hello.bin"
        b.write_bytes(target, b"payload", mode=0o600)
        assert b.exists(target)
        assert b.read_bytes(target) == b"payload"
        # Parents were created as part of write_bytes.
        assert Path(target).parent.is_dir()

    def test_size_matches_disk(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        target = f"{b.cache_root}/s.bin"
        b.write_bytes(target, b"x" * 1024)
        assert b.size(target) == 1024

    def test_remove_is_idempotent_on_missing(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        b.remove(f"{b.cache_root}/never-existed.bin")  # must not raise

    def test_makedirs_sets_mode(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        target = f"{b.cache_root}/deep/nested/dir"
        b.makedirs(target, mode=0o700)
        assert Path(target).is_dir()

    def test_upload_and_download_round_trip(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        ref = f"{b.cache_root}/dest/sub/cp.bin"
        b.upload(src, ref)
        assert Path(ref).read_bytes() == b"contents"

        out = tmp_path / "out.bin"
        b.download(ref, out)
        assert out.read_bytes() == b"contents"


class TestLocalStorageBackendRuns:
    """Per-run scratch-dir lifecycle."""

    def test_make_and_cleanup_run(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        ref = b.make_run_dir("run-42")
        assert Path(ref).is_dir()
        assert ref == f"{b.cache_root}/runs/run-42"

        # World-readable so the qemu daemon (libvirt-qemu user) can
        # open disk images placed inside the run dir.
        assert Path(ref).stat().st_mode & 0o005 == 0o005

        b.cleanup_run("run-42")
        assert not Path(ref).exists()

    def test_cleanup_run_missing_is_no_op(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        b.cleanup_run("never-existed")  # must not raise

    def test_make_run_dir_idempotent(self, tmp_path: Path) -> None:
        b = LocalStorageBackend(tmp_path)
        ref1 = b.make_run_dir("run-x")
        ref2 = b.make_run_dir("run-x")
        assert ref1 == ref2


class TestLocalStorageBackendQemuImg:
    """qemu-img calls must delegate to the existing typed wrapper — no
    new subprocess code inside the backend, just dispatch."""

    def test_create_overlay_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, str]] = []
        import testrange.storage.local as mod
        monkeypatch.setattr(
            mod, "_qemu_img_create_overlay",
            lambda backing, dest: calls.append((str(backing), str(dest))),
        )
        b = LocalStorageBackend(tmp_path)
        b.qemu_img_create_overlay(
            str(tmp_path / "base.qcow2"), str(tmp_path / "ov.qcow2"),
        )
        assert calls == [
            (str(tmp_path / "base.qcow2"), str(tmp_path / "ov.qcow2")),
        ]

    def test_qemu_img_resize_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[tuple[str, str]] = []
        import testrange.storage.local as mod
        monkeypatch.setattr(
            mod, "_qemu_img_resize",
            lambda path, size: seen.append((str(path), size)),
        )
        LocalStorageBackend(tmp_path).qemu_img_resize(
            str(tmp_path / "d.qcow2"), "64G",
        )
        assert seen == [(str(tmp_path / "d.qcow2"), "64G")]


# ---------------------------------------------------------------------------
# SSHStorageBackend
# ---------------------------------------------------------------------------


class TestSSHStorageBackendConstruction:
    """URI / kwarg parsing, default cache-root derivation.  No real
    SSH traffic here — paramiko is mocked."""

    def test_defaults_cache_root_to_user_subdir(self) -> None:
        b = SSHStorageBackend(host="kvm.example.com", username="alice")
        assert b.cache_root == "/var/tmp/testrange/alice"

    def test_explicit_cache_root_wins(self) -> None:
        b = SSHStorageBackend(
            host="x", username="y", cache_root="/opt/tr",
        )
        assert b.cache_root == "/opt/tr"

    def test_username_defaults_to_env_user(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("USER", "cfg")
        b = SSHStorageBackend(host="x")
        assert b._user == "cfg"

    def test_cache_root_interpolates_user_token(self) -> None:
        # Default path literally uses {user} — make sure it's
        # substituted by the resolved SSH username, not the outer
        # process's $USER.
        b = SSHStorageBackend(host="x", username="impersonated")
        assert b.cache_root == "/var/tmp/testrange/impersonated"


class TestSSHStorageBackendConnectFailure:
    """Connection errors surface as :class:`CacheError` — never a raw
    paramiko exception.  Prevents ``with Orchestrator(host=…)`` from
    dumping a paramiko stacktrace at the user."""

    def test_connect_error_wrapped_in_cache_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import paramiko

        client = MagicMock()
        client.connect.side_effect = paramiko.SSHException("bad creds")
        monkeypatch.setattr(
            "testrange.storage.ssh.paramiko.SSHClient",
            lambda: client,
        )

        b = SSHStorageBackend(host="x", username="y")
        with pytest.raises(CacheError, match="SSH connect"):
            b._connect()

    def test_oserror_wrapped_in_cache_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MagicMock()
        client.connect.side_effect = OSError("network unreachable")
        monkeypatch.setattr(
            "testrange.storage.ssh.paramiko.SSHClient",
            lambda: client,
        )

        b = SSHStorageBackend(host="x", username="y")
        with pytest.raises(CacheError, match="SSH connect"):
            b._connect()


class TestSSHStorageBackendExec:
    """``_exec`` and ``_exec_check`` cleanly expose exit code + stderr."""

    def _mocked_backend(self) -> tuple[SSHStorageBackend, MagicMock]:
        """Return (backend, client_mock) with ``_connect`` short-circuited."""
        client = MagicMock()
        b = SSHStorageBackend(host="x", username="y")
        b._client = client  # bypass real paramiko connection
        return b, client

    def test_exec_returns_exit_code_stdout_stderr(self) -> None:
        b, client = self._mocked_backend()

        stdin = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stdout.read.return_value = b"hello\n"
        stderr = MagicMock()
        stderr.read.return_value = b""
        client.exec_command.return_value = (stdin, stdout, stderr)

        code, out, err = b._exec(["echo", "hello"])
        assert code == 0
        assert out == "hello\n"
        assert err == ""
        # argv was shell-quoted into a single command string.
        client.exec_command.assert_called_once_with("echo hello")

    def test_exec_check_raises_on_nonzero(self) -> None:
        b, client = self._mocked_backend()
        stdin = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 1
        stdout.read.return_value = b""
        stderr = MagicMock()
        stderr.read.return_value = b"permission denied"
        client.exec_command.return_value = (stdin, stdout, stderr)

        with pytest.raises(CacheError, match="permission denied"):
            b._exec_check(["false"])


class TestSSHStorageBackendQemuImg:
    """qemu-img ops route through ``_exec_check`` with the exact argv
    the local backend's subprocess path uses.  Drift here breaks
    remote orchestration on mixed-host fleets."""

    def _backend(self) -> SSHStorageBackend:
        return SSHStorageBackend(host="x", username="y")

    def test_create_overlay_sends_expected_argv(self) -> None:
        b = self._backend()
        with patch.object(b, "_exec_check") as check:
            b.qemu_img_create_overlay("/t/base", "/t/ov")
            check.assert_called_once_with(
                [
                    "qemu-img", "create",
                    "-f", "qcow2",
                    "-b", "/t/base",
                    "-F", "qcow2",
                    "/t/ov",
                ]
            )

    def test_create_blank_sends_expected_argv(self) -> None:
        b = self._backend()
        with patch.object(b, "_exec_check") as check:
            b.qemu_img_create_blank("/t/blank.qcow2", "40G")
            check.assert_called_once_with(
                ["qemu-img", "create", "-f", "qcow2", "/t/blank.qcow2", "40G"]
            )

    def test_resize_sends_expected_argv(self) -> None:
        b = self._backend()
        with patch.object(b, "_exec_check") as check:
            b.qemu_img_resize("/t/d.qcow2", "64G")
            check.assert_called_once_with(
                ["qemu-img", "resize", "/t/d.qcow2", "64G"]
            )

    def test_convert_compressed_sends_expected_argv(self) -> None:
        b = self._backend()
        with patch.object(b, "_exec_check") as check:
            b.qemu_img_convert_compressed("/t/s.qcow2", "/t/d.qcow2")
            check.assert_called_once_with(
                [
                    "qemu-img", "convert",
                    "-f", "qcow2",
                    "-O", "qcow2",
                    "-c",
                    "/t/s.qcow2",
                    "/t/d.qcow2",
                ]
            )


class TestSSHStorageBackendFileOps:
    """SFTP primitives — write_bytes / read_bytes / exists / remove —
    must route through paramiko's SFTPClient rather than local ``open``.
    """

    def _backend_with_sftp(self) -> tuple[SSHStorageBackend, MagicMock]:
        sftp = MagicMock()
        b = SSHStorageBackend(host="x", username="y")
        b._sftp = sftp
        b._client = MagicMock()  # prevent lazy connect
        return b, sftp

    def test_write_bytes_parents_and_chmod(self) -> None:
        b, sftp = self._backend_with_sftp()
        file_ctx = MagicMock()
        file_ctx.__enter__.return_value = file_ctx
        sftp.file.return_value = file_ctx

        with patch.object(b, "_ensure_parent") as ep:
            b.write_bytes("/remote/x.bin", b"data", mode=0o600)

        ep.assert_called_once_with("/remote/x.bin")
        file_ctx.write.assert_called_once_with(b"data")
        sftp.chmod.assert_called_once_with("/remote/x.bin", 0o600)

    def test_exists_true_on_stat_ok(self) -> None:
        b, sftp = self._backend_with_sftp()
        sftp.stat.return_value = MagicMock()
        assert b.exists("/any") is True

    def test_exists_false_on_missing(self) -> None:
        b, sftp = self._backend_with_sftp()
        sftp.stat.side_effect = FileNotFoundError
        assert b.exists("/any") is False

    def test_remove_swallows_missing(self) -> None:
        b, sftp = self._backend_with_sftp()
        sftp.remove.side_effect = FileNotFoundError
        b.remove("/any")  # must not raise

    def test_upload_creates_parents(self) -> None:
        b, sftp = self._backend_with_sftp()
        with patch.object(b, "_ensure_parent") as ep:
            b.upload(Path("/local/src.qcow2"), "/remote/dest.qcow2")
        ep.assert_called_once_with("/remote/dest.qcow2")
        sftp.put.assert_called_once_with("/local/src.qcow2", "/remote/dest.qcow2")


class TestSSHStorageBackendClose:
    """close() tears down both SFTP and SSH cleanly — and is
    idempotent so __exit__ never re-raises."""

    def test_close_closes_both_channels(self) -> None:
        sftp = MagicMock()
        client = MagicMock()
        b = SSHStorageBackend(host="x", username="y")
        b._sftp = sftp
        b._client = client

        b.close()

        sftp.close.assert_called_once()
        client.close.assert_called_once()
        assert b._sftp is None
        assert b._client is None

    def test_close_is_idempotent(self) -> None:
        b = SSHStorageBackend(host="x", username="y")
        b.close()
        b.close()  # must not raise

    def test_close_swallows_underlying_errors(self) -> None:
        """close() must never raise — it's called during teardown where
        the original exception must propagate unobstructed."""
        sftp = MagicMock()
        sftp.close.side_effect = RuntimeError("boom")
        client = MagicMock()
        client.close.side_effect = RuntimeError("boom")

        b = SSHStorageBackend(host="x", username="y")
        b._sftp = sftp
        b._client = client
        b.close()  # must not raise


# ---------------------------------------------------------------------------
# Orchestrator backend selection
# ---------------------------------------------------------------------------


class TestOrchestratorBackendSelection:
    """Regression: ``Orchestrator(host=…)`` must pick the right backend
    without extra kwargs, and must respect an explicit ``storage_backend=``
    override."""

    def test_localhost_picks_local_backend(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator

        orch = Orchestrator()  # defaults: host="localhost"
        backend = orch._select_storage_backend()
        assert isinstance(backend, LocalStorageBackend)

    def test_bare_hostname_picks_ssh_backend(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator

        orch = Orchestrator(host="kvm.example.com")
        backend = orch._select_storage_backend()
        assert isinstance(backend, SSHStorageBackend)
        assert backend._host == "kvm.example.com"

    def test_qemu_ssh_uri_parses_user_host_port(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator

        orch = Orchestrator(host="qemu+ssh://alice@kvm.example.com:2222/system")
        backend = orch._select_storage_backend()
        assert isinstance(backend, SSHStorageBackend)
        assert backend._user == "alice"
        assert backend._host == "kvm.example.com"
        assert backend._port == 2222

    def test_qemu_ssh_uri_without_user_uses_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator

        monkeypatch.setenv("USER", "local")
        orch = Orchestrator(host="qemu+ssh://kvm.example.com/system")
        backend = orch._select_storage_backend()
        assert isinstance(backend, SSHStorageBackend)
        assert backend._user == "local"

    def test_explicit_storage_backend_wins(self) -> None:
        from testrange.backends.libvirt.orchestrator import Orchestrator

        sentinel = MagicMock(spec=AbstractStorageBackend)
        orch = Orchestrator(
            host="qemu+ssh://kvm.example.com/system",
            storage_backend=sentinel,
        )
        assert orch._select_storage_backend() is sentinel
