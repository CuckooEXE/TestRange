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
  PVE forwards our base64 straight to QGA's ``buf-b64``. QGA caps a single write
  at ~60 KB of content; a larger write raises rather than silently truncating
  (chunking is deferred until a real payload needs it — no premature machinery).
- **file-read** returns PVE-decoded content (16 MiB cap), re-encoded to bytes.
  The orchestrator's use is the dnsmasq lease file (text).
"""

from __future__ import annotations

import base64
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
# ~4/3, so 60000 encoded chars ≈ a ~45 KB raw payload. Larger needs chunking
# (deferred).
_MAX_ENCODED_WRITE_LEN = 60000


def _to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", "surrogateescape")


def make_execute(client: ProxmoxClient, backend_name: str) -> GuestExec:
    vmid = resolve_vmid(client, backend_name)
    agent = client.api.nodes(client.node).qemu(vmid).agent

    def _execute(
        argv: Sequence[str], *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        del cwd  # QGA exec has no working-directory concept
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
        return _to_bytes(result.get("content"))

    return _read_file


def make_write_file(client: ProxmoxClient, backend_name: str) -> GuestWriteFile:
    vmid = resolve_vmid(client, backend_name)
    agent = client.api.nodes(client.node).qemu(vmid).agent

    def _write_file(path: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        if len(encoded) > _MAX_ENCODED_WRITE_LEN:
            raise GuestAgentError(
                f"QGA file-write of {len(data)} bytes to {path!r} exceeds the agent's "
                "single-write cap; chunked writes are not implemented"
            )
        try:
            with client.call_lock:
                agent("file-write").post(file=path, content=encoded, encode=0)
        except Exception as e:
            raise GuestAgentError(f"QGA file-write {path!r} failed on {backend_name!r}: {e}") from e

    return _write_file
