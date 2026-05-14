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
    "get_lease_ip",
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
