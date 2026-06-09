"""QEMU Guest Agent transport for the Proxmox backend (PVE-4).

The native in-guest channel: QGA over the PVE REST API. Unauthenticated (no
guest credentials, matching the QGA contract — so the ABC's ``native_guest_*``
accessors stay credential-free). Wiring these makes all three ``native_guest_*``
accessors live, which unblocks ``NativeCommunicator`` and the sidecar
DHCP-lease readback on Proxmox.

Each ``make_*`` returns a VM-bound callable matching the corresponding
``guest_io`` Protocol; the orchestrator binds them onto ``NativeCommunicator``.
The vmid is resolved once (from the stamped name) when the callable is built.

Wire details (full confirmation rides the PVE-7 live integration suite, which
boots a guest with the agent installed):

- **exec is async** — ``agent/exec`` returns a ``pid``; poll ``agent/exec-status``
  until ``exited``. No stdin, no cwd (QGA has neither).
- **file-write is binary-safe** — we base64-encode here and pass ``encode=0`` so
  PVE forwards our base64 straight to QGA's ``buf-b64``. PVE's ``agent/file-write``
  caps the encoded ``content`` field (~60 KB ≈ ~45 KB raw) and is one-shot
  (open+write+close, no append/offset). A payload over the cap is staged as raw
  part files via repeated writes, then concatenated in-guest with a single
  ``cat`` exec — the only chunking mechanism PVE's REST agent exposes (it assumes
  a POSIX shell + ``cat``/``rm`` in the guest, which the Linux cert corpus has).
- **file-read** returns PVE-decoded content (16 MiB cap), re-encoded to bytes.
  The orchestrator's use is the dnsmasq lease file (text).
"""

from __future__ import annotations

import base64
import contextlib
import shlex
import time
from typing import TYPE_CHECKING, Any

from testrange.communicators.base import ExecResult
from testrange.drivers.proxmox._vm import resolve_vmid
from testrange.exceptions import GuestAgentError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.drivers.proxmox._client import ProxmoxClient
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile

_POLL_INTERVAL_S = 0.25
# PVE's agent file-write caps the (base64-encoded) `content` field length; the
# check below is against the *encoded* length, hence the name. Base64 inflates
# ~4/3, so 60000 encoded chars ≈ a ~45 KB raw payload.
_MAX_ENCODED_WRITE_LEN = 60000
# Raw bytes per chunk whose base64 fits the cap exactly: base64(45000) == 60000.
# Keep it a multiple of 3 so each chunk encodes without interior padding (each is
# decoded independently by QGA, then the raw parts are concatenated).
_RAW_WRITE_CHUNK = (_MAX_ENCODED_WRITE_LEN // 4) * 3


def _to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    # QGA payloads (exec out/err-data, file-read content) arrive as latin-1
    # strings — PVE base64-decodes the guest bytes and the JSON transport surfaces
    # each raw byte 0x00-0xFF as one U+0000..U+00FF codepoint — so recover the
    # exact bytes with a latin-1 encode. A utf-8 encode doubled every 0x80-0xFF
    # byte (PVE-58 cert: a 256 KiB binary file-read came back 393216 bytes).
    return str(value).encode("latin-1", "surrogateescape")


def _run_exec(
    client: ProxmoxClient, agent: Any, argv: Sequence[str], backend_name: str, *, timeout: float
) -> ExecResult:
    """Run ``argv`` in the guest via QGA (async exec + poll). Shared by the
    public exec accessor and the chunked-write in-guest assembly step."""
    start = time.monotonic()
    try:
        with client.call_lock:
            pid = agent("exec").post(command=list(argv))["pid"]
    except Exception as e:
        raise GuestAgentError(f"QGA exec failed on {backend_name!r}: {e}") from e
    while True:
        with client.call_lock:
            status = agent("exec-status").get(pid=pid)
        if status.get("exited"):
            return ExecResult(
                exit_code=int(status.get("exitcode", -1)),
                stdout=_to_bytes(status.get("out-data")),
                stderr=_to_bytes(status.get("err-data")),
                duration=time.monotonic() - start,
            )
        if time.monotonic() - start > timeout:
            raise GuestAgentError(
                f"QGA exec of {list(argv)!r} timed out after {timeout}s on {backend_name!r}"
            )
        time.sleep(_POLL_INTERVAL_S)


def make_execute(client: ProxmoxClient, backend_name: str) -> GuestExec:
    vmid = resolve_vmid(client, backend_name)
    agent = client.api.nodes(client.node).qemu(vmid).agent

    def _execute(
        argv: Sequence[str], *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        del cwd  # QGA exec has no working-directory concept
        return _run_exec(client, agent, argv, backend_name, timeout=timeout)

    return _execute


def make_read_file(client: ProxmoxClient, backend_name: str) -> GuestReadFile:
    vmid = resolve_vmid(client, backend_name)
    agent = client.api.nodes(client.node).qemu(vmid).agent

    def _read_file(path: str) -> bytes:
        try:
            with client.call_lock:
                result = agent("file-read").get(file=path)
        except Exception as e:
            raise GuestAgentError(f"QGA file-read {path!r} failed on {backend_name!r}: {e}") from e
        if result.get("truncated"):
            # PVE's agent/file-read caps a single read (~16 MiB). Returning the
            # head silently would corrupt a large readback, so fail loud.
            raise GuestAgentError(
                f"QGA file-read {path!r} on {backend_name!r} was truncated "
                "(file exceeds PVE's single-read cap)"
            )
        return _to_bytes(result.get("content"))

    return _read_file


def make_write_file(client: ProxmoxClient, backend_name: str) -> GuestWriteFile:
    vmid = resolve_vmid(client, backend_name)
    agent = client.api.nodes(client.node).qemu(vmid).agent

    def _post_write(file: str, encoded: str) -> None:
        try:
            with client.call_lock:
                agent("file-write").post(file=file, content=encoded, encode=0)
        except Exception as e:
            raise GuestAgentError(f"QGA file-write {file!r} failed on {backend_name!r}: {e}") from e

    def _write_chunked(path: str, data: bytes) -> None:
        # Stage each raw chunk as a part file (one-shot writes within the cap),
        # then concatenate them in-guest. Part files are always cleaned up.
        parts: list[str] = []
        try:
            for n, start in enumerate(range(0, len(data), _RAW_WRITE_CHUNK)):
                part = f"{path}.tr-part{n}"
                _post_write(part, base64.b64encode(data[start : start + _RAW_WRITE_CHUNK]).decode())
                parts.append(part)
            joined = " ".join(shlex.quote(p) for p in parts)
            res = _run_exec(
                client,
                agent,
                ["sh", "-c", f"cat {joined} > {shlex.quote(path)}"],
                backend_name,
                timeout=120.0,
            )
            if res.exit_code != 0:
                raise GuestAgentError(
                    f"QGA chunked file-write assembly of {path!r} failed on "
                    f"{backend_name!r}: rc={res.exit_code} {res.stderr.decode('utf-8', 'replace')!r}"
                )
        finally:
            if parts:
                rm = "rm -f " + " ".join(shlex.quote(p) for p in parts)
                with contextlib.suppress(GuestAgentError):
                    _run_exec(client, agent, ["sh", "-c", rm], backend_name, timeout=60.0)

    def _write_file(path: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        if len(encoded) <= _MAX_ENCODED_WRITE_LEN:
            _post_write(path, encoded)  # fast path: a single REST write
        else:
            _write_chunked(path, data)

    return _write_file
