"""Preflight findings + report.

Preflight is read-only (PLAN.md decision: side-effect-free invariant).
Findings have two severities — error and warning, no info; informational
state belongs in logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning"]


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
