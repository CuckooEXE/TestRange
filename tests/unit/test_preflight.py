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
    PreflightFinding,
    PreflightReport,
    builder_origin_findings,
    preflight_switches,
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
