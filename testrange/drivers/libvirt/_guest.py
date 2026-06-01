"""QEMU Guest Agent transport for the libvirt backend (BACKEND-1.B).

The native in-guest channel: QGA JSON over ``libvirt_qemu.qemuAgentCommand``
against the domain's unconditional ``org.qemu.guest_agent.0`` virtio channel.
Unauthenticated (no guest credentials, matching the QGA contract — so the ABC's
``native_guest_*`` accessors stay credential-free). Wiring these makes all three
accessors live, which unblocks ``NativeCommunicator`` and the sidecar
DHCP-lease readback.

Unlike PVE's pre-wrapped ``agent/exec`` REST helpers, libvirt forwards our JSON
verbatim to the agent and returns its reply, so this speaks the QGA protocol
directly:

- **exec is async** — ``guest-exec`` returns a ``pid``; poll ``guest-exec-status``
  until ``exited``. ``out-data``/``err-data`` are base64. No stdin, no cwd (QGA
  has neither).
- **file I/O** is ``guest-file-open`` → ``guest-file-read``/``write`` (base64
  ``buf-b64``) → ``guest-file-close``, always closing the handle.

Each ``make_*`` returns a VM-bound callable matching the corresponding
``guest_io`` Protocol; the domain is resolved once when the callable is built.
Functions take the live :class:`LibvirtClient`; unit tests inject a duck-typed
fake.
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING, Any

from testrange.communicators.base import ExecResult
from testrange.drivers.libvirt._conn import _import_libvirt_qemu
from testrange.exceptions import GuestAgentError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.drivers.libvirt._conn import LibvirtClient
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile

# Per-RPC timeout for a single agent command (seconds). The agent answers each
# request fast (guest-exec returns a pid immediately; status polls don't block on
# the command's runtime), so this only guards a wedged channel.
_AGENT_TIMEOUT_S = 30
_POLL_INTERVAL_S = 0.25
# QGA reads return base64 in bounded chunks; loop until eof. 256 KiB/read keeps
# the lease-file / marker reads to one or two round-trips.
_READ_CHUNK = 256 * 1024


def _resolve_domain(client: LibvirtClient, backend_name: str) -> Any:
    from testrange.drivers.libvirt._vm import _resolve_domain as resolve

    return resolve(client, backend_name)


def _agent_command(client: LibvirtClient, dom: Any, command: dict[str, Any]) -> Any:
    """Run one QGA command, returning its ``return`` payload (or ``None``).

    Serialized on ``client.call_lock`` so concurrent readiness polls across
    guests (ADR-0023) don't issue overlapping commands on the one shared
    connection; the hold is brief (one command), so the polls' sleeps overlap.
    """
    libvirt_qemu = _import_libvirt_qemu()
    with client.call_lock:
        raw = libvirt_qemu.qemuAgentCommand(dom, json.dumps(command), _AGENT_TIMEOUT_S, 0)
    reply = json.loads(raw)
    return reply.get("return")


def make_execute(client: LibvirtClient, backend_name: str) -> GuestExec:
    dom = _resolve_domain(client, backend_name)

    def _execute(
        argv: Sequence[str], *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        del cwd  # QGA exec has no working-directory concept
        argv = list(argv)
        start = time.monotonic()
        try:
            ret = _agent_command(
                client,
                dom,
                {
                    "execute": "guest-exec",
                    "arguments": {"path": argv[0], "arg": argv[1:], "capture-output": True},
                },
            )
            pid = ret["pid"]
        except Exception as e:
            raise GuestAgentError(f"QGA exec failed on {backend_name!r}: {e}") from e
        while True:
            status = _agent_command(
                client, dom, {"execute": "guest-exec-status", "arguments": {"pid": pid}}
            )
            if status.get("exited"):
                return ExecResult(
                    exit_code=int(status.get("exitcode", -1)),
                    stdout=_b64(status.get("out-data")),
                    stderr=_b64(status.get("err-data")),
                    duration=time.monotonic() - start,
                )
            if time.monotonic() - start > timeout:
                raise GuestAgentError(
                    f"QGA exec of {argv!r} timed out after {timeout}s on {backend_name!r}"
                )
            time.sleep(_POLL_INTERVAL_S)

    return _execute


def make_read_file(client: LibvirtClient, backend_name: str) -> GuestReadFile:
    dom = _resolve_domain(client, backend_name)

    def _read_file(path: str) -> bytes:
        try:
            handle = _agent_command(
                client,
                dom,
                {"execute": "guest-file-open", "arguments": {"path": path, "mode": "r"}},
            )
            chunks: list[bytes] = []
            try:
                while True:
                    res = _agent_command(
                        client,
                        dom,
                        {
                            "execute": "guest-file-read",
                            "arguments": {"handle": handle, "count": _READ_CHUNK},
                        },
                    )
                    chunks.append(_b64(res.get("buf-b64")))
                    if res.get("eof"):
                        break
            finally:
                _agent_command(
                    client, dom, {"execute": "guest-file-close", "arguments": {"handle": handle}}
                )
        except Exception as e:
            raise GuestAgentError(f"QGA file-read {path!r} failed on {backend_name!r}: {e}") from e
        return b"".join(chunks)

    return _read_file


def make_write_file(client: LibvirtClient, backend_name: str) -> GuestWriteFile:
    dom = _resolve_domain(client, backend_name)

    def _write_file(path: str, data: bytes) -> None:
        try:
            handle = _agent_command(
                client,
                dom,
                {"execute": "guest-file-open", "arguments": {"path": path, "mode": "w"}},
            )
            try:
                _agent_command(
                    client,
                    dom,
                    {
                        "execute": "guest-file-write",
                        "arguments": {
                            "handle": handle,
                            "buf-b64": base64.b64encode(data).decode("ascii"),
                        },
                    },
                )
            finally:
                _agent_command(
                    client, dom, {"execute": "guest-file-close", "arguments": {"handle": handle}}
                )
        except Exception as e:
            raise GuestAgentError(f"QGA file-write {path!r} failed on {backend_name!r}: {e}") from e

    return _write_file


def _b64(value: Any) -> bytes:
    """Decode a QGA base64 field; ``None``/empty → ``b""``."""
    if not value:
        return b""
    return base64.b64decode(value)
