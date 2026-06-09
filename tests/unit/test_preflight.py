"""Tests for PreflightFinding / PreflightReport and the shared finding gates."""

from __future__ import annotations

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.builders.base import Builder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.networks import Network, Sidecar, Switch
from testrange.preflight import (
    HostCapacity,
    PreflightCheck,
    PreflightFinding,
    PreflightReport,
    builder_origin_findings,
    describe_capacity,
    preflight_switches,
    resource_check,
    resource_findings,
    unknown_uplink_findings,
    unsupported_firmware_findings,
)
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockHypervisor, OriginlessBuilder


def _plan_with(builder: Builder, *, firmware: str = "bios") -> Plan:
    return Plan(
        "p",
        MockHypervisor(
            networks=[Switch("sw", Network("n"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))],
            pools=[StoragePool("pool1", 16)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="vm",
                        firmware=firmware,
                        devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
                    ),
                    builder=builder,
                    communicator=SSHCommunicator("u"),
                )
            ],
        ),
    )


def _resource_plan(*, vms: list[tuple[str, int, int]], pool_gb: int = 16) -> Plan:
    """A plan whose VMs are ``(name, memory_mb, cpus)`` triples, for resource tests."""
    return Plan(
        "p",
        MockHypervisor(
            networks=[Switch("sw", Network("n"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))],
            pools=[StoragePool("pool1", pool_gb)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name=name,
                        devices=[CPU(cpus), Memory(memory_mb), OSDrive("pool1", 8)],
                    ),
                    builder=CloudInitBuilder(base=CacheEntry("x")),
                    communicator=SSHCommunicator("u"),
                )
                for name, memory_mb, cpus in vms
            ],
        ),
    )


class TestBuilderOriginFindings:
    def test_image_origin_is_clean(self) -> None:
        plan = _plan_with(CloudInitBuilder(base=CacheEntry("x")))
        assert builder_origin_findings(plan) == ()

    def test_no_origin_is_flagged(self) -> None:
        findings = builder_origin_findings(_plan_with(OriginlessBuilder()))
        assert len(findings) == 1
        assert findings[0].code == "no-os-disk-origin"

    def test_installer_origin_is_clean(self) -> None:
        class _InstallerBuilder(OriginlessBuilder):
            def boot_media(self) -> CacheEntry:
                return CacheEntry("installer-iso")

        assert builder_origin_findings(_plan_with(_InstallerBuilder())) == ()


class TestUnsupportedFirmwareFindings:
    """The per-driver firmware-capability gate (BUILD-1b): a VM requesting a
    firmware the bound backend can't realize is rejected at preflight, before any
    resource stands up. Each driver passes the set it realizes."""

    def test_supported_firmware_is_clean(self) -> None:
        plan = _plan_with(CloudInitBuilder(base=CacheEntry("x")), firmware="uefi")
        assert unsupported_firmware_findings(plan, {"bios", "uefi"}, driver_name="D") == ()

    def test_unsupported_firmware_is_flagged(self) -> None:
        plan = _plan_with(CloudInitBuilder(base=CacheEntry("x")), firmware="uefi")
        findings = unsupported_firmware_findings(plan, {"bios"}, driver_name="BiosOnly")
        assert [f.code for f in findings] == ["unsupported-firmware"]
        assert "uefi" in findings[0].message
        assert "BiosOnly" in findings[0].message


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

    def test_render_clean(self) -> None:
        assert PreflightReport().render() == "preflight: clean"


class TestPreflightSwitches:
    """The shared switch-sweep builder (CORE-65): run-phase switches plus the
    transient build switch *only when there is one*. A cache-only run
    (``require_cache``) never realizes its build switch, so the orchestrator
    passes ``None`` and it drops out of every preflight check."""

    _BUILD = Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )

    def test_concrete_build_switch_is_appended(self) -> None:
        plan = _plan_with(CloudInitBuilder(base=CacheEntry("x")))
        switches = preflight_switches(plan, self._BUILD)
        assert switches == [*plan.hypervisor.all_switches, self._BUILD]

    def test_none_build_switch_is_excluded(self) -> None:
        plan = _plan_with(CloudInitBuilder(base=CacheEntry("x")))
        switches = preflight_switches(plan, None)
        assert switches == list(plan.hypervisor.all_switches)
        assert self._BUILD not in switches


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


class TestResourceFindings:
    """The shared host-resource gate (CORE-84): a plan whose demand can't fit the
    host that will run it is rejected before any resource stands up."""

    def test_vm_larger_than_host_memory_is_flagged(self) -> None:
        plan = _resource_plan(vms=[("big", 5_242_880, 1)])  # 5 TiB on a small host
        findings = resource_findings(plan, HostCapacity(memory_mb=32_768))
        codes = {f.code for f in findings}
        assert "insufficient-memory" in codes
        assert "insufficient-memory-aggregate" in codes  # the sole VM is also the sum

    def test_aggregate_memory_flagged_without_per_vm(self) -> None:
        # Neither VM alone exceeds the host, but together they cannot be co-resident.
        plan = _resource_plan(vms=[("a", 600, 1), ("b", 600, 1)])
        findings = resource_findings(plan, HostCapacity(memory_mb=1024))
        codes = [f.code for f in findings]
        assert codes == ["insufficient-memory-aggregate"]

    def test_memory_within_host_is_clean(self) -> None:
        plan = _resource_plan(vms=[("a", 512, 1)])
        assert resource_findings(plan, HostCapacity(memory_mb=4096)) == ()

    def test_vcpus_over_host_is_flagged(self) -> None:
        plan = _resource_plan(vms=[("a", 512, 8)])
        findings = resource_findings(plan, HostCapacity(logical_cpus=4))
        assert [f.code for f in findings] == ["insufficient-vcpus"]

    def test_pool_over_storage_is_flagged(self) -> None:
        plan = _resource_plan(vms=[("a", 512, 1)], pool_gb=64)
        findings = resource_findings(plan, HostCapacity(storage_free_gb=16))
        assert [f.code for f in findings] == ["insufficient-storage"]

    def test_unreported_dimensions_are_skipped(self) -> None:
        # An all-None capacity compares nothing — every dimension is unknown.
        plan = _resource_plan(vms=[("a", 5_242_880, 64)], pool_gb=999)
        assert resource_findings(plan, HostCapacity()) == ()


class TestResourceCheck:
    def test_none_capacity_is_skipped(self) -> None:
        check = resource_check(_resource_plan(vms=[("a", 512, 1)]), None)
        assert check.status == "skipped"
        assert check.findings == ()

    def test_blocking_capacity_reports_findings(self) -> None:
        check = resource_check(_resource_plan(vms=[("a", 9000, 1)]), HostCapacity(memory_mb=512))
        assert check.status == "blocked"
        assert any(f.code == "insufficient-memory" for f in check.findings)

    def test_clean_capacity_is_ok(self) -> None:
        check = resource_check(
            _resource_plan(vms=[("a", 512, 1)]), HostCapacity(memory_mb=4096, logical_cpus=8)
        )
        assert check.status == "ok"
        assert check.findings == ()


class TestDescribeCapacity:
    def test_reports_present_dimensions(self) -> None:
        text = describe_capacity(HostCapacity(memory_mb=1024, logical_cpus=4, storage_free_gb=10))
        assert "1024 MiB RAM" in text and "4 vCPUs" in text and "10 GiB free" in text

    def test_empty_capacity(self) -> None:
        assert describe_capacity(HostCapacity()) == "no dimensions reported"


class TestPreflightCheck:
    def test_evaluate_ok_when_no_findings(self) -> None:
        assert PreflightCheck.evaluate("x", ()).status == "ok"

    def test_evaluate_blocked_when_findings(self) -> None:
        check = PreflightCheck.evaluate("x", [PreflightFinding(code="c", message="m")])
        assert check.status == "blocked"
        assert len(check.findings) == 1

    def test_skipped(self) -> None:
        check = PreflightCheck.skipped("x", "unavailable")
        assert check.status == "skipped" and check.detail == "unavailable"


class TestReportFromChecks:
    def test_findings_flatten_across_checks(self) -> None:
        report = PreflightReport.from_checks(
            [
                PreflightCheck.evaluate("a", [PreflightFinding(code="a1", message="m")]),
                PreflightCheck.evaluate("b", ()),
                PreflightCheck.skipped("c", "n/a"),
                PreflightCheck.evaluate("d", [PreflightFinding(code="d1", message="m")]),
            ]
        )
        assert bool(report) is False
        assert [f.code for f in report.findings] == ["a1", "d1"]
        assert len(report.checks) == 4

    def test_merged_combines_findings_and_checks(self) -> None:
        a = PreflightReport.from_checks([PreflightCheck.evaluate("a", ())])
        b = PreflightReport(findings=(PreflightFinding(code="b", message="m"),))
        merged = a.merged(b)
        assert len(merged.checks) == 1
        assert [f.code for f in merged.findings] == ["b"]

    def test_render_full_shows_every_check_and_status(self) -> None:
        report = PreflightReport.from_checks(
            [
                PreflightCheck.evaluate("clean-check", (), detail="2 items"),
                PreflightCheck.skipped("host-resources", "host capacity unavailable"),
                PreflightCheck.evaluate(
                    "bad-check",
                    [PreflightFinding(code="boom", message="it broke", fix_hint="fix it")],
                ),
            ]
        )
        text = report.render_full()
        assert "1 blocker(s)" in text
        assert "[ OK ] clean-check — 2 items" in text
        assert "[SKIP] host-resources" in text
        assert "[FAIL] bad-check" in text
        assert "boom: it broke" in text
        assert "fix: fix it" in text
        assert text.count("boom: it broke") == 1  # each blocker rendered once, not duplicated

    def test_render_full_shows_unattached_findings(self) -> None:
        # A merged findings-only report (e.g. the portability lint) is still shown.
        report = PreflightReport(findings=(PreflightFinding(code="lone", message="m"),))
        text = report.render_full()
        assert "lone: m" in text
        assert text.count("lone: m") == 1

    def test_render_full_does_not_double_render_a_checked_finding(self) -> None:
        # A finding attached to a check must not ALSO appear in the unattached tail.
        finding = PreflightFinding(code="dup", message="once")
        report = PreflightReport.from_checks([PreflightCheck.evaluate("c", [finding])])
        assert report.render_full().count("dup: once") == 1

    def test_render_full_clean(self) -> None:
        report = PreflightReport.from_checks([PreflightCheck.evaluate("ok-check", ())])
        text = report.render_full()
        assert text.startswith("preflight: clean")
        assert "[ OK ] ok-check" in text
