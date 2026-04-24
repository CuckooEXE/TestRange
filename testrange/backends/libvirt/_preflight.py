"""Preflight resource checks for the libvirt orchestrator.

A declared test plan can easily overcommit the host's RAM — a 7 GiB
sum of ``Memory(...)`` allocations on a 16 GiB laptop plus whatever
else is running quietly pushes the system into swap thrash.  The
failure mode is miserable: the desktop freezes, the user hard-reboots,
and any partial libvirt state is left to orphan.

Rather than trust the user (or QEMU's overcommit heuristics) to notice
this in advance, we check before provisioning.  If satisfying the plan
would push ``(total - available + declared) / total`` above
:data:`_DEFAULT_THRESHOLD`, :class:`Orchestrator.__enter__` refuses
with an :class:`OrchestratorError` that names each VM and its
allocation so the user knows what to trim.

Only RAM is checked here; swap is deliberately excluded — we are
trying to prevent swap thrash, not tolerate more of it.  Disk and
vCPU oversubscription are separate concerns and not covered.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from testrange.exceptions import OrchestratorError
from testrange.storage.transport.base import AbstractFileTransport

_DEFAULT_THRESHOLD = 0.85
"""Projected-usage ceiling for the memory preflight, as a fraction
of total RAM.  Crossing this implies at minimum significant page
cache pressure and typically the start of swap use — the failure
mode we're preventing.  Override via ``TESTRANGE_MEMORY_THRESHOLD``."""

_THRESHOLD_ENV = "TESTRANGE_MEMORY_THRESHOLD"


@dataclass(frozen=True)
class MemInfo:
    """Relevant fields from ``/proc/meminfo``, expressed in bytes.

    :param total_bytes: ``MemTotal`` — physical RAM size.
    :param available_bytes: ``MemAvailable`` — bytes the kernel
        estimates can be handed out to a new workload without swapping,
        accounting for reclaimable page cache.  Preferred over
        ``MemFree`` which ignores reclaimable memory.
    """

    total_bytes: int
    available_bytes: int

    @property
    def used_bytes(self) -> int:
        """Effective used RAM = ``total - available``."""
        return self.total_bytes - self.available_bytes


def _parse_meminfo(text: str) -> MemInfo:
    """Parse ``/proc/meminfo`` output into a :class:`MemInfo`.

    :raises OrchestratorError: If the required fields are missing
        (e.g. when pointed at a non-Linux host or a truncated file).
    """
    total_kib: int | None = None
    available_kib: int | None = None
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key == "MemTotal":
            total_kib = int(rest.split()[0])
        elif key == "MemAvailable":
            available_kib = int(rest.split()[0])
        if total_kib is not None and available_kib is not None:
            break
    if total_kib is None or available_kib is None:
        raise OrchestratorError(
            "Could not parse /proc/meminfo: MemTotal and MemAvailable "
            f"are required fields (got total={total_kib!r}, "
            f"available={available_kib!r}).  Non-Linux host?"
        )
    return MemInfo(total_bytes=total_kib * 1024, available_bytes=available_kib * 1024)


def read_meminfo(transport: AbstractFileTransport) -> MemInfo:
    """Read and parse the target host's ``/proc/meminfo`` via *transport*.

    Uses the transport abstraction so the call is identical whether
    the libvirt backend is local (``LocalFileTransport``) or remote
    (``SSHFileTransport``).  Reading ``/proc/meminfo`` over SFTP is
    a cheap one-round-trip operation — the file is a few kilobytes.
    """
    raw = transport.read_bytes("/proc/meminfo")
    return _parse_meminfo(raw.decode("utf-8", errors="replace"))


def declared_gib_per_vm(vms: Sequence[object]) -> dict[str, float]:
    """Return ``{vm.name: declared_gib}`` for each VM in *vms*.

    Uses the libvirt VM's ``_memory_kib`` accessor (which falls back
    to a 2 GiB default when no :class:`~testrange.devices.Memory`
    device is attached) so the preflight agrees with what the domain
    XML will actually request.

    Nested :class:`~testrange.vms.hypervisor_base.AbstractHypervisor`
    VMs are **not** double-counted — their inner VMs run inside the
    hypervisor's allocation, so only the hypervisor's own
    ``Memory(...)`` matters for the host-level budget.
    """
    out: dict[str, float] = {}
    for vm in vms:
        kib = vm._memory_kib()  # type: ignore[attr-defined]
        out[vm.name] = kib / (1024 * 1024)  # type: ignore[attr-defined]
    return out


def _resolve_threshold() -> float:
    """Return the effective threshold, honouring ``TESTRANGE_MEMORY_THRESHOLD``.

    Invalid values (non-numeric, <= 0, > 1) fall back to the default
    with a log-worthy message baked into the exception path — we don't
    silently run a different check than the user asked for.
    """
    raw = os.environ.get(_THRESHOLD_ENV)
    if raw is None:
        return _DEFAULT_THRESHOLD
    try:
        value = float(raw)
    except ValueError as exc:
        raise OrchestratorError(
            f"{_THRESHOLD_ENV}={raw!r} is not a number; expected a "
            "positive float (values > 1.0 effectively disable the check)."
        ) from exc
    if value <= 0:
        raise OrchestratorError(
            f"{_THRESHOLD_ENV}={raw!r} must be > 0."
        )
    return value


def check_memory(
    meminfo: MemInfo,
    declared_gib: dict[str, float],
    threshold: float | None = None,
) -> None:
    """Raise :class:`OrchestratorError` if the plan would push the host
    above *threshold* (default :data:`_DEFAULT_THRESHOLD`, env override
    ``TESTRANGE_MEMORY_THRESHOLD``).

    The error message enumerates every VM with its declared GiB so the
    user can see which one to shrink.
    """
    if threshold is None:
        threshold = _resolve_threshold()
    declared_total_bytes = int(sum(declared_gib.values()) * 1024 * 1024 * 1024)
    projected_bytes = meminfo.used_bytes + declared_total_bytes
    projected = projected_bytes / meminfo.total_bytes if meminfo.total_bytes else 0.0
    if projected < threshold:
        return

    def _gib(b: int) -> str:
        return f"{b / (1024**3):.2f} GiB"

    breakdown = "\n".join(
        f"  - {name}: {gib:.2f} GiB"
        for name, gib in sorted(declared_gib.items())
    ) or "  (none)"
    raise OrchestratorError(
        f"Memory preflight: the declared plan would bring host RAM "
        f"usage to {projected * 100:.1f}% (threshold {threshold * 100:.0f}%).\n"
        f"  Host total:     {_gib(meminfo.total_bytes)}\n"
        f"  Currently used: {_gib(meminfo.used_bytes)}\n"
        f"  Declared total: {_gib(declared_total_bytes)}\n"
        f"Per-VM breakdown (outer layer only; nested inner VMs use the "
        f"hypervisor's allocation):\n{breakdown}\n"
        f"Shrink one or more Memory(...) allocations, or set "
        f"{_THRESHOLD_ENV}=1.0 to bypass."
    )


__all__ = [
    "MemInfo",
    "check_memory",
    "declared_gib_per_vm",
    "read_meminfo",
]
