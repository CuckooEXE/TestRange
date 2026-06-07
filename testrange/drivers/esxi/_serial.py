"""ESXI-8: the ESXi build-result sink (datastore-file-backed serial console).

The build VM carries a virtual serial port backed by a datastore file
(``[ds] <vm>/serial0.log``, attached in ``_vm.create_vm``). The builder writes
its ``TESTRANGE-RESULT:`` record to that console; this module tails the file over
the ``/folder`` HTTPS channel and yields it as the ``read_build_result_sink``
generator the orchestrator reads (ADR-0012, serial-everywhere contract,
base.py).

Two contract points (base.py:401):

- yield a ``b""`` heartbeat on every idle poll so the orchestrator's build-timeout
  watchdog keeps ticking against a silent guest;
- end iteration when the build VM powers off — a guest that powered off without
  emitting ``ok`` is a build failure (crashed mid-provision). We poll the VM
  power state and, once it is off, do one final read (to catch a record written
  just before poweroff) and return.

Incremental: a byte offset advances past consumed data so each poll fetches only
the new tail (``/folder`` Range GET).
"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import TYPE_CHECKING

from testrange._log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.esxi._client import EsxiClient

_log = get_logger(__name__)

# Idle poll cadence: how often a silent guest yields a heartbeat so the
# orchestrator can re-check its build deadline.
_POLL_INTERVAL_S = 1.0


def read_build_result_sink(client: EsxiClient, backend_name: str) -> Generator[bytes, None, None]:
    """Live-stream the build VM's datastore-file serial console.

    Yields raw console ``bytes`` as the file grows, ``b""`` on each idle poll, and
    ends when the VM powers off (after a final drain).
    """
    vim = client.vim
    serial_path = f"{backend_name}/serial0.log"
    offset = 0
    while True:
        chunk = client.folder_read_from(serial_path, offset)
        if chunk:
            offset += len(chunk)
            yield chunk
            continue
        # No new bytes: is the build VM still running?
        vm = client.find_vm(backend_name)
        powered_off = (
            vm is None or vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOff
        )
        if powered_off:
            # Final drain — a record may have landed just before poweroff.
            tail = client.folder_read_from(serial_path, offset)
            if tail:
                yield tail
            return
        yield b""  # heartbeat: hand control back so the deadline check ticks
        time.sleep(_POLL_INTERVAL_S)
