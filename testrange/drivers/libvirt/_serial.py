"""Build-result sink for the libvirt backend (BACKEND-1.B, reshaped by BACKEND-5).

The build phase keys success on a ``TESTRANGE-RESULT:`` record the builder writes
to the guest's serial console (ADR-0012). Every libvirt guest carries a
``<serial type='pty'>`` (see ``_vm``); this generator live-tails it host-side via
``virDomainOpenConsole`` + a non-blocking ``virStream`` over the *existing*
libvirt connection. The console bytes ride the connection's own transport, so
nothing host-local is baked into the domain XML and the same code path serves a
local ``qemu:///system`` and a remote ``qemu+ssh://`` daemon (BACKEND-5 — the
former orchestrator-local unix-socket listener path failed ``virDomainCreate``
remotely, since QEMU on the remote host could not stat it). With the socket gone
so is the spoofing surface its CORE-91 uid filter had to guard: there is no
world-connectable path a co-tenant can reach ahead of QEMU to forge a
``TESTRANGE-RESULT`` — reading the console now requires the same libvirt
connection privileges as every other driver operation.

Per the ABC sink contract: an idle interval yields a ``b""`` heartbeat (so the
orchestrator's build-timeout watchdog keeps ticking against a silent guest), and
iteration ends when the console closes (the build VM powered off when done). The
orchestrator wraps this in ``contextlib.closing``, so the stream is released via
the generator's ``finally`` even when the loop breaks early on a record.
Functions take the live :class:`LibvirtClient`; unit tests inject a duck-typed
fake.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers.libvirt._conn import _import_libvirt
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Generator

    from testrange.drivers.libvirt._conn import LibvirtClient

_log = get_logger(__name__)

# How long to keep retrying virDomainOpenConsole before declaring the console
# unreachable. The sink is opened right after start_vm, but openConsole fails
# while the domain isn't running yet (and QEMU's pty may lag the create); be
# generous — the wait heartbeats, so the orchestrator's deadline still ticks.
_OPEN_TIMEOUT_S = 60.0

# Idle cadence: how often a silent guest (or a not-yet-open console) yields a
# b"" heartbeat so the orchestrator can re-check its build-timeout deadline.
# Pacing is the sink's job per the ABC contract — the orchestrator loops
# immediately on a heartbeat, so an unslept would-block loop would busy-spin.
_POLL_INTERVAL_S = 1.0

# Per-recv read size draining the console stream. 64 KiB keeps the call count
# low on a chatty build boot; the poll cadence above bounds latency, so a larger
# buffer costs nothing when the guest is quiet.
_CHUNK = 1 << 16

# virStream.recv's would-block sentinel: on a VIR_STREAM_NONBLOCK stream the
# binding returns the *int* -2 (not bytes) when no data is pending. Verified
# against libvirt-python's virStream.recv: errors raise libvirtError, EOF is
# b"", would-block is -2 passed through from virStreamRecv.
_RECV_WOULD_BLOCK = -2


def _resolve_domain(client: LibvirtClient, backend_name: str) -> Any:
    from testrange.drivers.libvirt._vm import _resolve_domain as resolve

    return resolve(client, backend_name)


def read_build_result_sink(
    client: LibvirtClient, backend_name: str
) -> Generator[bytes, None, None]:
    """Live-stream a build VM's serial console as the build-result sink."""
    libvirt = _import_libvirt()
    dom = _resolve_domain(client, backend_name)
    # Open the console in short retries, yielding a heartbeat between attempts,
    # so the orchestrator can re-check its build deadline *during* the wait —
    # a single blocking wait would otherwise hold off every deadline check
    # until the console comes up (ORCH-15). A fresh stream per attempt: a
    # failed openConsole leaves its stream unused (never finished), and reusing
    # one across attempts is undefined.
    deadline = time.monotonic() + _OPEN_TIMEOUT_S
    while True:
        st = client.raw.newStream(libvirt.VIR_STREAM_NONBLOCK)
        try:
            dom.openConsole(None, st, 0)
            break
        except libvirt.libvirtError as e:
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"serial console for {backend_name!r} did not open within "
                    f"{_OPEN_TIMEOUT_S:.0f}s ({e}); build result not determined"
                ) from e
            yield b""  # heartbeat: still waiting for the console
            time.sleep(_POLL_INTERVAL_S)
    try:
        while True:
            try:
                data = st.recv(_CHUNK)
            except libvirt.libvirtError as e:
                # The stream died under us: the domain powered off / the
                # console was torn down mid-read. That is the *normal* end of
                # a build (the build VM powers itself off after the result),
                # so end iteration rather than raise — the orchestrator judges
                # success by the record it has in hand, not by how the
                # transport closed.
                _log.debug("serial console stream for %s ended: %s", backend_name, e)
                return
            if data == _RECV_WOULD_BLOCK:
                yield b""  # heartbeat: nothing yet, re-check the deadline
                time.sleep(_POLL_INTERVAL_S)
                continue
            if not data:
                return  # EOF: VM powered off, console closed
            yield data
    finally:
        # Best-effort stream release: finish() is the clean handshake, but it
        # fails on a stream that ended in an error/abort state, so fall back
        # to abort() and suppress that too — teardown must never mask the
        # build verdict.
        try:
            st.finish()
        except libvirt.libvirtError:
            with contextlib.suppress(libvirt.libvirtError):
                st.abort()
