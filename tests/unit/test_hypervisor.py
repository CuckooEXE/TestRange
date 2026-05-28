"""Tests for the generic, backend-agnostic ``Hypervisor`` topology type (CORE-7).

The generic ``Hypervisor`` carries only portable topology (networks/pools/vms);
it selects no driver and carries no connection. ``driver_for`` must reject it
(it is deliberately unregistered) so a backend-agnostic plan fails loud and
points the author at a connection profile.
"""

from __future__ import annotations

import pytest

from testrange import Hypervisor, Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import driver_for
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec


def _basic_recipe(name: str = "web", net: str = "netA", pool: str = "pool1") -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive(pool, 8), NetworkIface(net)],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
            packages=[Apt("nginx")],
        ),
        communicator=SSHCommunicator("u"),
    )


def _topology() -> dict[str, object]:
    return {
        "networks": [
            Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        ],
        "pools": [StoragePool("pool1", 32)],
        "vms": [_basic_recipe()],
    }


class TestGenericHypervisor:
    def test_empty_ok(self) -> None:
        hyp = Hypervisor()
        assert hyp.networks == ()
        assert hyp.pools == ()
        assert hyp.vms == ()

    def test_construction_freezes_sequences(self) -> None:
        hyp = Hypervisor(**_topology())  # type: ignore[arg-type]
        assert isinstance(hyp.networks, tuple)
        assert isinstance(hyp.pools, tuple)
        assert isinstance(hyp.vms, tuple)

    def test_frozen(self) -> None:
        hyp = Hypervisor()
        with pytest.raises(AttributeError):
            hyp.networks = ()  # type: ignore[misc]

    def test_all_switches_returns_switch_tuple(self) -> None:
        hyp = Hypervisor(**_topology())  # type: ignore[arg-type]
        assert hyp.all_switches == tuple(hyp.networks)

    def test_runs_plan_validation(self) -> None:
        # A NIC referencing a network no Switch declares is a validation error,
        # proving validate_hypervisor_plan runs at construction.
        with pytest.raises(ValueError, match="unknown network"):
            Hypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe(net="netZZ")],
            )

    def test_usable_as_plan_entry(self) -> None:
        hyp = Hypervisor(**_topology())  # type: ignore[arg-type]
        plan = Plan("t", hyp)
        assert plan.hypervisor is hyp

    def test_carries_no_build_switch_field(self) -> None:
        # Build egress is a backend BINDING concern (CORE-10), not portable
        # topology; the generic type must not grow a build_switch field.
        assert not hasattr(Hypervisor(), "build_switch")

    def test_not_pinned_driver_for_raises(self) -> None:
        # Unregistered by design: it selects no driver. driver_for must reject
        # it with a clear error (final --connect wording lands in CORE-10).
        with pytest.raises(DriverError, match="no driver registered"):
            driver_for(Hypervisor(**_topology()))  # type: ignore[arg-type]
