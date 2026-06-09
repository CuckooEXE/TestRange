"""BACKEND-1.D: integration suite against a live local libvirt (qemu:///system).

Exercises the real :class:`LibvirtDriver` (no fakes) end-to-end. Gated two ways:

- the whole module is marked ``libvirt`` and excluded from the default gate
  (``pytest -m "not libvirt"``);
- it skips unless ``qemu:///system`` is reachable (membership in the ``libvirt``
  group — no root) and, for the VM test, unless the ``debian-13`` base image is
  in the local cache.

Reachability-independent by design: the VM test boots a **NIC-less** guest and
drives only host-side operations (lifecycle, the serial build-result sink, and
snapshots), so it needs neither networking nor an in-guest agent. Every test
cleans up its own resources in a ``finally`` so a failure mid-way does not leak
domains/volumes/networks/pools onto the host.

Environment override:
    TESTRANGE_LIBVIRT_URI   libvirt connect URI (default qemu:///system)
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from testrange.cache import CacheEntry, CacheManager
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.pool.base import StoragePool
from testrange.drivers.libvirt import LibvirtDriver
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.exceptions import DriverError
from testrange.networks import Network, Switch
from testrange.vms import VMSpec

pytestmark = pytest.mark.libvirt


@pytest.fixture(scope="session")
def driver() -> Iterator[LibvirtDriver]:
    uri = os.environ.get("TESTRANGE_LIBVIRT_URI", "qemu:///system")
    d = LibvirtDriver(LibvirtConn(libvirt_uri=uri))
    try:
        d.connect()
    except DriverError as e:
        pytest.skip(f"libvirt {uri} not reachable: {e}")
    yield d
    d.disconnect()


@pytest.fixture
def run_tag() -> str:
    return "it" + uuid.uuid4().hex[:6]


@pytest.fixture
def base_image() -> Path:
    info = CacheManager().resolve(CacheEntry("debian-13"), fetch=False)
    if info.path is None:
        pytest.skip("debian-13 base image not in the local cache")
    return info.path


_GiB = 1024**3


def test_storage_pool_and_stream_roundtrip(driver: LibvirtDriver, run_tag: str) -> None:
    pool = driver.compose_resource_name(run_tag, "pool", "p1")
    iso_ref = driver.compose_volume_ref(pool, "seed.iso")
    blank_ref = driver.compose_volume_ref(pool, "data0.qcow2")
    upload_ref = driver.compose_volume_ref(pool, "img.qcow2")
    src = Path(f"/tmp/tr-it-{run_tag}.bin")
    payload = os.urandom(2 * 1024 * 1024) + b"END"
    try:
        driver.create_pool(StoragePool("p1", 8), pool)

        # write_to_pool (raw ISO bytes) round-trips through download.
        driver.write_to_pool(iso_ref, b"ISO-CONTENT-123")
        got = driver.download_from_pool(iso_ref, Path(f"/tmp/tr-it-{run_tag}-iso.out"))
        assert got.read_bytes()[:15] == b"ISO-CONTENT-123"

        # blank qcow2 is sized; resize grows it (no-shrink contract is libvirt's).
        driver.create_blank_volume(blank_ref, 2)
        vol = driver._client.lookup_volume(pool, "data0.qcow2")
        assert vol is not None and vol.info()[1] == 2 * _GiB
        driver.resize_volume(blank_ref, 4)
        grown = driver._client.lookup_volume(pool, "data0.qcow2")
        assert grown is not None and grown.info()[1] == 4 * _GiB

        # upload a host file → download → byte-exact (the cache round-trip).
        src.write_bytes(payload)
        driver.upload_to_pool(upload_ref, src)
        driver.upload_to_pool(upload_ref, src)  # idempotent (no re-upload, no error)
        out = driver.download_from_pool(upload_ref, Path(f"/tmp/tr-it-{run_tag}-img.out"))
        assert out.read_bytes() == payload

        driver.delete_volume(upload_ref)
        driver.delete_volume(upload_ref)  # tolerant of absence
        assert driver._client.lookup_volume(pool, "img.qcow2") is None
    finally:
        driver.destroy_pool(pool)
        for p in (
            src,
            Path(f"/tmp/tr-it-{run_tag}-iso.out"),
            Path(f"/tmp/tr-it-{run_tag}-img.out"),
        ):
            p.unlink(missing_ok=True)
    assert driver._client.lookup_pool(pool) is None


def test_isolated_network_lifecycle(driver: LibvirtDriver, run_tag: str) -> None:
    switch = Switch("itsw", Network("net0"), cidr="10.231.0.0/24")
    backend = driver.compose_resource_name(run_tag, "switch", "itsw")
    try:
        assert driver.create_switch(switch, backend) is None  # isolated: no uplink segment
        net = driver._client.lookup_network(backend)
        assert net is not None and net.isActive()
        # Networks share the switch's one libvirt network (one bridge, many labels).
        net_backend = driver.compose_resource_name(run_tag, "network", "net0")
        assert (
            driver.create_network(Network("net0"), switch, net_backend, switch_backend_name=backend)
            == backend
        )
    finally:
        driver.destroy_switch(backend)
    assert driver._client.lookup_network(backend) is None


def test_vm_lifecycle_serial_sink_and_snapshots(
    driver: LibvirtDriver, base_image: Path, run_tag: str
) -> None:
    name = driver.compose_resource_name(run_tag, "vm", "box")
    pool = driver.compose_resource_name(run_tag, "pool", "p1")
    os_ref = driver.compose_volume_ref(pool, f"{name}.qcow2")
    seed_ref = driver.compose_volume_ref(pool, f"{name}-seed.iso")
    media_ref = driver.compose_volume_ref(pool, f"{name}-media.iso")
    # NIC-less: pure host-side lifecycle, no networking / no in-guest agent needed.
    spec = VMSpec(name="box", devices=[CPU(2), Memory(1024), OSDrive("p1", 4)])
    try:
        driver.create_pool(StoragePool("p1", 8), pool)
        driver.upload_to_pool(os_ref, base_image)
        driver.resize_volume(os_ref, 4)
        driver.write_to_pool(seed_ref, b"dummy-seed")
        # create_vm opens the unix-socket serial sink for a *provisioning* boot —
        # a build VM (build_nic) or an installer-origin boot (boot_media_ref);
        # a seed alone is a sidecar, monitored via QGA, and gets a throwaway pty.
        # Pass a boot medium to trigger the sink without a NIC: the OS disk is
        # bootable (boot order 1) so the guest still boots the base image and
        # emits console bytes; the medium (order 2) is never reached.
        driver.write_to_pool(media_ref, b"dummy-media")
        driver.create_vm(
            name,
            spec,
            run_tag,
            os_disk_ref=os_ref,
            seed_iso_ref=seed_ref,
            network_refs={},
            boot_media_ref=media_ref,
        )
        assert driver.get_vm_power_state(name) == "shutoff"
        driver.start_vm(name)
        assert driver.get_vm_power_state(name) == "running"

        # Serial build-result sink: tail the console; a booting guest emits bytes
        # (we don't wait for a TESTRANGE-RESULT — there's no builder here).
        saw_bytes = False
        deadline = time.monotonic() + 45
        with contextlib.closing(driver.read_build_result_sink(name)) as stream:
            for chunk in stream:
                if chunk:
                    saw_bytes = True
                    break
                if time.monotonic() > deadline:
                    break
        assert saw_bytes, "serial sink produced no console bytes from a booting guest"

        # Snapshots are host-side (no agent): full internal qcow2 checkpoint.
        driver.create_snapshot(name, "snap1", "integration", mem=True)
        assert "snap1" in driver.list_snapshots(name)
        with pytest.raises(DriverError, match="already exists"):
            driver.create_snapshot(name, "snap1")
        driver.restore_snapshot(name, "snap1")
        driver.delete_snapshot(name, "snap1")
        assert "snap1" not in driver.list_snapshots(name)

        driver.shutdown_vm(name, timeout=60.0)
        assert driver.get_vm_power_state(name) == "shutoff"
    finally:
        with contextlib.suppress(Exception):
            driver.destroy_vm(name)
        driver.destroy_pool(pool)
    assert driver._client.lookup_domain(name) is None
