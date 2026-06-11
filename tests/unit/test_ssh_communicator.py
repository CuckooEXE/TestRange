"""Tests for SSHCommunicator with paramiko entirely mocked."""

from __future__ import annotations

import threading
from io import StringIO
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange.communicators import SSHCommunicator
from testrange.communicators.base import ExecResult
from testrange.credentials import PosixCred
from testrange.exceptions import CommunicatorError
from testrange.gateways import GuestGateway
from testrange.utils import SSHKey


class _FakeChannel:
    def __init__(self, exit_code: int = 0, *, status_ready: bool = True) -> None:
        self._ec = exit_code
        self._status_ready = status_ready
        self.closed = False

    def exit_status_ready(self) -> bool:
        return self._status_ready

    def recv_exit_status(self) -> int:
        return self._ec

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(
        self,
        data: bytes,
        exit_code: int = 0,
        read_exc: BaseException | None = None,
        *,
        status_ready: bool = True,
    ) -> None:
        self._data = data
        self._read_exc = read_exc
        self.channel = _FakeChannel(exit_code, status_ready=status_ready)

    def read(self) -> bytes:
        if self._read_exc is not None:
            raise self._read_exc
        return self._data


class _FakeSFTPFile:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.written: bytes = b""

    def __enter__(self) -> _FakeSFTPFile:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._data

    def write(self, data: bytes) -> None:
        self.written += data


class _FakeSFTP:
    def __init__(self) -> None:
        self.files: dict[str, _FakeSFTPFile] = {}
        self.opened: list[tuple[str, str]] = []
        self.put_short = False  # simulate a truncated transfer for confirm=True

    def open(self, path: str, mode: str) -> _FakeSFTPFile:
        self.opened.append((path, mode))
        return self.files.setdefault(path, _FakeSFTPFile())

    def putfo(self, fl: Any, path: str, confirm: bool = True) -> None:
        data = fl.read()
        stored = data[:-1] if self.put_short else data
        f = self.files.setdefault(path, _FakeSFTPFile())
        f.written = stored
        if confirm and len(stored) != len(data):
            raise OSError(f"size mismatch: {len(stored)} of {len(data)} bytes")

    def close(self) -> None:
        pass


class _FakeClient:
    def __init__(self) -> None:
        self.connect_args: dict[str, Any] = {}
        self.connect_calls = 0
        self.fail_first_n = 0
        self.exec_commands: list[tuple[str, dict[str, Any]]] = []
        self.stdout_payload = b""
        self.stderr_payload = b""
        self.exit_code = 0
        self.read_exc: BaseException | None = None  # stdout.read() raises this when set
        self.status_ready = True  # stdout channel reports exit-status-ready
        self.closed = False
        self.sftp = _FakeSFTP()

    def set_missing_host_key_policy(self, _p: Any) -> None:
        pass

    def connect(self, **kwargs: Any) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_first_n:
            raise OSError("connection refused")
        self.connect_args = kwargs

    def exec_command(self, cmd: str, **kwargs: Any) -> tuple[Any, _FakeStream, _FakeStream]:
        self.exec_commands.append((cmd, kwargs))
        return (
            object(),
            _FakeStream(
                self.stdout_payload,
                exit_code=self.exit_code,
                read_exc=self.read_exc,
                status_ready=self.status_ready,
            ),
            _FakeStream(self.stderr_payload, exit_code=self.exit_code),
        )

    def open_sftp(self) -> _FakeSFTP:
        return self.sftp

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_paramiko(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _FakeClient]:
    """Replace the lazy-imported paramiko with a mock module."""
    client = _FakeClient()

    paramiko_mock = MagicMock()
    paramiko_mock.SSHClient.return_value = client
    paramiko_mock.AutoAddPolicy = MagicMock
    paramiko_mock.SSHException = Exception

    class _FakeKey:
        @classmethod
        def from_private_key(cls, fp: StringIO) -> _FakeKey:
            return cls()

    paramiko_mock.Ed25519Key = _FakeKey
    paramiko_mock.RSAKey = _FakeKey
    paramiko_mock.ECDSAKey = _FakeKey
    paramiko_mock.DSSKey = _FakeKey

    monkeypatch.setattr("testrange.communicators.ssh._import_paramiko", lambda: paramiko_mock)
    monkeypatch.setattr("testrange.communicators.ssh.time.sleep", lambda _s: None)
    return paramiko_mock, client


class TestExecute:
    def test_basic_command(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        client.stdout_payload = b"Linux\n"
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        result = c.execute(["uname", "-s"])
        assert isinstance(result, ExecResult)
        assert result.exit_code == 0
        assert result.stdout == b"Linux\n"
        assert client.exec_commands[0][0] == "uname -s"

    def test_argv_quoting(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.execute(["echo", "hello world"])
        # shlex.join quotes "hello world"
        assert client.exec_commands[0][0] == "echo 'hello world'"

    def test_cwd_via_cd(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.execute(["pwd"], cwd="/tmp")
        assert "cd -- /tmp && exec" in client.exec_commands[0][0]

    def test_nonzero_exit(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        client.exit_code = 7
        client.stderr_payload = b"oops\n"
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        r = c.execute(["false"])
        assert r.exit_code == 7
        assert r.stderr == b"oops\n"
        assert not r.ok

    def test_missing_exit_status_is_bounded(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        # COMM-6: the peer sends EOF (stdout.read() returns) but never delivers an
        # exit status and keeps the channel half-open. paramiko's recv_exit_status()
        # would block forever; execute() must instead time out as a
        # CommunicatorError (no run/test-phase watchdog covers this).
        _, client = fake_paramiko
        client.status_ready = False
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        with pytest.raises(CommunicatorError, match="no exit status"):
            c.execute(["hang"], timeout=0.05)

    def test_read_timeout_is_wrapped(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        # COMM-4: a socket.timeout (TimeoutError) from stdout.read() — the
        # bound on a chatty-stderr wedge — must surface as CommunicatorError,
        # not leak paramiko's raw exception past the communicator boundary.
        _, client = fake_paramiko
        client.read_exc = TimeoutError("read timed out")
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        with pytest.raises(CommunicatorError, match="timed out"):
            c.execute(["sleep", "999"])


class TestAuthSelection:
    def test_pkey_when_present(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        kp = SSHKey.generate()
        cred = PosixCred("u", password="pw", ssh_key=kp)
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=cred)
        c.execute(["true"])
        assert "pkey" in client.connect_args
        assert "password" not in client.connect_args

    def test_password_when_no_key(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        cred = PosixCred("u", password="pw")
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=cred)
        c.execute(["true"])
        assert "password" in client.connect_args
        assert "pkey" not in client.connect_args


class TestRetry:
    def test_retries_on_initial_failure(
        self,
        fake_paramiko: tuple[Any, _FakeClient],
    ) -> None:
        _, client = fake_paramiko
        client.fail_first_n = 3
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.execute(["true"])
        assert client.connect_calls == 4  # 3 failures + 1 success

    def test_total_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        from testrange.communicators import ssh as ssh_mod

        paramiko_mock = MagicMock()
        client = _FakeClient()
        client.fail_first_n = 10**9  # always fail
        paramiko_mock.SSHClient.return_value = client
        paramiko_mock.SSHException = Exception
        monkeypatch.setattr(ssh_mod, "_import_paramiko", lambda: paramiko_mock)
        # Advance monotonic so the deadline is reached after a few attempts.
        # ssh_mod uses `import time`, so patching the shared time module here
        # affects the calls inside the communicator's retry loop.
        ticks = iter([0.0] + [9999.0] * 50)
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        with pytest.raises(CommunicatorError, match="SSH connect"):
            c.execute(["true"])


class _FakeSock:
    """A closable stand-in for the gateway's per-attempt direct-tcpip channel."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeGateway(GuestGateway):
    """A GuestGateway stand-in: hands back a fresh sock per call and records use."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, int]] = []
        self.socks: list[_FakeSock] = []
        self.closed = False

    def open_socket(self, host: str, port: int) -> Any:
        self.opened.append((host, port))
        sock = _FakeSock()
        self.socks.append(sock)
        return sock

    def open_local_forward(self, host: str, port: int) -> int:
        raise NotImplementedError

    def close(self) -> None:
        self.closed = True


class TestGateway:
    def test_connect_tunnels_through_gateway_sock(
        self, fake_paramiko: tuple[Any, _FakeClient]
    ) -> None:
        # When a gateway is bound, paramiko is dialled over the gateway's socket,
        # and host stays the guest's own address (the gateway knows how to reach
        # it). This is the off-box-reach contract for a remote backend.
        _, client = fake_paramiko
        gw = _FakeGateway()
        c = SSHCommunicator("u")
        c.bind(host="10.30.0.41", credential=PosixCred("u", password="p"), gateway=gw)
        c.execute(["true"])
        assert client.connect_args["sock"] is gw.socks[-1]
        assert client.connect_args["hostname"] == "10.30.0.41"
        assert gw.opened == [("10.30.0.41", 22)]

    def test_no_gateway_means_direct_connect(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.execute(["true"])
        assert "sock" not in client.connect_args

    def test_gateway_reopened_each_retry(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        # A spent sock can't be reused; the loop asks the gateway for a fresh one
        # each attempt while the guest's sshd is still coming up.
        _, client = fake_paramiko
        client.fail_first_n = 2
        gw = _FakeGateway()
        c = SSHCommunicator("u")
        c.bind(host="10.30.0.41", credential=PosixCred("u", password="p"), gateway=gw)
        c.execute(["true"])
        assert len(gw.opened) == 3  # 2 failed attempts + 1 success

    def test_failed_attempt_socket_is_closed(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        # COMM-7: paramiko does not close a user-supplied sock on connect failure,
        # so the loop must close the spent gateway channel each retry — else a
        # slow-booting guest piles up open direct-tcpip channels on the bastion.
        _, client = fake_paramiko
        client.fail_first_n = 2
        gw = _FakeGateway()
        c = SSHCommunicator("u")
        c.bind(host="10.30.0.41", credential=PosixCred("u", password="p"), gateway=gw)
        c.execute(["true"])
        # The two failed attempts' socks are closed; the successful one is kept.
        assert [s.closed for s in gw.socks] == [True, True, False]

    def test_close_releases_gateway(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        gw = _FakeGateway()
        c = SSHCommunicator("u")
        c.bind(host="10.30.0.41", credential=PosixCred("u", password="p"), gateway=gw)
        c.execute(["true"])
        c.close()
        assert gw.closed is True


class TestSFTP:
    def test_read_file(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        client.sftp.files["/etc/hosts"] = _FakeSFTPFile(b"127.0.0.1 localhost\n")
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        data = c.read_file("/etc/hosts")
        assert data == b"127.0.0.1 localhost\n"

    def test_write_file(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.write_file("/tmp/foo", b"data")
        assert client.sftp.files["/tmp/foo"].written == b"data"

    def test_write_file_truncated_raises(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        # A short transfer (remote size != source size) must fail loud, not be
        # silently swallowed (COMM-5).
        _, client = fake_paramiko
        client.sftp.put_short = True
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        with pytest.raises(CommunicatorError, match="SFTP write"):
            c.write_file("/tmp/foo", b"data")


class TestConcurrentDrain:
    """stdout and stderr share one channel and must be drained concurrently."""

    def test_stderr_flood_does_not_wedge_stdout(
        self, fake_paramiko: tuple[Any, _FakeClient]
    ) -> None:
        # Regression (COMM-5): the old code read stdout fully *before* stderr.
        # Here stdout cannot complete until stderr has been drained (mimicking
        # the shared-channel-window backpressure) — sequential draining would
        # deadlock; concurrent draining completes.
        _, client = fake_paramiko
        stderr_drained = threading.Event()

        class _BlockingStdout:
            def __init__(self) -> None:
                self.channel = _FakeChannel(0)

            def read(self) -> bytes:
                if not stderr_drained.wait(timeout=5.0):
                    raise AssertionError("stdout wedged: stderr was never drained")
                return b"out"

        class _SignalStderr:
            channel = _FakeChannel(0)

            def read(self) -> bytes:
                stderr_drained.set()
                return b"E" * 100_000

        def _exec(cmd: str, **kwargs: Any) -> tuple[Any, Any, Any]:
            client.exec_commands.append((cmd, kwargs))
            return (object(), _BlockingStdout(), _SignalStderr())

        client.exec_command = _exec  # type: ignore[method-assign]
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        res = c.execute(["noisy"])
        assert res.stdout == b"out"
        assert res.stderr == b"E" * 100_000


class TestClose:
    def test_close_is_idempotent(self, fake_paramiko: tuple[Any, _FakeClient]) -> None:
        _, client = fake_paramiko
        c = SSHCommunicator("u")
        c.bind(host="10.0.0.1", credential=PosixCred("u", password="p"))
        c.execute(["true"])
        c.close()
        c.close()  # no raise
        assert client.closed
