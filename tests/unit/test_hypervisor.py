"""Tests for the mutable ``Hypervisor`` node container (ADR-0030, DAG-4).

The 2.0 construction surface: ``add_pool``/``add_switch``/``add_vm`` register
a node and return its typed handle; registries expose handles by name with a
loud ``KeyError``; ``Plan(...)`` freezes the container. The generic type still
selects no driver (CORE-19) — the binding resolver rejects it without a
``--profile``.
"""

from __future__ import annotations

import pytest

from testrange import Hypervisor, Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import is_pinned, scheme_for_hypervisor
from testrange.handles import NetworkHandle, PoolHandle, SwitchHandle, VMHandle
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec


def _recipe(hyp: Hypervisor, name: str = "web") -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive(hyp.pools["pool1"], 8),
                NetworkIface(hyp.networks["netA"]),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
            packages=[Apt("nginx")],
        ),
        communicator=SSHCommunicator("u"),
    )


def _populated() -> Hypervisor:
    hyp = Hypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True)))
    hyp.add_vm(_recipe(hyp))
    return hyp


class TestRegistration:
    def test_add_pool_returns_typed_handle(self) -> None:
        hyp = Hypervisor()
        handle = hyp.add_pool(StoragePool("pool1", 32))
        assert isinstance(handle, PoolHandle)
        assert handle == "pool1"  # a handle IS the plan-level name
        assert handle.node_name == "pool:pool1"

    def test_add_switch_returns_handle_and_flattens_networks(self) -> None:
        hyp = Hypervisor()
        handle = hyp.add_switch(Switch("sw1", Network("netA"), Network("netB"), cidr="10.0.0.0/24"))
        assert isinstance(handle, SwitchHandle)
        assert set(hyp.networks) == {"netA", "netB"}
        net = hyp.networks["netA"]
        assert isinstance(net, NetworkHandle)
        assert net.switch == "sw1"
        # Both networks realize as the one switch unit, so both handles point
        # at the same graph node.
        assert net.node_name == hyp.networks["netB"].node_name == "network:sw1"

    def test_add_vm_returns_handle(self) -> None:
        hyp = _populated()
        assert isinstance(hyp.vms["web"], VMHandle)
        assert hyp.vms["web"].node_name == "vm:web"

    def test_registry_keyerror_is_loud_and_teaching(self) -> None:
        hyp = _populated()
        with pytest.raises(KeyError, match="no pool 'pool11'; known: pool1"):
            hyp.pools["pool11"]
        with pytest.raises(KeyError, match="no network 'netZZ'; known: netA"):
            hyp.networks["netZZ"]

    def test_declared_accessors_preserve_registration_order(self) -> None:
        hyp = Hypervisor()
        hyp.add_pool(StoragePool("b-pool", 1))
        hyp.add_pool(StoragePool("a-pool", 1))
        assert [p.name for p in hyp.declared_pools] == ["b-pool", "a-pool"]

    def test_duplicate_pool_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_pool(StoragePool("pool1", 64))

    def test_duplicate_vm_rejected_at_add(self) -> None:
        hyp = _populated()
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_vm(_recipe(hyp))

    def test_duplicate_network_across_switches_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24"))
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_switch(Switch("sw2", Network("netA"), cidr="10.0.1.0/24"))

    def test_wrong_type_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        with pytest.raises(TypeError, match="takes a StoragePool"):
            hyp.add_pool("pool1")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="takes a Switch"):
            hyp.add_switch(Network("netA"))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="takes a VMRecipe"):
            hyp.add_vm(StoragePool("pool1", 1))  # type: ignore[arg-type]


class TestFreeze:
    def test_plan_freezes_container(self) -> None:
        hyp = _populated()
        assert hyp.frozen is False
        Plan("t", hyp)
        assert hyp.frozen is True
        with pytest.raises(ValueError, match="frozen"):
            hyp.add_pool(StoragePool("late", 1))
        with pytest.raises(ValueError, match="frozen"):
            hyp.add_explicit_edge("vm:web", "pool:pool1")

    def test_needs_after_freeze_rejected(self) -> None:
        hyp = _populated()
        web = hyp.vms["web"]
        other = hyp.add_vm(_recipe(hyp, "db"))
        Plan("t", hyp)
        with pytest.raises(ValueError, match="frozen"):
            web.needs(other)


class TestExplicitEdges:
    def test_needs_records_edges_in_order(self) -> None:
        hyp = _populated()
        db = hyp.add_vm(_recipe(hyp, "db"))
        web = hyp.vms["web"]
        web.needs(db)
        assert hyp.explicit_edges == (("vm:web", "vm:db"),)

    def test_needs_self_rejected(self) -> None:
        hyp = _populated()
        with pytest.raises(ValueError, match="cannot need itself"):
            hyp.vms["web"].needs(hyp.vms["web"])

    def test_needs_non_handle_rejected(self) -> None:
        hyp = _populated()
        with pytest.raises(TypeError, match="takes handles"):
            hyp.vms["web"].needs("db")  # type: ignore[arg-type]


class TestBuildSwitch:
    def test_build_switch_is_portable_topology(self) -> None:
        # ADR-0016: uplink is a profile-resolved logical name, so the build
        # switch carries nothing host-specific and lives on the Hypervisor.
        assert Hypervisor().build_switch is None
        bs = Switch("build", Network("b"), cidr="10.97.99.0/24", sidecar=Sidecar(dhcp=True))
        assert Hypervisor(build_switch=bs).build_switch is bs


class TestSchemePin:
    def test_generic_not_pinned(self) -> None:
        # Unregistered by design: it selects no scheme. is_pinned must report
        # False so the binding resolver routes it through the --profile path.
        hyp = _populated()
        assert is_pinned(hyp) is False
        assert scheme_for_hypervisor(hyp) is None


class TestVmFacade:
    """``Hypervisor.vm(...)`` — sugar over ``add_vm(VMRecipe(VMSpec(...)))`` (CORE-101).

    Promotes the singleton devices to named typed params and splits the 0+
    devices into ``nics``/``data_disks``, then delegates to the unchanged
    ``add_vm``. The contract under test is that it is *pure sugar*: same
    underlying VMSpec/VMRecipe, same registration order, builder/communicator
    forwarded untouched.
    """

    def _hyp(self) -> tuple[Hypervisor, PoolHandle, NetworkHandle]:
        hyp = Hypervisor()
        pool1 = hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        )
        return hyp, pool1, hyp.networks["netA"]

    @staticmethod
    def _builder() -> CloudInitBuilder:
        return CloudInitBuilder(
            base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
        )

    def test_returns_registered_vmhandle(self) -> None:
        hyp, pool1, netA = self._hyp()
        web = hyp.vm(
            "web",
            cpu=CPU(1),
            memory=Memory(512),
            os_drive=OSDrive(pool1, 8),
            nics=[NetworkIface(netA)],
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        assert isinstance(web, VMHandle)
        assert web.node_name == "vm:web"
        assert hyp.vms["web"] is web

    def test_equivalent_to_explicit_add_vm(self) -> None:
        # Same builder/communicator INSTANCES on both paths: vm() forwards them
        # unchanged (stovepipes intact) and VMSpec compares by value, so the
        # registered recipe must equal the hand-built one. This is the byte-
        # identical-build pin (DAG-5 key parity): same devices tuple in, same
        # config_hash out.
        hyp, pool1, netA = self._hyp()
        builder, comm = self._builder(), SSHCommunicator("u")
        cpu, mem, osd, nic = CPU(2), Memory(1024), OSDrive(pool1, 8), NetworkIface(netA)
        explicit = VMRecipe(
            spec=VMSpec(name="web", devices=[cpu, mem, osd, nic]),
            builder=builder,
            communicator=comm,
        )
        hyp.vm(
            "web", cpu=cpu, memory=mem, os_drive=osd, nics=[nic], builder=builder, communicator=comm
        )
        registered = hyp.declared_vms[0]
        assert registered.spec == explicit.spec
        assert registered.builder is builder  # forwarded, not rebuilt
        assert registered.communicator is comm
        assert registered == explicit

    def test_packs_devices_in_canonical_order(self) -> None:
        # [cpu, memory, os_drive, *data_disks, *nics] — a fixed order so the
        # order-sensitive config_hash channels (spec.data_drives, spec.nics)
        # are deterministic.
        hyp, pool1, netA = self._hyp()
        cpu, mem, osd = CPU(1), Memory(512), OSDrive(pool1, 8)
        d0, d1 = HardDrive(pool1, 4), HardDrive(pool1, 8)
        nic = NetworkIface(netA)
        hyp.vm(
            "web",
            cpu=cpu,
            memory=mem,
            os_drive=osd,
            data_disks=[d0, d1],
            nics=[nic],
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        spec = hyp.declared_vms[0].spec
        assert spec.devices == (cpu, mem, osd, d0, d1, nic)
        assert spec.data_drives == (d0, d1)  # relative order preserved
        assert spec.nics == (nic,)

    def test_nics_and_data_disks_default_empty(self) -> None:
        hyp, pool1, _ = self._hyp()
        hyp.vm(
            "headless",
            cpu=CPU(1),
            memory=Memory(512),
            os_drive=OSDrive(pool1, 8),
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        spec = hyp.declared_vms[0].spec
        assert spec.nics == ()
        assert spec.data_drives == ()
        assert spec.devices == (spec.cpu, spec.memory, spec.os_drive)

    def test_firmware_param_flows_to_spec(self) -> None:
        hyp, pool1, _ = self._hyp()
        hyp.vm(
            "uefi-vm",
            cpu=CPU(1),
            memory=Memory(512),
            os_drive=OSDrive(pool1, 8),
            firmware="uefi",
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        assert hyp.declared_vms[0].spec.firmware == "uefi"

    def test_preserves_declared_registration_order(self) -> None:
        hyp, pool1, _ = self._hyp()
        for name in ("beta", "alpha"):
            hyp.vm(
                name,
                cpu=CPU(1),
                memory=Memory(512),
                os_drive=OSDrive(pool1, 8),
                builder=self._builder(),
                communicator=SSHCommunicator("u"),
            )
        assert [r.name for r in hyp.declared_vms] == ["beta", "alpha"]

    def test_handle_supports_needs_edge(self) -> None:
        hyp, pool1, _ = self._hyp()
        db = hyp.vm(
            "db",
            cpu=CPU(1),
            memory=Memory(512),
            os_drive=OSDrive(pool1, 8),
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        web = hyp.vm(
            "web",
            cpu=CPU(1),
            memory=Memory(512),
            os_drive=OSDrive(pool1, 8),
            builder=self._builder(),
            communicator=SSHCommunicator("u"),
        )
        web.needs(db)
        assert hyp.explicit_edges == (("vm:web", "vm:db"),)

    def test_singleton_slot_miswire_rejected(self) -> None:
        # mypy rejects a non-CPU in the cpu slot at the call site; the
        # ``# type: ignore[arg-type]`` is load-bearing under warn_unused_ignores
        # — if the param type ever loosens, the ignore goes unused and the gate
        # fails. VMSpec's arity check is the runtime backstop.
        hyp, pool1, _ = self._hyp()
        with pytest.raises(ValueError, match="exactly one CPU"):
            hyp.vm(
                "bad",
                cpu=Memory(512),  # type: ignore[arg-type]
                memory=Memory(512),
                os_drive=OSDrive(pool1, 8),
                builder=self._builder(),
                communicator=SSHCommunicator("u"),
            )
