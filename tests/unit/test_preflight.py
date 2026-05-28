"""Tests for PreflightFinding / PreflightReport and the shared finding gates."""

from __future__ import annotations

from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    managed_build_egress_findings,
)


class TestReport:
    def test_a_finding_makes_the_report_falsy(self) -> None:
        r = PreflightReport(findings=(PreflightFinding(code="x", message="m"),))
        assert bool(r) is False
        assert len(r.findings) == 1

    def test_empty_report_is_truthy(self) -> None:
        assert bool(PreflightReport()) is True

    def test_merge(self) -> None:
        a = PreflightReport(findings=(PreflightFinding(code="a", message="a"),))
        b = PreflightReport(findings=(PreflightFinding(code="b", message="b"),))
        assert len(a.merged(b).findings) == 2

    def test_render(self) -> None:
        r = PreflightReport(
            findings=(
                PreflightFinding(
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
    """The egress gate (CORE-19) is a free function the orchestrator runs.

    It takes the user-declared build switch (lives on the profile) plus the
    driver's :attr:`supports_managed_build_egress` capability flag.
    """

    def test_unsupported_backend_rejects_managed_build_switch(self) -> None:
        findings = managed_build_egress_findings(
            ManagedBuildSwitch(uplink="vmbr9"),
            supports_managed_egress=False,
        )
        assert [f.code for f in findings] == ["managed-build-egress-unsupported"]

    def test_supporting_backend_is_clean(self) -> None:
        assert (
            managed_build_egress_findings(
                ManagedBuildSwitch(uplink="vmbr9"),
                supports_managed_egress=True,
            )
            == ()
        )

    def test_plain_switch_is_clean_even_when_unsupported(self) -> None:
        plain = Switch("b", Network("n"), cidr="10.9.9.0/24", sidecar=Sidecar(dhcp=True))
        assert managed_build_egress_findings(plain, supports_managed_egress=False) == ()

    def test_no_build_switch_is_clean(self) -> None:
        assert managed_build_egress_findings(None, supports_managed_egress=False) == ()
