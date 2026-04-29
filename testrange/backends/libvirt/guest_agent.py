"""QEMU Guest Agent communicator.

Communicates with a running VM via the QEMU guest agent (``qemu-guest-agent``)
over a ``virtio-serial`` channel.  No network access to the VM is required.

The guest agent must be installed and running inside the VM.  For Linux guests
this is handled automatically by :class:`~testrange.vms.cloud_init.CloudInitBuilder`
which adds ``qemu-guest-agent`` to the package list and enables the service.

Protocol reference:
    https://qemu.readthedocs.io/en/latest/interop/qemu-ga-ref.html
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from typing import Any, cast

import libvirt
import libvirt_qemu

from testrange._logging import get_logger
from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import GuestAgentError, VMTimeoutError

_log = get_logger(__name__)

_POLL_INTERVAL = 1.0
"""Seconds to wait between guest-agent status polls."""

_CHUNK_SIZE = 65536
"""Bytes to read per ``guest-file-read`` call."""

_ERROR_HANDLER_LOCK = threading.Lock()
"""Serialise the ``libvirt.registerErrorHandler`` install + restore.

``libvirt.registerErrorHandler`` is process-global — there's no
per-thread or per-connection handler scope.  Under
``run_tests(..., concurrency=N)`` two threads racing on the same
install/restore pair would have the second-finishing thread restore
the default handler while the first was still polling, dumping the
expected ``Guest agent is not responding`` errors that the silencer
is supposed to swallow.  The counter below tracks active silencers
so the handler stays installed until the last one exits."""

_error_handler_active: int = 0


@contextlib.contextmanager
def _silenced_libvirt_errors() -> Any:
    """Context manager that installs a no-op libvirt error handler.

    Reference-counted so concurrent ``wait_ready`` calls share one
    handler install instead of racing on register/restore.  The
    handler is only restored when the last in-flight silencer
    exits the ``with`` block.
    """
    global _error_handler_active
    with _ERROR_HANDLER_LOCK:
        if _error_handler_active == 0:
            libvirt.registerErrorHandler(
                lambda _ctx, _err: None, None,
            )
        _error_handler_active += 1
    try:
        yield
    finally:
        with _ERROR_HANDLER_LOCK:
            _error_handler_active -= 1
            if _error_handler_active == 0:
                # libvirt's stub annotates ``f`` as a required
                # callback, but ``None`` is the documented way
                # to reset to the default stderr handler.
                libvirt.registerErrorHandler(None, None)  # pyright: ignore[reportArgumentType]

class GuestAgentCommunicator(AbstractCommunicator):
    """QEMU Guest Agent communicator backed by libvirt.

    Uses :meth:`libvirt.virDomain.qemuAgentCommand` to send JSON-RPC
    commands to the guest agent daemon running inside the VM.

    :param domain: An active ``libvirt.virDomain`` object for the target VM.
    """

    _dom: libvirt.virDomain
    """Active libvirt domain object used to send guest agent commands."""

    def __init__(self, domain: libvirt.virDomain) -> None:
        self._dom = domain

    def _send(
        self,
        execute: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        """Send a single JSON-RPC command to the guest agent.

        :param execute: Guest agent command name (e.g. ``'guest-exec'``).
        :param arguments: Optional dict of command arguments.
        :param timeout: libvirt-level timeout in seconds.
        :returns: The ``return`` field of the JSON response.
        :raises GuestAgentError: If the guest agent returns an error, or if
            libvirt raises a :class:`libvirt.libvirtError`.
        """
        payload: dict[str, Any] = {"execute": execute}
        if arguments:
            payload["arguments"] = arguments
        try:
            raw = libvirt_qemu.qemuAgentCommand(
                self._dom,
                json.dumps(payload),
                timeout,
                0,  # flags
            )
        except libvirt.libvirtError as exc:
            raise GuestAgentError(
                f"libvirt error calling {execute!r}: {exc}"
            ) from exc
        response = json.loads(raw)
        if "error" in response:
            err = response["error"]
            raise GuestAgentError(
                f"Guest agent error ({err.get('class', '?')}): {err.get('desc', err)}"
            )
        return response.get("return")

    def wait_ready(self, timeout: int = 120) -> None:
        """Poll ``guest-ping`` until the agent responds or *timeout* expires.

        libvirt raises :class:`libvirt.libvirtError` (``VIR_ERR_AGENT_UNRESPONSIVE``)
        while the agent channel is not yet open; this is silently retried.
        During the poll loop we install a no-op libvirt error handler so
        the expected ``Guest agent is not responding`` messages do not
        flood stderr — the real failure signal is the timeout raised here.

        :param timeout: Maximum seconds to wait.
        :raises VMTimeoutError: If the agent is still unresponsive after
            *timeout* seconds.
        """
        _log.debug("waiting for guest agent on %r (timeout %ds)", self._dom.name(), timeout)
        attempts = 0
        with _silenced_libvirt_errors():
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                attempts += 1
                try:
                    self._send("guest-ping", timeout=5)
                    _log.debug(
                        "guest agent on %r responded after %d ping(s)",
                        self._dom.name(),
                        attempts,
                    )
                    return
                except (GuestAgentError, libvirt.libvirtError):
                    time.sleep(_POLL_INTERVAL)
            raise VMTimeoutError(
                f"QEMU guest agent not ready after {timeout}s "
                f"(domain: {self._dom.name()!r})"
            )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Execute *argv* inside the VM and return captured output.

        Sends ``guest-exec`` then polls ``guest-exec-status`` until the
        process exits.

        :param argv: Command and arguments (e.g. ``['uname', '-n']``).
        :param env: Extra environment variables (merged with the guest's
            default environment).
        :param timeout: Maximum seconds to wait for the command to complete.
        :returns: :class:`~testrange.communication.base.ExecResult` with exit
            code and captured stdout/stderr.
        :raises VMTimeoutError: If the command does not exit within *timeout*
            seconds.
        :raises GuestAgentError: On agent protocol errors.
        """
        args: dict[str, Any] = {
            "path": argv[0],
            "arg": argv[1:],
            "capture-output": True,
        }
        if env:
            args["env"] = [f"{k}={v}" for k, v in env.items()]

        result = self._send("guest-exec", args, timeout=30)
        pid: int = result["pid"]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._send("guest-exec-status", {"pid": pid}, timeout=30)
            if status.get("exited"):
                stdout = base64.b64decode(status.get("out-data", ""))
                stderr = base64.b64decode(status.get("err-data", ""))
                return ExecResult(
                    exit_code=status.get("exitcode", 0),
                    stdout=stdout,
                    stderr=stderr,
                )
            time.sleep(_POLL_INTERVAL)

        raise VMTimeoutError(
            f"Command timed out after {timeout}s: {argv!r}"
        )

    def get_file(self, path: str) -> bytes:
        """Read *path* from the VM's filesystem.

        Uses ``guest-file-open`` / ``guest-file-read`` / ``guest-file-close``.

        :param path: Absolute file path inside the VM.
        :returns: Raw file contents.
        :raises GuestAgentError: If the file cannot be opened or read.
        """
        handle: int = self._send(
            "guest-file-open", {"path": path, "mode": "r"}
        )
        chunks: list[bytes] = []
        try:
            while True:
                chunk = self._send(
                    "guest-file-read",
                    {"handle": handle, "count": _CHUNK_SIZE},
                )
                if chunk.get("buf-b64"):
                    chunks.append(base64.b64decode(chunk["buf-b64"]))
                if chunk.get("eof"):
                    break
        finally:
            try:
                self._send("guest-file-close", {"handle": handle})
            except GuestAgentError:
                pass  # best-effort close
        return b"".join(chunks)

    def put_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* inside the VM.

        Uses ``guest-file-open`` / ``guest-file-write`` / ``guest-file-close``.

        :param path: Absolute destination path inside the VM.
        :param data: Raw bytes to write.
        :raises GuestAgentError: If the file cannot be opened or written.
        """
        handle: int = self._send(
            "guest-file-open", {"path": path, "mode": "wb+"}
        )
        try:
            for offset in range(0, len(data), _CHUNK_SIZE):
                chunk = data[offset: offset + _CHUNK_SIZE]
                self._send(
                    "guest-file-write",
                    {
                        "handle": handle,
                        "buf-b64": base64.b64encode(chunk).decode(),
                        "count": len(chunk),
                    },
                )
        finally:
            try:
                self._send("guest-file-close", {"handle": handle})
            except GuestAgentError:
                pass

    def hostname(self) -> str:
        """Return the VM's hostname via ``guest-get-host-name``.

        :returns: Hostname string as reported by the guest OS.
        :raises GuestAgentError: On agent error.
        """
        result = self._send("guest-get-host-name")
        return cast(str, result["host-name"])

    def get_interfaces(self) -> list[dict[str, Any]]:
        """Return network interface information from the guest.

        Calls ``guest-network-get-interfaces``.

        :returns: List of interface dicts as returned by the guest agent.
            Each dict has at minimum a ``name`` key and an ``ip-addresses``
            list.
        :raises GuestAgentError: On agent error.
        """
        return cast(list[dict[str, Any]], self._send("guest-network-get-interfaces"))

    def guest_info(self) -> dict[str, Any]:
        """Return basic guest agent metadata via ``guest-info``.

        :returns: Dict with at minimum a ``version`` key.
        :raises GuestAgentError: On agent error.
        """
        return cast(dict[str, Any], self._send("guest-info"))
