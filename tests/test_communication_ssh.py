"""Unit tests for :mod:`testrange.communication.ssh`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange.exceptions import SSHError, VMTimeoutError


@pytest.fixture
def SSHCommunicator():
    from testrange.communication.ssh import SSHCommunicator as _C
    return _C


class TestConstruction:
    def test_defaults(self, SSHCommunicator) -> None:
        c = SSHCommunicator("10.0.0.5", "alice")
        assert c._host == "10.0.0.5"
        assert c._username == "alice"
        assert c._password is None
        assert c._key_filename is None
        assert c._port == 22
        assert c._client is None

    def test_key_filename_stringified(self, SSHCommunicator, tmp_path) -> None:
        c = SSHCommunicator("h", "u", key_filename=tmp_path / "id")
        assert c._key_filename == str(tmp_path / "id")
        assert isinstance(c._key_filename, str)


class TestRequireClient:
    def test_raises_before_wait_ready(self, SSHCommunicator) -> None:
        c = SSHCommunicator("h", "u")
        with pytest.raises(SSHError):
            c._require_client()


class TestWaitReady:
    def _mock_client_class(
        self, monkeypatch: pytest.MonkeyPatch, connect_side_effect=None
    ):
        import testrange.communication.ssh as ssh_mod

        clients: list[MagicMock] = []

        def _factory():
            client = MagicMock()
            client.connect.side_effect = connect_side_effect
            clients.append(client)
            return client

        monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", _factory)
        return clients

    def test_first_attempt_succeeds(
        self, SSHCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clients = self._mock_client_class(monkeypatch)
        c = SSHCommunicator("h", "u", password="pw")
        c.wait_ready(timeout=5)
        assert c._client is clients[0]
        clients[0].connect.assert_called_once()

    def test_connect_enables_agent_and_key_discovery(
        self, SSHCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Debian cloud images disable root password SSH; auth relies
        on the private key that matches Credential.ssh_key being
        reachable via ssh-agent or a standard ``~/.ssh/`` key file.
        Regression guard: don't reintroduce ``allow_agent=False`` /
        ``look_for_keys=False``."""
        clients = self._mock_client_class(monkeypatch)
        c = SSHCommunicator("h", "u")
        c.wait_ready(timeout=5)
        kwargs = clients[0].connect.call_args.kwargs
        assert kwargs["allow_agent"] is True
        assert kwargs["look_for_keys"] is True

    def test_connection_failures_retry_until_timeout(
        self, SSHCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.communication.ssh as ssh_mod

        monkeypatch.setattr(ssh_mod, "_POLL_INTERVAL", 0.01)

        self._mock_client_class(
            monkeypatch,
            connect_side_effect=ssh_mod.paramiko.SSHException("refused"),
        )
        c = SSHCommunicator("h", "u")
        with pytest.raises(VMTimeoutError) as excinfo:
            c.wait_ready(timeout=0.05)
        assert "not ready" in str(excinfo.value)
        assert "refused" in str(excinfo.value)


class TestExec:
    def _prime(self, SSHCommunicator):
        c = SSHCommunicator("h", "u")
        client = MagicMock()
        c._client = client
        return c, client

    def test_exec_captures_output_and_exit_code(self, SSHCommunicator) -> None:
        c, client = self._prime(SSHCommunicator)
        stdout = MagicMock()
        stdout.read.return_value = b"hello\n"
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        stderr.read.return_value = b""
        client.exec_command.return_value = (MagicMock(), stdout, stderr)

        result = c.exec(["echo", "hi"])
        assert result.exit_code == 0
        assert result.stdout == b"hello\n"
        assert result.stderr == b""

    def test_exec_quotes_arguments_with_spaces(self, SSHCommunicator) -> None:
        c, client = self._prime(SSHCommunicator)
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.read.return_value = b""
        stdout.channel.recv_exit_status.return_value = 0
        stderr.read.return_value = b""
        client.exec_command.return_value = (MagicMock(), stdout, stderr)

        c.exec(["echo", "hello world", "it's me"])
        sent_cmd = client.exec_command.call_args[0][0]
        # shlex.quote should wrap the multi-word arg
        assert "'hello world'" in sent_cmd
        assert "'it'\"'\"'s me'" in sent_cmd

    def test_exec_prepends_env(self, SSHCommunicator) -> None:
        c, client = self._prime(SSHCommunicator)
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.read.return_value = b""
        stdout.channel.recv_exit_status.return_value = 0
        stderr.read.return_value = b""
        client.exec_command.return_value = (MagicMock(), stdout, stderr)

        c.exec(["printenv"], env={"FOO": "bar"})
        sent_cmd = client.exec_command.call_args[0][0]
        assert sent_cmd.startswith("env FOO=bar ")

    def test_exec_ssh_exception_wrapped(self, SSHCommunicator) -> None:
        import testrange.communication.ssh as ssh_mod
        c, client = self._prime(SSHCommunicator)
        client.exec_command.side_effect = ssh_mod.paramiko.SSHException("dead")

        with pytest.raises(SSHError) as excinfo:
            c.exec(["ls"])
        assert "dead" in str(excinfo.value)

    def test_exec_socket_timeout_raises_vm_timeout(
        self, SSHCommunicator
    ) -> None:
        c, client = self._prime(SSHCommunicator)
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.read.side_effect = TimeoutError()
        client.exec_command.return_value = (MagicMock(), stdout, stderr)

        with pytest.raises(VMTimeoutError):
            c.exec(["sleep", "10"], timeout=1)


class TestFileOps:
    def _prime(self, SSHCommunicator):
        c = SSHCommunicator("h", "u")
        client = MagicMock()
        c._client = client
        sftp = MagicMock()
        # context-manager protocol
        client.open_sftp.return_value.__enter__.return_value = sftp
        return c, client, sftp

    def test_get_file_concatenates_chunks(self, SSHCommunicator) -> None:
        c, _, sftp = self._prime(SSHCommunicator)
        fh = MagicMock()
        fh.read.side_effect = [b"foo", b"bar", b""]
        sftp.open.return_value.__enter__.return_value = fh

        assert c.get_file("/etc/x") == b"foobar"

    def test_get_file_wraps_sftp_error(self, SSHCommunicator) -> None:
        import testrange.communication.ssh as ssh_mod
        c, _, sftp = self._prime(SSHCommunicator)
        sftp.open.side_effect = ssh_mod.paramiko.SSHException("no")

        with pytest.raises(SSHError):
            c.get_file("/etc/x")

    def test_put_file_writes_chunks(
        self, SSHCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.communication.ssh as ssh_mod
        monkeypatch.setattr(ssh_mod, "_SFTP_CHUNK_SIZE", 3)

        c, _, sftp = self._prime(SSHCommunicator)
        fh = MagicMock()
        sftp.open.return_value.__enter__.return_value = fh

        c.put_file("/tmp/x", b"abcdefgh")
        writes = [call.args[0] for call in fh.write.call_args_list]
        assert writes == [b"abc", b"def", b"gh"]

    def test_put_file_wraps_sftp_error(self, SSHCommunicator) -> None:
        c, _, sftp = self._prime(SSHCommunicator)
        sftp.open.side_effect = OSError("disk full")

        with pytest.raises(SSHError):
            c.put_file("/tmp/x", b"data")


class TestHostname:
    def test_hostname_strips_trailing_newline(
        self, SSHCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = SSHCommunicator("h", "u")
        from testrange.communication.base import ExecResult
        monkeypatch.setattr(
            c, "exec", lambda *_a, **_k: ExecResult(0, b"web01\n", b"")
        )
        assert c.hostname() == "web01"


class TestClose:
    def test_close_is_idempotent(self, SSHCommunicator) -> None:
        c = SSHCommunicator("h", "u")
        c.close()  # no session, must not raise
        c._client = MagicMock()
        c.close()
        assert c._client is None
        c.close()  # second close — still fine
