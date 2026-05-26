"""Tests for PreflightFinding / PreflightReport and shared finding helpers."""

from __future__ import annotations

from testrange import Plan
from testrange.drivers.mock import MockHypervisor
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    managed_build_egress_findings,
)


class TestReport:
    def test_with_error(self) -> None:
        r = PreflightReport(findings=(PreflightFinding(severity="error", code="x", message="m"),))
        assert bool(r) is False
        assert len(r.errors) == 1

    def test_warning_only(self) -> None:
        r = PreflightReport(findings=(PreflightFinding(severity="warning", code="x", message="m"),))
        assert bool(r) is True
        assert len(r.warnings) == 1

    def test_merge(self) -> None:
        a = PreflightReport(findings=(PreflightFinding(severity="error", code="a", message="a"),))
        b = PreflightReport(findings=(PreflightFinding(severity="warning", code="b", message="b"),))
        merged = a.merged(b)
        assert len(merged.findings) == 2

    def test_render(self) -> None:
        r = PreflightReport(
            findings=(
                PreflightFinding(
                    severity="error",
                    code="cache_miss",
                    message="not in cache",
                    fix_hint="testrange cache add ...",
                ),
            )
        )
        text = r.render()
        assert "cache_miss" in text
        assert "ERROR" in text
        assert "fix:" in text


class TestManagedBuildEgressFindings:
    def _plan(self, build_switch: object) -> Plan:
        return Plan(MockHypervisor(build_switch=build_switch), name="t")  # type: ignore[arg-type]

    def test_managed_unsupported_is_error(self) -> None:
        findings = managed_build_egress_findings(
            self._plan(ManagedBuildSwitch(uplink="vmbr9")), supported=False
        )
        assert [f.code for f in findings] == ["managed-build-egress-unsupported"]
        assert findings[0].severity == "error"

    def test_managed_supported_is_clean(self) -> None:
        assert (
            managed_build_egress_findings(
                self._plan(ManagedBuildSwitch(uplink="vmbr9")), supported=True
            )
            == ()
        )

    def test_plain_switch_is_clean_even_when_unsupported(self) -> None:
        plain = Switch("b", Network("n"), cidr="10.9.9.0/24", sidecar=Sidecar(dhcp=True))
        assert managed_build_egress_findings(self._plan(plain), supported=False) == ()

    def test_no_build_switch_is_clean(self) -> None:
        assert managed_build_egress_findings(self._plan(None), supported=False) == ()
