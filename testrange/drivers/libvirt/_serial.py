"""Build-result sink for the libvirt backend (BACKEND-1.B).

The build phase keys success on a ``TESTRANGE-RESULT:`` record the builder writes
to the guest's serial console (ADR-0012). On libvirt the domain's
``<serial type='unix' mode='connect'>`` makes QEMU connect to a unix socket the
driver already listens on (opened in ``create_vm``); this generator accepts that
connection and live-tails the raw console bytes.

Per the ABC sink contract: an idle interval yields a ``b""`` heartbeat (so the
orchestrator's build-timeout watchdog keeps ticking against a silent guest), and
iteration ends when the socket closes (EOF — the build VM powered off / QEMU hung
up). No websocket/termproxy framing — it is a plain ``AF_UNIX`` byte stream, the
most direct serial transport of any backend.

The orchestrator wraps this in ``contextlib.closing``, so the accepted connection
is released via the generator's ``finally`` even when the loop breaks early on a
record. Functions take the live :class:`LibvirtClient`; unit tests inject a
duck-typed fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Generator

    from testrange.drivers.libvirt._conn import LibvirtClient

_log = get_logger(__name__)

# How long to wait for QEMU to connect to the pre-bound listener. The connection
# is usually already in the backlog (the listener exists before the domain
# starts), but the QEMU process still has to spawn; be generous.
_ACCEPT_TIMEOUT_S = 60.0

# Per-read timeout: the cadence at which a silent guest yields a heartbeat so the
# orchestrator can re-check its build-timeout deadline.
_RECV_TIMEOUT_S = 1.0

_CHUNK = 1 << 16


def read_build_result_sink(
    client: LibvirtClient, backend_name: str
) -> Generator[bytes, None, None]:
    """Live-stream a build VM's serial console as the build-result sink."""
    try:
        conn = client.accept_serial(backend_name, timeout=_ACCEPT_TIMEOUT_S)
    except TimeoutError as e:
        raise DriverError(
            f"serial console for {backend_name!r}: QEMU never connected within "
            f"{_ACCEPT_TIMEOUT_S:.0f}s; build result not determined"
        ) from e
    conn.settimeout(_RECV_TIMEOUT_S)
    try:
        while True:
            try:
                chunk = conn.recv(_CHUNK)
            except TimeoutError:
                yield b""  # heartbeat: nothing yet, re-check the deadline
                continue
            if not chunk:
                return  # EOF: VM powered off / QEMU hung up
            yield chunk
    finally:
        try:
            conn.close()
        except OSError as e:  # pragma: no cover - best-effort teardown
            _log.debug("serial socket close failed: %s", e)
