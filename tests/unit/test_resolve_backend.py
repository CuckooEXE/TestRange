"""Tests for the backend binding resolver (CORE-10 / CORE-19).

Exercises the collapsed pin/override matrix — a concrete ``*Hypervisor`` is now
a topology-only scheme marker (CORE-19), so the connection ALWAYS comes from
``--connect`` and every "+none" cell errors. The compatibility-lint hook and
the orchestrator's preflight merge are exercised too.
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
    def test_concrete_plus_none_errors_pointing_at_connect(self) -> None:
        # A topology-only scheme marker carries no connection (CORE-19); the
        # resolver names the pinned scheme so the dev knows which flavor of
        # profile to point --connect at.
        with pytest.raises(DriverError, match=r"ProxmoxHypervisor pins the 'proxmox'"):
            resolve_backend(Plan("t", ProxmoxHypervisor()), None)

    def test_concrete_plus_matching_profile_binds(self) -> None:
        # Profile scheme matches the pin: driver built from the profile
        # connection; build switch from the profile; topology stays the entry's.
        hyp = ProxmoxHypervisor()
        profile = ProxmoxProfile(
            host="PROFILE-HOST",
            user="root",
            password="profilepw",
            build_switch=ManagedBuildSwitch(uplink="vmbr7"),
        )
        resolved = resolve_backend(Plan("t", hyp), profile)
        assert isinstance(resolved.driver, ProxmoxDriver)
        assert resolved.driver._conn.host == "PROFILE-HOST"
        assert resolved.build_switch == ManagedBuildSwitch(uplink="vmbr7")
        assert resolved.driver_uri.startswith("proxmox://")

    def test_concrete_plus_mismatched_profile_hard_errors(self) -> None:
        hyp = ProxmoxHypervisor()
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
        o = Orchestrator(Plan("t", MockHypervisor()), profile=MockProfile())
        with pytest.raises(PreflightError, match="incompat"):
            o.build()


class TestResolvedBackendShape:
    def test_is_frozen(self) -> None:
        rb = ResolvedBackend(driver=MockDriver(), build_switch=None, driver_uri="mock:///")
        with pytest.raises(AttributeError):
            rb.driver_uri = "x"  # type: ignore[misc]
