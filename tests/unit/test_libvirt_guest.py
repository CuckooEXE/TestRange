"""QGA native guest transport for the libvirt backend (BACKEND-1.B).

``libvirt_qemu`` is monkeypatched to a fake whose ``qemuAgentCommand`` dispatches
on the QGA verb, so the exec-poll loop, base64 decode, chunked file-read, and
file-write base64 framing are exercised without a daemon or a real agent.
"""

from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from testrange.drivers.libvirt import _guest
from testrange.exceptions import GuestAgentError


class FakeClient:
    # Mirrors LibvirtClient.call_lock (serializes agent commands; ADR-0023).
    call_lock = threading.RLock()

    def lookup_domain(self, name: str) -> object:
        return SimpleNamespace(name=name)


class FakeQGA:
    """Scriptable QGA: maps verbs to canned ``return`` payloads.

    ``exec_status_sequence`` lets a test return ``exited=False`` before ``True``
    to exercise the poll loop.
    """

    def __init__(self, **returns: Any) -> None:
        self.returns = returns
        self.calls: list[dict[str, Any]] = []
        self._status_seq = list(returns.get("__exec_status_seq__", []))

    def qemuAgentCommand(self, dom: Any, cmd: str, timeout: int, flags: int) -> str:
        command = json.loads(cmd)
        self.calls.append(command)
        verb = command["execute"]
        if verb == "guest-exec-status" and self._status_seq:
            return json.dumps({"return": self._status_seq.pop(0)})
        return json.dumps({"return": self.returns[verb]})


def _patch(monkeypatch: pytest.MonkeyPatch, qga: FakeQGA) -> None:
    monkeypatch.setattr(_guest, "_import_libvirt_qemu", lambda: qga)


class TestExecute:
    def test_polls_until_exited_and_decodes_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA(
            **{
                "guest-exec": {"pid": 42},
                "__exec_status_seq__": [
                    {"exited": False},
                    {
                        "exited": True,
                        "exitcode": 0,
                        "out-data": base64.b64encode(b"hello\n").decode(),
                        "err-data": base64.b64encode(b"warn").decode(),
                    },
                ],
            }
        )
        _patch(monkeypatch, qga)
        execute = _guest.make_execute(FakeClient(), "vm")  # type: ignore[arg-type]
        r = execute(["echo", "hello"])
        assert r.exit_code == 0 and r.stdout == b"hello\n" and r.stderr == b"warn"
        # argv split into path + arg[]
        exec_call = next(c for c in qga.calls if c["execute"] == "guest-exec")
        assert exec_call["arguments"]["path"] == "echo"
        assert exec_call["arguments"]["arg"] == ["hello"]

    def test_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA(
            **{"guest-exec": {"pid": 1}, "__exec_status_seq__": [{"exited": False}] * 100}
        )
        _patch(monkeypatch, qga)
        execute = _guest.make_execute(FakeClient(), "vm")  # type: ignore[arg-type]
        with pytest.raises(GuestAgentError, match="timed out"):
            execute(["sleep"], timeout=0.0)

    def test_exec_failure_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA()  # no 'guest-exec' key => KeyError inside
        _patch(monkeypatch, qga)
        execute = _guest.make_execute(FakeClient(), "vm")  # type: ignore[arg-type]
        with pytest.raises(GuestAgentError, match="exec failed"):
            execute(["x"])

    def test_poll_error_wrapped_as_guest_agent_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # CORE-97: the exec succeeds but a mid-poll status call raises a raw
        # library error (libvirtError-like); the poll loop must normalize it to
        # GuestAgentError, not leak it past the module's error contract.
        class _BoomQGA:
            def qemuAgentCommand(self, dom: Any, cmd: str, timeout: int, flags: int) -> str:
                if json.loads(cmd)["execute"] == "guest-exec":
                    return json.dumps({"return": {"pid": 5}})
                raise RuntimeError("QGA channel wedged")

        monkeypatch.setattr(_guest, "_import_libvirt_qemu", _BoomQGA)
        execute = _guest.make_execute(FakeClient(), "vm")  # type: ignore[arg-type]
        with pytest.raises(GuestAgentError, match="poll failed"):
            execute(["x"])


class TestReadFile:
    def test_reads_until_eof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA(
            **{
                "guest-file-open": 7,
                "guest-file-read": {
                    "buf-b64": base64.b64encode(b"lease-data\n").decode(),
                    "eof": True,
                },
                "guest-file-close": {},
            }
        )
        _patch(monkeypatch, qga)
        read = _guest.make_read_file(FakeClient(), "vm")  # type: ignore[arg-type]
        assert read("/var/lib/misc/dnsmasq.leases") == b"lease-data\n"
        assert any(c["execute"] == "guest-file-close" for c in qga.calls)  # handle closed

    def test_read_failure_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA()  # missing 'guest-file-open'
        _patch(monkeypatch, qga)
        read = _guest.make_read_file(FakeClient(), "vm")  # type: ignore[arg-type]
        with pytest.raises(GuestAgentError, match="file-read"):
            read("/x")


class TestWriteFile:
    def test_writes_base64_and_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qga = FakeQGA(
            **{
                "guest-file-open": 9,
                "guest-file-write": {"count": 9},
                "guest-file-close": {},
            }
        )
        _patch(monkeypatch, qga)
        write = _guest.make_write_file(FakeClient(), "vm")  # type: ignore[arg-type]
        write("/root/marker", b"native-io\n")
        wcall = next(c for c in qga.calls if c["execute"] == "guest-file-write")
        assert base64.b64decode(wcall["arguments"]["buf-b64"]) == b"native-io\n"
        assert any(c["execute"] == "guest-file-close" for c in qga.calls)
