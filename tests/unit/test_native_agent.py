"""CORE-90: each backend driver declares the native agent its channel needs.

The orchestrator brokers ``HypervisorDriver.native_agent_provision()`` into the
builder for a ``NativeCommunicator`` VM (see ``test_cloudinit`` for the builder
injection + cache-key folding, and the gating ``isinstance`` in
``orchestrator/build_phase._probe_vm``). Here we pin each backend's declared
recipe: QGA backends install qemu-guest-agent, ESXi installs open-vm-tools.
"""

from __future__ import annotations

from testrange.builders.base import NativeAgentProvision
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver
from testrange.packages import Apt
from tests.esxi_fakes import FakeEsxiClient
from tests.mock_driver import MockDriver

_QGA = NativeAgentProvision(
    (Apt("qemu-guest-agent"),), ("systemctl enable --now qemu-guest-agent",)
)
_TOOLS = NativeAgentProvision((Apt("open-vm-tools"),), ("systemctl enable --now open-vm-tools",))


def test_mock_driver_declares_qga() -> None:
    # The reference backend models a QGA-style agent (same recipe libvirt and
    # proxmox declare), so the orchestrator unit suite exercises auto-injection.
    assert MockDriver().native_agent_provision() == _QGA


def test_esxi_driver_declares_open_vm_tools() -> None:
    d = ESXiDriver(EsxiConn(host="h", datastore="datastore1"), client=FakeEsxiClient())  # type: ignore[arg-type]
    assert d.native_agent_provision() == _TOOLS
