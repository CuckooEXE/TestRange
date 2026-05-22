"""HypervisorDriver ABC — the native-guest-agent accessors default-raise.

A backend with no native in-guest agent inherits the three
``native_guest_*`` accessors unchanged; calling any of them must raise a
clean ``DriverError`` rather than returning a half-formed callable.
"""

from __future__ import annotations

import pytest

from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError

# Every abstract method on the ABC, stubbed so the subclass is concrete.
_ABSTRACT_METHODS = (
    "connect",
    "disconnect",
    "preflight",
    "compose_resource_name",
    "compose_mac",
    "compose_volume_ref",
    "create_switch",
    "destroy_switch",
    "create_network",
    "destroy_network",
    "create_pool",
    "destroy_pool",
    "volume_suffix",
    "write_to_pool",
    "create_disk_from_base",
    "upload_to_pool",
    "download_from_pool",
    "delete_volume",
    "create_vm",
    "start_vm",
    "shutdown_vm",
    "destroy_vm",
    "get_vm_power_state",
    "create_snapshot",
    "list_snapshots",
    "delete_snapshot",
    "restore_snapshot",
)


def _stub(*_a: object, **_k: object) -> None:
    raise NotImplementedError


_NoAgentDriver = type(
    "_NoAgentDriver",
    (HypervisorDriver,),
    {name: _stub for name in _ABSTRACT_METHODS},
)


class TestNativeGuestAccessors:
    def test_execute_default_raises(self) -> None:
        d = _NoAgentDriver()
        with pytest.raises(DriverError, match="no native guest agent"):
            d.native_guest_execute("tr_vm_x")

    def test_read_file_default_raises(self) -> None:
        d = _NoAgentDriver()
        with pytest.raises(DriverError, match="no native guest agent"):
            d.native_guest_read_file("tr_vm_x")

    def test_write_file_default_raises(self) -> None:
        d = _NoAgentDriver()
        with pytest.raises(DriverError, match="no native guest agent"):
            d.native_guest_write_file("tr_vm_x")


class TestDestroyDispatcher:
    """`destroy(kind, ...)` is the state-driven cleanup entry point.

    Phase-prefixed kinds (``install_*``) must dispatch to the same
    underlying destructor as their base kind, since the cleanup CLI
    walks state.json LIFO and never inspects phase context.
    """

    def _driver_recording(self) -> tuple[HypervisorDriver, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []

        def _record(method: str) -> object:
            def _stub(self: object, *a: object, **_k: object) -> None:
                del self
                calls.append((method, str(a[0]) if a else ""))

            return _stub

        attrs: dict[str, object] = {name: _stub for name in _ABSTRACT_METHODS}
        attrs["destroy_network"] = _record("destroy_network")
        attrs["destroy_pool"] = _record("destroy_pool")
        attrs["destroy_vm"] = _record("destroy_vm")
        attrs["destroy_switch"] = _record("destroy_switch")
        attrs["delete_volume"] = _record("delete_volume")
        attrs["compose_volume_ref"] = lambda self, p, v: f"{p}/{v}"
        cls = type("_RecDriver", (HypervisorDriver,), attrs)
        return cls(), calls

    def test_install_switch_dispatches_to_destroy_switch(self) -> None:
        d, calls = self._driver_recording()
        d.destroy("install_switch", "tr_switch_install")
        assert calls == [("destroy_switch", "tr_switch_install")]

    def test_switch_dispatches_to_destroy_switch(self) -> None:
        d, calls = self._driver_recording()
        d.destroy("switch", "tr_switch_sw1")
        assert calls == [("destroy_switch", "tr_switch_sw1")]

    def test_install_network_dispatches_to_destroy_network(self) -> None:
        d, calls = self._driver_recording()
        d.destroy("install_network", "tr_net_install")
        assert calls == [("destroy_network", "tr_net_install")]

    def test_install_vm_dispatches_to_destroy_vm(self) -> None:
        d, calls = self._driver_recording()
        d.destroy("install_vm", "tr_install_vm_x")
        assert calls == [("destroy_vm", "tr_install_vm_x")]

    def test_unknown_kind_raises(self) -> None:
        d, _ = self._driver_recording()
        with pytest.raises(NotImplementedError, match="destroy"):
            d.destroy("nonsense_kind", "x")
