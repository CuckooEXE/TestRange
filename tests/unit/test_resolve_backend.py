"""Tests for the backend binding resolver (CORE-10).

Exercises all four cells of the pin/override matrix, the env-knob (build
switch) source per cell, the compatibility-lint hook, and the orchestrator
merging a compatibility finding into preflight.
"""

from __future__ import annotations

import pytest

from testrange import Hypervisor, Plan
from testrange.drivers.mock import MockDriver, MockHypervisor, MockProfile
from testrange.drivers.proxmox import ProxmoxProfile
from testrange.drivers.proxmox.driver import ProxmoxDriver, ProxmoxHypervisor
from testrange.exceptions import DriverError
from testrange.networks.base import ManagedBuildSwitch
from testrange.orchestrator.backend import (
    ResolvedBackend,
    compatibility_findings,
    resolve_backend,
)


class TestPinMatrix:
    def test_concrete_plus_none_is_todays_path(self) -> None:
        # Driver from the entry's type; build switch + uri from the entry.
        bs = ManagedBuildSwitch(uplink="vmbr9")
        hyp = ProxmoxHypervisor(host="10.0.0.5", password="pw", build_switch=bs)
        resolved = resolve_backend(Plan("t", hyp), None)
        assert isinstance(resolved.driver, ProxmoxDriver)
        assert resolved.build_switch is bs
        assert resolved.driver_uri.startswith("proxmox://")

    def test_concrete_plus_matching_profile_overrides_connection(self) -> None:
        # Profile scheme matches the pin: driver built from the profile
        # connection; build switch from the profile; topology stays the entry's.
        hyp = ProxmoxHypervisor(host="ENTRY-HOST", password="entry")
        profile = ProxmoxProfile(
            host="PROFILE-HOST",
            user="root",
            password="profilepw",
            build_switch=ManagedBuildSwitch(uplink="vmbr7"),
        )
        resolved = resolve_backend(Plan("t", hyp), profile)
        assert isinstance(resolved.driver, ProxmoxDriver)
        assert resolved.driver._conn.host == "PROFILE-HOST"  # connection overridden
        assert resolved.build_switch == ManagedBuildSwitch(uplink="vmbr7")

    def test_concrete_plus_mismatched_profile_hard_errors(self) -> None:
        hyp = ProxmoxHypervisor(host="h", password="pw")
        profile = MockProfile()
        with pytest.raises(DriverError) as ei:
            resolve_backend(Plan("t", hyp), profile)
        msg = str(ei.value)
        assert "'mock'" in msg and "proxmox" in msg  # names both schemes

    def test_generic_plus_none_names_connect(self) -> None:
        with pytest.raises(DriverError, match=r"backend-agnostic.*--connect"):
            resolve_backend(Plan("t", Hypervisor()), None)

    def test_generic_plus_profile_binds(self) -> None:
        profile = ProxmoxProfile(
            host="10.0.0.9",
            password="pw",
            build_switch=ManagedBuildSwitch(uplink="vmbr9"),
        )
        resolved = resolve_backend(Plan("t", Hypervisor()), profile)
        assert isinstance(resolved.driver, ProxmoxDriver)
        assert resolved.driver._conn.host == "10.0.0.9"
        assert resolved.build_switch == ManagedBuildSwitch(uplink="vmbr9")
        assert resolved.driver_uri.startswith("proxmox://")

    def test_generic_plus_mock_profile(self) -> None:
        resolved = resolve_backend(Plan("t", Hypervisor()), MockProfile())
        assert isinstance(resolved.driver, MockDriver)
        assert resolved.build_switch is None


class TestCompatibilityFindings:
    def test_clean_for_generic_plan_on_mock(self) -> None:
        plan = Plan("t", MockHypervisor())
        assert compatibility_findings(plan, MockDriver()) == ()

    def test_finding_blocks_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The orchestrator merges layer-2 findings into preflight; a non-empty
        # result blocks bring-up with a PreflightError.
        from testrange.exceptions import PreflightError
        from testrange.orchestrator.runtime import Orchestrator
        from testrange.preflight import PreflightFinding

        monkeypatch.setattr(
            "testrange.orchestrator.runtime.compatibility_findings",
            lambda plan, driver: (PreflightFinding(code="incompat", message="nope"),),
        )
        o = Orchestrator(Plan("t", MockHypervisor()))
        with pytest.raises(PreflightError, match="incompat"):
            o.build()


class TestResolvedBackendShape:
    def test_is_frozen(self) -> None:
        rb = ResolvedBackend(driver=MockDriver(), build_switch=None, driver_uri="mock:///")
        with pytest.raises(AttributeError):
            rb.driver_uri = "x"  # type: ignore[misc]
