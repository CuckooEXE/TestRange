"""TestRange — VM-based test environment orchestrator.

TestRange makes it easy to spin up isolated KVM/QEMU virtual machine
environments for integration testing, version compatibility testing, and
any other scenario that requires a real OS.

Quick-start example::

    from testrange import (
        Test, Orchestrator, VM, VirtualNetwork,
        Credential, Apt, vCPU, Memory, VirtualNetworkRef, HardDrive,
    )

    def smoke_test(orchestrator: Orchestrator) -> None:
        vm = orchestrator.vms["web"]
        assert vm.hostname() == "web"
        assert b"nginx" in vm.exec(["systemctl", "status", "nginx"]).stdout

    tests = [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.0.1.0/24", internet=True, dhcp=True),
                ],
                vms=[
                    VM(
                        name="web",
                        iso="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
                        users=[
                            Credential("root", "Password123!"),
                            Credential("deploy", "Password123!", sudo=True),
                        ],
                        pkgs=[Apt("nginx")],
                        post_install_cmds=["systemctl enable --now nginx"],
                        devices=[
                            vCPU(2),
                            Memory(2),
                            VirtualNetworkRef("Net"),
                            HardDrive(20),  # 20 GiB OS disk
                        ],
                    ),
                ],
            ),
            smoke_test,
        ),
    ]

Prerequisites (system packages, not pip):
    - ``libvirt-daemon-system`` + ``qemu-kvm``  (or ``qemu-system-x86``)
    - ``qemu-utils``  (provides ``qemu-img``)
    - ``libvirt-dev``  (C headers, required by ``libvirt-python`` at install)
    - User must be in the ``libvirt`` group or run as root
"""

from testrange._version import __version__
from testrange.backends.libvirt import (
    VM,
    LibvirtOrchestrator,
    Orchestrator,
    VirtualNetwork,
)
from testrange.communication.base import ExecResult
from testrange.credentials import Credential
from testrange.devices import HardDrive, Memory, VirtualNetworkRef, vCPU
from testrange.exceptions import (
    CacheError,
    CloudInitError,
    GuestAgentError,
    ImageNotFoundError,
    NetworkError,
    OrchestratorError,
    TestRangeError,
    VMBuildError,
    VMNotRunningError,
    VMTimeoutError,
)
from testrange.networks.base import AbstractVirtualNetwork
from testrange.orchestrator_base import AbstractOrchestrator
from testrange.packages import Apt, Dnf, Homebrew, Pip, Winget
from testrange.storage import (
    AbstractStorageBackend,
    LocalStorageBackend,
    SSHStorageBackend,
)
from testrange.test import Test, TestResult, run_tests
from testrange.vms.base import AbstractVM
from testrange.vms.builders import (
    Builder,
    CloudInitBuilder,
    NoOpBuilder,
    WindowsUnattendedBuilder,
)

__all__ = [
    "__version__",
    # Core
    "Test",
    "TestResult",
    "run_tests",
    "Orchestrator",
    "LibvirtOrchestrator",
    "AbstractOrchestrator",
    # VM + credentials
    "VM",
    "AbstractVM",
    "Credential",
    # Builders
    "Builder",
    "CloudInitBuilder",
    "WindowsUnattendedBuilder",
    "NoOpBuilder",
    # Storage backends
    "AbstractStorageBackend",
    "LocalStorageBackend",
    "SSHStorageBackend",
    # Networks
    "VirtualNetwork",
    "AbstractVirtualNetwork",
    # Devices
    "vCPU",
    "Memory",
    "HardDrive",
    "VirtualNetworkRef",
    # Packages
    "Apt",
    "Dnf",
    "Homebrew",
    "Pip",
    "Winget",
    # Communication
    "ExecResult",
    # Exceptions
    "TestRangeError",
    "VMBuildError",
    "VMTimeoutError",
    "VMNotRunningError",
    "GuestAgentError",
    "NetworkError",
    "CacheError",
    "ImageNotFoundError",
    "CloudInitError",
    "OrchestratorError",
]
