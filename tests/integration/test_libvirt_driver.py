"""Integration tests against a real libvirt + KVM setup.

Skipped automatically when libvirt-python is missing or no libvirtd is
reachable. Run via ``pytest -m libvirt`` when you have one.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.libvirt


def _libvirt_available() -> bool:
    try:
        import libvirt
    except ImportError:
        return False
    try:
        import libvirt

        conn = libvirt.open("qemu:///system")
        if conn is None:
            return False
        conn.close()
        return True
    except Exception:
        return False


if not _libvirt_available():
    pytest.skip(
        "libvirt-python not installed or qemu:///system unreachable",
        allow_module_level=True,
    )


from pathlib import Path  # noqa: E402

from testrange.devices import StoragePool  # noqa: E402
from testrange.drivers.libvirt import LibvirtDriver  # noqa: E402
from testrange.networks import Network, Switch  # noqa: E402


def test_create_destroy_network(tmp_path: Path) -> None:
    driver = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
    driver.connect()
    try:
        backend_name = driver.compose_resource_name("integ-1", "network", "netA")
        n = Network("netA")
        sw = Switch("sw1", n, cidr="192.0.2.0/24", dhcp=True, dns=True)
        driver.create_network(n, sw, backend_name)
        driver.destroy_network(backend_name)
    finally:
        driver.disconnect()


def test_create_destroy_pool(tmp_path: Path) -> None:
    driver = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
    driver.connect()
    try:
        backend_name = driver.compose_resource_name("integ-1", "pool", "pool1")
        pool = StoragePool("pool1", 1)
        driver.create_pool(pool, backend_name)
        driver.destroy_pool(backend_name)
    finally:
        driver.disconnect()
