"""PVE-17: the Proxmox build-result sink (serial0 over termproxy->vncwebsocket).

PVE serves a guest's ``serial0`` console only over a websocket — there is no
REST GET for serial bytes (RESEARCH.md "PVE-16 spike"). This module turns that
websocket (opened by :meth:`ProxmoxClient.open_serial_websocket`) into the
``read_build_result_sink`` generator the orchestrator tails for the
``TESTRANGE-RESULT:`` record (ADR-0012).

The termproxy stream is **raw PTY bytes** in binary websocket frames (no VNC/RFB
framing, no base64 — PVE-18 addendum), so the generator simply yields each
frame's bytes. On an idle interval it yields a ``b""`` heartbeat so the
orchestrator's build-timeout watchdog keeps ticking against a silent guest, and
sends PVE's app-level keepalive so the termproxy session isn't culled. The
socket is closed on exit (the orchestrator wraps the generator in
``contextlib.closing``, so this runs even when it breaks early on a record).
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.proxmox import _vm
from testrange.drivers.proxmox._client import _import_websocket
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.proxmox._client import ProxmoxClient

_log = get_logger(__name__)

# Per-read timeout: the cadence at which a silent guest yields a heartbeat (so
# the orchestrator can re-check its build-timeout deadline) and how often we
# nudge PVE's idle-connection culler with the app-level keepalive.
_RECV_TIMEOUT_S = 1.0

# PVE's web client periodically sends this app-level frame to keep a termproxy
# session alive through quiet stretches; mirror it during heartbeats.
_KEEPALIVE_FRAME = "2"


def read_build_result_sink(
    client: ProxmoxClient, backend_name: str
) -> Generator[bytes, None, None]:
    """Live-stream a build VM's serial console as the build-result sink.

    Yields raw console ``bytes`` as they arrive, ``b""`` on each idle interval
    (heartbeat), and ends when the console closes (VM powered off / hung up).
    """
    websocket = _import_websocket()
    vmid = _vm.resolve_vmid(client, backend_name)
    ws = client.open_serial_websocket(vmid)
    ws.settimeout(_RECV_TIMEOUT_S)
    try:
        while True:
            try:
                frame = ws.recv()
            except websocket.WebSocketTimeoutException:
                # Idle interval: keep the session alive, hand control back so
                # the caller can re-check its deadline.
                try:
                    ws.send(_KEEPALIVE_FRAME)
                except websocket.WebSocketException as e:
                    # A keepalive-send failure is a *transport* death, not a guest
                    # poweroff. Returning here would exhaust the generator, which
                    # the orchestrator reads as "console closed without ok" =
                    # BuildFailedError — silently failing a possibly-healthy build
                    # on a network blip (PVE-29). Raise a typed error so a transport
                    # failure surfaces as itself, distinct from a build verdict.
                    raise DriverError(
                        f"serial transport for {backend_name!r} failed mid-build "
                        f"(keepalive send error: {e}); build result not determined"
                    ) from e
                yield b""
                continue
            except websocket.WebSocketConnectionClosedException:
                return  # console closed (VM powered off / hung up)
            if not frame:
                # An empty *data* frame (e.g. a keepalive echo) is NOT a close —
                # a real close raises WebSocketConnectionClosedException above.
                # Yield a b"" heartbeat (not a bare ``continue``) so the
                # orchestrator's deadline check still ticks; a steady trickle of
                # empty frames would otherwise busy-spin past the watchdog (PVE-29).
                yield b""
                continue
            yield frame if isinstance(frame, bytes) else frame.encode("utf-8", "replace")
    finally:
        try:
            ws.close()
        except Exception as e:  # pragma: no cover - best-effort teardown
            _log.debug("serial websocket close failed: %s", e)
