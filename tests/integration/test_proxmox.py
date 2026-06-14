"""PVE-7: integration suite against a live Proxmox VE host.

Exercises the real :class:`ProxmoxDriver` (no fakes) end-to-end with
driver-primitive tests (connect/SDN/storage/VM/QGA) that drive the driver
directly, gated on ``TESTRANGE_PVE_HOST`` (+ ``TESTRANGE_PVE_BASE_QCOW2`` for the
disk/VM tests).

Backend certification no longer lives here: it moved to the ``tests/plans/``
corpus (run via ``testrange run --profile <proxmox> tests/plans/...``), which
supersedes the old ``examples/capabilities.py`` example-as-pytest gate.

The whole module is marked ``proxmox`` and excluded from the default gate
(``pytest -m "not proxmox"``).

Environment:
    TESTRANGE_PVE_HOST       host/IP of the PVE node (driver-primitive tests)
    TESTRANGE_PVE_NODE       node name (default: derived if a single node)
    TESTRANGE_PVE_PASSWORD   root@pam password
    TESTRANGE_PVE_STORAGE    backing storage id (default: local)
    TESTRANGE_PVE_BRIDGE     existing bridge for VM NICs (default: vmbr0)
    TESTRANGE_PVE_BASE_QCOW2 local qcow2 to import for disk/VM tests
    TESTRANGE_PVE_SSH_PASSWORD  root SSH password (only the download test needs it)

Every test cleans up its own resources in a ``finally`` so a failure mid-way
does not leak VMs/volumes/SDN objects onto the host.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network import NetworkIface
from testrange.drivers.proxmox import ProxmoxDriver
from testrange.drivers.proxmox._client import ProxmoxConn
from testrange.handles import NetworkHandle, PoolHandle
from testrange.networks import Network, Switch
from testrange.vms import VMSpec

pytestmark = pytest.mark.proxmox


def _resolve_node(conn_kwargs: dict[str, str | bool | int]) -> str:
    node = os.environ.get("TESTRANGE_PVE_NODE")
    if node:
        return node
    # single-node convenience: ask the API for the only node
    import proxmoxer

    api = proxmoxer.ProxmoxAPI(service="PVE", **conn_kwargs)
    nodes = api.nodes.get()
    if len(nodes) != 1:
        pytest.skip("TESTRANGE_PVE_NODE not set and host is not single-node")
    return str(nodes[0]["node"])


@pytest.fixture(scope="session")
def driver() -> Iterator[ProxmoxDriver]:
    host = os.environ.get("TESTRANGE_PVE_HOST")
    if not host:
        pytest.skip("TESTRANGE_PVE_HOST not set")
    password = os.environ.get("TESTRANGE_PVE_PASSWORD", "")
    storage = os.environ.get("TESTRANGE_PVE_STORAGE", "local")
    node = _resolve_node(
        {"host": host, "user": "root@pam", "password": password, "verify_ssl": False}
    )
    conn = ProxmoxConn(
        host=host,
        node=node,
        password=password,
        backing_storage=storage,
        ssh_password=os.environ.get("TESTRANGE_PVE_SSH_PASSWORD", ""),
    )
    d = ProxmoxDriver(conn)
    d.connect()
    yield d
    d.disconnect()


@pytest.fixture
def run_tag() -> str:
    return "it" + uuid.uuid4().hex[:6]


@pytest.fixture
def base_qcow2() -> Path:
    p = os.environ.get("TESTRANGE_PVE_BASE_QCOW2")
    if not p:
        pytest.skip("TESTRANGE_PVE_BASE_QCOW2 not set (a local qcow2 to import)")
    path = Path(p)
    if not path.is_file():
        pytest.skip(f"TESTRANGE_PVE_BASE_QCOW2 {p!r} is not a file")
    return path


@pytest.fixture
def bridge() -> str:
    return os.environ.get("TESTRANGE_PVE_BRIDGE", "vmbr0")


def test_connect_and_preflight_storage(driver: ProxmoxDriver) -> None:
    # connect() already ran in the fixture; the import-content preflight reads
    # live storage config without mutating anything.
    findings = driver._import_content_findings()
    # Either import is enabled (clean) or we get exactly the actionable finding.
    assert all(f.code == "proxmox-import-content-missing" for f in findings)


def test_sdn_switch_roundtrip(driver: ProxmoxDriver, run_tag: str) -> None:
    switch = Switch("itsw", Network("net0"), cidr="10.231.0.0/24")
    backend = driver.compose_resource_name(run_tag, "switch", "itsw")
    try:
        assert driver.create_switch(switch, backend) is None  # isolated
        net_backend = driver.compose_resource_name(run_tag, "network", "net0")
        vnet = driver.create_network(
            Network("net0"), switch, net_backend, switch_backend_name=backend
        )
        assert isinstance(vnet, str) and vnet
        vnets = {v["vnet"] for v in driver._client.api.cluster.sdn.vnets.get()}
        assert vnet in vnets
    finally:
        driver.destroy_switch(backend)
    assert vnet not in {v["vnet"] for v in driver._client.api.cluster.sdn.vnets.get()}


def test_storage_upload_and_delete(driver: ProxmoxDriver, base_qcow2: Path, run_tag: str) -> None:
    pool = driver.compose_resource_name(run_tag, "pool", "p")
    ref = driver.compose_volume_ref(pool, f"tr-{run_tag}-base.qcow2")
    try:
        driver.upload_to_pool(ref, base_qcow2)
        volids = {
            v["volid"]
            for v in driver._client.api.nodes(driver._client.node)
            .storage(driver._client.storage)
            .content.get()
        }
        assert str(ref) in volids
    finally:
        driver.delete_volume(ref)


def test_vm_lifecycle_and_snapshot(
    driver: ProxmoxDriver, base_qcow2: Path, bridge: str, run_tag: str
) -> None:
    name = driver.compose_resource_name(run_tag, "vm", "web")
    pool = driver.compose_resource_name(run_tag, "pool", "p")
    os_ref = driver.compose_volume_ref(pool, f"{name}.qcow2")
    spec = VMSpec(
        name="web",
        devices=[
            CPU(1),
            Memory(512),
            OSDrive(PoolHandle("p"), 2),
            NetworkIface(NetworkHandle("net0", switch="itsw")),
        ],
    )
    try:
        driver.upload_to_pool(os_ref, base_qcow2)
        driver.create_vm(
            name,
            spec,
            run_tag,
            os_disk_ref=os_ref,
            seed_iso_ref=None,
            network_refs={"net0": bridge},
        )
        assert driver.get_vm_power_state(name) == "shutoff"
        driver.start_vm(name)
        assert driver.get_vm_power_state(name) == "running"

        driver.create_snapshot(name, "snap1", mem=False)
        assert "snap1" in driver.list_snapshots(name)
        driver.restore_snapshot(name, "snap1")
        driver.delete_snapshot(name, "snap1")
        assert "snap1" not in driver.list_snapshots(name)
    finally:
        try:
            driver.destroy_vm(name)
        finally:
            driver.delete_volume(os_ref)


@pytest.mark.skipif(
    not os.environ.get("TESTRANGE_PVE_AGENT_VM"),
    reason="QGA needs a booted guest with qemu-guest-agent (set TESTRANGE_PVE_AGENT_VM=<backend_name>)",
)
def test_qga_exec_against_running_agent(driver: ProxmoxDriver) -> None:
    # Point this at a VM (by its stamped backend name) that is up with the agent.
    name = os.environ["TESTRANGE_PVE_AGENT_VM"]
    result = driver.native_guest_execute(name)(["echo", "tr-ok"])
    assert result.exit_code == 0
    assert b"tr-ok" in result.stdout
