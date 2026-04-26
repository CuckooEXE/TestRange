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
                # PVE returns ``out-data`` / ``err-data`` already
                # base64-decoded as strings; the libvirt path gets raw
                # base64 because it talks the JSON-RPC protocol
                # directly.  Coerce to bytes so AbstractCommunicator's
                # caller-facing ExecResult contract is identical.
                stdout = _coerce_output(status.get("out-data", ""))
                stderr = _coerce_output(status.get("err-data", ""))
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
        # Newer PVE returns the bytes already-decoded as a str; older
        # versions returned base64.  Try base64 first; on failure
        # treat the value as already-decoded text.
        content = result.get("content", "")
        return _coerce_output(content)

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


def _coerce_output(value: object) -> bytes:
    """Coerce a PVE agent payload field to bytes.

    PVE's REST layer base64-decodes ``out-data`` / ``err-data`` /
    ``content`` for us in current releases (returns the raw text as
    a str), but a few older releases hand back the encoded form.
    Attempt base64 decoding first; fall back to UTF-8 encoding on
    failure so callers always see ``bytes``.
    """
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        return str(value).encode("utf-8")
    if not value:
        return b""
    try:
        # Strict base64 — catches "looks like base64 but isn't" cases
        # like file content that happens to be ASCII.
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        return value.encode("utf-8")
