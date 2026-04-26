"""TestRange — VM-based test environment orchestrator.

TestRange makes it easy to spin up isolated virtual machine environments
for integration testing, version compatibility testing, and any other
scenario that requires a real OS.  The top-level API is
hypervisor-neutral — the same ``Test`` / ``Orchestrator`` / ``VM``
shape drives every shipped backend (see :mod:`testrange.backends`);
backend-specific prerequisites are documented alongside each backend.

Quick-start example::

    from testrange import (
        Test, Orchestrator, VM, VirtualNetwork,
        Credential, Apt, vCPU, Memory, vNIC, HardDrive,
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
                            vNIC("Net"),
                            HardDrive(20),  # 20 GiB OS disk
                        ],
                    ),
                ],
            ),
            smoke_test,
        ),
    ]

``Orchestrator`` / ``VM`` / ``VirtualNetwork`` re-exported at the top
level resolve to the default backend.  Alternative backends can be
pulled in directly from :mod:`testrange.backends` — each is a peer
implementation of the same abstract surface.
"""

from testrange._version import __version__
from testrange.backends.libvirt import (
    VM,
    Hypervisor,
    LibvirtOrchestrator,
    Orchestrator,
    VirtualNetwork,
)
from testrange.communication.base import ExecResult
from testrange.credentials import Credential
from testrange.devices import HardDrive, Memory, vNIC, vCPU
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
from testrange.vms.hypervisor_base import AbstractHypervisor

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
    "Hypervisor",
    "AbstractHypervisor",
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
    "vNIC",
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
