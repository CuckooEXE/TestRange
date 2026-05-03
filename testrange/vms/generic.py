"""Backend-agnostic VM spec.

:class:`GenericVM` is a sibling of every backend-specific VM
(``<Backend>VM`` under :mod:`testrange.backends.<backend>`) on
:class:`~testrange.vms.base.AbstractVM` — same architecture as the
device split (``HardDrive`` vs ``<Backend>HardDrive``).  Use it for
tests that don't need any backend-specific knob; the orchestrator
converts each ``GenericVM`` into its own native VM type at
``__enter__`` time.

Cannot itself :meth:`build`, :meth:`start_run`, or :meth:`shutdown`
— it's a pure spec container.  Calling those raises immediately
with a clear message: a ``GenericVM`` should never reach the
provisioning code paths because the orchestrator has converted it
first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.exceptions import VMBuildError
from testrange.vms.base import AbstractVM
from testrange.vms.builders import auto_select_builder

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.builders.base import Builder


_COMMUNICATOR_KINDS = ("guest-agent", "ssh", "winrm")


class GenericVM(AbstractVM):
    """Backend-agnostic VM specification.

    Same constructor surface as every backend-specific VM (since the
    orchestrator translates field-for-field at provision time).
    Pass it as ``vms=[GenericVM(...)]`` when the test doesn't care
    which backend runs it; the orchestrator's :meth:`__enter__` will
    swap each ``GenericVM`` for its native concrete VM class (the
    matching ``<Backend>VM`` under :mod:`testrange.backends`) before
    any backend code touches it.

    :param name: Unique name for this VM within a test run.
    :param iso: OS image reference (URL or absolute local path).
    :param users: List of credentials to provision / pass through.
    :param pkgs: Packages to install during the build phase.
    :param post_install_cmds: Shell / PowerShell commands run at the
        end of the install phase.
    :param devices: Virtual hardware (vCPU, Memory, vNIC,
        HardDrive, …).  Only generic devices and devices specific to
        the chosen backend are accepted by the converted VM; mixing
        a foreign-backend device with ``GenericVM`` will surface as
        a ``VMBuildError`` at orchestrator entry.
    :param builder: Explicit
        :class:`~testrange.vms.builders.base.Builder` strategy.  When
        ``None`` (the default) the registry's auto-selector picks
        one from ``iso`` (Windows install ISO →
        ``WindowsUnattendedBuilder``, everything else →
        ``CloudInitBuilder``).
    :param communicator: ``"guest-agent"``, ``"ssh"``, or ``"winrm"``.
        ``None`` (default) lets the builder pick.
    """

    _name: str

    def __init__(
        self,
        name: str,
        iso: str,
        users: list[Credential],
        pkgs: list[AbstractPackage] | None = None,
        post_install_cmds: list[str] | None = None,
        devices: list[AbstractDevice] | None = None,
        builder: Builder | None = None,
        communicator: str | None = None,
    ) -> None:
        self._name = name
        self.iso = iso
        self.users = users
        self.pkgs = list(pkgs or [])
        self.post_install_cmds = list(post_install_cmds or [])
        self.devices = list(devices or [])

        if builder is None:
            builder = auto_select_builder(iso)
        self.builder = builder

        if communicator is None:
            communicator = self.builder.default_communicator()
        self.communicator = communicator
        if communicator not in _COMMUNICATOR_KINDS:
            raise VMBuildError(
                f"VM {name!r}: communicator={communicator!r} is not "
                f"one of {_COMMUNICATOR_KINDS}"
            )

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Provisioning methods — should never run on a GenericVM.  The
    # orchestrator's __enter__ replaces every GenericVM in its vm list
    # with the backend's concrete VM class before any of these are
    # called.  If one fires anyway, surface the wiring bug clearly.
    # ------------------------------------------------------------------

    def _generic_vm_misuse(self) -> VMBuildError:
        return VMBuildError(
            f"GenericVM {self.name!r}: backend operation called on a "
            "spec-only GenericVM.  This means the orchestrator failed "
            "to convert it to its backend-specific VM type at "
            "__enter__; either call the orchestrator inside a "
            "``with`` block or pass a backend-specific VM directly."
        )

    def build(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
        install_network_ip: str = "",
    ) -> str:
        del context, cache, run, install_network_name, install_network_mac
        del install_network_ip
        raise self._generic_vm_misuse()

    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> None:
        del context, run, installed_disk, network_entries, mac_ip_pairs
        raise self._generic_vm_misuse()

    def shutdown(self) -> None:
        raise self._generic_vm_misuse()

    def __repr__(self) -> str:
        return f"GenericVM(name={self._name!r}, iso={self.iso!r})"


__all__ = ["GenericVM"]
