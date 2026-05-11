"""Tests for PreflightFinding / PreflightReport."""

from __future__ import annotations

from testrange.preflight import PreflightFinding, PreflightReport


class TestReport:
    def test_empty(self) -> None:
        r = PreflightReport()
        assert bool(r) is True
        assert r.errors == ()
        assert r.warnings == ()

    def test_with_error(self) -> None:
        r = PreflightReport(
            findings=(PreflightFinding(severity="error", code="x", message="m"),)
        )
        assert bool(r) is False
        assert len(r.errors) == 1

    def test_warning_only(self) -> None:
        r = PreflightReport(
            findings=(PreflightFinding(severity="warning", code="x", message="m"),)
        )
        assert bool(r) is True
        assert len(r.warnings) == 1

    def test_merge(self) -> None:
        a = PreflightReport(
            findings=(PreflightFinding(severity="error", code="a", message="a"),)
        )
        b = PreflightReport(
            findings=(PreflightFinding(severity="warning", code="b", message="b"),)
        )
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
