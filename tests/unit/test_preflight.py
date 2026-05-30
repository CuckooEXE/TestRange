"""Tests for PreflightFinding / PreflightReport and the shared finding gates."""

from __future__ import annotations

from testrange.networks import Network, Sidecar, Switch
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    unknown_uplink_findings,
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


class TestUnknownUplinkFindings:
    """The uplink-resolution gate (ADR-0016): a Switch.uplink logical name the
    bound profile's [uplinks] map doesn't define is rejected at preflight. Shared
    across drivers; each calls it with its own resolved map + the run/build
    switches.
    """

    def _sw(self, name: str, uplink: str | None) -> Switch:
        return Switch(
            name,
            Network(f"{name}-net"),
            cidr="10.9.9.0/24",
            uplink=uplink,
            sidecar=Sidecar(dhcp=True, dns=True, nat=True) if uplink else Sidecar(dhcp=True),
        )

    def test_mapped_name_is_clean(self) -> None:
        sw = self._sw("a", "egress")
        assert unknown_uplink_findings([sw], {"egress": "vmbr9"}) == ()

    def test_unmapped_name_is_flagged(self) -> None:
        sw = self._sw("a", "egress")
        findings = unknown_uplink_findings([sw], {"other": "vmbr3"})
        assert [f.code for f in findings] == ["unknown-uplink"]
        assert "egress" in findings[0].message

    def test_no_uplink_is_clean(self) -> None:
        sw = self._sw("a", None)
        assert unknown_uplink_findings([sw], {}) == ()

    def test_one_finding_per_offending_switch(self) -> None:
        switches = [self._sw("a", "x"), self._sw("b", "y"), self._sw("c", "egress")]
        findings = unknown_uplink_findings(switches, {"egress": "vmbr9"})
        assert len(findings) == 2  # a and b unmapped; c is fine
