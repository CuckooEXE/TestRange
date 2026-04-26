"""Unit tests for :mod:`testrange.devices`."""

from __future__ import annotations

import pytest

from testrange.devices import (
    AbstractDevice,
    HardDrive,
    Memory,
    vNIC,
    normalise_size,
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


class TestNormaliseSize:
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
        assert normalise_size(text) == expected


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
    """Tests for the generic, backend-neutral HardDrive."""

    def test_default(self) -> None:
        d = HardDrive()
        assert d.size == "20GB"

    def test_custom_size(self) -> None:
        d = HardDrive("64GB")
        assert d.size_bytes == 64 * 1024 ** 3

    def test_size_string(self) -> None:
        assert HardDrive("64GB").size_string == "64G"

    def test_invalid_size_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            HardDrive("abc")

    def test_int_size_interpreted_as_gib(self) -> None:
        d = HardDrive(32)
        assert d.size_bytes == 32 * 1024 ** 3
        assert d.size_string == "32G"

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

    def test_repr(self) -> None:
        assert repr(HardDrive("32GB")) == "HardDrive('32GB')"

    def test_no_backend_specific_attrs(self) -> None:
        """The generic HardDrive must not carry libvirt's bus/nvme
        knobs — those live on LibvirtHardDrive.  Regression guard
        against accidentally re-leaking backend specifics into the
        generic class."""
        d = HardDrive(32)
        assert not hasattr(d, "bus")
        assert not hasattr(d, "nvme")


class TestLibvirtHardDrive:
    """Tests for the libvirt-specific HardDrive subclass."""

    def test_default_bus_is_none_for_backend_to_pick(self) -> None:
        # Regression: the documented contract is that ``bus=None`` lets
        # the backend choose — libvirt picks virtio on Linux guests and
        # sata on Windows (so Windows Setup doesn't need virtio-win
        # drivers pre-threaded).  Forcing a default here would break
        # the Windows install path.
        from testrange.backends.libvirt import LibvirtHardDrive
        assert LibvirtHardDrive().bus is None

    def test_nvme_bus(self) -> None:
        from testrange.backends.libvirt import LibvirtHardDrive
        d = LibvirtHardDrive(nvme=True)
        assert d.bus == "nvme"
        assert d.nvme is True

    def test_explicit_virtio_bus(self) -> None:
        from testrange.backends.libvirt import LibvirtHardDrive
        assert LibvirtHardDrive(bus="virtio").bus == "virtio"

    def test_invalid_bus_rejected(self) -> None:
        from testrange.backends.libvirt import LibvirtHardDrive
        with pytest.raises(ValueError, match="bus="):
            LibvirtHardDrive(bus="bogus")  # type: ignore[arg-type]

    def test_repr_nvme(self) -> None:
        from testrange.backends.libvirt import LibvirtHardDrive
        assert (
            repr(LibvirtHardDrive("64GB", nvme=True))
            == "LibvirtHardDrive('64GB', nvme=True)"
        )

    def test_is_a_hard_drive(self) -> None:
        """Sibling-of-HardDrive contract: LibvirtHardDrive is an
        AbstractHardDrive (so cache + run-dir treat it uniformly) but
        NOT a HardDrive (so type-narrowing against HardDrive doesn't
        accidentally accept it in cross-backend contexts)."""
        from testrange.backends.libvirt import LibvirtHardDrive
        from testrange.devices import AbstractHardDrive
        d = LibvirtHardDrive(10)
        assert isinstance(d, AbstractHardDrive)
        assert not isinstance(d, HardDrive)


class TestvNIC:
    def test_dhcp_default(self) -> None:
        ref = vNIC("NetA")
        assert ref.ref == "NetA"
        assert ref.ip is None

    def test_static_ip(self) -> None:
        ref = vNIC("NetA", ip="10.0.0.5")
        assert ref.ip == "10.0.0.5"

    def test_device_type(self) -> None:
        assert vNIC("NetA").device_type == "vnic"

    def test_repr_dhcp(self) -> None:
        assert repr(vNIC("NetA")) == "vNIC('NetA')"

    def test_repr_static(self) -> None:
        r = vNIC("NetA", ip="10.0.0.5")
        assert repr(r) == "vNIC('NetA', ip='10.0.0.5')"


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
