"""Tests for the driver registry's scheme map + pin introspection (CORE-8).

Covers the three lookups the binding resolver (CORE-10) and the ``--connect``
path rely on: ``driver_for_profile`` (scheme -> driver), ``scheme_for_hypervisor``
(concrete -> scheme, generic -> None), and ``is_pinned``.
"""

from __future__ import annotations

import pytest

from testrange import Hypervisor
from testrange.drivers import (
    driver_for_profile,
    is_pinned,
    scheme_for_hypervisor,
)
from testrange.drivers.libvirt.driver import LibvirtDriver, LibvirtHypervisor
from testrange.drivers.mock import MockDriver, MockHypervisor
from testrange.drivers.proxmox.driver import ProxmoxDriver, ProxmoxHypervisor
from testrange.exceptions import DriverError


class TestDriverForProfile:
    def test_mock_scheme_builds_mock_driver(self) -> None:
        drv = driver_for_profile({"driver": "mock"})
        assert isinstance(drv, MockDriver)

    def test_mock_scheme_honours_knobs(self, tmp_path: object) -> None:
        drv = driver_for_profile(
            {"driver": "mock", "pool_root": str(tmp_path), "backing_capacity_gb": 64}
        )
        assert isinstance(drv, MockDriver)
        assert drv.backing_capacity_gb == 64

    def test_libvirt_scheme_builds_libvirt_driver(self) -> None:
        drv = driver_for_profile({"driver": "libvirt", "uri": "qemu:///system"})
        assert isinstance(drv, LibvirtDriver)

    def test_proxmox_scheme_normalises_realm(self) -> None:
        # A bare user takes @pam, and SSH defaults derive from the API user —
        # the same normalisation as the in-Plan ProxmoxHypervisor.conn() path.
        drv = driver_for_profile(
            {"driver": "proxmox", "host": "10.0.0.5", "user": "root", "password": "pw"}
        )
        assert isinstance(drv, ProxmoxDriver)
        conn = drv._conn  # internal: assert resolved connection without a live PVE
        assert conn.user == "root@pam"
        assert conn.ssh_user == "root"
        assert conn.ssh_password == "pw"  # reuses API password by default
        assert conn.host == "10.0.0.5"

    def test_unknown_scheme_lists_known(self) -> None:
        with pytest.raises(DriverError, match="unknown driver scheme 'bogus'") as ei:
            driver_for_profile({"driver": "bogus"})
        msg = str(ei.value)
        assert "mock" in msg and "proxmox" in msg and "libvirt" in msg

    def test_missing_scheme_errors(self) -> None:
        with pytest.raises(DriverError, match="no 'driver' scheme"):
            driver_for_profile({"host": "10.0.0.5"})


class TestPinIntrospection:
    def test_concrete_entries_report_their_scheme(self) -> None:
        assert scheme_for_hypervisor(MockHypervisor()) == "mock"
        assert scheme_for_hypervisor(ProxmoxHypervisor(host="h")) == "proxmox"
        assert scheme_for_hypervisor(LibvirtHypervisor()) == "libvirt"

    def test_generic_entry_has_no_scheme(self) -> None:
        assert scheme_for_hypervisor(Hypervisor()) is None

    def test_is_pinned_agrees(self) -> None:
        assert is_pinned(MockHypervisor()) is True
        assert is_pinned(ProxmoxHypervisor(host="h")) is True
        assert is_pinned(Hypervisor()) is False
