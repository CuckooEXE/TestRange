"""Preflight findings + report.

Preflight is read-only by design — no backend writes. Findings have two
severities (error, warning); informational state belongs in logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from testrange.plan import Plan

Severity = Literal["error", "warning"]

NATIVE_AGENT_OPS = frozenset({"execute", "read_file", "write_file"})


@dataclass(frozen=True)
class PreflightFinding:
    """One preflight result entry."""

    severity: Severity
    code: str
    message: str
    fix_hint: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Collected preflight findings."""

    findings: tuple[PreflightFinding, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[PreflightFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[PreflightFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    def __bool__(self) -> bool:
        """True iff there are no error-level findings."""
        return not self.errors

    def merged(self, other: PreflightReport) -> PreflightReport:
        return PreflightReport(findings=self.findings + other.findings)

    def render(self) -> str:
        """Human-readable report text."""
        if not self.findings:
            return "preflight: clean"
        lines = []
        for f in self.findings:
            marker = "ERROR" if f.severity == "error" else "warn "
            lines.append(f"  [{marker}] {f.code}: {f.message}")
            if f.fix_hint:
                lines.append(f"          fix: {f.fix_hint}")
        return "preflight:\n" + "\n".join(lines)


def mgmt_unsupported_findings(plan: Plan) -> tuple[PreflightFinding, ...]:
    """Gate ``Switch(mgmt=True)`` until its cross-backend semantics are settled.

    No driver realizes the mgmt host adapter yet, and what ``.2`` *promises*
    differs by backend (host-reachable only when the orchestrator is on-box;
    ambiguous "which host?" on vCenter+DVS / Proxmox clusters). Rather than
    silently provision an adapter the test runner may not reach, we fail loud
    at preflight. One error finding per offending Switch. See ADR-0009.

    Shared across drivers (like :func:`native_capability_findings`): a backend
    that grows real mgmt support drops the call from its ``preflight``.
    """
    return tuple(
        PreflightFinding(
            severity="error",
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


def native_capability_findings(
    plan: Plan, capabilities: frozenset[str]
) -> tuple[PreflightFinding, ...]:
    """Findings for VMs needing a native-agent op the driver does not declare.

    Shared across drivers: a driver passes its
    ``native_guest_capabilities()`` set and the plan, and gets back an error
    finding per gap, so a missing op fails at preflight rather than mid-run.

    - A VM on a :class:`NativeCommunicator` needs all of
      :data:`NATIVE_AGENT_OPS` (the shim exposes execute/read/write).
    - A VM with a DHCP-addressed NIC needs ``read_file`` — the orchestrator
      reads the per-Switch sidecar's dnsmasq lease file over the native agent
      to discover the lease.
    """
    from testrange.communicators.native import NativeCommunicator
    from testrange.devices.network.base import DHCPAddr

    findings: list[PreflightFinding] = []
    needs_dhcp_discovery = False
    for vm in plan.hypervisor.vms:
        if isinstance(vm.communicator, NativeCommunicator):
            missing = NATIVE_AGENT_OPS - capabilities
            if missing:
                findings.append(
                    PreflightFinding(
                        severity="error",
                        code="native-agent-missing-ops",
                        message=(
                            f"vm {vm.name!r} uses a NativeCommunicator but the driver's "
                            f"native agent does not support {sorted(missing)}"
                        ),
                        fix_hint=(
                            "use an SSHCommunicator, or a backend whose native agent "
                            "supports execute/read_file/write_file"
                        ),
                    )
                )
        if any(isinstance(nic.addr, DHCPAddr) for nic in vm.spec.nics):
            needs_dhcp_discovery = True
    if needs_dhcp_discovery and "read_file" not in capabilities:
        findings.append(
            PreflightFinding(
                severity="error",
                code="dhcp-discovery-unsupported",
                message=(
                    "plan uses DHCP addressing, but the driver's native agent cannot read "
                    "the sidecar lease file (no read_file capability)"
                ),
                fix_hint="use static addressing, or a backend whose native agent can read files",
            )
        )
    return tuple(findings)
