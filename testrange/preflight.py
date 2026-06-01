"""Preflight findings + report.

Preflight is read-only by design — no backend writes. Every finding is a
*blocker*: something that would stop a test from running. There is no
warning/informational tier — that state belongs in logs, not here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.networks.base import Switch
    from testrange.plan import Plan


@dataclass(frozen=True)
class PreflightFinding:
    """One preflight blocker."""

    code: str
    message: str
    fix_hint: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Collected preflight findings. Every finding is a blocker."""

    findings: tuple[PreflightFinding, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        """True iff there are no findings (preflight is clean)."""
        return not self.findings

    def merged(self, other: PreflightReport) -> PreflightReport:
        return PreflightReport(findings=self.findings + other.findings)

    def render(self) -> str:
        """Human-readable report text."""
        if not self.findings:
            return "preflight: clean"
        lines = []
        for f in self.findings:
            lines.append(f"  [ERROR] {f.code}: {f.message}")
            if f.fix_hint:
                lines.append(f"          fix: {f.fix_hint}")
        return "preflight:\n" + "\n".join(lines)


def unknown_uplink_findings(
    switches: Iterable[Switch],
    uplinks: Mapping[str, str],
    *,
    profile_hint: str = "the connection profile",
) -> tuple[PreflightFinding, ...]:
    """Reject a ``Switch.uplink`` whose logical name the bound profile doesn't map.

    ``Switch.uplink`` is a logical name (ADR-0016) the driver resolves against the
    profile's ``[uplinks]`` map to a host iface. A name the bound profile does not
    map cannot be realized, so it fails loud here rather than at ``create_switch``.
    Shared across drivers; each calls it from ``preflight`` with its own resolved
    ``uplinks`` and the run + build switches. One finding per offending Switch.
    """
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
    """Reject a VM whose builder declares no OS-disk origin (BUILD-1).

    Every builder must populate the OS disk one of two ways: an image base
    (:meth:`Builder.os_disk_base`) the orchestrator uploads + grows, or a boot
    medium (:meth:`Builder.boot_media`) it boots against a freshly-materialized
    blank disk (installer-origin, ADR-0010 §6). A builder that returns ``None``
    from *both* has no way to produce a disk; that is a plan misconfiguration,
    so it fails loud here — before any backend resource stands up — rather than
    at the build probe. Backend-agnostic: every driver calls it from
    ``preflight``. Both seams are pure (no I/O), so this stays read-only.
    """
    out: list[PreflightFinding] = []
    for vm in plan.hypervisor.vms:
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
    """Reject a VM whose ``spec.firmware`` the bound backend cannot realize.

    ``spec.firmware`` (``bios``/``uefi``) must be reproduced identically at build
    and run — a UEFI-installed disk panics under SeaBIOS (BUILD-1b) — so a backend
    that cannot realize the requested firmware must fail loud here, before any
    resource stands up, rather than define a VM under the wrong firmware. Each
    driver passes the firmware set it realizes; a backend that grows support
    widens its set. Backend-agnostic helper; one finding per offending VM.
    """
    supported_set = frozenset(supported)
    out: list[PreflightFinding] = []
    for vm in plan.hypervisor.vms:
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
    """Gate ``Switch(mgmt=True)`` until its cross-backend semantics are settled.

    No driver realizes the mgmt host adapter yet, and what ``.2`` *promises*
    differs by backend (host-reachable only when the orchestrator is on-box;
    ambiguous "which host?" on vCenter+DVS / Proxmox clusters). Rather than
    silently provision an adapter the test runner may not reach, we fail loud
    at preflight. One finding per offending Switch. See ADR-0009.

    Shared across drivers: a backend that grows real mgmt support drops the
    call from its ``preflight``.
    """
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
        for sw in plan.hypervisor.all_switches
        if sw.mgmt
    )
