"""PVE-4: QGA native guest transport (exec / read_file / write_file).

A chained fake API stands in for the agent endpoints — no proxmoxer, no real
guest. The QGA *wire* (encodings, async exec) is confirmed end-to-end by the
PVE-7 live integration suite; here we pin the transport logic: pid+poll, binary
base64 write, byte decoding, and error mapping.
"""

from __future__ import annotations

import base64
import shlex
import threading
from typing import Any

import pytest

from testrange.drivers.proxmox import _guest
from testrange.exceptions import GuestAgentError


class _Endpoint:
    def __init__(self, api: _FakeApi, path: str) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "post", "put", "delete"):
            return lambda **kw: self._api._call(name, self._path, kw)
        return _Endpoint(self._api, f"{self._path}/{name}")

    def __call__(self, *args: Any) -> _Endpoint:
        return _Endpoint(self._api, f"{self._path}/{'/'.join(str(a) for a in args)}")


class _FakeApi:
    def __init__(self) -> None:
        self.exec_status: dict[str, Any] = {"exited": 1, "exitcode": 0, "out-data": "hi\n"}
        self.file_content = "lease-data"
        self.written: dict[str, Any] = {}
        self.writes: list[dict[str, Any]] = []
        self.exec_raises = False
        # A simulated guest filesystem so the chunked-write assembly (cat/rm via
        # exec) can be round-trip verified. file-write lands raw decoded bytes;
        # exec interprets the `sh -c` cat/rm scripts the chunked path emits.
        self.files: dict[str, bytes] = {}
        self.fail_assembly = False
        self._exec_by_pid: dict[int, dict[str, Any]] = {}
        self._next_pid = 7

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path.endswith("/qemu") and method == "get":
            return [{"vmid": 100, "name": "tr-vm-x-web"}]
        if path.endswith("/agent/exec") and method == "post":
            if self.exec_raises:
                raise RuntimeError("guest agent not running")
            pid = self._next_pid
            self._next_pid += 1
            self._exec_by_pid[pid] = self._run_exec(list(kwargs.get("command", [])))
            return {"pid": pid}
        if path.endswith("/agent/exec-status") and method == "get":
            return self._exec_by_pid.get(kwargs["pid"], self.exec_status)
        if path.endswith("/agent/file-read") and method == "get":
            return {"content": self.file_content, "truncated": 0}
        if path.endswith("/agent/file-write") and method == "post":
            self.written = kwargs
            self.writes.append(kwargs)
            self.files[kwargs["file"]] = base64.b64decode(kwargs["content"])
            return None
        raise AssertionError(f"unexpected API call: {method} {path} {kwargs}")

    def _run_exec(self, argv: list[str]) -> dict[str, Any]:
        """Minimal in-guest interpreter for the chunked-write `cat`/`rm` steps."""
        ok = {"exited": 1, "exitcode": 0, "out-data": ""}
        if argv[:2] != ["sh", "-c"]:
            return self.exec_status
        toks = shlex.split(argv[2])
        if toks and toks[0] == "cat" and ">" in toks:
            if self.fail_assembly:
                return {"exited": 1, "exitcode": 1, "err-data": "cat: no such file"}
            sources = toks[1 : toks.index(">")]
            dest = toks[toks.index(">") + 1]
            self.files[dest] = b"".join(self.files[s] for s in sources)
            return ok
        if toks and toks[0] == "rm":
            for f in toks[2:]:  # skip "rm -f"
                self.files.pop(f, None)
            return ok
        return ok


class _FakeClient:
    def __init__(self) -> None:
        self.api = _FakeApi()
        self.node = "ns1001849"
        self.call_lock = threading.RLock()  # mirrors ProxmoxClient.call_lock (ADR-0023)


def _client() -> Any:
    return _FakeClient()


class TestExecute:
    def test_returns_exec_result_from_status(self) -> None:
        c = _client()
        r = _guest.make_execute(c, "tr-vm-x-web")(["echo", "hi"])
        assert r.exit_code == 0
        assert r.stdout == b"hi\n"

    def test_nonzero_exit_propagates(self) -> None:
        c = _client()
        c.api.exec_status = {"exited": 1, "exitcode": 3, "out-data": "", "err-data": "boom"}
        r = _guest.make_execute(c, "tr-vm-x-web")(["false"])
        assert r.exit_code == 3 and r.stderr == b"boom"

    def test_timeout_raises(self) -> None:
        c = _client()
        c.api.exec_status = {"exited": 0}  # never finishes
        with pytest.raises(GuestAgentError, match="timed out"):
            _guest.make_execute(c, "tr-vm-x-web")(["sleep"], timeout=0.0)

    def test_exec_failure_maps_to_guest_agent_error(self) -> None:
        c = _client()
        c.api.exec_raises = True
        with pytest.raises(GuestAgentError, match="QGA exec failed"):
            _guest.make_execute(c, "tr-vm-x-web")(["x"])


class TestFiles:
    def test_read_file_returns_bytes(self) -> None:
        c = _client()
        c.api.file_content = "100 02:aa ip host *"
        assert _guest.make_read_file(c, "tr-vm-x-web")("/leases") == b"100 02:aa ip host *"

    def test_write_file_is_binary_safe_base64(self) -> None:
        c = _client()
        data = b"\x00\x01\x02binary\xff"
        _guest.make_write_file(c, "tr-vm-x-web")("/tmp/f", data)
        assert c.api.written["file"] == "/tmp/f"
        assert c.api.written["encode"] == 0
        assert base64.b64decode(c.api.written["content"]) == data

    def test_small_write_uses_single_rest_call(self) -> None:
        # A payload within the cap takes the fast path: exactly one file-write,
        # no part files, no exec.
        c = _client()
        _guest.make_write_file(c, "tr-vm-x-web")("/tmp/small", b"hi")
        assert [w["file"] for w in c.api.writes] == ["/tmp/small"]

    def test_large_write_chunks_and_reassembles(self) -> None:
        # A payload over the cap is staged as raw part files then concatenated
        # in-guest; the reassembled file must byte-match the original and the
        # part files must be cleaned up.
        c = _client()
        data = bytes(i % 256 for i in range(_guest._RAW_WRITE_CHUNK * 2 + 123))  # 3 chunks
        _guest.make_write_file(c, "tr-vm-x-web")("/tmp/big", data)
        assert c.api.files["/tmp/big"] == data
        part_writes = [w["file"] for w in c.api.writes if ".tr-part" in w["file"]]
        assert len(part_writes) == 3  # ceil(2*chunk+123 / chunk)
        assert not [p for p in c.api.files if ".tr-part" in p]  # parts removed

    def test_chunk_boundary_is_within_the_encoded_cap(self) -> None:
        # Each staged part's base64 must fit PVE's single-write cap.
        c = _client()
        data = bytes(i % 256 for i in range(_guest._RAW_WRITE_CHUNK * 2))
        _guest.make_write_file(c, "tr-vm-x-web")("/tmp/big", data)
        for w in c.api.writes:
            assert len(w["content"]) <= _guest._MAX_ENCODED_WRITE_LEN

    def test_failed_assembly_raises_and_cleans_up(self) -> None:
        c = _client()
        c.api.fail_assembly = True
        data = bytes(i % 256 for i in range(_guest._RAW_WRITE_CHUNK + 1))  # 2 chunks
        with pytest.raises(GuestAgentError, match="chunked file-write assembly"):
            _guest.make_write_file(c, "tr-vm-x-web")("/tmp/big", data)
        assert not [p for p in c.api.files if ".tr-part" in p]  # parts still cleaned up
