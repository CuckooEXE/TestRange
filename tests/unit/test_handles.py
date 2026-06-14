"""Tests for the typed plan-construction handles (ADR-0030, DAG-3/DAG-4)."""

from __future__ import annotations

import pytest

from testrange.devices import HardDrive, OSDrive
from testrange.devices.network import NetworkIface
from testrange.handles import NetworkHandle, PoolHandle, SwitchHandle, VMHandle


class _Sink:
    """Records explicit edges like a Hypervisor container would."""

    def __init__(self) -> None:
        self.edges: list[tuple[str, str]] = []

    def add_explicit_edge(self, dependent: str, dependency: str) -> None:
        self.edges.append((dependent, dependency))


class TestHandleIdentity:
    def test_a_handle_is_its_name(self) -> None:
        # The str-subclass contract: every downstream consumer (builders,
        # drivers, the sidecar's dnsmasq records, config_hash) reads the
        # plan-level name, and the handle IS that name.
        assert PoolHandle("pool1") == "pool1"
        assert NetworkHandle("netA", switch="sw1") == "netA"
        assert {"pool1": 1}[PoolHandle("pool1")] == 1

    def test_node_names_are_kind_qualified(self) -> None:
        assert PoolHandle("p").node_name == "pool:p"
        assert SwitchHandle("sw1").node_name == "network:sw1"
        assert NetworkHandle("netA", switch="sw1").node_name == "network:sw1"
        assert VMHandle("web", edges=_Sink()).node_name == "vm:web"

    def test_network_handle_carries_owning_switch(self) -> None:
        net = NetworkHandle("netA", switch="sw1")
        assert net.switch == "sw1"

    def test_empty_names_rejected(self) -> None:
        with pytest.raises(ValueError):
            PoolHandle("")
        with pytest.raises(ValueError):
            NetworkHandle("", switch="sw1")
        with pytest.raises(ValueError):
            NetworkHandle("netA", switch="")

    def test_repr_names_the_kind(self) -> None:
        assert repr(PoolHandle("p")) == "PoolHandle('p')"


class TestMiswireRejection:
    """A wrong handle kind (or a bare string) is rejected at the device boundary."""

    def test_disk_rejects_bare_string(self) -> None:
        with pytest.raises(TypeError, match="PoolHandle"):
            OSDrive("pool1", 8)  # type: ignore[arg-type]

    def test_disk_rejects_network_handle(self) -> None:
        with pytest.raises(TypeError, match="PoolHandle"):
            HardDrive(NetworkHandle("netA", switch="sw1"), 8)  # type: ignore[arg-type]

    def test_nic_rejects_bare_string(self) -> None:
        with pytest.raises(TypeError, match="NetworkHandle"):
            NetworkIface("netA")  # type: ignore[arg-type]

    def test_nic_rejects_pool_handle(self) -> None:
        with pytest.raises(TypeError, match="NetworkHandle"):
            NetworkIface(PoolHandle("pool1"))  # type: ignore[arg-type]


class TestNeeds:
    def test_needs_records_one_edge_per_handle(self) -> None:
        sink = _Sink()
        web = VMHandle("web", edges=sink)
        db = VMHandle("db", edges=sink)
        pool = PoolHandle("pool1")
        web.needs(db, pool)
        assert sink.edges == [("vm:web", "vm:db"), ("vm:web", "pool:pool1")]

    def test_needs_rejects_self(self) -> None:
        sink = _Sink()
        web = VMHandle("web", edges=sink)
        with pytest.raises(ValueError, match="cannot need itself"):
            web.needs(web)
        assert sink.edges == []
