"""Unit tests for :mod:`testrange.devices`."""

from __future__ import annotations

import pytest

from testrange.devices import (
    AbstractDevice,
    HardDrive,
    Memory,
    VirtualNetworkRef,
    normalise_qemu_size,
    parse_size,
    vCPU,
)


class TestParseSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1B", 1),
            ("1K", 1024),
            ("1KB", 1024),
            ("1M", 1024 ** 2),
            ("1MB", 1024 ** 2),
            ("1MiB", 1024 ** 2),
            ("1G", 1024 ** 3),
            ("1GiB", 1024 ** 3),
            ("1T", 1024 ** 4),
            ("1.5GB", int(1.5 * 1024 ** 3)),
            ("64GB", 64 * 1024 ** 3),
            ("  512M  ", 512 * 1024 ** 2),
        ],
    )
    def test_valid_size_strings(self, text: str, expected: int) -> None:
        assert parse_size(text) == expected

    def test_case_insensitive_unit(self) -> None:
        assert parse_size("2gb") == parse_size("2GB") == parse_size("2Gb")

    @pytest.mark.parametrize("bad", ["", "abc", "10", "5XB", "GB5", "5.5.5G"])
    def test_invalid_size_strings_raise(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_size(bad)


class TestNormaliseQemuSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1GB", "1G"),
            ("64GB", "64G"),
            ("1024M", "1G"),
            ("1.5G", "1G"),       # integer truncation
            ("2.5G", "2G"),
            ("500M", "1G"),       # minimum clamp to 1G
            ("1B", "1G"),
        ],
    )
    def test_normalisation(self, text: str, expected: str) -> None:
        assert normalise_qemu_size(text) == expected


class TestVCPU:
    def test_default(self) -> None:
        assert vCPU().count == 2

    def test_custom_count(self) -> None:
        assert vCPU(8).count == 8

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_invalid_count_raises(self, bad: int) -> None:
        with pytest.raises(ValueError):
            vCPU(bad)

    def test_device_type(self) -> None:
        assert vCPU().device_type == "vcpu"

    def test_repr(self) -> None:
        assert repr(vCPU(4)) == "vCPU(4)"


class TestMemory:
    def test_default(self) -> None:
        assert Memory().gib == 2.0

    def test_custom(self) -> None:
        assert Memory(8).gib == 8

    def test_kib_conversion(self) -> None:
        assert Memory(2).kib == 2 * 1024 * 1024

    def test_fractional_kib_rounds(self) -> None:
        assert Memory(1.5).kib == round(1.5 * 1024 * 1024)

    @pytest.mark.parametrize("bad", [0, -1, -0.5])
    def test_invalid_gib_raises(self, bad: float) -> None:
        with pytest.raises(ValueError):
            Memory(bad)

    def test_device_type(self) -> None:
        assert Memory().device_type == "memory"

    def test_repr(self) -> None:
        assert repr(Memory(4)) == "Memory(4)"


class TestHardDrive:
    def test_default(self) -> None:
        d = HardDrive()
        assert d.size == "20GB"
        assert d.nvme is False

    def test_custom_size(self) -> None:
        d = HardDrive("64GB")
        assert d.size_bytes == 64 * 1024 ** 3

    def test_qemu_size(self) -> None:
        assert HardDrive("64GB").qemu_size == "64G"

    def test_nvme_bus(self) -> None:
        assert HardDrive(nvme=True).bus == "nvme"

    def test_default_bus_is_none_for_backend_to_pick(self) -> None:
        # Regression: the documented contract is that ``bus=None`` lets
        # the backend choose — libvirt picks virtio on Linux guests and
        # sata on Windows (so Windows Setup doesn't need virtio-win
        # drivers pre-threaded).  Forcing a default here would break
        # the Windows install path.
        assert HardDrive().bus is None

    def test_explicit_virtio_bus(self) -> None:
        # Callers who want to pin virtio across backends do so
        # explicitly — the default path (bus=None) intentionally
        # declines to answer on behalf of the backend.
        assert HardDrive(bus="virtio").bus == "virtio"

    def test_invalid_size_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            HardDrive("abc")

    def test_int_size_interpreted_as_gib(self) -> None:
        d = HardDrive(32)
        assert d.size_bytes == 32 * 1024 ** 3
        assert d.qemu_size == "32G"

    def test_float_size_interpreted_as_gib(self) -> None:
        d = HardDrive(1.5)
        # parse_size treats the numeric form as GiB
        assert d.size_bytes == int(1.5 * 1024 ** 3)

    def test_numeric_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            HardDrive(0)
        with pytest.raises(ValueError):
            HardDrive(-5)

    def test_device_type(self) -> None:
        assert HardDrive().device_type == "harddrive"

    def test_repr_default(self) -> None:
        # Regression: default nvme=False is omitted from repr.
        assert repr(HardDrive("32GB")) == "HardDrive('32GB')"

    def test_repr_nvme(self) -> None:
        assert repr(HardDrive("64GB", nvme=True)) == "HardDrive('64GB', nvme=True)"


class TestVirtualNetworkRef:
    def test_dhcp_default(self) -> None:
        ref = VirtualNetworkRef("NetA")
        assert ref.name == "NetA"
        assert ref.ip is None

    def test_static_ip(self) -> None:
        ref = VirtualNetworkRef("NetA", ip="10.0.0.5")
        assert ref.ip == "10.0.0.5"

    def test_device_type(self) -> None:
        assert VirtualNetworkRef("NetA").device_type == "network_ref"

    def test_repr_dhcp(self) -> None:
        assert repr(VirtualNetworkRef("NetA")) == "VirtualNetworkRef('NetA')"

    def test_repr_static(self) -> None:
        r = VirtualNetworkRef("NetA", ip="10.0.0.5")
        assert repr(r) == "VirtualNetworkRef('NetA', ip='10.0.0.5')"


class TestAbstractDevice:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            AbstractDevice()  # type: ignore[abstract]

    def test_subclass_must_implement_device_type(self) -> None:
        class Incomplete(AbstractDevice):  # missing device_type
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class Custom(AbstractDevice):
            @property
            def device_type(self) -> str:
                return "custom"

        assert Custom().device_type == "custom"
