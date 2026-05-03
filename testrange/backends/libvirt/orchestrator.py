"""Orchestrator — the central coordinator for a test run.

The :class:`Orchestrator` owns the libvirt connection and drives the full
lifecycle of networks and VMs:

1. Open a libvirt connection (local or remote via SSH)
2. Create an ephemeral NAT network for the install phase
3. For each VM: resolve image → build (or hit cache) → create overlay
4. Create test networks with DNS/DHCP entries for all VMs
5. Start each VM, wait for its guest agent to respond
6. Expose VMs via :attr:`vms` dict for use in test functions
7. On exit: destroy VMs, destroy networks, clean up run directory

The orchestrator is designed to be used as a context manager::

    with Orchestrator(networks=[...], vms=[...]) as orch:
        do_test(orch)

It is also used directly by :class:`~testrange.test.Test`.
"""

from __future__ import annotations

import contextlib
import ipaddress
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast
from xml.etree import ElementTree as ET

import libvirt

from testrange._concurrency import install_subnet_lock
from testrange._logging import get_logger, log_duration
from testrange._run import RunDir
from testrange.backends.libvirt._preflight import (
    check_memory,
    declared_gib_per_vm,
    read_meminfo,
)
from testrange.backends.libvirt.network import (
    VirtualNetwork,
    _mac_for_vm_network,
)
from testrange.cache import CacheManager
from testrange.exceptions import NetworkError, OrchestratorError
from testrange.orchestrator_base import AbstractOrchestrator
from testrange.vms.generic import GenericVM
from testrange.backends.libvirt.storage import (
    LocalStorageBackend,
    SSHStorageBackend,
)
from testrange.storage import AbstractStorageBackend

_log = get_logger(__name__)

if TYPE_CHECKING:
    from testrange.backends.libvirt.vm import LibvirtVM
    from testrange.credentials import Credential
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM
    from testrange.vms.hypervisor_base import AbstractHypervisor

def check_name_collisions(
    vms: Sequence[AbstractVM],
    networks: Sequence[AbstractVirtualNetwork],
) -> None:
    """Validate VM and network names for a libvirt orchestrator layer.

    Raises :class:`OrchestratorError` on:

    - duplicate VM names (``orch.vms`` would silently overwrite);
    - VM names whose first 10 characters collide (libvirt domain
      names are formatted as ``tr-<name[:10]>-<runid[:8]>`` — two VMs
      sharing a 10-char prefix would try to ``defineXML`` the same
      domain name);
    - duplicate network names;
    - network names whose first 6 characters collide after
      lowercasing and dropping underscores (libvirt network names are
      formatted as ``tr-<name[:6]>-<runid[:4]>`` with the same
      normalisation — see :meth:`VirtualNetwork.backend_name`).

    Raising at construction time (rather than at ``defineXML``) keeps
    the error message pointing at the config, not at an opaque
    libvirt fault.
    """
    seen_vm: dict[str, str] = {}
    seen_vm_trunc: dict[str, str] = {}
    for vm in vms:
        if vm.name in seen_vm:
            raise OrchestratorError(
                f"duplicate VM name {vm.name!r}; VM names must be "
                "unique within an orchestrator (and within a "
                "Hypervisor's inner vms list)."
            )
        seen_vm[vm.name] = vm.name
        trunc = vm.name[:10]
        prior = seen_vm_trunc.get(trunc)
        if prior is not None and prior != vm.name:
            raise OrchestratorError(
                f"VM name {vm.name!r} collides with {prior!r} after "
                f"10-character truncation ({trunc!r}); libvirt domain "
                "names would clash.  Shorten or disambiguate within "
                "the first 10 characters."
            )
        seen_vm_trunc[trunc] = vm.name

    seen_net: set[str] = set()
    seen_net_trunc: dict[str, str] = {}
    for net in networks:
        if net.name in seen_net:
            raise OrchestratorError(
                f"duplicate network name {net.name!r}; network names "
                "must be unique within an orchestrator."
            )
        seen_net.add(net.name)
        trunc = net.name[:6].lower().replace("_", "")
        prior = seen_net_trunc.get(trunc)
        if prior is not None and prior != net.name:
            raise OrchestratorError(
                f"network name {net.name!r} collides with {prior!r} "
                f"after 6-character truncation ({trunc!r}); libvirt "
                "network names would clash.  Disambiguate within the "
                "first 6 characters (case- and underscore-insensitive)."
            )
        seen_net_trunc[trunc] = net.name


_HYPERVISOR_PKGS: tuple[str, ...] = (
    "libvirt-daemon-system",
    "qemu-system-x86",
    "qemu-utils",
    "libvirt-clients",
)
"""APT packages a libvirt-on-libvirt hypervisor VM needs.

Just enough to answer ``virsh -c qemu:///system`` from the inner
orchestrator and run domains it defines.  Heavier tooling
(``virtinst``, ``libguestfs``) is unnecessary — TestRange drives
libvirt through its Python bindings."""


def _hypervisor_post_install_cmds(users: list[Credential]) -> list[str]:
    """Post-install steps that get libvirtd reachable on the outer VM.

    - ``systemctl enable --now libvirtd`` so the daemon survives a
      reboot.
    - ``virsh net-autostart default`` + ``net-start default`` (both
      ``|| true``) so inner VMs get upstream connectivity out of the
      box; idempotent, both succeed harmlessly when the network is
      already in the desired state.
    - Add every declared user to ``libvirt`` and ``kvm`` so the inner
      ``qemu+ssh://user@.../system`` URI authenticates without sudo.
    """
    cmds: list[str] = [
        "systemctl enable --now libvirtd",
        "virsh net-autostart default || true",
        "virsh net-start default || true",
    ]
    for cred in users:
        cmds.append(f"usermod -aG libvirt,kvm {cred.username}")
    return cmds


def _promote_to_libvirt(vm: LibvirtVM | GenericVM) -> LibvirtVM:
    """Convert a backend-agnostic :class:`GenericVM` (or generic
    :class:`~testrange.vms.hypervisor.Hypervisor`) to the libvirt
    backend's concrete :class:`LibvirtVM` /
    :class:`~testrange.backends.libvirt.hypervisor.Hypervisor`.

    Hypervisors take a separate path because the result must have
    *both* the libvirt VM lifecycle methods and the
    :class:`~testrange.vms.hypervisor_base.AbstractHypervisor` data
    fields — that's what the libvirt-flavoured
    :class:`~testrange.backends.libvirt.hypervisor.Hypervisor`
    concrete class provides.  An already-libvirt input (concrete
    LibvirtVM or libvirt-flavoured Hypervisor) passes through
    unchanged.
    """
    from testrange.backends.libvirt.hypervisor import Hypervisor as _LibvirtHV
    from testrange.backends.libvirt.vm import LibvirtVM as _LibvirtVM
    from testrange.vms.hypervisor_base import AbstractHypervisor

    if isinstance(vm, _LibvirtVM):
        # Includes the libvirt-flavoured concrete Hypervisor.
        return vm
    if isinstance(vm, AbstractHypervisor):
        return _LibvirtHV(
            name=vm.name,
            iso=vm.iso,
            users=vm.users,
            pkgs=vm.pkgs,
            post_install_cmds=vm.post_install_cmds,
            devices=vm.devices,  # type: ignore[arg-type]
            builder=vm.builder,
            communicator=vm.communicator,
            orchestrator=vm.orchestrator,
            vms=vm.vms,
            networks=vm.networks,
        )
    if isinstance(vm, GenericVM):
        return _LibvirtVM(
            name=vm.name,
            iso=vm.iso,
            users=vm.users,
            pkgs=vm.pkgs,
            post_install_cmds=vm.post_install_cmds,
            devices=vm.devices,  # type: ignore[arg-type]
            builder=vm.builder,
            communicator=vm.communicator,
        )
    return vm


_INSTALL_SUBNET_POOL = tuple(f"192.168.{o}.0/24" for o in range(240, 255))
"""Candidate subnets for the ephemeral install-phase network.

The orchestrator picks the first one not already claimed by another
libvirt network at start-up time, so stale state from a crashed prior
run (or an unrelated libvirt network) does not wedge new runs.
"""


def _list_network_names(
    conn: libvirt.virConnect, *, defined_only: bool = False,
) -> list[str]:
    """Return libvirt network names as a proper ``list[str]``.

    Works around libvirt-python's inaccurate type stubs: both
    :meth:`virConnect.listNetworks` and
    :meth:`virConnect.listDefinedNetworks` are annotated as returning
    ``str`` but actually return ``list[str]`` (or ``None`` on some
    older builds).  We normalise ``None`` → ``[]`` and cast so the
    rest of the code is statically clean.

    :param conn: Open libvirt connection.
    :param defined_only: If ``True``, return only inactive (defined-
        but-not-running) networks.  If ``False`` (default), return
        the union of active and inactive names.
    """
    if defined_only:
        return cast(list[str], conn.listDefinedNetworks() or [])
    active = cast(list[str], conn.listNetworks() or [])
    defined = cast(list[str], conn.listDefinedNetworks() or [])
    return active + defined


class Orchestrator(AbstractOrchestrator):
    """libvirt / KVM / QEMU implementation of
    :class:`~testrange.orchestrator_base.AbstractOrchestrator`.

    Coordinates networks and VMs for a single test run.

    :param host: The libvirt host to connect to.  Use ``'localhost'`` or
        ``'127.0.0.1'`` for the local system, or a remote hostname /
        ``user@host`` string for an SSH-tunnelled connection.  You may also
        pass a full libvirt URI (e.g. ``'qemu+ssh://user@host/system'``).
    :param networks: Virtual networks to create for this test.
    :param vms: Virtual machines to provision and start.
    :param cache_root: Override the default cache directory (outer host).
    :param storage_backend: Override the auto-selected
        :class:`~testrange.storage.AbstractStorageBackend`.  Defaults:
        ``qemu:///system`` → :class:`LocalStorageBackend`,
        ``qemu+ssh://[user@]host/system`` → :class:`SSHStorageBackend`.
        Pass explicitly when the auto-selection logic can't guess the
        right thing (custom cache dirs on the remote, tunnelled
        connections, a test harness wanting a fake backend, etc.).

    Remote hosts
    ------------

    When *host* resolves to an SSH-backed libvirt URI, disk images are
    staged to ``/var/tmp/testrange/<ssh_user>/`` on the remote host
    over SFTP and all ``qemu-img`` work runs there — no silent-failure
    "path doesn't exist on the remote".  The remote must have
    ``qemu-utils`` + ``libvirt-daemon-system`` installed and the SSH
    user must be able to run ``qemu-img`` (usually via the ``libvirt``
    group).

    Example::

        orchestrator = Orchestrator(
            host="localhost",
            networks=[VirtualNetwork("TestNet", "10.1.0.0/24", internet=True)],
            vms=[LibvirtVM("server", "debian-12", users=[...], devices=[vCPU(2)])],
        )
        with orchestrator as orch:
            result = orch.vms["server"].exec(["uname", "-r"])

        # Same API against a remote libvirtd:
        with Orchestrator(host="qemu+ssh://kvm.example.com/system", vms=[...]):
            ...
    """

    _host: str
    """libvirt connection target: ``'localhost'``, a hostname, or a full URI."""

    # Narrowed to the concrete libvirt types because only this class
    # ever populates these collections.  Pyright's strict override
    # rule rejects narrowing a mutable ``list[AbstractVM]`` base
    # attribute to ``list[VM]``; the per-symbol ignore is scoped to
    # just this declaration, which is safe because external code
    # reads through the ABC attribute (typed as ``list[AbstractVM]``)
    # and internal code genuinely only stores the concrete type.
    _networks: list[VirtualNetwork]  # pyright: ignore[reportIncompatibleVariableOverride]
    """Test networks to create for this run."""

    _vm_list: list[LibvirtVM]  # pyright: ignore[reportIncompatibleVariableOverride]
    """VM specifications to provision."""

    _cache: CacheManager
    """Disk-image cache manager used for this run."""

    # Same story as _vm_list / _networks above: only this class
    # populates the dict, so it's always ``VM`` at runtime.  Narrowing
    # the declared type tightens checks inside this module; the
    # per-symbol ignore suppresses Pyright's strict-override rule
    # which doesn't accept a narrower mutable override even though
    # nothing external mutates this attribute.
    vms: dict[str, LibvirtVM]  # pyright: ignore[reportIncompatibleVariableOverride]
    """Running VMs keyed by name; populated after :meth:`__enter__`."""

    _conn: libvirt.virConnect | None
    """Active libvirt connection; ``None`` before :meth:`__enter__`."""

    _run: RunDir | None
    """Scratch directory for the current test run; ``None`` outside a run."""

    _install_network: VirtualNetwork | None
    """Ephemeral NAT network used during the install phase; ``None`` outside install."""

    _nested_stack: contextlib.ExitStack | None
    """ExitStack holding every entered inner orchestrator (nested
    hypervisor VMs).  LIFO-unwound during teardown so inner state is
    cleaned up before the outer VMs it lives on are destroyed.
    ``None`` before :meth:`__enter__` and after :meth:`_teardown`.
    """

    _inner_orchestrators: list[AbstractOrchestrator]
    """Entered inner orchestrators keyed by outer Hypervisor VM order.

    Kept alongside ``_nested_stack`` so tests / tooling can introspect
    the live nested structure without reaching into the stack.
    """

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[VirtualNetwork] | None = None,
        vms: Sequence[LibvirtVM | GenericVM] | None = None,
        cache_root: Path | None = None,
        cache: str | None = None,
        cache_verify: bool | str = True,
        storage_backend: AbstractStorageBackend | None = None,
        switches: Sequence[object] | None = None,
    ) -> None:
        # ``switches`` is accepted for cross-backend portability —
        # libvirt's network model puts every network on its own
        # bridge with no separate switch layer, so the field is
        # ignored after construction.  See
        # :doc:`/usage/networks`'s "Multiple networks per switch"
        # section: passing a ``Switch`` works on libvirt for
        # config portability but the underlying virsh net definition
        # doesn't carry a switch concept.  An earlier cut omitted
        # this kwarg entirely, so passing one TypeErrored — docs
        # claimed portability the code didn't deliver.
        del switches
        self._host = host
        # The narrower concrete types declared in the class body are
        # what internal code relies on.  Pyright re-checks the type
        # of the assignment against the ABC's declaration on each
        # assignment too; ignore per-line here for the same reason
        # we ignored the class-body declarations above.
        self._networks = list(networks) if networks else []  # pyright: ignore[reportIncompatibleVariableOverride]
        # Convert any GenericVM specs to LibvirtVM up front so the
        # rest of the orchestrator (and external readers of
        # ``self._vm_list``) see only the backend-native type.
        self._vm_list = [_promote_to_libvirt(v) for v in (vms or [])]  # pyright: ignore[reportIncompatibleVariableOverride]
        check_name_collisions(self._vm_list, self._networks)
        remote = None
        if cache is not None:
            from testrange.cache_http import HttpCache  # noqa: PLC0415
            remote = HttpCache(cache, verify=cache_verify)
        self._cache = (
            CacheManager(root=cache_root, remote=remote)
            if cache_root
            else CacheManager(remote=remote)
        )
        # The orchestrator owns the cache for its lifetime; tag it
        # with our backend identity so HTTP-cache URL keys are
        # namespaced correctly when more than one backend shares a
        # remote.  Not user-facing.
        self._cache.backend_name = self.backend_type()
        # Explicit override wins.  When ``None``, _select_storage_backend
        # inspects the libvirt URI at __enter__ and picks LocalStorage
        # for ``qemu:///system`` or SSHStorage for ``qemu+ssh://…``.
        self._storage: AbstractStorageBackend | None = storage_backend

        # Populated after __enter__
        self.vms = {}  # pyright: ignore[reportIncompatibleVariableOverride]
        self._conn = None
        self._run = None
        self._install_network = None
        self._nested_stack = None
        self._inner_orchestrators = []

    @classmethod
    def backend_type(cls) -> str:
        """Return ``"libvirt"``."""
        return "libvirt"

    @classmethod
    def prepare_outer_vm(cls, hv: AbstractHypervisor) -> None:
        """Inject the libvirtd-host payload on top of the base VM spec.

        For an outer VM to serve as an inner ``LibvirtOrchestrator``'s
        host, the VM's installed Debian needs ``libvirt-daemon-system``
        + ``qemu-system-x86`` + ``qemu-utils`` + ``libvirt-clients``,
        ``libvirtd`` enabled, the default NAT network started, and
        every declared user added to ``libvirt`` + ``kvm``.  The
        original concrete ``Hypervisor`` class did this in
        ``__init__``; with the generic
        :class:`~testrange.vms.hypervisor.Hypervisor`, the per-inner
        payload moves here so that ``Hypervisor(orchestrator=
        ProxmoxOrchestrator, …)`` doesn't drag libvirt apt packages
        through its cache hash.
        """
        from testrange.packages import Apt
        # Prepend rather than extend: libvirtd needs to be installed +
        # running before any caller-supplied post-install commands run
        # (those typically depend on it — e.g. ``virsh net-define``
        # against the inner default network).  Same ordering the
        # original libvirt-only Hypervisor used.
        hv.pkgs[:0] = [Apt(p) for p in _HYPERVISOR_PKGS]
        hv.post_install_cmds[:0] = _hypervisor_post_install_cmds(hv.users)

    @classmethod
    def root_on_vm(
        cls,
        hypervisor: AbstractHypervisor,
        outer: AbstractOrchestrator,
    ) -> Orchestrator:
        """Build a nested :class:`Orchestrator` rooted on ``hypervisor``.

        Derives ``qemu+ssh://<user>@<ip>/system`` from the hypervisor's
        resolved communicator host and the first credential that carries
        an ``ssh_key`` (falling back to the first credential declared on
        the VM).  The inner orchestrator reuses the outer cache root so
        builder cache keys still hit on the outer side — the storage
        backend is auto-selected to :class:`SSHStorageBackend` against
        the hypervisor VM.

        The resulting orchestrator is **not yet entered** — the outer
        orchestrator manages that lifecycle via :class:`ExitStack`.

        :param hypervisor: The just-booted hypervisor VM.  Must have a
            static IP (see :class:`vNIC`) and a credential
            whose matching private key is reachable by ``ssh-agent`` or
            ``~/.ssh/`` — otherwise the nested libvirt URI will fail to
            authenticate.
        :param outer: The outer orchestrator that booted ``hypervisor``;
            used to source the shared cache root.
        :returns: A configured (not yet entered) inner orchestrator.
        :raises OrchestratorError: If the hypervisor's communicator has
            no host we can reach (DHCP-only networking is not supported
            for nested libvirt in v1).
        """
        if not hypervisor.users:
            raise OrchestratorError(
                f"Hypervisor VM {hypervisor.name!r} has no users — "
                "root_on_vm needs at least one Credential for SSH."
            )
        cred = next(
            (c for c in hypervisor.users if c.ssh_key),
            hypervisor.users[0],
        )

        # The hypervisor VM's live communicator stores its reachable
        # host.  Both SSHCommunicator and WinRMCommunicator expose
        # ``_host`` — we only care about the SSH case here (libvirtd
        # over SSH), so anything else is a misconfigured hypervisor.
        comm = hypervisor._require_communicator()
        host = getattr(comm, "_host", None)
        if not host:
            raise OrchestratorError(
                f"Hypervisor VM {hypervisor.name!r}: communicator has "
                "no resolvable host.  Nested libvirt requires "
                "communicator='ssh' + a static IP "
                "(vNIC('Net', ip='10.x.x.x'))."
            )

        # ``no_verify=1`` skips host-key checking for the ephemeral VM,
        # matching how the outer SSH communicator already connected.
        # libvirt-client honours this query parameter in the URI.
        uri = f"qemu+ssh://{cred.username}@{host}/system?no_verify=1"

        # Reuse the outer cache root so post-install cache hits don't
        # re-build identical VMs layer-over-layer.  Inner CacheManager
        # still operates on the outer host for index / sidecar files;
        # the SSH storage backend handles remote disk IO transparently.
        #
        # Image shipping is implicit: the inner orchestrator constructs
        # an :class:`SSHStorageBackend` from the ``qemu+ssh://`` URI,
        # and :meth:`CacheManager.stage_source` uploads from the outer
        # host's ``images/`` cache into the hypervisor VM's cache via
        # SFTP on first access.  No explicit copy loop is needed.
        outer_cache_root: Path | None = None
        outer_cache = getattr(outer, "_cache", None)
        if outer_cache is not None:
            outer_cache_root = outer_cache.root

        return cls(
            host=uri,
            networks=hypervisor.networks,  # pyright: ignore[reportArgumentType]
            vms=hypervisor.vms,  # pyright: ignore[reportArgumentType]
            cache_root=outer_cache_root,
        )

    def keep_alive_hints(self) -> list[str]:
        """Emit ``virsh`` commands the user would run to clean up
        domains and networks left behind by ``--keep``.

        The domain/network names come straight off the live orchestrator
        state — same names the normal teardown path would target.
        """
        hints: list[str] = []
        run_id = self._run.run_id if self._run else ""
        for vm in self._vm_list:
            domain = f"tr-{vm.name[:10]}-{run_id[:8]}"
            hints.append(
                f"sudo virsh destroy {domain} && sudo virsh undefine {domain}"
            )
        for net in self._networks:
            try:
                net_name = net.backend_name()
            except Exception:
                net_name = net.name
            hints.append(
                f"sudo virsh net-destroy {net_name} "
                f"&& sudo virsh net-undefine {net_name}"
            )
        return hints

    def _build_uri(self) -> str:
        """Translate :attr:`host` into a libvirt connection URI.

        :returns: A libvirt URI string.
        """
        if self._host in ("localhost", "127.0.0.1", "::1"):
            return "qemu:///system"
        if "://" in self._host:
            # Already a full URI
            return self._host
        return f"qemu+ssh://{self._host}/system"

    def _select_storage_backend(self) -> AbstractStorageBackend:
        """Pick a storage backend based on the libvirt URI.

        Local URIs (``qemu:///system``) → :class:`LocalStorageBackend`
        rooted at the outer cache root.  ``qemu+ssh://[user@]host/…``
        URIs → :class:`SSHStorageBackend` connecting to the same host.
        Explicit overrides via ``storage_backend=`` win; anything else
        falls through to local so unknown URI shapes fail loud at
        domain-define time rather than silently-corrupt some path.
        """
        if self._storage is not None:
            return self._storage

        if self._host in ("localhost", "127.0.0.1", "::1"):
            return LocalStorageBackend(self._cache.root)

        # Parse ``qemu+ssh://[user@]host[:port]/system`` — we only use
        # the user, host, and port to build the SSH connection; the
        # libvirt connection itself is handled by libvirt's own URI
        # parser via libvirt.open().
        if "://" in self._host:
            _, _, rest = self._host.partition("://")
            hostpart, _, _ = rest.partition("/")
        else:
            hostpart = self._host
        user: str | None = None
        if "@" in hostpart:
            user, _, hostpart = hostpart.partition("@")
        port = 22
        if ":" in hostpart:
            hostpart, _, port_s = hostpart.partition(":")
            try:
                port = int(port_s)
            except ValueError:
                pass

        return SSHStorageBackend(host=hostpart, username=user, port=port)

    def __enter__(self) -> Orchestrator:
        """Open libvirt connection, provision all networks and VMs.

        :returns: ``self``, with :attr:`vms` fully populated.
        :raises OrchestratorError: On libvirt connection failure.
        :raises NetworkError: If a network cannot be created.
        :raises VMBuildError: If a VM install phase fails.
        """
        uri = self._build_uri()
        try:
            self._conn = libvirt.open(uri)
        except libvirt.libvirtError as exc:
            raise OrchestratorError(
                f"Cannot connect to libvirt at {uri!r}: {exc}"
            ) from exc

        # Build the storage backend that matches the libvirt connection.
        # Failure here (e.g. SSH auth rejected) closes the libvirt
        # connection we just opened so teardown doesn't leak it.
        if self._storage is None:
            try:
                self._storage = self._select_storage_backend()
            except Exception:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
                raise

        # Refuse to provision if the declared plan would push the host
        # over the memory threshold.  Runs before RunDir is created so
        # a failed preflight leaves zero filesystem state behind.
        with log_duration(_log, "memory preflight"):
            self._preflight_memory()

        self._run = RunDir(self._storage)

        try:
            self._provision(self._run)
        except BaseException:
            # Best-effort cleanup on partial setup.  ``BaseException`` —
            # not ``Exception`` — so Ctrl+C during a long install wait
            # still runs teardown before the interrupt propagates.
            from testrange._debug import pause_on_error_if_enabled
            pause_on_error_if_enabled(
                "libvirt orchestrator __enter__ raised; "
                "VMs are still alive on the libvirt host",
                orchestrator=self,
            )
            self._teardown()
            raise

        return self

    def __exit__(self, *_: object) -> None:
        """Destroy all VMs and networks and clean up the run directory.

        Any exception raised during teardown is swallowed so it cannot mask
        the exception that caused the ``with`` block to exit.  Returns
        ``None`` so the original exception (if any) still propagates.
        """
        try:
            self._teardown()
        except Exception:
            # _teardown() is already defensively coded to never raise; this
            # is a belt-and-braces guard against future regressions.
            pass

    def cleanup(self, run_id: str) -> None:
        """Tear down resources from a prior run that exited uncleanly.

        Reconstructs every libvirt domain and network name this
        orchestrator's ``__enter__`` would have created for *run_id*
        — the names are deterministic functions of (vm_name,
        run_id) and (network_name, run_id) — and best-effort
        destroys + undefines each.  Already-deleted resources are
        silently skipped so this is idempotent.

        Names checked for *run_id*:

        * ``tr-<vm[:10]>-<run_id[:8]>`` — run-phase domain per VM
        * ``tr-build-<vm[:10]>-<run_id[:8]>`` — install-phase domain
          per VM (only present if a build was in flight when the
          run died)
        * ``tr-<net[:6]>-<run_id[:4]>`` — per-test network (with
          the same name normalisation
          :class:`VirtualNetwork.backend_name` applies)
        * ``tr-instal-<run_id[:4]>`` — the ephemeral install
          network for the run
        * ``<cache_root>/runs/<run_id>/`` — per-run scratch
          directory (overlays, seed ISOs, NVRAM)

        Opens its own libvirt connection — does **not** call
        :meth:`__enter__`, since there's nothing to provision.

        :param run_id: UUID4 of the leaked run, the only
            nondeterministic input.
        """
        from testrange.backends.libvirt.network import VirtualNetwork as _VN

        uri = self._build_uri()
        _log.info("cleanup: connecting to %s for run %s", uri, run_id[:8])
        try:
            conn = libvirt.open(uri)
        except libvirt.libvirtError as exc:
            raise OrchestratorError(
                f"cleanup: cannot open libvirt connection {uri!r}: {exc}"
            ) from exc

        try:
            # 1. Per-VM domains.
            for vm in self._vm_list:
                for prefix in ("tr-", "tr-build-"):
                    name = f"{prefix}{vm.name[:10]}-{run_id[:8]}"
                    self._cleanup_domain(conn, name)

            # 2. Per-test networks (compute their backend names by
            #    binding each spec to this run_id).
            for net in self._networks:
                if not isinstance(net, _VN):
                    _log.debug(
                        "cleanup: skipping non-libvirt network %r", net.name,
                    )
                    continue
                # Binding mutates per-instance state; we're outside an
                # active run so no concurrent code looks at it.
                net.bind_run(run_id)
                self._cleanup_network(conn, net.backend_name())

            # 3. The ephemeral install network for this run.  Mirrors
            #    the construction in _create_install_network(): name is
            #    ``install-<run_id[:4]>`` which truncates to libvirt
            #    ``tr-instal-<run_id[:4]>``.
            install_logical = f"install-{run_id[:4]}"
            install_backend = (
                f"tr-{install_logical[:6].lower().replace('_','')}"
                f"-{run_id[:4]}"
            )
            self._cleanup_network(conn, install_backend)
        finally:
            try:
                conn.close()
            except libvirt.libvirtError:
                pass

        # 4. Per-run scratch dir (filesystem op, no libvirt needed).
        run_dir = self._cache.root / "runs" / run_id
        if run_dir.exists():
            import shutil
            try:
                shutil.rmtree(run_dir)
                _log.info("cleanup: removed run dir %s", run_dir)
            except OSError as exc:
                _log.warning(
                    "cleanup: failed to remove run dir %s: %s", run_dir, exc,
                )

    @staticmethod
    def _cleanup_domain(conn: libvirt.virConnect, name: str) -> None:
        """Best-effort destroy + undefine of a libvirt domain by name."""
        try:
            domain = conn.lookupByName(name)
        except libvirt.libvirtError:
            return  # not present — nothing to clean up
        _log.info("cleanup: destroying domain %r", name)
        try:
            if domain.isActive():
                domain.destroy()
        except libvirt.libvirtError as exc:
            _log.warning(
                "cleanup: destroy of domain %r failed (ignored): %s",
                name, exc,
            )
        try:
            domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        except (libvirt.libvirtError, AttributeError):
            try:
                domain.undefine()
            except libvirt.libvirtError as exc:
                _log.warning(
                    "cleanup: undefine of domain %r failed (ignored): %s",
                    name, exc,
                )

    @staticmethod
    def _cleanup_network(conn: libvirt.virConnect, name: str) -> None:
        """Best-effort destroy + undefine of a libvirt network by name."""
        try:
            net = conn.networkLookupByName(name)
        except libvirt.libvirtError:
            return  # not present
        _log.info("cleanup: destroying network %r", name)
        try:
            if net.isActive():
                net.destroy()
        except libvirt.libvirtError as exc:
            _log.warning(
                "cleanup: destroy of network %r failed (ignored): %s",
                name, exc,
            )
        try:
            net.undefine()
        except libvirt.libvirtError as exc:
            _log.warning(
                "cleanup: undefine of network %r failed (ignored): %s",
                name, exc,
            )

    def _preflight_memory(self) -> None:
        """Refuse to provision if the declared memory budget would
        push the target host above the memory-usage threshold.

        Reads the *target* host's ``/proc/meminfo`` via the already-
        selected storage transport, so the check reports what actually
        matters: for local libvirt that's this machine, for
        ``qemu+ssh://`` backends it's the remote host.  See
        :mod:`testrange.backends.libvirt._preflight` for the algorithm
        and environment-variable override.

        :raises OrchestratorError: If the projected post-allocation
            usage would reach the threshold.  The message enumerates
            each VM so the user knows what to trim.
        """
        assert self._storage is not None
        meminfo = read_meminfo(self._storage.transport)
        declared = declared_gib_per_vm(self._vm_list)
        check_memory(meminfo, declared)

    def _provision(self, run: RunDir) -> None:
        """Internal provisioning sequence.

        :param run: Scratch dir for this test run.
        """
        assert self._conn is not None

        _log.info(
            "provisioning run %s: %d VM(s), %d network(s)",
            run.run_id[:8],
            len(self._vm_list),
            len(self._networks),
        )

        # Builders whose needs_install_phase() returns False (NoOp) skip
        # the install domain entirely — they hand back a ready disk.
        install_free_vms = [
            vm for vm in self._vm_list
            if not vm.builder.needs_install_phase()
        ]
        needs_install_network = any(
            vm.builder.needs_install_phase() for vm in self._vm_list
        )

        if install_free_vms:
            _log.info(
                "VMs skipping install phase: %s",
                [vm.name for vm in install_free_vms],
            )

        if needs_install_network:
            # 0. Remove any install networks left over from prior crashed
            # runs — they would collide with our new one on the
            # 192.168.24x.0/24 subnet.
            self._cleanup_stale_install_networks()

            # 1. Create and start the install-phase NAT network.
            #
            # Subnet picking is a check-then-act race: two concurrent runs
            # would both see the same pool slot free.  The file lock
            # serialises the pick + define + start across runs in this
            # process and across processes, so only the bring-up is
            # sequentialised — the rest of provisioning runs in parallel.
            with install_subnet_lock():
                self._install_network = self._create_install_network(run.run_id)
                with log_duration(
                    _log,
                    f"start install network "
                    f"{self._install_network.backend_name()!r}",
                ):
                    self._install_network.start(self)

        # 2. Bind test networks + assign per-VM IPs.  Pure
        # bookkeeping (no libvirt calls, no network bring-up) — but
        # ordered BEFORE the build phase so that
        # :meth:`_setup_test_networks`'s "stamp the picked IP back
        # onto the vNIC" step has happened before any builder reads
        # ``vNIC.ip``.  ``ProxmoxAnswerBuilder._network_block`` is
        # the one that cares: without the stamp it falls back to
        # ``source = "from-dhcp"`` and the PVE installer freezes the
        # install-phase 192.168.24x lease as the run-phase IP.
        self._setup_test_networks(run.run_id)

        # 3. Build (or retrieve from cache) installed disk images.
        # Refs are backend-local strings — for LocalStorageBackend
        # these are outer-host paths identical to the pre-backend
        # behaviour; for SSH backends they're paths on the remote.
        installed_disks: dict[str, str] = {}
        with log_duration(_log, f"install phase for {len(self._vm_list)} VM(s)"):
            for vm in self._vm_list:
                if vm.builder.needs_install_phase():
                    assert self._install_network is not None
                    install_net_name = self._install_network.backend_name()
                    install_mac = _mac_for_vm_network(vm.name, "__install__")
                    # Only look up the install IP when the builder will
                    # actually use it for a post-install hook over SSH.
                    # Most builders (cloud-init, Windows, NoOp) don't
                    # need it, and keeping the lookup conditional means
                    # we don't poke at ``_vm_entries`` (a libvirt-
                    # backend internal) for VMs that won't reach the
                    # SSH dance.  The ledger's tuples are
                    # ``(vm_name, mac, ip)``.
                    # Strict bool comparison (``is True``) so the
                    # IP lookup doesn't fire on Mock-spec'd builders
                    # in tests, whose ``has_post_install_hook()``
                    # returns a truthy ``MagicMock`` instance by
                    # default rather than ``True``.  The contract
                    # documents the return as ``bool``; treat
                    # anything else as opt-out.
                    if vm.builder.has_post_install_hook() is True:
                        install_ip = next(
                            (entry_ip for entry_name, _, entry_ip
                             in self._install_network._vm_entries
                             if entry_name == vm.name),
                            "",
                        )
                    else:
                        install_ip = ""
                else:
                    # Install-free VMs (NoOp / BYOI) don't need a NIC on
                    # the install network; pass empty strings through to
                    # keep build()'s signature stable.
                    install_net_name = ""
                    install_mac = ""
                    install_ip = ""
                with log_duration(_log, f"build VM {vm.name!r}"):
                    installed_disks[vm.name] = vm.build(
                        context=self,
                        cache=self._cache,
                        run=run,
                        install_network_name=install_net_name,
                        install_network_mac=install_mac,
                        install_network_ip=install_ip,
                    )

        # 4. Stop the install network (VMs are off at this point)
        if self._install_network is not None:
            _log.debug("stopping install network")
            self._install_network.stop(self)
            self._install_network = None

        # 5. Start test networks
        for net in self._networks:
            with log_duration(_log, f"start test network {net.name!r}"):
                net.start(self)

        # 6. Start each VM and wait for guest agent
        with log_duration(_log, f"boot {len(self._vm_list)} VM(s) to ready"):
            for vm in self._vm_list:
                network_entries, mac_ip_pairs = self._build_nic_entries(vm)
                with log_duration(_log, f"start VM {vm.name!r}"):
                    vm.start_run(
                        context=self,
                        run=run,
                        installed_disk=installed_disks[vm.name],
                        network_entries=network_entries,
                        mac_ip_pairs=mac_ip_pairs,
                    )
                self.vms[vm.name] = vm

        # 7. Enter inner orchestrators for any Hypervisor VMs.  The
        # ExitStack owns the unwind so a partial failure here exits
        # every already-entered inner orchestrator before propagating
        # to the outer teardown path.
        self._enter_nested_orchestrators()
        _log.info("all VMs ready; handing off to test function")

    def _enter_nested_orchestrators(self) -> None:
        """Enter an inner orchestrator for each
        :class:`AbstractHypervisor` VM in the run.

        Called last in :meth:`_provision` so every outer VM is already
        booted and its communicator is ready.  Inner orchestrators are
        entered sequentially — a single-threaded ExitStack is enough
        because ``root_on_vm`` does not block on anything expensive
        beyond the inner bring-up (which itself parallelises).
        """
        from testrange.vms.hypervisor_base import AbstractHypervisor

        hypervisors = [
            vm for vm in self._vm_list
            if isinstance(vm, AbstractHypervisor)
        ]
        if not hypervisors:
            return

        stack = contextlib.ExitStack()
        entered: list[AbstractOrchestrator] = []
        try:
            for hv in hypervisors:
                with log_duration(
                    _log, f"enter inner orchestrator on {hv.name!r}",
                ):
                    inner = hv.orchestrator.root_on_vm(hv, self)
                    stack.enter_context(inner)
                    entered.append(inner)
        except BaseException:
            # ExitStack closes in LIFO order — whatever succeeded gets
            # unwound before the original exception propagates.
            stack.close()
            raise

        self._nested_stack = stack
        self._inner_orchestrators = entered

    def _cleanup_stale_install_networks(self) -> None:
        """Undefine any *inactive* install networks left by crashed runs.

        Install networks all have the ``tr-instal-`` name prefix (the
        15-char libvirt limit truncates the full ``tr-install-<id>``).
        Any that are **not currently active** are necessarily leftovers
        from a crash — active ones belong to a peer run on the same
        host and must not be touched.

        Without this cleanup, a stale definition from a prior crash
        would keep its subnet reserved on next startup.
        """
        assert self._conn is not None
        try:
            defined = _list_network_names(self._conn, defined_only=True)
        except libvirt.libvirtError:
            return

        # ``listDefinedNetworks()`` only returns *inactive* networks, which
        # is exactly what we want — it will not include install networks
        # owned by a concurrent peer run.
        for name in defined:
            if not name.startswith("tr-instal-"):
                continue
            try:
                net = self._conn.networkLookupByName(name)
                if net.isActive():
                    # Paranoia: skip anything that somehow shows up active.
                    continue
                net.undefine()
            except libvirt.libvirtError:
                # Best-effort: if this fails, the next step will surface
                # a more useful error when it hits the actual conflict.
                pass

    def _pick_install_subnet(self) -> str:
        """Choose an install subnet no other libvirt network is using.

        Iterates :data:`_INSTALL_SUBNET_POOL` and returns the first
        entry whose CIDR does not overlap any existing libvirt network.
        Falls back to the first pool entry if every slot is taken (in
        which case the subsequent :meth:`~VirtualNetwork.start` will
        fail with a clearer diagnostic).

        :returns: CIDR string such as ``'192.168.240.0/24'``.
        """
        assert self._conn is not None
        used: list[ipaddress.IPv4Network] = []
        try:
            names = _list_network_names(self._conn)
        except libvirt.libvirtError:
            names = []

        for name in names:
            try:
                net_obj = self._conn.networkLookupByName(name)
                root = ET.fromstring(net_obj.XMLDesc())
                ip_el = root.find("ip")
                if ip_el is None:
                    continue
                addr = ip_el.attrib.get("address")
                mask = ip_el.attrib.get("netmask", "255.255.255.0")
                if addr:
                    used.append(
                        ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
                    )
            except (libvirt.libvirtError, ET.ParseError, ValueError):
                continue

        for candidate in _INSTALL_SUBNET_POOL:
            cand_net = ipaddress.IPv4Network(candidate, strict=False)
            if not any(cand_net.overlaps(u) for u in used):
                return candidate

        # Pool exhausted — raise rather than fall back to
        # ``_INSTALL_SUBNET_POOL[0]`` (which would then collide
        # with whichever peer run already owns it).  Symmetric
        # with the proxmox backend's ``_pick_install_subnet``
        # error path.  Operators with bigger CI fleets can widen
        # the pool by editing the constant; backpressure here is
        # the right failure mode.
        raise OrchestratorError(
            f"every install-subnet pool entry "
            f"({_INSTALL_SUBNET_POOL[0]} – {_INSTALL_SUBNET_POOL[-1]}) "
            f"overlaps an existing libvirt network on this host.  "
            "Either wait for an in-flight run to finish, or expand "
            "the pool by editing ``_INSTALL_SUBNET_POOL`` in "
            "``testrange/backends/libvirt/orchestrator.py``."
        )

    def _create_install_network(self, run_id: str) -> VirtualNetwork:
        """Create an ephemeral NAT network for the install phase.

        :param run_id: Current run UUID.
        :returns: A configured (but not yet started) :class:`VirtualNetwork`.
        """
        subnet = self._pick_install_subnet()
        net = VirtualNetwork(
            name=f"install-{run_id[:4]}",
            subnet=subnet,
            dhcp=True,
            internet=True,
            # DNS must be on: install-phase VMs need name resolution for
            # apt/dnf to reach upstream repos. Libvirt's dnsmasq advertises
            # itself as the DHCP-handed resolver, so disabling DNS here
            # would leave guests pointed at a port that isn't listening.
            dns=True,
        )
        net.bind_run(run_id)
        # Register install-phase VMs so they get DHCP leases during install.
        # Prebuilt VMs are skipped — they never boot on this network.  The
        # install-phase MAC is derived from (vm_name, "__install__") rather
        # than (vm_name, net.name), so bypass register_vm (which computes
        # its own MAC) and use register_vm_with_mac.
        net_obj = ipaddress.IPv4Network(subnet, strict=False)
        hosts = list(net_obj.hosts())
        install_phase_vms = [
            vm for vm in self._vm_list
            if vm.builder.needs_install_phase()
        ]
        # Bound the host-index loop so a fleet larger than the
        # subnet raises a clear NetworkError instead of a bare
        # IndexError ("list index out of range" with no context).
        if len(install_phase_vms) > len(hosts) - 1:
            raise NetworkError(
                f"install network subnet {subnet} has {len(hosts) - 1} "
                f"non-gateway host(s) but {len(install_phase_vms)} "
                "VMs need an install-phase NIC.  Either pick a wider "
                "install-subnet pool entry (edit "
                "``_INSTALL_SUBNET_POOL`` in "
                "``testrange/backends/libvirt/orchestrator.py``) or "
                "split the run across multiple orchestrator instances."
            )
        for idx, vm in enumerate(install_phase_vms):
            ip = str(hosts[idx + 1])  # skip gateway (.1)
            mac = _mac_for_vm_network(vm.name, "__install__")
            net.register_vm_with_mac(vm.name, mac, ip)
        return net

    def _setup_test_networks(self, run_id: str) -> None:
        """Bind the run ID to all test networks and register VM IPs.

        :param run_id: Current run UUID.
        """
        # Bind every network once up-front so ``backend_name()`` works below
        # and so that re-used network objects get the current run's suffix
        # rather than a stale one from a prior run.
        for net in self._networks:
            net.bind_run(run_id)

        # Per-network counter for auto-IP assignment.
        net_counters: dict[str, int] = {net.name: 0 for net in self._networks}

        for vm in self._vm_list:
            for nic in vm._network_refs():
                net = self._find_network(nic.ref)
                if net is None:
                    raise NetworkError(
                        f"VM {vm.name!r} references unknown network {nic.ref!r}. "
                        f"Available networks: {[n.name for n in self._networks]}"
                    )

                if nic.ip:
                    # Static IP — register with the explicit address
                    net.register_vm(vm.name, nic.ip)
                else:
                    # Auto-assign from the subnet, then stamp the
                    # picked IP back onto the vNIC so downstream
                    # readers (ProxmoxAnswerBuilder._network_block in
                    # particular — it inspects ``vNIC.ip`` directly
                    # and falls back to a broken ``from-dhcp`` mode
                    # when None) see a unified static-IP view.  The
                    # libvirt cloud-init builder also benefits: it
                    # gets to emit a static network-config block
                    # instead of ``dhcp4: true``, skipping a DHCP
                    # round-trip at boot for the same end address.
                    idx = net_counters[nic.ref]
                    ip = net.static_ip_for_index(idx)
                    net_counters[nic.ref] = idx + 1
                    nic.ip = ip
                    net.register_vm(vm.name, ip)

    def _find_network(self, name: str) -> VirtualNetwork | None:
        """Find a network by its logical name.

        :param name: Network name to search for.
        :returns: The matching network, or ``None``.
        """
        for net in self._networks:
            if net.name == name:
                return net
        return None

    def _build_nic_entries(
        self, vm: LibvirtVM
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
        """Build the NIC parameters needed for domain XML and network-config.

        :param vm: The VM whose network refs to process.
        :returns: A tuple of ``(network_entries, mac_ip_pairs)`` where:
            - ``network_entries`` is a list of ``(lv_net_name, mac)`` for
              domain XML
            - ``mac_ip_pairs`` is a list of
              ``(mac, ip_with_cidr, gateway, nameserver)`` for cloud-init
              network-config. ``gateway`` is empty unless the network has
              ``internet=True``; ``nameserver`` is empty unless ``dns=True``.
        """
        network_entries: list[tuple[str, str]] = []
        mac_ip_pairs: list[tuple[str, str, str, str]] = []

        for nic in vm._network_refs():
            net = self._find_network(nic.ref)
            if net is None:
                continue
            mac = _mac_for_vm_network(vm.name, nic.ref)
            lv_name = net.backend_name()
            network_entries.append((lv_name, mac))

            # Only networks with internet=True should advertise a default
            # gateway — otherwise two default routes fight for egress and
            # traffic meant for the public internet can leak onto an
            # isolated bridge. Likewise, only dns=True networks contribute
            # a resolver (dnsmasq is disabled when dns=False, so the
            # gateway IP is not a listening DNS server).
            gateway = net.gateway_ip if net.internet else ""
            nameserver = net.gateway_ip if net.dns else ""
            cidr = f"{nic.ip}/{net.prefix_len}" if nic.ip else ""
            mac_ip_pairs.append((mac, cidr, gateway, nameserver))

        return network_entries, mac_ip_pairs

    def _teardown(self) -> None:
        """Destroy every active VM, network, run artifact, and connection.

        Every step is independently guarded.  A failure in one VM shutdown
        does not stop the remaining VMs from being shut down, nor does it
        prevent networks from being destroyed, the run directory from being
        cleaned, or the libvirt connection from being closed.

        This method is declared never to raise: any bug elsewhere in the
        library that surfaces during provisioning is the *reason* teardown
        is running, and a cleanup failure must not mask the original bug.

        When :attr:`_leaked` is set (via :meth:`leak`), the VM/network/run-dir
        steps are skipped and only the process-local handles (libvirt
        connection, storage backend) are closed.  See :meth:`leak` for the
        full contract and footguns.
        """
        if self._conn is None:
            return

        if self._leaked:
            self._leak_and_close()
            return

        _log.info("teardown starting")

        # Inner orchestrators first: their VMs live *inside* outer
        # hypervisor VMs, so unwinding the inner stack before the
        # outer VMs get destroyed keeps the "teardown from the top"
        # invariant intact.  ExitStack.close() propagates any single
        # exception; aggregate wrapping isn't needed — nothing in
        # this path should mask the original reason teardown ran.
        if self._nested_stack is not None:
            try:
                self._nested_stack.close()
            except Exception as exc:
                _log.debug("inner orchestrator teardown raised (ignored): %s", exc)
            self._nested_stack = None
            self._inner_orchestrators = []

        for vm in self._vm_list:
            try:
                vm.shutdown()
            except Exception as exc:
                _log.debug("shutdown of VM %r raised (ignored): %s", vm.name, exc)

        for net in self._networks:
            try:
                net.stop(self)
            except Exception as exc:
                _log.debug(
                    "stop of network %r raised (ignored): %s", net.name, exc
                )

        if self._install_network is not None:
            try:
                self._install_network.stop(self)
            except Exception as exc:
                _log.debug("stop of install network raised (ignored): %s", exc)
            self._install_network = None

        if self._run is not None:
            try:
                self._run.cleanup()
            except Exception as exc:
                _log.debug("run dir cleanup raised (ignored): %s", exc)
            self._run = None

        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

        # Close the storage backend (SSH connection, if any).  Local
        # backends no-op; SSH backends actually tear down paramiko
        # channels, which matters for long-running processes that
        # create and destroy many orchestrators.
        if self._storage is not None:
            close = getattr(self._storage, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    _log.debug(
                        "storage backend close raised (ignored): %s", exc,
                    )
            self._storage = None

        self.vms = {}  # pyright: ignore[reportIncompatibleVariableOverride]
        _log.info("teardown complete")

    def _leak_and_close(self) -> None:
        """Leak-mode teardown: preserve VMs/networks/run-dir, but still
        close the libvirt connection and storage-backend handles so
        the Python process can exit cleanly (paramiko threads in
        particular will hang interpreter shutdown otherwise).

        Inner orchestrators inherit the leak via ``inner._leaked = True``
        so the nested stack's unwind short-circuits the same way —
        otherwise closing the stack would tear the inner VMs down,
        defeating the whole point for nested configurations.
        """
        assert self._conn is not None
        _log.info(
            "leak=True — preserving %d VM(s) and %d network(s); no teardown",
            len(self._vm_list), len(self._networks),
        )
        if self._run is not None:
            _log.info("run directory preserved: %s", self._run.path)

        hints = self.keep_alive_hints()
        if hints:
            _log.info("manual cleanup commands:")
            for hint in hints:
                _log.info("  %s", hint)

        # Propagate leak into nested orchestrators before closing the
        # ExitStack — without this, inner ``__exit__`` runs its full
        # teardown and destroys the inner VMs we're trying to preserve.
        for inner in self._inner_orchestrators:
            inner._leaked = True
        if self._nested_stack is not None:
            try:
                self._nested_stack.close()
            except Exception as exc:
                _log.debug(
                    "inner orchestrator close raised (ignored): %s", exc,
                )
            self._nested_stack = None
            self._inner_orchestrators = []

        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

        if self._storage is not None:
            close = getattr(self._storage, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    _log.debug(
                        "storage backend close raised (ignored): %s", exc,
                    )
            self._storage = None

        # Drop the vms dict — the orchestrator is done managing them;
        # the user's own references to VM objects (from before the
        # ``with`` block exited) still work for inspection, they just
        # aren't routed through ``orch.vms`` anymore.
        self.vms = {}  # pyright: ignore[reportIncompatibleVariableOverride]
        # Note: do NOT touch self._run — it still points at the run
        # dir we just declared preserved.  Clearing it would make a
        # future caller with a reference to this orchestrator think
        # the dir is gone.


LibvirtOrchestrator = Orchestrator
"""Explicit alias for :class:`Orchestrator`.

Use this name in code that wants to be clear about which backend it's
asking for — e.g. when other backends also exist.  The unqualified
:class:`Orchestrator` is the documented default.
"""
