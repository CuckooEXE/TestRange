"""Tests for device dataclasses (CPU/Memory/disks/NIC/Pool)."""

from __future__ import annotations

import pytest

from testrange.devices import (
    CPU,
    DHCPAddr,
    HardDrive,
    Memory,
    NetworkIface,
    OSDrive,
    StaticAddr,
    StoragePool,
)


class TestCPU:
    def test_valid(self) -> None:
        c = CPU(4)
        assert c.count == 4

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_count(self, bad: int) -> None:
        with pytest.raises(ValueError):
            CPU(bad)


class TestMemory:
    def test_valid(self) -> None:
        m = Memory(2048)
        assert m.size_mb == 2048

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid(self, bad: int) -> None:
        with pytest.raises(ValueError):
            Memory(bad)


class TestDisks:
    def test_os_drive(self) -> None:
        d = OSDrive("pool1", 16)
        assert d.pool == "pool1"
        assert d.size_gb == 16

    def test_hard_drive(self) -> None:
        d = HardDrive("pool2", 100)
        assert d.size_gb == 100

    def test_invalid_pool(self) -> None:
        with pytest.raises(ValueError):
            OSDrive("", 8)

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError):
            OSDrive("pool1", 0)


class TestNICs:
    def test_iface_defaults(self) -> None:
        n = NetworkIface("netA")
        assert n.network == "netA"
        assert n.addr is None  # default: unconfigured, not DHCP
        assert isinstance(n, NetworkIface)

    def test_invalid_network(self) -> None:
        with pytest.raises(ValueError):
            NetworkIface("")

    def test_dhcp_addr(self) -> None:
        n = NetworkIface("netA", addr=DHCPAddr())
        assert n.addr == DHCPAddr()

    def test_static_addr(self) -> None:
        n = NetworkIface("netA", addr=StaticAddr("172.31.0.50"))
        assert n.addr == StaticAddr("172.31.0.50")

    def test_static_addr_base(self) -> None:
        n = NetworkIface("netA", addr=StaticAddr("10.0.0.5"))
        assert n.addr == StaticAddr("10.0.0.5")

    def test_rejects_non_address_mode(self) -> None:
        with pytest.raises(TypeError, match="must be DHCPAddr, StaticAddr, or None"):
            NetworkIface("netA", addr="172.31.0.50")  # type: ignore[arg-type]


class TestStaticAddr:
    def test_bare_host_no_prefix(self) -> None:
        s = StaticAddr("172.31.0.50")
        assert s.host == "172.31.0.50"
        assert s.cidr(24) == "172.31.0.50/24"  # prefix derived

    def test_explicit_prefix_wins(self) -> None:
        s = StaticAddr("172.31.0.50/25")
        assert s.host == "172.31.0.50"
        assert s.cidr(24) == "172.31.0.50/25"  # explicit beats derived

    def test_bare_host_underivable_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="needs a netmask"):
            StaticAddr("172.31.0.50").cidr(None)

    def test_dns_normalized_to_tuple(self) -> None:
        # Passing a list is the point: StaticAddr accepts any iterable of str at
        # the user boundary and normalizes to a tuple in __post_init__.
        s = StaticAddr("10.0.0.5", dns=["8.8.8.8", "1.1.1.1"])  # type: ignore[arg-type]
        assert s.dns == ("8.8.8.8", "1.1.1.1")
        assert hash(s)  # frozen + tuple => hashable for config_hash

    @pytest.mark.parametrize("bad", ["not-an-ip", "300.300.300.300", "::1", ""])
    def test_invalid_addr(self, bad: str) -> None:
        with pytest.raises(ValueError, match="not a valid IPv4 address"):
            StaticAddr(bad)

    def test_invalid_gw(self) -> None:
        with pytest.raises(ValueError, match="gw is not a valid"):
            StaticAddr("10.0.0.5", gw="nope")

    def test_invalid_dns_entry(self) -> None:
        with pytest.raises(ValueError, match="dns entry is not a valid"):
            StaticAddr("10.0.0.5", dns=("8.8.8.8", "bad"))

    def test_bare_string_dns_rejected(self) -> None:
        # A bare str is itself an iterable of chars, so dns="8.8.8.8" would
        # char-split to ('8','.','8',…) and fail with the misleading
        # "entry '8' is not a valid IPv4". Reject it with an actionable
        # "wrap it in a tuple" message at the boundary instead (CORE-95).
        with pytest.raises(ValueError, match="not a single string"):
            StaticAddr("10.0.0.5", dns="8.8.8.8")  # type: ignore[arg-type]


class TestPool:
    def test_valid(self) -> None:
        p = StoragePool("p1", 32)
        assert p.size_gb == 32

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError):
            StoragePool("p1", 0)
