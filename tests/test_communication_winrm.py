"""Unit tests for :mod:`testrange.communication.winrm`."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from testrange.exceptions import VMTimeoutError, WinRMError


@pytest.fixture
def WinRMCommunicator():
    from testrange.communication.winrm import WinRMCommunicator as _C
    return _C


class TestConstruction:
    def test_defaults(self, WinRMCommunicator) -> None:
        c = WinRMCommunicator("10.0.0.5", "Administrator", "pw")
        assert c._endpoint == "http://10.0.0.5:5985/wsman"
        assert c._username == "Administrator"
        assert c._password == "pw"
        assert c._transport == "ntlm"
        assert c._session is None

    def test_https_endpoint(self, WinRMCommunicator) -> None:
        c = WinRMCommunicator("h", "u", "p", port=5986, scheme="https")
        assert c._endpoint == "https://h:5986/wsman"


class TestRequireSession:
    def test_raises_before_wait_ready(self, WinRMCommunicator) -> None:
        c = WinRMCommunicator("h", "u", "p")
        with pytest.raises(WinRMError):
            c._require_session()


class TestWaitReady:
    def _stub_session_factory(
        self, monkeypatch: pytest.MonkeyPatch, responses: list
    ):
        import testrange.communication.winrm as wm

        sessions: list[MagicMock] = []
        iterator = iter(responses)

        def _factory(*_a, **_kw):
            session = MagicMock()

            def _run(_script):
                result = next(iterator)
                if isinstance(result, Exception):
                    raise result
                return result

            session.run_ps.side_effect = _run
            sessions.append(session)
            return session

        monkeypatch.setattr(wm.winrm, "Session", _factory)
        monkeypatch.setattr(wm, "_POLL_INTERVAL", 0.01)
        return sessions

    def test_first_probe_succeeds(
        self, WinRMCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = self._stub_session_factory(
            monkeypatch,
            [MagicMock(status_code=0, std_out=b"True", std_err=b"")],
        )
        c = WinRMCommunicator("h", "u", "p")
        c.wait_ready(timeout=5)
        assert c._session is sessions[0]

    def test_nonzero_status_retries_until_timeout(
        self, WinRMCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_session_factory(
            monkeypatch,
            [MagicMock(status_code=1, std_out=b"", std_err=b"busy")] * 100,
        )
        c = WinRMCommunicator("h", "u", "p")
        with pytest.raises(VMTimeoutError):
            c.wait_ready(timeout=0.05)

    def test_exception_retries_until_timeout(
        self, WinRMCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_session_factory(
            monkeypatch, [ConnectionError("refused")] * 100,
        )
        c = WinRMCommunicator("h", "u", "p")
        with pytest.raises(VMTimeoutError) as excinfo:
            c.wait_ready(timeout=0.05)
        assert "refused" in str(excinfo.value)


class TestExec:
    def _prime(self, WinRMCommunicator):
        c = WinRMCommunicator("h", "u", "p")
        c._session = MagicMock()
        return c

    def test_exec_uses_run_cmd_without_env(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_cmd.return_value = MagicMock(
            status_code=0, std_out=b"ok", std_err=b""
        )
        result = c.exec(["ipconfig", "/all"])
        c._session.run_cmd.assert_called_once_with("ipconfig", ["/all"])
        c._session.run_ps.assert_not_called()
        assert result.exit_code == 0
        assert result.stdout == b"ok"

    def test_exec_uses_run_ps_with_env(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=0, std_out=b"", std_err=b""
        )
        c.exec(["ipconfig"], env={"FOO": "bar"})
        c._session.run_ps.assert_called_once()
        script = c._session.run_ps.call_args[0][0]
        assert '$env:FOO="bar";' in script
        assert script.endswith("ipconfig")

    def test_exec_wraps_session_error(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_cmd.side_effect = ConnectionError("dead")
        with pytest.raises(WinRMError):
            c.exec(["hostname"])


class TestFileOps:
    def _prime(self, WinRMCommunicator):
        c = WinRMCommunicator("h", "u", "p")
        c._session = MagicMock()
        return c

    def test_get_file_decodes_base64(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        content = b"hello world"
        c._session.run_ps.return_value = MagicMock(
            status_code=0,
            std_out=base64.b64encode(content) + b"\n",
            std_err=b"",
        )
        assert c.get_file("C:/file.txt") == content

    def test_get_file_nonzero_raises(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=1, std_out=b"", std_err=b"access denied",
        )
        with pytest.raises(WinRMError) as excinfo:
            c.get_file("C:/secret")
        assert "access denied" in str(excinfo.value)

    def test_get_file_escapes_quotes(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=0, std_out=b"", std_err=b"",
        )
        c.get_file("C:/weird'name.txt")
        script = c._session.run_ps.call_args[0][0]
        # Single quotes doubled in PowerShell literal string
        assert "weird''name" in script

    def test_put_file_single_chunk(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=0, std_out=b"", std_err=b"",
        )
        c.put_file("C:/x.bin", b"hello")
        assert c._session.run_ps.call_count == 1
        script = c._session.run_ps.call_args[0][0]
        assert "WriteAllBytes" in script
        assert base64.b64encode(b"hello").decode() in script

    def test_put_file_empty_still_truncates(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=0, std_out=b"", std_err=b"",
        )
        c.put_file("C:/x.bin", b"")
        # Exactly one call: the WriteAllBytes truncating call.
        assert c._session.run_ps.call_count == 1
        assert "WriteAllBytes" in c._session.run_ps.call_args[0][0]

    def test_put_file_multichunk_appends(
        self, WinRMCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.communication.winrm as wm
        monkeypatch.setattr(wm, "_FILE_CHUNK_SIZE", 4)

        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=0, std_out=b"", std_err=b"",
        )
        c.put_file("C:/x.bin", b"abcdefghij")  # 10 bytes → 3 chunks

        calls = [call.args[0] for call in c._session.run_ps.call_args_list]
        assert len(calls) == 3
        assert "WriteAllBytes" in calls[0]
        assert "File]::Open" in calls[1]
        assert "File]::Open" in calls[2]

    def test_put_file_nonzero_raises(self, WinRMCommunicator) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.return_value = MagicMock(
            status_code=1, std_out=b"", std_err=b"full",
        )
        with pytest.raises(WinRMError):
            c.put_file("C:/x.bin", b"data")

    def test_put_file_session_exception_wrapped(
        self, WinRMCommunicator
    ) -> None:
        c = self._prime(WinRMCommunicator)
        c._session.run_ps.side_effect = RuntimeError("network down")
        with pytest.raises(WinRMError):
            c.put_file("C:/x.bin", b"data")


class TestHostname:
    def test_hostname_strips_trailing_whitespace(
        self, WinRMCommunicator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from testrange.communication.base import ExecResult

        c = WinRMCommunicator("h", "u", "p")
        monkeypatch.setattr(
            c, "exec", lambda *_a, **_k: ExecResult(0, b"WIN-ABC\r\n", b"")
        )
        assert c.hostname() == "WIN-ABC"


class TestLazyImportFromPackage:
    def test_lazy_import_works(self) -> None:
        from testrange.communication import WinRMCommunicator as lazy
        from testrange.communication.winrm import WinRMCommunicator as direct
        assert lazy is direct

    def test_unknown_attribute_raises(self) -> None:
        import testrange.communication as pkg
        with pytest.raises(AttributeError):
            pkg.DoesNotExist  # noqa: B018
