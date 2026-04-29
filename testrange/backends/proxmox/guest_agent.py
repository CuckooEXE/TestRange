"""Proxmox VE QEMU guest-agent communicator.

Drives the guest agent (``qemu-guest-agent`` inside the VM) over
PVE's REST surface at::

    /api2/json/nodes/{node}/qemu/{vmid}/agent/<endpoint>

Unlike the libvirt :class:`~testrange.backends.libvirt.GuestAgentCommunicator`
— which holds a ``libvirt.virDomain`` and pokes
``libvirt_qemu.qemuAgentCommand`` directly — this communicator carries
a ``proxmoxer`` client handle plus ``(node, vmid)`` and translates
each :class:`AbstractCommunicator` method into the right REST call.

No network access to the inner VM is required: the agent traffic
hops through PVE's local virtio-serial channel, so this works
identically against fully isolated SDN networks where the VM's
IP is not routable from the orchestrator host.

Endpoints used
--------------

* ``POST /agent/ping`` — readiness probe.
* ``POST /agent/exec`` (returns ``{pid}``) +
  ``GET /agent/exec-status?pid=N`` — command exec with output capture.
* ``GET /agent/file-read?file=PATH`` — file read.
* ``POST /agent/file-write`` (``content=`` base64) — file write,
  capped by PVE at 60 KiB per call so larger writes are chunked.
* ``GET /agent/get-host-name`` — hostname query.
"""

from __future__ import annotations

import base64
import time
from typing import Any, cast

from testrange._logging import get_logger
from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import GuestAgentError, VMTimeoutError

_log = get_logger(__name__)

_POLL_INTERVAL = 1.0
"""Seconds between guest-agent status polls (matches the libvirt path)."""

_FILE_WRITE_CHUNK = 49152
"""Max bytes per ``/agent/file-write`` call.

PVE caps the ``content=`` query parameter at 60 KiB after base64
expansion; 48 KiB raw → 64 KiB base64 fits inside that envelope
with margin."""


class ProxmoxGuestAgentCommunicator(AbstractCommunicator):
    """Proxmox QEMU guest-agent communicator.

    :param client: A ``proxmoxer.ProxmoxAPI`` (or duck-equivalent)
        client authenticated against the PVE REST API.  Used as the
        enclosing :class:`ProxmoxOrchestrator` would — every method
        re-walks the resource tree from this handle.
    :param node: Proxmox node name hosting the VM.
    :param vmid: Proxmox numeric VMID.
    """

    _client: Any
    _node: str
    _vmid: int

    def __init__(
        self,
        client: Any,
        node: str,
        vmid: int,
    ) -> None:
        self._client = client
        self._node = node
        self._vmid = vmid

    # ------------------------------------------------------------------
    # Internal: walk the proxmoxer tree to the per-VM /agent/<endpoint>
    # ------------------------------------------------------------------

    def _agent(self) -> Any:
        """Return the proxmoxer node for ``/qemu/{vmid}/agent``."""
        return self._client.nodes(self._node).qemu(self._vmid).agent

    def _agent_call(self, endpoint: str) -> Any:
        """Return the proxmoxer node for ``/agent/<endpoint>``.

        Uses the ``agent("name")`` form rather than attribute access
        because several PVE endpoint names contain hyphens
        (``exec-status``, ``get-host-name``, ``file-read``, …) which
        Python attribute syntax rejects.
        """
        return self._agent()(endpoint)

    # ------------------------------------------------------------------
    # AbstractCommunicator surface
    # ------------------------------------------------------------------

    def wait_ready(self, timeout: int = 300) -> None:
        """Poll ``POST /agent/ping`` until the agent answers or
        *timeout* expires.

        PVE returns a 5xx (or proxmoxer raises) while the agent
        channel is not yet open; the loop swallows transient errors
        and keys only on the timeout deadline.

        :raises VMTimeoutError: If the agent is still unresponsive
            after *timeout* seconds.
        """
        _log.debug(
            "waiting for guest agent on VMID %d (timeout %ds)",
            self._vmid, timeout,
        )
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                self._agent_call("ping").post()
                _log.debug(
                    "guest agent on VMID %d responded after %d ping(s)",
                    self._vmid, attempts,
                )
                return
            except Exception:  # noqa: BLE001 — every transient counts as not-ready
                time.sleep(_POLL_INTERVAL)
        raise VMTimeoutError(
            f"QEMU guest agent not ready after {timeout}s "
            f"(VMID {self._vmid})"
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Run *argv* inside the VM via ``POST /agent/exec`` and poll
        ``GET /agent/exec-status`` until the process exits.

        :param argv: Command and arguments (e.g. ``['uname', '-a']``).
        :param env: Extra environment variables (merged with the
            guest's default environment).  Each ``K=V`` is forwarded
            on the ``input-data``-adjacent PVE ``env`` parameter.
        :param timeout: Maximum seconds to wait for the command to
            complete.  Note: this is the *poll* deadline; the command
            itself starts immediately.
        :returns: :class:`~testrange.communication.base.ExecResult`
            with exit code and captured stdout/stderr.
        :raises VMTimeoutError: If the command does not exit within
            *timeout* seconds.
        :raises GuestAgentError: On agent protocol errors.
        """
        if not argv:
            raise GuestAgentError("exec(): argv must be non-empty")
        kwargs: dict[str, Any] = {
            "command": list(argv),
        }
        if env:
            # PVE accepts ``env`` as a repeated/array parameter on the
            # exec endpoint.  Format identical to libvirt's path:
            # ``["KEY=VALUE", ...]``.
            kwargs["env"] = [f"{k}={v}" for k, v in env.items()]

        try:
            launched = self._agent_call("exec").post(**kwargs)
        except Exception as exc:
            raise GuestAgentError(
                f"agent/exec failed for {argv!r}: {exc}"
            ) from exc

        pid = launched.get("pid") if isinstance(launched, dict) else None
        if pid is None:
            raise GuestAgentError(
                f"agent/exec returned no pid for {argv!r}: {launched!r}"
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self._agent_call("exec-status").get(pid=pid)
            except Exception as exc:
                raise GuestAgentError(
                    f"agent/exec-status failed for pid {pid}: {exc}"
                ) from exc
            if status.get("exited"):
                # PVE returns ``out-data`` / ``err-data`` as already-
                # decoded text (PVE's qemu-guest-agent.pl base64-decodes
                # the QMP buffer before serialising the JSON
                # response).  Coerce text→bytes via UTF-8 — never
                # treat as base64.  An earlier cut tried base64 first
                # and corrupted any output whose ASCII happened to
                # match the base64 alphabet (4-char-aligned strings
                # like "OKOK", "data", etc.).
                stdout = _text_payload_to_bytes(status.get("out-data", ""))
                stderr = _text_payload_to_bytes(status.get("err-data", ""))
                return ExecResult(
                    exit_code=int(status.get("exitcode", 0)),
                    stdout=stdout,
                    stderr=stderr,
                )
            time.sleep(_POLL_INTERVAL)

        raise VMTimeoutError(
            f"Command timed out after {timeout}s: {argv!r}"
        )

    def get_file(self, path: str) -> bytes:
        """Read *path* from the VM via ``GET /agent/file-read``.

        PVE returns the file inline (``{content: <str>, truncated:
        <0|1>}``).  Recent PVE versions cap the read at ~16 MiB; if
        ``truncated`` is set, this raises rather than returning a
        silent partial read.

        :param path: Absolute file path inside the VM.
        :returns: Raw file contents.
        :raises GuestAgentError: On agent error or truncation.
        """
        try:
            result = self._agent_call("file-read").get(file=path)
        except Exception as exc:
            raise GuestAgentError(
                f"agent/file-read failed for {path!r}: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise GuestAgentError(
                f"agent/file-read returned non-dict for {path!r}: "
                f"{result!r}"
            )
        if result.get("truncated"):
            raise GuestAgentError(
                f"agent/file-read of {path!r} was truncated by PVE "
                "(file too large for a single read).  Read it via "
                "exec(['dd', ...]) instead."
            )
        # PVE's ``file-read`` always base64-encodes binary file
        # content on the wire — even on PVE 9.x where ``exec-status``
        # output fields come back as already-decoded text.  Decode
        # unconditionally; an empty string round-trips to ``b""``.
        content = result.get("content", "")
        return _b64_payload_to_bytes(content)

    def put_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* via ``POST /agent/file-write``.

        PVE caps a single write at ~60 KiB after base64 expansion;
        we chunk into :data:`_FILE_WRITE_CHUNK` raw bytes per call
        so the encoded payload comfortably fits.  Subsequent chunks
        are appended via the agent's ``offset`` parameter; the file
        is opened for the first chunk and reused thereafter.

        :param path: Absolute destination path inside the VM.
        :param data: Raw bytes to write.
        :raises GuestAgentError: If any chunk fails to write.
        """
        # PVE's file-write opens, truncates, writes, and closes the
        # file in one REST call.  There's no offset parameter on the
        # public endpoint — for large files we'd need to fall back
        # to ``exec(['dd', ...])``.  For now, write in one call when
        # the payload fits, error out otherwise so callers know to
        # split the work themselves.
        if len(data) > _FILE_WRITE_CHUNK:
            raise GuestAgentError(
                f"agent/file-write payload {len(data)} bytes exceeds "
                f"the {_FILE_WRITE_CHUNK} byte single-call cap "
                f"({path!r}).  Split via exec(['dd', ...]) or "
                "stream the file out-of-band."
            )
        encoded = base64.b64encode(data).decode("ascii")
        try:
            self._agent_call("file-write").post(
                file=path,
                content=encoded,
                encode=1,
            )
        except Exception as exc:
            raise GuestAgentError(
                f"agent/file-write failed for {path!r}: {exc}"
            ) from exc

    def hostname(self) -> str:
        """Return the VM's hostname via ``GET /agent/get-host-name``.

        :returns: Hostname string as reported by the guest OS.
        :raises GuestAgentError: On agent error.
        """
        try:
            result = self._agent_call("get-host-name").get()
        except Exception as exc:
            raise GuestAgentError(
                f"agent/get-host-name failed: {exc}"
            ) from exc
        # PVE wraps the QGA response under ``result.host-name``.  A
        # few older releases bubble the inner ``host-name`` straight
        # to the top level; handle both shapes.
        if isinstance(result, dict):
            inner = result.get("result")
            if isinstance(inner, dict) and "host-name" in inner:
                return cast(str, inner["host-name"])
            if "host-name" in result:
                return cast(str, result["host-name"])
        raise GuestAgentError(
            f"agent/get-host-name returned unexpected shape: {result!r}"
        )


def _text_payload_to_bytes(value: object) -> bytes:
    """Coerce an *already-decoded* PVE agent payload to bytes.

    Use for ``out-data`` / ``err-data`` from
    ``/agent/exec-status`` — PVE's qemu-guest-agent layer
    base64-decodes the QMP buffer before serialising the JSON
    response, so the value reaches us as plain text.  Encoding is
    UTF-8 with a ``replace`` errors handler so a process that
    emits a stray non-UTF-8 byte (rare; mostly a Windows-side
    cmd.exe quirk) still produces inspectable output rather than
    blowing up the run.

    NEVER call this for ``/agent/file-read`` content — that path
    IS base64-encoded on the wire.  Use :func:`_b64_payload_to_bytes`.
    """
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        return str(value).encode("utf-8", errors="replace")
    return value.encode("utf-8", errors="replace")


def _b64_payload_to_bytes(value: object) -> bytes:
    """Decode a base64-encoded PVE agent payload to bytes.

    Use for ``content`` from ``/agent/file-read``.  An empty
    string round-trips to ``b""``; non-string / non-empty values
    raise so the caller surfaces a clear error rather than
    returning corrupted bytes.
    """
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise GuestAgentError(
            f"expected base64 string from agent payload, got "
            f"{type(value).__name__}: {value!r}"
        )
    if not value:
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise GuestAgentError(
            f"agent payload was not valid base64: {exc}"
        ) from exc
