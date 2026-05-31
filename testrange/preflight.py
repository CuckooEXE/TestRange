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
