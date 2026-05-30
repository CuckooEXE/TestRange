"""Tests for the driver registry's pin introspection (CORE-8).

Covers the two introspection helpers the binding resolver (CORE-10) uses to
tell a pinned (concrete) Plan entry from a generic one: ``scheme_for_hypervisor``
and ``is_pinned``. The ``--connect`` profile path no longer goes through the
driver registry (CORE-18): each :class:`~testrange.connect.BackendProfile`
subclass owns its own driver construction, so per-scheme-to-driver coverage
lives next to the concrete subclasses themselves.
"""

from __future__ import annotations

from testrange import Hypervisor
from testrange.drivers import (
    is_pinned,
    scheme_for_hypervisor,
)
from testrange.drivers.libvirt.driver import LibvirtHypervisor
from testrange.drivers.proxmox.driver import ProxmoxHypervisor
from tests.mock_driver import MockHypervisor


class TestPinIntrospection:
    def test_concrete_entries_report_their_scheme(self) -> None:
        assert scheme_for_hypervisor(MockHypervisor()) == "mock"
        assert scheme_for_hypervisor(ProxmoxHypervisor()) == "proxmox"
        assert scheme_for_hypervisor(LibvirtHypervisor()) == "libvirt"

    def test_generic_entry_has_no_scheme(self) -> None:
        assert scheme_for_hypervisor(Hypervisor()) is None

    def test_is_pinned_agrees(self) -> None:
        assert is_pinned(MockHypervisor()) is True
        assert is_pinned(ProxmoxHypervisor()) is True
        assert is_pinned(Hypervisor()) is False
