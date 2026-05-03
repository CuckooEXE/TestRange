"""Abstract base class for virtual machine definitions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import VMBuildError

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.builders.base import Builder


_COMMUNICATOR_KINDS: tuple[str, ...] = ("guest-agent", "ssh", "winrm")
"""Legal values for the ``communicator=`` kwarg.  Shared across every
backend — the transport kind is part of the hypervisor-neutral spec,
not any backend-specific runtime."""


class AbstractVM(ABC):
    """Abstract interface for a virtual machine instance.

    The constructor populates the hypervisor-neutral spec
    (:attr:`name`, :attr:`iso`, :attr:`users`, :attr:`pkgs`,
    :attr:`post_install_cmds`, :attr:`devices`, :attr:`builder`,
    :attr:`communicator`) so concrete backends only need to add
    backend-specific runtime fields and the lifecycle methods.

    Concrete subclasses must implement :meth:`build`, :meth:`start_run`,
    and :meth:`shutdown`.  Before any of the runtime-call helpers can be
    invoked, the VM must be started and its communicator initialised
    (done automatically by an
    :class:`~testrange.orchestrator_base.AbstractOrchestrator` context
    manager).

    Subclass this to support alternative hypervisors or provisioning
    mechanisms.
    """

    _communicator: AbstractCommunicator | None = None
    """Active communicator instance; ``None`` until the VM is started."""

    # ------------------------------------------------------------------
    # Spec attributes populated by :meth:`__init__`.  Declared at class
    # scope as well so shared helpers and builders (which type ``vm``
    # as :class:`AbstractVM`) can read them without Pyright complaining
    # about attribute-access issues.  Concrete backends may override
    # with compatible types.
    # ------------------------------------------------------------------
    users: list[Credential]
    """Credentials to configure on / pass through to the guest."""

    communicator: str
    """Transport kind: ``"guest-agent"``, ``"ssh"``, or ``"winrm"``."""

    iso: str
    """Source image reference — URL or absolute local path to a
    cloud image, installer ISO, or prebuilt disk image.  See
    :func:`testrange.vms.images.resolve_image`."""

    pkgs: list[AbstractPackage]
    """Packages to install during the install phase.  Empty for
    builders that don't provision packages (e.g.
    :class:`~testrange.vms.builders.NoOpBuilder`)."""

    post_install_cmds: list[str]
    """Shell commands run after package installation during the
    install phase.  Empty for builders that have no install phase."""

    devices: list[AbstractDevice]
    """Virtual hardware attached to this VM — vCPU count, memory
    allocation, hard drives, NIC references."""

    builder: Builder
    """Provisioning strategy.  Reads this VM's spec and produces the
    install- and run-phase domain descriptions the backend consumes."""

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
        """Populate the hypervisor-neutral spec.

        Concrete backends override only to add their own runtime state;
        the spec construction (auto-selecting a builder from ``iso=``,
        defaulting the communicator from the builder, validating it
        against :data:`_COMMUNICATOR_KINDS`) is identical across
        backends and lives here.

        :raises VMBuildError: If ``communicator`` is not one of
            :data:`_COMMUNICATOR_KINDS`.
        """
        # Lazy import: ``vms.builders`` re-exports concrete builders
        # whose own modules pull in pycdlib / passlib at import time;
        # importing them at the top of the abstract base would force
        # those deps on anyone who merely imports :class:`AbstractVM`.
        from testrange.vms.builders import auto_select_builder

        self._name = name
        self.iso = iso
        self.users = users
        self.pkgs = pkgs or []
        self.post_install_cmds = post_install_cmds or []
        self.devices = devices or []

        self.builder = builder if builder is not None else auto_select_builder(iso)

        if communicator is None:
            communicator = self.builder.default_communicator()
        if communicator not in _COMMUNICATOR_KINDS:
            raise VMBuildError(
                f"VM {name!r}: communicator={communicator!r} is not one "
                f"of {_COMMUNICATOR_KINDS}"
            )
        self.communicator = communicator

    @property
    def name(self) -> str:
        """The VM's human-readable name as specified at construction.

        :returns: VM name string.
        """
        return self._name

    def _primary_disk_size(self) -> str:
        """Return the primary (OS) disk's size as a backend-friendly
        string (e.g. ``'64G'``).

        Default implementation: the first
        :class:`~testrange.devices.HardDrive` in :attr:`devices`, or
        ``'20G'`` when no drive is declared.  Backends that need a
        different default override this.
        """
        from testrange.devices import HardDrive
        drives = [d for d in self.devices if isinstance(d, HardDrive)]
        return drives[0].size_string if drives else "20G"

    def _vcpu_count(self) -> int:
        """Return the declared vCPU count, or 2 when none is declared.

        Pure spec lookup — reads :attr:`devices` for the first
        :class:`~testrange.devices.vCPU` entry.  Generic enough that
        every backend reuses it; previously duplicated on
        :class:`LibvirtVM` and :class:`ProxmoxVM`.
        """
        from testrange.devices import vCPU
        vcpus = [d for d in self.devices if isinstance(d, vCPU)]
        return vcpus[0].count if vcpus else 2

    def _memory_kib(self) -> int:
        """Return the declared memory in **KiB**, or 2 GiB when none.

        Reads the first :class:`~testrange.devices.Memory` entry on
        :attr:`devices`.  Backends that render in different units
        compose this with a unit conversion (see
        :meth:`_memory_mib`).  Lifted to the abstract base because the
        same body lived verbatim on both LibvirtVM and ProxmoxVM, and
        the libvirt memory preflight needs it on every VM in the list
        — including a top-level :class:`Hypervisor` that hasn't yet
        been promoted to a concrete backend class.
        """
        from testrange.devices import Memory
        mems = [d for d in self.devices if isinstance(d, Memory)]
        return mems[0].kib if mems else 2 * 1024 * 1024

    def _memory_mib(self) -> int:
        """Return the declared memory in **MiB**.  Convenience wrapper
        around :meth:`_memory_kib` for backends (Proxmox) whose API
        takes mebibytes."""
        return self._memory_kib() // 1024

    def _network_refs(self) -> list[Any]:
        """Return every :class:`~testrange.devices.vNIC`
        on :attr:`devices` in declaration order.

        Pure spec lookup with no backend dependencies — used by
        orchestrators to walk a VM's NICs when assigning IPs and
        building network configs.
        """
        from testrange.devices import vNIC
        return [d for d in self.devices if isinstance(d, vNIC)]

    def _require_communicator(self) -> AbstractCommunicator:
        """Return the active communicator or raise an error.

        :returns: The active :class:`~testrange.communication.base.AbstractCommunicator`.
        :raises VMNotRunningError: If the VM has not been started yet.
        """
        from testrange.exceptions import VMNotRunningError
        if self._communicator is None:
            raise VMNotRunningError(
                f"VM {self.name!r} is not running. "
                "Start the Orchestrator before calling VM methods."
            )
        return self._communicator

    def hostname(self) -> str:
        """Return the VM's hostname as reported by the guest OS.

        Equivalent to running ``hostname`` inside the VM.

        :returns: Hostname string.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: On communication failure.
        """
        return self._require_communicator().hostname()

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Execute a command inside the VM.

        :param argv: Command and arguments list (e.g. ``['uname', '-r']``).
        :param env: Optional extra environment variables.
        :param timeout: Maximum seconds to wait for the command to finish.
        :returns: :class:`~testrange.communication.base.ExecResult` with exit
            code and captured output.
        :raises VMNotRunningError: If the VM is not running.
        :raises VMTimeoutError: If the command exceeds *timeout*.
        :raises CommunicationError: On communication errors.
        """
        return self._require_communicator().exec(argv, env=env, timeout=timeout)

    def get_file(self, path: str) -> bytes:
        """Read a file from the VM's filesystem.

        :param path: Absolute path inside the VM (e.g. ``'/etc/os-release'``).
        :returns: Raw file contents as bytes.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: If the file cannot be read.
        """
        return self._require_communicator().get_file(path)

    def put_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* inside the VM.

        :param path: Absolute destination path inside the VM.
        :param data: Raw bytes to write.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: If the file cannot be written.
        """
        self._require_communicator().put_file(path, data)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read a text file from the VM and decode it.

        Thin wrapper over :meth:`get_file` that decodes the returned bytes.

        :param path: Absolute path inside the VM.
        :param encoding: Text encoding. Defaults to ``utf-8``.
        :returns: Decoded file contents.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: If the file cannot be read.
        :raises UnicodeDecodeError: If the file is not valid for *encoding*.
        """
        return self.get_file(path).decode(encoding)

    def write_text(
        self,
        path: str,
        text: str,
        encoding: str = "utf-8",
    ) -> None:
        """Write *text* to *path* inside the VM.

        Thin wrapper over :meth:`put_file` that encodes *text* first.

        :param path: Absolute destination path inside the VM.
        :param text: String to write.
        :param encoding: Text encoding. Defaults to ``utf-8``.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: If the file cannot be written.
        """
        self.put_file(path, text.encode(encoding))

    def download(self, remote_path: str, local_path: str | Path) -> Path:
        """Copy a file from the VM to the host.

        Parent directories on the host are created automatically so tests
        can point at throwaway ``tmp_path`` subpaths without boilerplate.

        :param remote_path: Absolute path inside the VM.
        :param local_path: Destination on the host. Accepts ``str`` or
            :class:`pathlib.Path`.
        :returns: The resolved host path the bytes were written to.
        :raises VMNotRunningError: If the VM is not running.
        :raises CommunicationError: If the remote file cannot be read.
        :raises OSError: If the local path cannot be written.
        """
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.get_file(remote_path))
        return dest

    def upload(self, local_path: str | Path, remote_path: str) -> None:
        """Copy a file from the host to the VM.

        :param local_path: Source file on the host. Accepts ``str`` or
            :class:`pathlib.Path`.
        :param remote_path: Absolute destination path inside the VM.
        :raises VMNotRunningError: If the VM is not running.
        :raises FileNotFoundError: If *local_path* does not exist.
        :raises CommunicationError: If the remote file cannot be written.
        """
        self.put_file(remote_path, Path(local_path).read_bytes())

    # ------------------------------------------------------------------
    # Communicator factory — shared SSH / WinRM construction lives here
    # so each backend only has to implement the one piece it controls:
    # the guest-agent transport.  SSH and WinRM are hypervisor-neutral
    # (they need an IP and credentials, nothing else).
    # ------------------------------------------------------------------

    def _resolve_communicator_host(
        self,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> str:
        """Return the first non-empty static IP from *mac_ip_pairs*.

        The v1 SSH / WinRM paths require a static IP via a
        :class:`~testrange.devices.vNIC`.  Future
        DHCP-lease discovery plugs in here by replacing / extending
        this method — no other caller inspects ``mac_ip_pairs`` for
        host resolution.
        """
        for _, ip_cidr, _, _ in mac_ip_pairs:
            if ip_cidr:
                return ip_cidr.split("/", 1)[0]
        raise VMBuildError(
            f"VM {self.name!r}: communicator={self.communicator!r} "
            "requires a static IP on at least one vNIC "
            "(e.g. vNIC('Net', ip='10.0.0.10')). "
            "DHCP-only discovery is not supported in v1."
        )

    def _make_communicator(
        self,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> AbstractCommunicator:
        """Construct the configured communicator for this VM.

        Handles the hypervisor-neutral transports (SSH, WinRM) inline
        and delegates the backend-specific ``"guest-agent"`` path to
        :meth:`_make_guest_agent_communicator`.
        """
        if self.communicator == "guest-agent":
            return self._make_guest_agent_communicator()
        if self.communicator == "ssh":
            from testrange.communication.ssh import SSHCommunicator
            host = self._resolve_communicator_host(mac_ip_pairs)
            # Prefer a credential that brought its own SSH key; otherwise
            # fall back to the first credential (password auth).
            cred = next((c for c in self.users if c.ssh_key), self.users[0])
            return SSHCommunicator(
                host=host,
                username=cred.username,
                password=cred.password or None,
            )
        if self.communicator == "winrm":
            from testrange.communication.winrm import WinRMCommunicator
            host = self._resolve_communicator_host(mac_ip_pairs)
            # Windows' built-in Administrator account is represented as
            # the root Credential (see
            # WindowsUnattendedBuilder._admin_password).  Prefer it
            # because it has WinRM access baked in; fall back to the
            # first credential so BYOI images with a non-root admin
            # still work.
            cred = next(
                (c for c in self.users if c.is_root()), self.users[0]
            )
            admin_name = (
                "Administrator" if cred.is_root() else cred.username
            )
            return WinRMCommunicator(
                host=host,
                username=admin_name,
                password=cred.password,
            )
        raise VMBuildError(
            f"VM {self.name!r}: unknown communicator="
            f"{self.communicator!r}"
        )

    def _make_guest_agent_communicator(self) -> AbstractCommunicator:
        """Construct the backend's native guest-agent communicator.

        The default raises :class:`VMBuildError` — backends that do not
        ship a guest-agent path should let this stand.  Backends that
        do (e.g. a virtio-serial channel through their hypervisor, or
        a REST ``/agent`` endpoint) override this method to return
        their native implementation.
        """
        raise VMBuildError(
            f"VM {self.name!r}: communicator='guest-agent' is not "
            f"implemented on the {type(self).__name__} backend.  "
            "Use communicator='ssh' (Linux) or communicator='winrm' "
            "(Windows) instead, or install a backend that ships a "
            "guest-agent communicator."
        )

    @abstractmethod
    def build(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
        install_network_ip: str = "",
    ) -> str:
        """Produce (or fetch from cache) a runnable disk image.

        Called once per VM by the orchestrator during ``__enter__``.
        Install-phase builders boot a one-off domain against the
        context's backend and snapshot the result; no-op builders just
        stage a caller-supplied image.

        :param context: The orchestrator driving this run; concrete VM
            implementations downcast to pick up their backend handle.
        :param cache: Active :class:`~testrange.cache.CacheManager`.
        :param run: Scratch dir for this test run.
        :param install_network_name: Backend-specific name of the
            install-phase network (empty when the VM's builder skips
            the install phase).
        :param install_network_mac: MAC address for the install NIC
            (empty when the VM's builder skips the install phase).
        :param install_network_ip: Static IP allocated to this VM on
            the install network.  Used by builders whose
            :meth:`Builder.has_post_install_hook` returns ``True``
            (the orchestrator threads it through so the post-install
            hook can reach the VM over SSH).  Empty string is the
            default — most builders ignore it.
        :returns: Backend-local ref to the runnable disk image
            (outer-host path for a local backend; remote-host path or
            opaque storage-volume ID for remote ones).
        :raises VMBuildError: If the install phase fails or times out.
        """

    @abstractmethod
    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> None:
        """Create an overlay, start the run-phase domain, attach
        the configured communicator.

        :param context: The orchestrator driving this run.
        :param run: Scratch dir for this test run.
        :param installed_disk: Backend-local ref returned by
            :meth:`build`.
        :param network_entries: ``(backend_network_name, mac)`` pairs,
            one per NIC.
        :param mac_ip_pairs: ``(mac, ip_with_cidr, gateway, nameserver)``
            per NIC.  Empty ``ip_with_cidr`` means DHCP.
        :raises VMBuildError: If the domain cannot start.
        :raises VMTimeoutError: If the communicator never responds.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Gracefully shut down the VM.

        :raises VMNotRunningError: If the VM is not currently running.
        """
