"""Preflight checks, findings, and report.

Preflight is read-only by design — no backend writes. A *finding* is a
**blocker**: something that would stop a test from running. There is no
warning/informational tier — that state belongs in logs, not here.

Findings are grouped into named **checks** (:class:`PreflightCheck`) so the
``testrange preflight`` verb can show *what was checked* and its result
(ok / blocked / skipped), not just the blockers. A driver assembles its checks
and returns them via :meth:`PreflightReport.from_checks`; the orchestrator only
ever consults :attr:`PreflightReport.findings` (the flattened blockers) and the
report's truthiness, so the richer model is invisible to the run/build path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from testrange.networks.base import Switch
    from testrange.plan import Plan


@dataclass(frozen=True)
class PreflightFinding:
    """One preflight blocker."""

    code: str
    message: str
    fix_hint: str | None = None


CheckStatus = Literal["ok", "blocked", "skipped"]


@dataclass(frozen=True)
class PreflightCheck:
    """One named preflight check and its result.

    ``ok`` — ran and found no blocker; ``blocked`` — ran and produced one or more
    ``findings``; ``skipped`` — could not run (e.g. a live probe was unavailable),
    which is never itself a blocker. ``detail`` is a short human note (what was
    inspected) shown beside the result.
    """

    name: str
    status: CheckStatus
    findings: tuple[PreflightFinding, ...] = ()
    detail: str | None = None

    @classmethod
    def evaluate(
        cls, name: str, findings: Iterable[PreflightFinding], *, detail: str | None = None
    ) -> PreflightCheck:
        """A check that ran: ``blocked`` if it produced findings, else ``ok``."""
        found = tuple(findings)
        return cls(name=name, status="blocked" if found else "ok", findings=found, detail=detail)

    @classmethod
    def skipped(cls, name: str, detail: str) -> PreflightCheck:
        """A check that could not run (no findings, never a blocker)."""
        return cls(name=name, status="skipped", findings=(), detail=detail)


_STATUS_MARK: dict[CheckStatus, str] = {
    "ok": "[ OK ]",
    "blocked": "[FAIL]",
    "skipped": "[SKIP]",
}


@dataclass(frozen=True)
class PreflightReport:
    """Collected preflight findings (+ the named checks they came from).

    ``findings`` is the flattened list of blockers the orchestrator gates on;
    ``checks`` is the optional richer breakdown the ``preflight`` verb renders. A
    bare ``PreflightReport(findings=...)`` (no checks) stays valid — it is how the
    orchestrator merges the portability-lint layer.
    """

    findings: tuple[PreflightFinding, ...] = field(default_factory=tuple)
    checks: tuple[PreflightCheck, ...] = field(default_factory=tuple)

    @classmethod
    def from_checks(cls, checks: Iterable[PreflightCheck]) -> PreflightReport:
        """Build a report from named checks, flattening their findings."""
        collected = tuple(checks)
        flat = tuple(f for c in collected for f in c.findings)
        return cls(findings=flat, checks=collected)

    def __bool__(self) -> bool:
        """True iff there are no findings (preflight is clean)."""
        return not self.findings

    def merged(self, other: PreflightReport) -> PreflightReport:
        return PreflightReport(
            findings=self.findings + other.findings,
            checks=self.checks + other.checks,
        )

    def render(self) -> str:
        """Human-readable report of the **blockers** (the run/build failure text)."""
        if not self.findings:
            return "preflight: clean"
        lines = []
        for f in self.findings:
            lines.append(f"  [ERROR] {f.code}: {f.message}")
            if f.fix_hint:
                lines.append(f"          fix: {f.fix_hint}")
        return "preflight:\n" + "\n".join(lines)

    def render_full(self) -> str:
        """Every check with its result + the blockers under it (the ``preflight`` verb)."""
        blockers = len(self.findings)
        header = "preflight: clean" if not blockers else f"preflight: {blockers} blocker(s)"
        lines: list[str] = []
        covered: set[int] = set()
        for check in self.checks:
            suffix = f" — {check.detail}" if check.detail else ""
            lines.append(f"  {_STATUS_MARK[check.status]} {check.name}{suffix}")
            for f in check.findings:
                covered.add(id(f))
                lines.append(f"         {f.code}: {f.message}")
                if f.fix_hint:
                    lines.append(f"         fix: {f.fix_hint}")
        # Findings not attached to a named check (e.g. a merged findings-only
        # report) must still be shown — never silently drop a blocker.
        for f in self.findings:
            if id(f) in covered:
                continue
            lines.append(f"  {_STATUS_MARK['blocked']} {f.code}: {f.message}")
            if f.fix_hint:
                lines.append(f"         fix: {f.fix_hint}")
        return header + ("\n" + "\n".join(lines) if lines else "")


@dataclass(frozen=True)
class HostCapacity:
    """The live resource ceiling of the host a plan will run on (CORE-84).

    ``memory_mb`` is total physical RAM in MiB; ``logical_cpus`` is the host's
    logical processor count; ``storage_free_gb`` is free space (GiB) in the
    backing store the plan's pools carve from. Any dimension a backend cannot
    cheaply report is ``None`` and skipped by :func:`resource_findings`. A driver
    returns ``None`` from :meth:`~testrange.drivers.base.HypervisorDriver.host_capacity`
    when the whole probe is unavailable — preflight then skips the resource gate
    rather than blocking on a missing measurement.
    """

    memory_mb: int | None = None
    logical_cpus: int | None = None
    storage_free_gb: int | None = None


def describe_capacity(capacity: HostCapacity) -> str:
    """A one-line summary of the dimensions a :class:`HostCapacity` reports."""
    parts: list[str] = []
    if capacity.memory_mb is not None:
        parts.append(f"{capacity.memory_mb} MiB RAM")
    if capacity.logical_cpus is not None:
        parts.append(f"{capacity.logical_cpus} vCPUs")
    if capacity.storage_free_gb is not None:
        parts.append(f"{capacity.storage_free_gb} GiB free")
    return ", ".join(parts) if parts else "no dimensions reported"


def resource_findings(plan: Plan, capacity: HostCapacity) -> tuple[PreflightFinding, ...]:
    """Reject plans whose resource demand cannot fit the host (CORE-84).

    Each dimension is checked only when the host reports it. Memory has two
    blockers: a single VM larger than the whole host (the impossible ask), and an
    aggregate that cannot be co-resident. vCPU is per-VM only (over-commit is
    normal; *more vCPUs than the host has logical CPUs* is the impossible case).
    Storage is per-pool against the backing store's free space.
    """
    vms = plan.hypervisor.declared_vms
    out: list[PreflightFinding] = []

    if capacity.memory_mb is not None:
        for vm in vms:
            requested = vm.spec.memory.size_mb
            if requested > capacity.memory_mb:
                out.append(
                    PreflightFinding(
                        code="insufficient-memory",
                        message=(
                            f"vm {vm.spec.name!r} requests {requested} MiB of memory but the "
                            f"host has only {capacity.memory_mb} MiB"
                        ),
                        fix_hint=(
                            "lower the VM's Memory(size_mb=...) or run against a larger host"
                        ),
                    )
                )
        total = sum(vm.spec.memory.size_mb for vm in vms)
        if vms and total > capacity.memory_mb:
            out.append(
                PreflightFinding(
                    code="insufficient-memory-aggregate",
                    message=(
                        f"the plan's VMs request {total} MiB of memory in total but the host "
                        f"has only {capacity.memory_mb} MiB; they cannot be co-resident"
                    ),
                    fix_hint=(
                        "lower per-VM Memory(size_mb=...), reduce the VM count, or run against "
                        "a larger host"
                    ),
                )
            )

    if capacity.logical_cpus is not None:
        for vm in vms:
            requested = vm.spec.cpu.count
            if requested > capacity.logical_cpus:
                out.append(
                    PreflightFinding(
                        code="insufficient-vcpus",
                        message=(
                            f"vm {vm.spec.name!r} requests {requested} vCPUs but the host has "
                            f"only {capacity.logical_cpus} logical CPUs"
                        ),
                        fix_hint=(
                            "lower the VM's CPU(count=...) or run against a host with more CPUs"
                        ),
                    )
                )

    if capacity.storage_free_gb is not None:
        for pool in plan.hypervisor.declared_pools:
            if pool.size_gb > capacity.storage_free_gb:
                out.append(
                    PreflightFinding(
                        code="insufficient-storage",
                        message=(
                            f"pool {pool.name!r} needs {pool.size_gb} GiB but the backing store "
                            f"has only {capacity.storage_free_gb} GiB free"
                        ),
                        fix_hint=("lower the pool size_gb or point the driver at a larger store"),
                    )
                )

    return tuple(out)


def resource_check(plan: Plan, capacity: HostCapacity | None) -> PreflightCheck:
    """The ``host-resources`` check: skipped when capacity is unknown.

    A driver passes the result of its (defensive) ``host_capacity()`` probe; a
    ``None`` means the host could not be introspected, so the gate is skipped
    rather than turned into a false blocker.
    """
    if capacity is None:
        return PreflightCheck.skipped("host-resources", "host capacity unavailable")
    return PreflightCheck.evaluate(
        "host-resources", resource_findings(plan, capacity), detail=describe_capacity(capacity)
    )


def preflight_switches(plan: Plan, build_switch: Switch | None) -> list[Switch]:
    """The switches a driver's preflight sweeps: run-phase switches + the build one.

    ``build_switch`` is ``None`` for a run that will not build — a cache-only run
    (``require_cache``), such as a nested inner run whose disks were already warmed
    on L0. That run never realizes its build switch, so it has no live host
    resources (uplink pNIC/bridge, CIDR) to validate and is excluded from every
    preflight check. A concrete build switch is appended exactly as before.
    """
    extra = [build_switch] if build_switch is not None else []
    return [*plan.hypervisor.declared_switches, *extra]


def unknown_uplink_findings(
    switches: Iterable[Switch],
    uplinks: Mapping[str, str],
    *,
    profile_hint: str = "the connection profile",
) -> tuple[PreflightFinding, ...]:
    """Returns one finding per ``switch`` whose ``uplink`` isn't a key in ``uplinks``."""
    out: list[PreflightFinding] = []
    for sw in switches:
        if sw.uplink is not None and sw.uplink not in uplinks:
            out.append(
                PreflightFinding(
                    code="unknown-uplink",
                    message=(
                        f"switch {sw.name!r} uses uplink {sw.uplink!r}, but {profile_hint} "
                        f"maps no such uplink (known: {sorted(uplinks)})"
                    ),
                    fix_hint=(
                        f'add `{sw.uplink} = "<host-iface>"` under the profile\'s [uplinks] '
                        f"table, or change the switch's uplink= to a mapped name"
                    ),
                )
            )
    return tuple(out)


def builder_origin_findings(plan: Plan) -> tuple[PreflightFinding, ...]:
    """Returns one finding per VM whose builder declares neither an OS-disk base
    image (:meth:`Builder.os_disk_base`) nor a boot medium
    (:meth:`Builder.boot_media`) — it has no way to produce an OS disk."""
    out: list[PreflightFinding] = []
    for vm in plan.hypervisor.declared_vms:
        builder = vm.builder
        if builder.os_disk_base() is None and builder.boot_media() is None:
            out.append(
                PreflightFinding(
                    code="no-os-disk-origin",
                    message=(
                        f"vm {vm.spec.name!r}: builder {type(builder).__name__} declares "
                        "neither an OS-disk base image (os_disk_base) nor a boot medium "
                        "(boot_media) — it cannot populate an OS disk"
                    ),
                    fix_hint=(
                        "use an image-based builder (e.g. CloudInitBuilder with base=...) "
                        "or an installer-based builder that returns a boot_media()"
                    ),
                )
            )
    return tuple(out)


def unsupported_firmware_findings(
    plan: Plan, supported: Iterable[str], *, driver_name: str
) -> tuple[PreflightFinding, ...]:
    """Returns one finding per VM whose ``spec.firmware`` is not in ``supported``
    (the firmware set the ``driver_name`` backend can realize)."""
    supported_set = frozenset(supported)
    out: list[PreflightFinding] = []
    for vm in plan.hypervisor.declared_vms:
        fw = vm.spec.firmware
        if fw not in supported_set:
            out.append(
                PreflightFinding(
                    code="unsupported-firmware",
                    message=(
                        f"vm {vm.spec.name!r} requests firmware {fw!r}, but the "
                        f"{driver_name} backend realizes only {sorted(supported_set)}"
                    ),
                    fix_hint=(
                        f"set VMSpec.firmware to one of {sorted(supported_set)}, or run "
                        "the plan against a backend that realizes the requested firmware"
                    ),
                )
            )
    return tuple(out)


def mgmt_unsupported_findings(plan: Plan) -> tuple[PreflightFinding, ...]:
    """Returns one finding per ``Switch`` with ``mgmt=True`` — no backend realizes
    the mgmt host adapter yet and its cross-backend semantics are unsettled
    (ADR-0009)."""
    return tuple(
        PreflightFinding(
            code="mgmt-unsupported",
            message=(
                f"switch {sw.name!r} sets mgmt=True, but no backend realizes the "
                "mgmt host adapter yet and its cross-backend semantics are unsettled"
            ),
            fix_hint=(
                "drop mgmt=True for now; see ADR-0009 (mgmt switch semantics). "
                "Use uplink+nat for guest egress, or reach guests over their "
                "static/DHCP addresses"
            ),
        )
        for sw in plan.hypervisor.declared_switches
        if sw.mgmt
    )
