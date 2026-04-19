"""Unit tests for :mod:`testrange.communication`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import GuestAgentError, VMTimeoutError


class TestExecResult:
    def test_tuple_unpacking(self) -> None:
        r = ExecResult(exit_code=0, stdout=b"ok", stderr=b"")
        assert r.exit_code == 0
        assert r.stdout == b"ok"
        assert r.stderr == b""

    def test_stdout_text(self) -> None:
        r = ExecResult(0, b"hello\n", b"")
        assert r.stdout_text == "hello\n"

    def test_stderr_text(self) -> None:
        r = ExecResult(1, b"", b"bad")
        assert r.stderr_text == "bad"

    def test_decode_replaces_invalid_utf8(self) -> None:
        r = ExecResult(0, b"\xff\xfehi", b"")
        # Must not raise; invalid bytes become replacement characters
        assert "hi" in r.stdout_text

    def test_check_passes_on_zero(self) -> None:
        r = ExecResult(0, b"", b"")
        assert r.check() is r

    def test_check_raises_on_nonzero(self) -> None:
        r = ExecResult(1, b"", b"boom")
        with pytest.raises(RuntimeError) as excinfo:
            r.check()
        assert "boom" in str(excinfo.value)
        assert "1" in str(excinfo.value)


class TestAbstractCommunicator:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            AbstractCommunicator()  # type: ignore[abstract]


class TestGuestAgentCommunicator:
    @pytest.fixture
    def comm(self, monkeypatch):
        # Import inside the fixture so the libvirt stub (via conftest) is
        # installed before the guest_agent module imports libvirt.
        from testrange.backends.libvirt import guest_agent as ga

        dom = MagicMock()
        dom.name.return_value = "web01"

        fake_qemu = MagicMock()
        # Patch the imported reference inside guest_agent so _send() uses it.
        monkeypatch.setattr(ga.libvirt_qemu, "qemuAgentCommand", fake_qemu)

        return ga.GuestAgentCommunicator(dom), fake_qemu

    def test_send_error_wrapped(self, comm) -> None:
        c, fake_qemu = comm
        fake_qemu.return_value = '{"error": {"class": "X", "desc": "nope"}}'
        with pytest.raises(GuestAgentError) as excinfo:
            c._send("guest-ping")
        assert "nope" in str(excinfo.value)

    def test_send_returns_result_field(self, comm) -> None:
        c, fake_qemu = comm
        fake_qemu.return_value = '{"return": {"pid": 42}}'
        result = c._send("guest-exec", {"path": "ls"})
        assert result == {"pid": 42}

    def test_wait_ready_succeeds_on_first_ping(self, comm) -> None:
        c, fake_qemu = comm
        fake_qemu.return_value = '{"return": {}}'
        c.wait_ready(timeout=1)  # should not raise

    def test_wait_ready_times_out(self, comm, monkeypatch) -> None:
        import testrange.backends.libvirt.guest_agent as ga

        c, dom = comm

        def _fail(*_, **__):
            raise GuestAgentError("not yet")

        monkeypatch.setattr(c, "_send", _fail)
        monkeypatch.setattr(ga, "_POLL_INTERVAL", 0.01)
        with pytest.raises(VMTimeoutError):
            c.wait_ready(timeout=0.05)

    def test_exec_parses_exit_code_and_output(self, comm, monkeypatch) -> None:
        import base64

        c, _ = comm
        responses = iter(
            [
                {"pid": 7},
                {
                    "exited": True,
                    "exitcode": 42,
                    "out-data": base64.b64encode(b"hello").decode(),
                    "err-data": base64.b64encode(b"oops").decode(),
                },
            ]
        )
        monkeypatch.setattr(c, "_send", lambda *a, **kw: next(responses))
        result = c.exec(["echo", "hi"])
        assert result.exit_code == 42
        assert result.stdout == b"hello"
        assert result.stderr == b"oops"

    def test_exec_times_out_if_never_exits(self, comm, monkeypatch) -> None:
        import testrange.backends.libvirt.guest_agent as ga

        c, _ = comm
        responses = iter([{"pid": 7}])

        def _send(*_a, **_k):
            try:
                return next(responses)
            except StopIteration:
                return {"exited": False}

        monkeypatch.setattr(c, "_send", _send)
        monkeypatch.setattr(ga, "_POLL_INTERVAL", 0.01)
        with pytest.raises(VMTimeoutError):
            c.exec(["sleep", "100"], timeout=0.05)

    def test_get_file_concatenates_chunks(self, comm, monkeypatch) -> None:
        import base64

        c, _ = comm
        responses = iter(
            [
                3,  # open handle
                {"buf-b64": base64.b64encode(b"foo").decode(), "eof": False},
                {"buf-b64": base64.b64encode(b"bar").decode(), "eof": True},
                None,  # close
            ]
        )
        monkeypatch.setattr(c, "_send", lambda *a, **kw: next(responses))
        assert c.get_file("/etc/x") == b"foobar"

    def test_hostname(self, comm, monkeypatch) -> None:
        c, _ = comm
        monkeypatch.setattr(c, "_send", lambda *a, **kw: {"host-name": "web01"})
        assert c.hostname() == "web01"
