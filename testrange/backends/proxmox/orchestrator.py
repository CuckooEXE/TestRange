"""Proxmox VE orchestrator.

Authenticates against the PVE REST API (via the ``proxmoxer``
package), resolves a target node + image-capable storage pool,
ensures TestRange's SDN simple-zone exists, brings up an
ephemeral install-phase vnet + the user-declared run-phase vnets,
provisions VMs from cached PVE templates, and tears everything
down on exit.  Supports nested orchestration —
:meth:`root_on_vm` produces a ``ProxmoxOrchestrator`` rooted on a
just-booted PVE Hypervisor VM, and ``__enter__`` enters any
inner ``Hypervisor``-typed VMs the same way the libvirt backend
does (see :meth:`_enter_nested_orchestrators`).

Known limits documented in the project's TODO.md (no SDN-side
dnsmasq for run-phase ``<vm>.<net>`` name resolution, no memory
preflight against PVE node capacity, …) — open follow-ups, not
undocumented surprises.

The CLI URL form for this backend is
``proxmox://USER:PASS@HOST[:PORT]/NODE?storage=NAME`` — see
:func:`testrange.backends.proxmox.cli_build_orchestrator`.

Architecture
------------

The Proxmox backend drives a PVE node via:

- the REST API (``proxmoxer`` wraps auth + retries) for the majority
  of the lifecycle — authenticate, list nodes / storage, manage SDN
  zones / vnets / subnets / IPAM, create + start + stop + delete VMIDs,
  upload installer ISOs, snapshot post-install disks;
- fallback shell-outs to ``qm`` / ``pct`` over SSH for the handful of
  storage-pool operations the REST API doesn't cleanly expose (e.g.
  importing a qcow2 into an LVM-thin pool).

The builder layer (:class:`~testrange.vms.builders.CloudInitBuilder`,
:class:`~testrange.vms.builders.WindowsUnattendedBuilder`,
:class:`~testrange.vms.builders.NoOpBuilder`,
:class:`~testrange.vms.builders.ProxmoxAnswerBuilder`) is shared with
libvirt — their :class:`~testrange.vms.builders.base.InstallDomain` /
:class:`~testrange.vms.builders.base.RunDomain` outputs are
hypervisor-neutral.  Only the *rendering* into backend-native calls
differs: where libvirt emits domain XML,
:class:`~testrange.backends.proxmox.vm.ProxmoxVM` translates the same
dataclasses into ``qm create`` / REST parameters.

Roadmap
-------

In dependency order:

1. **Authentication + zone bootstrap** (this slice).  ``__enter__``
   logs in, picks a node, picks an image-capable storage pool, and
   ensures TestRange's SDN simple-zone exists.
2. **SDN vnet + IPAM** (next slice).
   :meth:`~testrange.backends.proxmox.network.ProxmoxVirtualNetwork.start`
   creates a vnet + subnet under the zone, registers static-IP
   entries via IPAM, reloads SDN.
3. **VM build / start_run** — translate
   :class:`~testrange.vms.builders.base.InstallDomain` into
   ``POST /nodes/{node}/qemu`` parameters; poll ``status/current``
   until the install domain stops; snapshot the disk; create an
   overlay clone for the run phase; start it; attach a communicator.
4. **Guest-agent communicator** —
   :class:`~testrange.backends.proxmox.guest_agent.ProxmoxGuestAgentCommunicator`
   talks to ``/nodes/{node}/qemu/{vmid}/agent``.
5. **Teardown** — stop, delete VMIDs, delete vnets, reload SDN.

Non-goals (for v1 of the Proxmox backend)
-----------------------------------------

- LXC containers — TestRange is VM-focused and LXC has different
  semantics for most features.
- HA failover / live migration — single-node use is the v1 target.
"""

from __future__ import annotations

import contextlib
import ipaddress
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from testrange._logging import get_logger, log_duration
from testrange.backends.proxmox.network import (
    ProxmoxSwitch,
    ProxmoxVirtualNetwork,
    _mac_for_vm_network,
)
from testrange.backends.proxmox.vm import ProxmoxVM
from testrange.cache import CacheManager
from testrange.exceptions import NetworkError, OrchestratorError
from testrange.orchestrator_base import AbstractOrchestrator, recursive_vm_iter

if TYPE_CHECKING:
    from testrange.networks.base import AbstractSwitch, AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM
    from testrange.vms.generic import GenericVM
    from testrange.vms.hypervisor_base import AbstractHypervisor

_log = get_logger(__name__)

_INSTALL_SUBNET_POOL: tuple[str, ...] = tuple(
    f"192.168.{o}.0/24" for o in range(230, 240)
)
"""Candidate subnets for the ephemeral install-phase SDN vnet.

Sits below libvirt's install pool (``192.168.240.0/24`` –
``192.168.254.0/24``) so concurrent libvirt + proxmox runs on the
same host don't confuse each other.  Ten candidates leaves enough
headroom for a small CI fleet hitting the same PVE node without
collisions; ``_pick_install_subnet`` consults PVE's existing SDN
subnets at ``__enter__`` time and chooses the first pool entry not
already claimed by another in-flight run.

Per-process atomicity comes from the SDN vnet's per-run name
(``inst<run_id[:4]>``) — the create call wins or fails outright, and
on conflict we'd surface a clear NetworkError.  No PVE-side lock is
required because picking a still-unused subnet from a 10-entry pool
plus the random-prefixed vnet name closes the race window past CI-
fleet scale.
"""

_PROXMOX_INSTALL_SUBNET = _INSTALL_SUBNET_POOL[0]
"""Backwards-compat alias for the first pool entry — the constant
some tests still import directly.  Prefer
:data:`_INSTALL_SUBNET_POOL` for new code."""


DEFAULT_ZONE = "tr"
"""Name of the SDN simple-zone TestRange creates and stashes its
vnets under.  One zone, shared across all runs against the same PVE
host — vnets are namespaced under the zone so concurrent runs don't
collide as long as their vnet names differ.

PVE caps SDN zone IDs at 8 characters.  We use ``"tr"`` to leave six
characters of headroom for users who want to override the default
with a deployment-specific zone name (``"trtest"``, ``"trprod"``,
…).  The default name itself is namespaced enough; it never conflicts
with PVE's built-in zones (which all start with ``localnetwork``)."""


def _parse_token_string(token: str) -> tuple[str, str, str] | None:
    """Decompose a PVE API-token string into ``(user, name, value)``.

    Token shape per PVE: ``<user>@<realm>!<token-name>=<secret>``.
    Returns ``None`` when the input doesn't fit that shape so callers
    can fall back to ticket auth.  Used for the
    ``proxmox://TOKEN@host`` CLI URL form, where the URL parser
    delivers the raw concatenated string and the orchestrator
    decomposes it for :meth:`_resolve_client_kwargs`.

    Examples accepted:

    * ``root@pam!ci=abcd-ef01-2345`` → ``("root@pam", "ci", "abcd-ef01-2345")``
    * ``automation@pve!buildbot=hex...`` → likewise.

    Examples rejected (returns ``None``):

    * ``"root"``  — no realm, no token marker
    * ``"root@pam"`` — no token marker, just a user
    * ``"root@pam!ci"`` — token name without secret
    """
    # ``=`` splits user@realm!name from secret; ``!`` splits user@realm
    # from name.  Order matters because the secret can contain ``!``.
    if "=" not in token or "!" not in token:
        return None
    user_realm_name, _, value = token.partition("=")
    if not value:
        return None
    user_realm, _, name = user_realm_name.partition("!")
    if not user_realm or "@" not in user_realm or not name:
        return None
    return user_realm, name, value


def _promote_to_proxmox(vm: ProxmoxVM | "GenericVM") -> ProxmoxVM:
    """Convert a backend-agnostic :class:`GenericVM` (or generic
    :class:`~testrange.vms.hypervisor.Hypervisor`) to the proxmox
    backend's concrete :class:`ProxmoxVM` /
    :class:`~testrange.backends.proxmox.hypervisor.Hypervisor`.

    Symmetric with
    :func:`testrange.backends.libvirt.orchestrator._promote_to_libvirt`:
    a hypervisor input becomes the proxmox-flavoured concrete
    Hypervisor (``ProxmoxVM + AbstractHypervisor``); a plain
    GenericVM becomes a plain ProxmoxVM.  Already-ProxmoxVM (or
    proxmox-flavoured Hypervisor) inputs pass through unchanged.
    """
    from testrange.backends.proxmox.hypervisor import (
        Hypervisor as _ProxmoxHV,
    )
    from testrange.vms.generic import GenericVM as _GenericVM
    from testrange.vms.hypervisor_base import AbstractHypervisor

    if isinstance(vm, ProxmoxVM):
        return vm
    if isinstance(vm, AbstractHypervisor):
        return _ProxmoxHV(
            name=vm.name,
            iso=vm.iso,
            users=vm.users,
            pkgs=vm.pkgs,
            post_install_cmds=vm.post_install_cmds,
            devices=vm.devices,
            builder=vm.builder,
            communicator=vm.communicator,
            orchestrator=vm.orchestrator,
            vms=vm.vms,
            networks=vm.networks,
        )
    if isinstance(vm, _GenericVM):
        return ProxmoxVM(
            name=vm.name,
            iso=vm.iso,
            users=vm.users,
            pkgs=vm.pkgs,
            post_install_cmds=vm.post_install_cmds,
            devices=vm.devices,
            builder=vm.builder,
            communicator=vm.communicator,
        )
    return vm


def _promote_to_proxmox_network(
    net: "AbstractVirtualNetwork",
) -> ProxmoxVirtualNetwork:
    """Convert any :class:`AbstractVirtualNetwork` to the proxmox
    backend's concrete :class:`ProxmoxVirtualNetwork`.

    Same shape-preserving translation as :func:`_promote_to_proxmox`
    but for networks: copy the user-facing fields (name, subnet,
    dhcp, internet, dns, switch) into a fresh ProxmoxVirtualNetwork.
    An already-ProxmoxVirtualNetwork input passes through unchanged.

    The translation is necessary because ``testrange.VirtualNetwork``
    re-exports the libvirt backend's class for top-level ergonomics —
    a user constructing ``Hypervisor(orchestrator=ProxmoxOrchestrator,
    networks=[VirtualNetwork(...)])`` would otherwise hand the inner
    ProxmoxOrchestrator a libvirt-flavoured network whose
    :meth:`start` reaches for ``context._conn`` and fails.
    """
    if isinstance(net, ProxmoxVirtualNetwork):
        return net
    return ProxmoxVirtualNetwork(
        name=net.name,
        subnet=net.subnet,
        dhcp=net.dhcp,
        internet=net.internet,
        dns=net.dns,
        switch=net.switch,
    )


def _registered_ip_for(net: ProxmoxVirtualNetwork, vm_name: str) -> str | None:
    """Return the IP *vm_name* is registered with on *net*, or ``None``.

    Walks the network's ``_vm_entries`` ledger written by
    :meth:`ProxmoxVirtualNetwork.register_vm`.  Used by
    :meth:`ProxmoxOrchestrator._vm_network_refs` to thread the
    DHCP-allocated IP through to cloud-init's network-config without
    each caller re-walking the ledger.
    """
    for entry_vm, _, entry_ip in net._vm_entries:
        if entry_vm == vm_name:
            return entry_ip
    return None


def _promote_to_proxmox_switch(sw: "AbstractSwitch") -> ProxmoxSwitch:
    """Convert any :class:`AbstractSwitch` (typically the generic
    :class:`testrange.networks.Switch` spec) to the proxmox backend's
    concrete :class:`ProxmoxSwitch`.

    Field-for-field translation; an already-ProxmoxSwitch input
    passes through unchanged.  Mirrors :func:`_promote_to_proxmox`
    for VMs and :func:`_promote_to_proxmox_network` for networks —
    every backend-agnostic spec class becomes its native peer at
    ``__init__`` time so the rest of the orchestrator only sees
    backend-native instances.
    """
    if isinstance(sw, ProxmoxSwitch):
        return sw
    return ProxmoxSwitch(
        name=sw.name,
        switch_type=sw.switch_type,
        uplinks=sw.uplinks,
    )


class ProxmoxOrchestrator(AbstractOrchestrator):
    """Proxmox VE implementation of
    :class:`~testrange.orchestrator_base.AbstractOrchestrator`.

    :param host: PVE node hostname or IP.  A single node is fine; for
        a cluster, point at any node and pass ``node=`` to pick the
        target.
    :param networks: Virtual networks to create as SDN vnets.  Any
        non-:class:`ProxmoxVirtualNetwork` (e.g. the libvirt-flavoured
        :class:`testrange.VirtualNetwork`) is promoted at __init__.
    :param vms: VMs to provision.  ``GenericVM`` and ``LibvirtVM``
        specs are promoted to :class:`ProxmoxVM` field-for-field at
        __init__ so the rest of the orchestrator only sees the
        backend-native type.
    :param cache_root: Override the default cache directory.
    :param node: Target node name.  Defaults to the only node in
        single-node setups; required for clusters.
    :param storage: Storage-pool name for VM disk images
        (``"local-lvm"``, ``"local-zfs"``, ``"ceph"``…).  Defaults
        to the first pool on the target node that lists ``images``
        in its content set.
    :param port: PVE REST API port.  Defaults to 8006.
    :param user: PVE user, e.g. ``"root@pam"``.  Required with
        ``password`` or ``token_value``.
    :param password: PVE user password.  Mutually exclusive with the
        ``token_*`` kwargs.
    :param token_name: PVE API-token name (the part after the ``!``
        in ``user@pam!tokenname``).
    :param token_value: PVE API-token secret.  Use with
        ``token_name`` and ``user`` for token-based auth (preferred
        over password for service accounts).
    :param verify_ssl: Verify the PVE TLS certificate.  Defaults to
        ``False`` because PVE ships a self-signed cert by default;
        flip to ``True`` once you've replaced the cert.
    :param zone: SDN simple-zone name TestRange uses for its vnets.
        Defaults to ``"testrange"``.  Created on ``__enter__`` if
        missing.
    :param token: **Legacy.**  Dict-shaped credential carrier used by
        :func:`testrange.backends.proxmox.cli_build_orchestrator` —
        ``{"user": ..., "password": ..., "token": ...}``.  Prefer
        the explicit kwargs above.
    """

    # Narrow the abstract ``list[AbstractVirtualNetwork]`` to our
    # concrete subclass so calls to ``bind_run`` / ``register_vm`` /
    # ``backend_name`` type-check without a per-call cast.  The
    # ``pyright: ignore`` matches the libvirt backend's convention —
    # ``list`` is invariant, so the narrow is technically a violation
    # of the LSP variance rule, but it's intentional and safe
    # (mixing backend types in one orchestrator is a user error
    # caught at construction).
    _networks: list[ProxmoxVirtualNetwork]  # type: ignore[assignment] # pyright: ignore[reportIncompatibleVariableOverride]
    _switches: list[ProxmoxSwitch]
    """User-declared :class:`ProxmoxSwitch` instances (one per SDN
    zone the orchestrator owns).  Promoted from any
    :class:`AbstractSwitch` (typically the generic
    :class:`testrange.Switch` spec) at __init__.  Started in
    :meth:`__enter__`, stopped in :meth:`__exit__`.  An empty list
    means "no user-declared switches" — the orchestrator's default
    zone (``self._zone``) handles every vnet, matching the
    pre-Switch behaviour."""
    _started_switches: list[ProxmoxSwitch]
    """Subset of :attr:`_switches` that :meth:`__enter__` actually
    brought up; tracked separately so the rollback path only tears
    down what we created (a partial-failure switch shouldn't be
    deleted)."""
    _started_networks: list[ProxmoxVirtualNetwork]
    _provisioned_vms: list[ProxmoxVM]

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[AbstractVirtualNetwork] | None = None,
        switches: Sequence[AbstractSwitch] | None = None,
        vms: Sequence[AbstractVM] | None = None,
        cache_root: Path | None = None,
        cache: str | None = None,
        cache_verify: bool | str = True,
        storage_backend: object | None = None,
        node: str | None = None,
        storage: str | None = None,
        port: int = 8006,
        user: str | None = None,
        password: str | None = None,
        token_name: str | None = None,
        token_value: str | None = None,
        verify_ssl: bool = False,
        zone: str = DEFAULT_ZONE,
        token: object | None = None,
    ) -> None:
        # ``storage_backend`` is a generic abstraction the libvirt
        # backend uses to switch transports (local FS / SSH SFTP).
        # The Proxmox backend bypasses it entirely — every disk-
        # image operation goes through the PVE REST API instead of
        # a transport-shaped tool wrapper.  Accepting the kwarg and
        # silently ignoring it (as an earlier cut did "for the
        # contract test") was a footgun: users could pass any
        # backend and nothing happened.  Reject explicitly so the
        # mismatch surfaces at construction time.
        if storage_backend is not None:
            raise OrchestratorError(
                "ProxmoxOrchestrator does not consume "
                "``storage_backend=`` — disk operations go through "
                "the PVE REST API.  Use ``node=`` and ``storage=`` "
                "to select the PVE storage pool instead."
            )
        super().__init__(
            host=host, networks=networks, vms=vms, cache_root=cache_root,
            cache=cache, cache_verify=cache_verify,
            storage_backend=None,  # type: ignore[arg-type]
        )
        self._host = host
        self._port = port
        # Promote any non-Proxmox networks to ProxmoxVirtualNetwork
        # up front so the rest of the orchestrator only ever sees
        # backend-native instances.  The top-level
        # ``testrange.VirtualNetwork`` re-export resolves to libvirt's
        # class, so a user constructing
        # ``Hypervisor(orchestrator=ProxmoxOrchestrator,
        # networks=[VirtualNetwork(...)])`` would otherwise hand us
        # libvirt-flavoured networks whose start() reaches for
        # ``context._conn`` and explodes.
        self._networks = [  # pyright: ignore[reportIncompatibleVariableOverride]
            _promote_to_proxmox_network(n) for n in (networks or [])
        ]
        # Promote user-declared switches the same way.  Each becomes
        # a PVE SDN zone that this orchestrator owns end-to-end:
        # created on __enter__, torn down on __exit__.  None or empty
        # is the common case — the orchestrator's default zone
        # (``self._zone``) covers it.
        self._switches = [
            _promote_to_proxmox_switch(s) for s in (switches or [])
        ]
        # Promote any backend-agnostic GenericVM specs to ProxmoxVM
        # up front so the rest of the orchestrator (and external
        # readers of ``self._vm_list``) see only the backend-native
        # type — same pattern as ``LibvirtOrchestrator._promote_to_libvirt``.
        self._vm_list = [_promote_to_proxmox(v) for v in (vms or [])]
        self._cache_root = cache_root
        self._cache_url = cache
        self._cache_verify = cache_verify
        self._node = node
        self._storage = storage
        self._zone = zone
        self._user = user
        self._password = password
        self._token_name = token_name
        self._token_value = token_value
        self._verify_ssl = verify_ssl

        # Translate legacy dict-shaped ``token=`` glue from the URL
        # handler.  The dict carries one of three credential shapes
        # depending on what the URL spelled — see
        # :func:`testrange.backends.proxmox.cli_build_orchestrator`.
        # Token strings have the PVE-canonical shape
        # ``user@realm!name=secret``; parse it into the proper
        # ``_user`` / ``_token_name`` / ``_token_value`` fields here
        # so :meth:`_resolve_client_kwargs` finds an API-token
        # combination.  An earlier cut stashed the unparsed string on
        # ``_legacy_token`` and silently failed auth.
        if isinstance(token, dict):
            if not self._user and token.get("user"):
                self._user = token["user"]
            if not self._password and token.get("password"):
                self._password = token["password"]
            raw_token = token.get("token")
            if raw_token:
                parsed = _parse_token_string(raw_token)
                if parsed is not None:
                    user, name, value = parsed
                    self._user = self._user or user
                    self._token_name = self._token_name or name
                    self._token_value = self._token_value or value

        self._client: Any = None
        self.vms = {}
        self._run = None
        self._run_id: str | None = None
        self._started_networks = []
        self._started_switches = []
        self._install_network: ProxmoxVirtualNetwork | None = None
        """Ephemeral SDN vnet used by every VM during the install
        phase.  Has ``internet=True`` so cloud-init can reach apt /
        dnf mirrors regardless of which user-declared NIC the VM
        ends up on at run time.  Created in :meth:`__enter__` (after
        ``_start_networks``), torn down in :meth:`__exit__`.
        Symmetric with the libvirt backend's ``_install_network``."""
        self._provisioned_vms = []
        # Nested-orchestrator state.  ``_nested_stack`` owns the
        # unwind for every entered inner orchestrator; closing it in
        # ``__exit__`` unwinds them in LIFO order before this
        # orchestrator's own teardown runs.  Mirrors libvirt's
        # convention so a Hypervisor VM hosted on PVE behaves the
        # same way as one hosted on libvirt.
        self._nested_stack: contextlib.ExitStack | None = None
        self._inner_orchestrators: list[AbstractOrchestrator] = []
        # CacheManager creation involves filesystem mutation
        # (mkdir/chmod on the cache root); defer it to __enter__ so
        # cheap construction patterns — CLI URL dispatch, tests
        # constructing instances for spec inspection — don't trip
        # on filesystem permissions.
        self._cache: CacheManager | None = None

        # CacheManager construction mirrors LibvirtOrchestrator's
        # wiring so the cross-backend ``cache.backend_name`` invariant
        # holds.  The contract test in
        # tests/test_backend_contract.py::TestScenarioConstructionContract
        # catches missing setup if this drifts.
        from testrange.cache import CacheManager
        remote = None
        if cache is not None:
            from testrange.cache_http import HttpCache
            remote = HttpCache(cache, verify=cache_verify)
        self._cache = (
            CacheManager(root=cache_root, remote=remote)
            if cache_root
            else CacheManager(remote=remote)
        )
        self._cache.backend_name = self.backend_type()

    @classmethod
    def backend_type(cls) -> str:
        """Return ``"proxmox"``."""
        return "proxmox"

    # ``prepare_outer_vm`` deliberately not overridden: the PVE
    # installer ISO is self-contained for everything *except*
    # dnsmasq, and dnsmasq gets installed via SSH bootstrap in
    # :meth:`_bootstrap_pve_node` rather than through
    # ``vm.pkgs`` / answer.toml ``[first-boot]``.  Inheriting the
    # base no-op keeps the Hypervisor spec's cache hash clean of
    # any payload that would otherwise rebuild every cached PVE
    # qcow2 if the bootstrap script changed.

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> AbstractOrchestrator:
        """Authenticate, resolve node + storage, ensure SDN zone exists.

        :raises OrchestratorError: If ``proxmoxer`` is not installed,
            credentials are missing or wrong, the host is unreachable,
            or no image-capable storage pool can be found.
        """
        try:
            # ``proxmoxer`` is an optional dep — pip-install
            # ``testrange[proxmox]`` to pull it in.  It ships no type
            # stubs, hence the pyright ignore.
            from proxmoxer import ProxmoxAPI  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise OrchestratorError(
                "ProxmoxOrchestrator needs the ``proxmoxer`` Python "
                "package — install with "
                "``pip install testrange[proxmox]``."
            ) from exc

        if self._cache is None:
            self._cache = (
                CacheManager(root=self._cache_root)
                if self._cache_root else CacheManager()
            )

        client_kwargs = self._resolve_client_kwargs()
        _log.info(
            "connecting to PVE %s:%d as %s",
            self._host, self._port, client_kwargs.get("user"),
        )
        try:
            self._client = ProxmoxAPI(**client_kwargs)
            nodes = self._client.nodes.get()
        except Exception as exc:
            raise OrchestratorError(
                f"ProxmoxOrchestrator: cannot reach "
                f"{self._host}:{self._port}: {exc}"
            ) from exc

        self._resolve_node(nodes)
        self._resolve_storage()
        self._preflight_dnsmasq_installed()
        self._ensure_sdn_zone()
        _log.info(
            "PVE ready: node=%s storage=%s zone=%s",
            self._node, self._storage, self._zone,
        )

        # Run setup — every entry gets a fresh RunDir so concurrent
        # runs against the same PVE namespace partition cleanly.
        # Even though ProxmoxVM uploads disks via REST and never
        # touches the local scratch path, ``run.run_id`` flows into
        # the deterministic clone / phase-2-seed names downstream.
        # The local scratch dir lands under the cache root and is
        # cleaned up in __exit__; nothing inside it is load-bearing
        # for the proxmox path today, but having it means any
        # future builder that wants a backend-local scratch file
        # gets the same ergonomics as the libvirt path.
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend
        self._run = RunDir(LocalStorageBackend(cache_root=self._cache.root))
        self._run_id = self._run.run_id
        _log.info("run id: %s", self._run_id[:8])

        try:
            # Switches first — vnets reference their switch's zone,
            # so the zone has to exist before any vnet's start()
            # tries to land in it.
            self._start_switches()
            self._setup_vm_networks()
            self._start_networks()
            # Bring up the install-phase vnet AFTER the user-declared
            # ones — every VM build attaches its install NIC here so
            # cloud-init has internet access regardless of where the
            # VM eventually lives at run time (a user network with
            # ``internet=False`` would otherwise hang ``apt install``
            # forever; see :meth:`_create_install_network`).  Skip
            # entirely if no VM in this run actually has an install
            # phase (NoOpBuilder VMs).
            if any(vm.builder.needs_install_phase() for vm in self._vm_list):
                self._install_network = self._create_install_network()
                with log_duration(
                    _log,
                    f"start install network "
                    f"{self._install_network.backend_name()!r}",
                ):
                    self._install_network.start(self)
            self._provision_vms()
            # Enter inner orchestrators for any Hypervisor VMs.
            # Same contract as the libvirt backend: ExitStack owns
            # the unwind so a partial failure here exits every
            # already-entered inner orchestrator before we propagate
            # to the outer teardown path.
            self._enter_nested_orchestrators()
        except BaseException:
            # ``BaseException`` (not ``Exception``) so that a Ctrl+C
            # during a long install also runs rollback before the
            # interrupt propagates — otherwise SDN vnets, VMIDs, and
            # nested-orchestrator state leak on operator-cancel.
            # Mirrors libvirt's ``__enter__`` rollback discipline.
            from testrange._debug import pause_on_error_if_enabled
            pause_on_error_if_enabled(
                "ProxmoxOrchestrator __enter__ raised; "
                "VMs and SDN vnets are still up on the PVE node",
                orchestrator=self,
            )
            if self._nested_stack is not None:
                self._nested_stack.close()
                self._nested_stack = None
                self._inner_orchestrators = []
            self._teardown_vms()
            self._teardown_install_network()
            self._teardown_networks()
            self._teardown_switches()
            if self._run is not None:
                try:
                    self._run.cleanup()
                except Exception:  # pragma: no cover — defensive
                    pass
                self._run = None
            self._client = None
            self._run_id = None
            raise

        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Tear down SDN vnets and close the PVE client.

        Honours :meth:`leak` by skipping resource teardown (just
        releases the client handle) — the same contract the libvirt
        backend follows.  Per the
        :class:`~testrange.orchestrator_base.AbstractOrchestrator`
        contract, never raises: per-network teardown errors are
        already swallowed by :meth:`ProxmoxVirtualNetwork.stop`, and
        any other unexpected error here is logged.
        """
        try:
            # Propagate ``leak()`` to every inner orchestrator BEFORE
            # closing the nested stack — without this, each inner's
            # ``__exit__`` runs full teardown on its own VMs while
            # the operator was trying to preserve them.  Mirrors
            # libvirt's pattern in ``backends/libvirt/orchestrator.py``.
            if self._leaked:
                for inner in self._inner_orchestrators:
                    try:
                        inner.leak()
                    except Exception as exc:  # pragma: no cover
                        _log.warning(
                            "could not propagate leak() to inner "
                            "orchestrator %r: %s", inner, exc,
                        )

            # Unwind nested inner orchestrators first (LIFO) so each
            # inner orchestrator's __exit__ runs while its hosting
            # PVE VM is still alive.  Tear down our own VMs / vnets
            # only after the inner stack is closed.
            if self._nested_stack is not None:
                try:
                    self._nested_stack.close()
                except Exception as exc:  # pragma: no cover — defensive
                    _log.warning(
                        "unexpected error while unwinding nested "
                        "orchestrators: %s", exc,
                    )
                self._nested_stack = None
                self._inner_orchestrators = []

            if self._leaked:
                hints = self.keep_alive_hints()
                _log.info(
                    "leak() set — leaving %d VM(s) and %d network(s) "
                    "in place; manual cleanup hints follow",
                    len(self._provisioned_vms),
                    len(self._started_networks),
                )
                for line in hints:
                    _log.info("  %s", line)
            else:
                self._teardown_vms()
                self._teardown_install_network()
                self._teardown_networks()
                self._teardown_switches()
                if self._run is not None:
                    try:
                        self._run.cleanup()
                    except Exception as exc:  # pragma: no cover — defensive
                        _log.warning(
                            "unexpected error cleaning run dir: %s", exc,
                        )
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning("unexpected error during PVE teardown: %s", exc)
        finally:
            self._client = None
            self._run_id = None
            self._run = None

    def cleanup(self, run_id: str) -> None:
        """Reconstruct + tear down per-run PVE resources for *run_id*.

        Symmetric with :meth:`testrange.backends.libvirt.Orchestrator.cleanup`:
        reconstructs the deterministic backend names this orchestrator's
        ``__enter__`` would have created and destroys them.

        **Templates are preserved.**  ``tr-template-<config_hash[:12]>``
        VMIDs are persistent cache state — a second run with the same
        spec hits them and skips install.  ``cleanup`` only removes the
        per-run clones (named ``tr-<vm_name[:10]>-<run_id[:8]>``) plus
        any per-run phase-2 seed ISOs.

        SDN vnets named ``<net[:4]><run_id[:4]>`` get destroyed.  The
        SDN zone (a global resource shared across runs) is left intact.

        Opens its own proxmoxer client; does NOT call ``__enter__``
        (no provisioning to redo).
        """
        from testrange.backends.proxmox.network import (
            ProxmoxVirtualNetwork,
        )
        from testrange.backends.proxmox.vm import _TEMPLATE_NAME_PREFIX

        client, node = self._open_admin_connection()

        # 1. Per-VM clones.  Reconstruct the clone name from
        #    (vm.name, run_id) — same formula ProxmoxVM.build uses
        #    for the clone's display name.
        for vm in self._vm_list:
            clone_name = f"tr-{vm.name[:10]}-{run_id[:8]}"
            clone_vmid = self._find_vm_by_name(client, node, clone_name)
            if clone_vmid is None:
                continue
            # Refuse to touch a template even if the name pattern
            # somehow matches.  Templates are persistent cache state.
            if self._is_template(client, node, clone_vmid):
                _log.warning(
                    "cleanup: skipping VMID %d (matches clone name %r "
                    "but is flagged as template)",
                    clone_vmid, clone_name,
                )
                continue
            _log.info(
                "cleanup: stopping + deleting clone VMID %d (%s)",
                clone_vmid, clone_name,
            )
            try:
                client.nodes(node).qemu(clone_vmid).status.stop.post()
            except Exception:
                pass
            try:
                client.nodes(node).qemu(clone_vmid).delete()
            except Exception as exc:
                _log.warning(
                    "cleanup: delete VMID %d failed: %s",
                    clone_vmid, exc,
                )

        # 2. Per-run phase-2 seed ISOs.  Filename pattern is
        #    ``tr-<vm[:10]>-<run_id[:8]>-seed.iso`` (see
        #    ProxmoxVM.start_run).
        for vm in self._vm_list:
            seed_name = f"tr-{vm.name[:10]}-{run_id[:8]}-seed.iso"
            try:
                client.nodes(node).storage("local").content(
                    f"local:iso/{seed_name}",
                ).delete()
            except Exception as exc:
                _log.debug(
                    "cleanup: phase-2 seed %r not deleted (probably "
                    "already gone): %s",
                    seed_name, exc,
                )

        # 3. Per-run SDN vnets.  ProxmoxVirtualNetwork.backend_name
        #    is a pure function of (network.name, run_id), so we
        #    can reconstruct each name without state.
        for net_spec in self._networks:
            if not isinstance(net_spec, ProxmoxVirtualNetwork):
                continue
            net_spec.bind_run(run_id)
            vnet_name = net_spec.backend_name()
            try:
                client.cluster.sdn.vnets(vnet_name).delete()
                _log.info("cleanup: deleted SDN vnet %r", vnet_name)
            except Exception as exc:
                _log.debug(
                    "cleanup: SDN vnet %r not deleted (probably "
                    "already gone): %s",
                    vnet_name, exc,
                )
        # Reload SDN config so the vnet deletes take effect.
        try:
            client.cluster.sdn.put()
        except Exception:
            pass

        _log.info(
            "cleanup: done for run %s; templates (%s*) preserved",
            run_id[:8], _TEMPLATE_NAME_PREFIX,
        )

    @staticmethod
    def _find_vm_by_name(
        client: Any, node: str, name: str,
    ) -> int | None:
        """Return the VMID of the VM on *node* with display name
        *name*, or ``None``."""
        try:
            vms = client.nodes(node).qemu.get()
        except Exception:
            return None
        for vm in vms or []:
            if vm.get("name") == name:
                return int(vm["vmid"])
        return None

    @staticmethod
    def _is_template(client: Any, node: str, vmid: int) -> bool:
        """Return True if VMID is a PVE template."""
        try:
            cfg = client.nodes(node).qemu(vmid).config.get()
        except Exception:
            return False
        return bool(cfg.get("template"))

    def list_templates(self) -> list[dict[str, Any]]:
        """Return TestRange-managed PVE templates on the configured node.

        Each entry is a ``{"vmid": int, "name": str}`` dict.  Names
        always start with the
        :data:`~testrange.backends.proxmox.vm._TEMPLATE_NAME_PREFIX`
        prefix so accidental matches against operator-managed templates
        are filtered out.

        Used by ``testrange proxmox-list-templates`` to surface the
        cache contents and by
        :meth:`prune_templates` to enumerate eviction candidates.
        Opens its own connection — does NOT call :meth:`__enter__`.
        """
        from testrange.backends.proxmox.vm import _TEMPLATE_NAME_PREFIX

        client, node = self._open_admin_connection()
        try:
            vms = client.nodes(node).qemu.get()
        except Exception as exc:
            raise OrchestratorError(
                f"list_templates: cannot list VMs on node {node!r}: {exc}"
            ) from exc

        return [
            {"vmid": int(vm["vmid"]), "name": vm["name"]}
            for vm in vms or []
            if vm.get("template")
            and vm.get("name", "").startswith(_TEMPLATE_NAME_PREFIX)
        ]

    def prune_templates(self, *, names: list[str] | None = None) -> int:
        """Delete TestRange-managed PVE templates from the configured node.

        :param names: Restrict the prune to templates with these
            display names.  ``None`` (default) deletes every
            ``tr-template-*`` VMID — careful with shared PVE hosts.
        :returns: Number of templates actually deleted.
        """
        client, node = self._open_admin_connection()
        candidates = self.list_templates()
        if names is not None:
            wanted = set(names)
            candidates = [t for t in candidates if t["name"] in wanted]

        deleted = 0
        for tpl in candidates:
            vmid = tpl["vmid"]
            try:
                client.nodes(node).qemu(vmid).delete()
                _log.info(
                    "prune: deleted template VMID %d (%r)",
                    vmid, tpl["name"],
                )
                deleted += 1
            except Exception as exc:
                _log.warning(
                    "prune: delete template VMID %d (%r) failed: %s",
                    vmid, tpl["name"], exc,
                )
        return deleted

    def _open_admin_connection(self) -> tuple[Any, str]:
        """Open a proxmoxer connection + resolve the target node.

        Shared between :meth:`cleanup`, :meth:`list_templates`, and
        :meth:`prune_templates` — none of which need ``__enter__``'s
        provisioning side effects but all need the same auth +
        node-resolution dance.
        """
        try:
            from proxmoxer import ProxmoxAPI  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise OrchestratorError(
                "ProxmoxOrchestrator: proxmoxer is required.  Install "
                "with ``pip install testrange[proxmox]``."
            ) from exc
        client_kwargs = self._resolve_client_kwargs()
        # ``_resolve_client_kwargs`` already populates ``host=`` in
        # the dict; passing ``self._host`` positionally too duplicates
        # it and raises ``TypeError: got multiple values for argument
        # 'host'``.  ``__enter__`` calls ``ProxmoxAPI(**client_kwargs)``
        # for the same reason — keep the two paths consistent.
        try:
            client = ProxmoxAPI(**client_kwargs)
        except Exception as exc:
            raise OrchestratorError(
                f"cannot connect to PVE at {self._host!r}: {exc}"
            ) from exc
        try:
            nodes = list(client.nodes.get())
        except Exception as exc:
            raise OrchestratorError(
                f"cannot list PVE nodes: {exc}"
            ) from exc
        self._resolve_node(nodes)
        node = self._node
        assert node is not None
        return client, node

    def keep_alive_hints(self) -> list[str]:
        """Return cleanup commands for resources left behind by
        :meth:`leak`.

        Each line is a self-contained ``pvesh`` invocation a human
        would run on the PVE node to release one resource — useful
        when ``leak()`` was set, the user is done poking, and they
        want to tidy up by hand without booting another orchestrator.
        """
        lines: list[str] = []
        for vm in self._provisioned_vms:
            if vm._vmid is None:
                continue
            lines.append(
                f"pvesh create /nodes/{self._node}/qemu/{vm._vmid}/status/stop"
            )
            lines.append(
                f"pvesh delete /nodes/{self._node}/qemu/{vm._vmid}"
            )
        for net in self._started_networks:
            try:
                vnet = net.backend_name()
            except RuntimeError:
                continue
            lines.append(f"pvesh delete /cluster/sdn/vnets/{vnet}")
        if any("/cluster/sdn/vnets/" in line for line in lines):
            lines.append("pvesh set /cluster/sdn  # apply pending deletes")
        return lines

    # ------------------------------------------------------------------
    # Network lifecycle
    # ------------------------------------------------------------------

    def _start_networks(self) -> None:
        """Bind + start every configured network under our run ID.

        Tracks successfully-started networks on
        :attr:`_started_networks` so :meth:`_teardown_networks`
        only stops what we actually brought up — important on the
        rollback path when a later network's :meth:`start` fails.
        """
        assert self._run_id is not None, "run id must be set first"
        # Networks are already bound to ``self._run_id`` by
        # :meth:`_setup_vm_networks` (it calls ``bind_run`` at the
        # top so subsequent ``register_vm`` calls write into the
        # right run's ledger).  An earlier cut bound networks here
        # too — but ``bind_run`` clears ``_vm_entries`` as a side
        # effect, so binding *after* registration wiped every IPAM
        # entry the orchestrator had just collected.  Don't re-bind.
        for net in self._networks:
            net.start(self)
            self._started_networks.append(net)
            _log.debug(
                "started network %r (backend=%s)",
                net.name, net.backend_name(),
            )

    def _teardown_networks(self) -> None:
        """Stop each network we brought up, in reverse start order.

        :meth:`ProxmoxVirtualNetwork.stop` is itself best-effort and
        never raises, so this loop just walks the list.  Reverse order
        mirrors the libvirt backend's teardown discipline — symmetric
        with :meth:`_start_networks` and reduces the chance of
        cross-resource dependencies tripping cleanup (not relevant
        for current SDN vnets, but a useful default if future
        backends add inter-network dependencies).
        """
        while self._started_networks:
            net = self._started_networks.pop()
            net.stop(self)

    def _start_switches(self) -> None:
        """Bring up every user-declared :class:`ProxmoxSwitch`.

        Each switch becomes a PVE SDN zone.  Per-switch failures
        re-raise; the rollback in :meth:`__enter__` undoes any
        already-started switches via :meth:`_teardown_switches`.

        :raises NetworkError: If any switch's ``start`` raises.
        """
        for sw in self._switches:
            with log_duration(_log, f"start switch {sw.name!r}"):
                sw.start(self)
            self._started_switches.append(sw)

    def _teardown_switches(self) -> None:
        """Drop every user-declared switch we brought up, in
        reverse start order.  Best-effort — :meth:`ProxmoxSwitch.stop`
        already swallows per-resource errors so this loop never
        raises.
        """
        while self._started_switches:
            sw = self._started_switches.pop()
            sw.stop(self)

    def _pick_install_subnet(self) -> str:
        """Choose an install-phase subnet from :data:`_INSTALL_SUBNET_POOL`
        not already claimed by another in-flight run on this PVE node.

        Queries ``cluster/sdn/subnets`` once to learn the in-use
        subnets across the whole PVE cluster (SDN subnet IDs are
        cluster-scoped, not zone-scoped).  Picks the first pool entry
        whose CIDR isn't already taken.  Falls back to a clear
        :class:`OrchestratorError` when every entry is in use rather
        than silently colliding — at that scale the operator wants
        the backpressure, not a broken install.

        Best-effort: a TOCTOU race between two near-simultaneous
        ``__enter__`` calls is closed downstream by the per-run
        ``inst<run_id[:4]>`` vnet name (collisions there surface as a
        loud REST error from PVE), so this picker only needs to
        avoid the obvious "subnet already in use" case.
        """
        assert self._client is not None, (
            "_pick_install_subnet requires an authenticated client"
        )
        # PVE 9.x's API has no cluster-wide ``GET /cluster/sdn/subnets``
        # endpoint (verified against the apidoc.js schema — only
        # ``GET /cluster/sdn/vnets/{vnet}/subnets`` exists, scoped to a
        # single vnet).  Walk all vnets and union their subnet CIDRs
        # to find what's claimed cluster-wide.  Each subnet entry's
        # CIDR lives in the ``cidr`` field; the auto-generated
        # ``subnet`` ID (``<zone>-<cidr-with-dashes>``) is the
        # fallback when ``cidr`` is missing on older PVE versions.
        in_use: set[str] = set()
        try:
            vnets = self._client.cluster.sdn.vnets.get() or []
        except Exception:  # pragma: no cover — REST hiccup
            vnets = []
        for v in vnets:
            vnet_name = v.get("vnet")
            if not vnet_name:
                continue
            try:
                subs = (
                    self._client.cluster.sdn.vnets(vnet_name).subnets.get()
                    or []
                )
            except Exception:  # pragma: no cover — vnet vanished mid-walk
                continue
            for entry in subs:
                cidr = entry.get("cidr")
                if not cidr:
                    # Older PVE: derive CIDR from the subnet ID
                    # (``<zone>-<addr>-<prefix>`` → ``<addr>/<prefix>``).
                    sub_id = str(entry.get("subnet", ""))
                    parts = sub_id.rsplit("-", 2)
                    if len(parts) == 3 and parts[2].isdigit():
                        cidr = f"{parts[1]}/{parts[2]}"
                if cidr:
                    in_use.add(str(cidr))
        for candidate in _INSTALL_SUBNET_POOL:
            if candidate not in in_use:
                _log.debug(
                    "install-vnet subnet picker: chose %s (in-use=%d)",
                    candidate, len(in_use),
                )
                return candidate
        raise OrchestratorError(
            f"every install-vnet subnet in the pool "
            f"({_INSTALL_SUBNET_POOL[0]}–{_INSTALL_SUBNET_POOL[-1]}) is "
            "already claimed by another SDN subnet on this PVE cluster.  "
            "Either wait for an in-flight run to finish, or expand the "
            "pool by editing ``_INSTALL_SUBNET_POOL`` in "
            "``testrange/backends/proxmox/orchestrator.py``."
        )

    def _create_install_network(self) -> ProxmoxVirtualNetwork:
        """Build the ephemeral install-phase SDN vnet.

        Internet must be on so cloud-init / dnf / apt can reach
        upstream package mirrors during the install pass.  DNS too
        — without name resolution apt times out on
        ``deb.debian.org``.  The subnet comes from
        :data:`_INSTALL_SUBNET_POOL` via :meth:`_pick_install_subnet`,
        avoiding collisions with concurrent runs on the same PVE
        cluster.

        :returns: A :class:`ProxmoxVirtualNetwork` already bound to
            this run and pre-registered with each install-phase VM's
            ``__install__`` MAC, ready for :meth:`start`.
        """
        assert self._run_id is not None, (
            "_create_install_network requires a bound run_id"
        )
        subnet = self._pick_install_subnet()
        net = ProxmoxVirtualNetwork(
            name="install",
            subnet=subnet,
            dhcp=True,
            internet=True,
            dns=True,
        )
        net.bind_run(self._run_id)
        # Register every install-phase VM under the deterministic
        # ``__install__`` MAC so the install-phase cloud-init seed's
        # network-config matches the NIC the orchestrator actually
        # attaches.  Same convention as the libvirt backend.
        #
        # Slice 2: walk the *whole* nested tree so descendants of any
        # Hypervisor in ``_vm_list`` also get install-network slots —
        # the bare-metal install loop builds them on this network too.
        # Pre-order traversal keeps allocations deterministic across
        # runs.
        net_obj = ipaddress.IPv4Network(subnet, strict=False)
        hosts = list(net_obj.hosts())
        install_phase_vms = [
            vm for vm in recursive_vm_iter(self._vm_list)
            if vm.builder.needs_install_phase()
        ]
        # ``hosts[idx + 1]`` indexing skips the gateway (.1).  Bound
        # the loop explicitly so a fleet larger than the subnet
        # raises a clear NetworkError instead of a bare IndexError.
        if len(install_phase_vms) > len(hosts) - 1:
            raise NetworkError(
                f"install vnet subnet {subnet} has {len(hosts) - 1} "
                f"non-gateway host(s) but {len(install_phase_vms)} "
                "VMs need an install-phase NIC.  Pick a wider install-"
                "subnet pool entry (edit ``_INSTALL_SUBNET_POOL`` in "
                "``testrange/backends/proxmox/orchestrator.py``) or "
                "split the run across multiple orchestrator instances."
            )
        for idx, vm in enumerate(install_phase_vms):
            ip = str(hosts[idx + 1])  # skip gateway (.1)
            mac = _mac_for_vm_network(vm.name, "__install__")
            net.register_vm_with_mac(vm.name, mac, ip)
        return net

    def _teardown_install_network(self) -> None:
        """Stop the install-phase vnet if one was created.

        Best-effort; matches :meth:`_teardown_networks` shape.
        Idempotent — safe to call from both the success and the
        exception paths in :meth:`__exit__`.
        """
        if self._install_network is None:
            return
        try:
            self._install_network.stop(self)
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning(
                "unexpected error stopping install vnet: %s", exc,
            )
        self._install_network = None

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    def _setup_vm_networks(self) -> None:
        """Register each VM's IPs against its networks.

        Static ``ip=`` values flow through verbatim.  vNICs without
        an explicit ``ip=`` get a deterministic IP allocated from
        the network's subnet — TestRange picks the next free host
        address (skipping the gateway and any IP already registered
        by an earlier vNIC) and registers it just like a static
        entry would have been.  The picked address feeds back to
        cloud-init / answer.toml via ``_vm_network_refs`` so the
        rest of the pipeline doesn't need to know whether the IP
        was user-supplied or auto-allocated.

        :raises NetworkError: When a vNIC names an unknown network,
            or when an auto-allocation runs out of host addresses
            on the chosen subnet.
        """
        # Bind every network to this run BEFORE registering any VMs.
        # ``bind_run`` clears the network's ``_vm_entries`` ledger
        # as a side effect (so a re-entered orchestrator doesn't
        # carry stale entries from a prior run); doing it here, not
        # in ``_start_networks``, means our registrations land in
        # the right run's ledger and survive to the IPAM-push step.
        # Mirrors ``LibvirtOrchestrator._setup_test_networks``.
        assert self._run_id is not None, (
            "_setup_vm_networks requires a bound run_id"
        )
        for net in self._networks:
            net.bind_run(self._run_id)

        for vm in self._vm_list:
            if not isinstance(vm, ProxmoxVM):
                raise OrchestratorError(
                    f"VM {vm.name!r} is not a ProxmoxVM; cannot mix "
                    "backends in one orchestrator."
                )
            for ref in vm._network_refs():
                net = self._find_network(ref.ref)
                if net is None:
                    raise NetworkError(
                        f"VM {vm.name!r} references unknown network "
                        f"{ref.ref!r}; available: "
                        f"{[n.name for n in self._networks]!r}"
                    )
                ip = ref.ip or self._allocate_dhcp_ip(net, vm.name)
                # Stamp the picked IP back onto the vNIC so any
                # downstream reader (cloud-init network-config,
                # ProxmoxAnswerBuilder._network_block, …) sees a
                # unified static-IP view.  Without this stamp the
                # answer builder reads ``ref.ip is None`` and falls
                # back to ``source = "from-dhcp"``, which freezes the
                # install-phase DHCP lease (from the throwaway
                # 192.168.23x install vnet) as the run-phase config —
                # leaving the VM unreachable.  Static ``ip=`` values
                # are unchanged because ``or`` short-circuits.
                ref.ip = ip
                net.register_vm(vm.name, ip)

    def _allocate_dhcp_ip(
        self,
        net: ProxmoxVirtualNetwork,
        vm_name: str,
    ) -> str:
        """Pick the next free host address on *net* for *vm_name*.

        Walks the subnet's host range in order, skipping:

        - the gateway (first host, ``.1``);
        - any address already registered on this network in this run
          (an earlier vNIC's static ``ip=``, or an earlier
          auto-allocation).

        The first un-skipped host wins.  Determinism makes test
        assertions stable: the Nth DHCP-discovery vNIC in declaration
        order lands on the Nth host address, regardless of how many
        run alongside it.

        :raises NetworkError: If the subnet has fewer host addresses
            than vNICs needing them.
        """
        net_obj = ipaddress.IPv4Network(net.subnet, strict=False)
        gateway = net.gateway_ip
        already_taken = {entry_ip for _, _, entry_ip in net._vm_entries}
        for host in net_obj.hosts():
            host_str = str(host)
            if host_str == gateway:
                continue
            if host_str in already_taken:
                continue
            return host_str
        raise NetworkError(
            f"VM {vm_name!r}: cannot auto-allocate an IP on network "
            f"{net.name!r} ({net.subnet}) — every host address is "
            "already claimed.  Add explicit ``ip=`` values or widen "
            "the subnet."
        )

    def _find_network(
        self, name: str,
    ) -> ProxmoxVirtualNetwork | None:
        for net in self._networks:
            if net.name == name:
                return net
        return None

    def _vm_network_refs(
        self,
        vm: ProxmoxVM,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
        """Build ``(network_entries, mac_ip_pairs)`` for a VM.

        Mirrors the libvirt backend's ``_build_nic_entries``:
        ``network_entries`` carries ``(backend_net_name, mac)`` (used
        to attach NICs to PVE bridges), and ``mac_ip_pairs`` carries
        ``(mac, ip_with_cidr, gateway, dns)`` (used by cloud-init's
        network-config and SSH host resolution).
        """
        network_entries: list[tuple[str, str]] = []
        mac_ip_pairs: list[tuple[str, str, str, str]] = []
        for ref in vm._network_refs():
            net = self._find_network(ref.ref)
            if net is None:
                continue
            mac = _mac_for_vm_network(vm.name, ref.ref)
            network_entries.append((net.backend_name(), mac))
            gateway = net.gateway_ip if net.internet else ""
            # Run-phase DNS: ``net.dns=True`` means "this network
            # provides name resolution to its guests."  Each PVE SDN
            # subnet now ships with ``dhcp = "dnsmasq"``, and dnsmasq
            # binds to the subnet's gateway address — so the gateway
            # IS the DNS server, mirroring the libvirt backend's
            # bridge-local dnsmasq pattern.  IPAM entries written by
            # ``ProxmoxVirtualNetwork._push_ipam_entries`` give it
            # the static ``<vm>.<vnet>`` records.
            nameserver = net.gateway_ip if net.dns else ""
            # Resolve the picked or static IP from the network's
            # ledger so DHCP-discovery vNICs (where ``ref.ip`` is
            # ``None``) still produce a valid CIDR for cloud-init.
            ip = ref.ip or _registered_ip_for(net, vm.name)
            cidr = f"{ip}/{net.prefix_len}" if ip else ""
            mac_ip_pairs.append((mac, cidr, gateway, nameserver))
        return network_entries, mac_ip_pairs

    def _provision_vms(self) -> None:
        """Build + start every configured VM.

        Build phase attaches each VM to the dedicated install vnet
        (``self._install_network``, ``internet=True``) so cloud-init
        can reach package mirrors regardless of whether any of the
        VM's user-declared NICs has internet.  After build, the
        per-run clone's NIC is swapped to the user's first declared
        network in :meth:`ProxmoxVM.start_run`.

        Slice 2: walks the *whole* nested tree.  Descendants of any
        Hypervisor in ``_vm_list`` also build on this orchestrator's
        install vnet, so the inner orchestrator's
        :meth:`Builder.adopt_prebuilt` (Slice 3) can pick them up by
        ``cache_key`` at run-phase entry.  Only top-level VMs go
        through ``start_run`` here — descendants are booted by their
        inner orchestrator after the bare-metal Hypervisor is up.

        Tracks successfully-started VMs in ``_provisioned_vms`` so
        a partial failure rolls back only what got created.
        """
        # __enter__ sets _cache + _install_network before
        # _provision_vms runs; the asserts narrow the Optionals for
        # pyright.
        assert self._cache is not None, "cache must be initialised"
        cache = self._cache
        installed_disks: dict[str, str] = {}
        top_level_ids = {id(v) for v in self._vm_list}
        all_vms_to_build = list(recursive_vm_iter(self._vm_list))
        for raw_vm in all_vms_to_build:
            # Top-level VMs were promoted to ProxmoxVM in __init__;
            # descendants come straight from a Hypervisor's ``vms``
            # field and may still be ``GenericVM``.  Promote on the fly
            # so ``vm.build()`` resolves to the proxmox path; idempotent
            # for already-proxmox instances.
            is_top_level = id(raw_vm) in top_level_ids
            vm = raw_vm if is_top_level else _promote_to_proxmox(raw_vm)  # type: ignore[arg-type]
            assert isinstance(vm, ProxmoxVM)

            if is_top_level:
                # Validate the user's spec early — every top-level VM
                # must declare at least one vNIC, otherwise there's
                # nowhere to attach the run-phase NIC after install.
                # Descendants don't need this check: their run-phase
                # network attaches inside their inner orchestrator,
                # not here.
                user_network_entries, _ = self._vm_network_refs(vm)
                if not user_network_entries:
                    raise NetworkError(
                        f"VM {vm.name!r}: no network refs — Proxmox "
                        "VMs need at least one vNIC."
                    )

            # Build phase always uses the install vnet, never the
            # user's first NIC.  An ``internet=False`` user network
            # would otherwise hang ``apt install`` forever.
            if vm.builder.needs_install_phase():
                assert self._install_network is not None, (
                    "install network must be up before build phase"
                )
                install_net_name = self._install_network.backend_name()
                install_mac = _mac_for_vm_network(vm.name, "__install__")
            else:
                # NoOpBuilder VMs skip install entirely; pass empty
                # strings to keep ``build()``'s signature stable.
                install_net_name = ""
                install_mac = ""

            vm.set_client(self._client)
            role = "top-level" if is_top_level else "descendant"
            with log_duration(_log, f"build {role} VM {vm.name!r}"):
                installed = vm.build(
                    context=self,
                    cache=cache,
                    run=self._run,
                    install_network_name=install_net_name,
                    install_network_mac=install_mac,
                )
            if is_top_level:
                installed_disks[vm.name] = installed
                self._provisioned_vms.append(vm)
            # Descendant builds live in the cache by config_hash; the
            # inner orchestrator reaches them via
            # :meth:`Builder.adopt_prebuilt` at run-phase entry
            # (Slice 3).  No tracking needed here.

        for vm in self._vm_list:
            assert isinstance(vm, ProxmoxVM)
            network_entries, mac_ip_pairs = self._vm_network_refs(vm)
            with log_duration(_log, f"start VM {vm.name!r}"):
                vm.start_run(
                    context=self,
                    run=self._run,
                    installed_disk=installed_disks[vm.name],
                    network_entries=network_entries,
                    mac_ip_pairs=mac_ip_pairs,
                )
            self.vms[vm.name] = vm

    def _enter_nested_orchestrators(self) -> None:
        """Enter an inner orchestrator for each
        :class:`AbstractHypervisor` VM in the run.

        Symmetric with the libvirt backend — called last in the
        provisioning sequence so every outer VM is already running
        and its communicator is ready.  Each hypervisor's declared
        ``orchestrator`` class is responsible for whatever inner
        bring-up it needs (auth, control-plane probe, etc.) inside
        its own :meth:`root_on_vm`.
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
            # ExitStack closes in LIFO order — whatever succeeded
            # gets unwound before the original exception propagates.
            stack.close()
            raise

        self._nested_stack = stack
        self._inner_orchestrators = entered

    def _teardown_vms(self) -> None:
        """Stop and DELETE each provisioned VMID, in reverse order.

        :meth:`ProxmoxVM.shutdown` swallows its own errors, so this
        loop just walks the list — symmetric with
        :meth:`_provision_vms`.
        """
        while self._provisioned_vms:
            vm = self._provisioned_vms.pop()
            vm.shutdown()
        self.vms.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_client_kwargs(self) -> dict[str, Any]:
        """Pick a credential combination and return ``ProxmoxAPI`` kwargs.

        Resolution order:

        1. ``user`` + ``token_name`` + ``token_value`` → API token.
        2. ``user`` + ``password`` → ticket auth.
        3. ``password`` alone → ticket auth as ``root@pam``.

        :raises OrchestratorError: If no credential combination works.
        """
        host = f"{self._host}:{self._port}"
        common: dict[str, Any] = {"host": host, "verify_ssl": self._verify_ssl}

        if self._user and self._token_name and self._token_value:
            return {
                **common,
                "user": self._user,
                "token_name": self._token_name,
                "token_value": self._token_value,
            }
        if self._user and self._password:
            return {**common, "user": self._user, "password": self._password}
        if self._password and not self._user:
            return {**common, "user": "root@pam", "password": self._password}

        raise OrchestratorError(
            "ProxmoxOrchestrator: no credentials.  Pass ``user=`` and "
            "``password=`` (ticket auth) or ``user=``, ``token_name=`` "
            "and ``token_value=`` (API-token auth)."
        )

    def _resolve_node(self, nodes: list[dict[str, Any]]) -> None:
        """Resolve :attr:`_node` against the cluster's node list.

        :raises OrchestratorError: If the cluster reports zero nodes,
            ``self._node`` is set but unknown to the cluster, or the
            cluster has multiple nodes and ``self._node`` was not
            given.
        """
        if not nodes:
            raise OrchestratorError(
                f"ProxmoxOrchestrator: PVE at {self._host!r} reports "
                "no nodes (cluster broken or auth scoped wrong)."
            )
        node_names = [n["node"] for n in nodes]
        if self._node is None:
            if len(node_names) > 1:
                raise OrchestratorError(
                    f"ProxmoxOrchestrator: cluster has {len(node_names)} "
                    f"nodes ({node_names!r}); pass ``node=`` to pick one."
                )
            self._node = node_names[0]
        elif self._node not in node_names:
            raise OrchestratorError(
                f"ProxmoxOrchestrator: node {self._node!r} not in "
                f"cluster (known: {node_names!r})."
            )

    def _resolve_storage(self) -> None:
        """Resolve :attr:`_storage` to the first image-capable pool.

        Caller-supplied storage is validated against the node's pool
        list; a missing default is filled in by picking the first pool
        whose ``content`` field includes ``images``.

        :raises OrchestratorError: If the node has no image-capable
            storage pool, or ``self._storage`` is set but unknown.
        """
        stores = self._client.nodes(self._node).storage.get()
        names = [s["storage"] for s in stores]
        if self._storage is None:
            image_stores = [
                s["storage"] for s in stores
                if "images" in s.get("content", "")
                and s.get("active", 1)
            ]
            if not image_stores:
                raise OrchestratorError(
                    f"ProxmoxOrchestrator: node {self._node!r} has no "
                    "active storage pool that accepts ``images`` "
                    f"content (saw: {names!r}).  Pass ``storage=`` "
                    "explicitly."
                )
            self._storage = image_stores[0]
        elif self._storage not in names:
            raise OrchestratorError(
                f"ProxmoxOrchestrator: storage {self._storage!r} is "
                f"not configured on node {self._node!r} "
                f"(known: {names!r})."
            )

    def _preflight_dnsmasq_installed(self) -> None:
        """Verify ``dnsmasq`` is installed on the target PVE node.

        TestRange flips every SDN subnet to ``dhcp = "dnsmasq"`` so
        guests get a per-vnet DHCP service plus DNS resolution for
        ``<vm>.<vnet>`` (libvirt-style).  PVE only spins up the
        dnsmasq instance if the binary is actually installed; without
        it, subnet creation succeeds but no leases are served and
        guests time out at boot.  Catching the dependency here turns
        what would be a cryptic boot timeout into a clear
        "install ``dnsmasq`` on this node" error.

        Probe shape: ``GET /nodes/{node}/apt/changelog?name=dnsmasq``.
        That endpoint runs ``apt-get changelog dnsmasq`` server-side
        and returns the changelog text on success or errors with a
        500 if the package isn't installed.  We *don't* use
        ``/apt/versions`` because PVE hardcodes that endpoint to a
        curated list of "important Proxmox packages" (kernel,
        pveproxy, qemu-server, …) and never lists ``dnsmasq``
        regardless of install state — using it gave false-negative
        preflights even on freshly-installed PVE nodes where
        ``dnsmasq`` was definitely present.

        When TestRange itself built the PVE node as the outer VM of
        a :class:`Hypervisor`, the SSH bootstrap from
        :meth:`_bootstrap_pve_node` (called by :meth:`root_on_vm`)
        runs ``apt-get install -y dnsmasq`` before the inner
        orchestrator's ``__enter__``, so this check passes by
        construction; the explicit probe matters only against
        pre-existing PVE clusters the user pointed us at and as a
        defence against the bootstrap having silently failed.

        :raises OrchestratorError: When the package isn't present.
        """
        assert self._client is not None
        try:
            self._client.nodes(self._node).apt.changelog.get(name="dnsmasq")
        except Exception as exc:
            raise OrchestratorError(
                f"ProxmoxOrchestrator: ``dnsmasq`` does not appear to "
                f"be installed on PVE node {self._node!r} "
                f"(``GET /apt/changelog?name=dnsmasq`` errored: {exc}).  "
                "TestRange relies on PVE's SDN dnsmasq integration for "
                "per-vnet DHCP + DNS.  Install on the node:\n"
                "  Debian/Ubuntu:  sudo apt-get install -y dnsmasq\n"
                "  RHEL/Rocky:     sudo dnf install -y dnsmasq\n"
                "Then ``systemctl disable --now dnsmasq`` (PVE owns the "
                "per-vnet instances; the default systemd service would "
                "conflict on port 53/67) and re-run.\n"
                "When TestRange builds the PVE host itself via "
                "testrange.Hypervisor, both steps run via the SSH "
                "bootstrap from ``ProxmoxOrchestrator._bootstrap_pve_node`` "
                "automatically — if the bootstrap ran but failed, look at "
                "``/var/log/testrange-pve-bootstrap.log`` on the PVE node."
            ) from exc
        _log.debug("dnsmasq present on node %r", self._node)

    def _ensure_sdn_zone(self) -> None:
        """Create our SDN simple-zone if it doesn't already exist.

        TestRange parks every vnet under one zone so concurrent runs
        only have to namespace by vnet name.  The zone carries
        ``dhcp = "dnsmasq"`` so PVE spawns a per-vnet dnsmasq
        instance for every subnet under it (the dhcp setting lives
        at zone scope per the PVE 9.x SDN schema, NOT at subnet
        scope — putting it on the subnet POST is a 400 with
        "property is not defined in schema").

        Idempotent: if the zone already exists *with* the dhcp
        field set, no-op.  If it exists without it (e.g. left over
        from an earlier TestRange version that wrote zone-less
        config), the field is added via PUT so the existing zone
        starts spawning dnsmasq instances on next subnet create.
        """
        zones = self._client.cluster.sdn.zones.get()
        existing = next(
            (z for z in zones if z.get("zone") == self._zone), None,
        )
        if existing is None:
            _log.info(
                "creating SDN simple-zone %s with dhcp=dnsmasq",
                self._zone,
            )
            self._client.cluster.sdn.zones.post(
                type="simple",
                zone=self._zone,
                dhcp="dnsmasq",
            )
        elif existing.get("dhcp") != "dnsmasq":
            # Pre-existing zone without dhcp — PUT to upgrade it
            # in place so VMs land on a dnsmasq-capable zone.
            _log.info(
                "updating SDN zone %s to set dhcp=dnsmasq", self._zone,
            )
            self._client.cluster.sdn.zones(self._zone).put(dhcp="dnsmasq")
        # ``cluster/sdn`` accepts an empty PUT to apply pending config.
        # Without it, the zone exists in the "pending" state and isn't
        # usable for vnets yet.
        self._client.cluster.sdn.put()

    @classmethod
    def root_on_vm(
        cls,
        hypervisor: AbstractHypervisor,
        outer: AbstractOrchestrator,
    ) -> ProxmoxOrchestrator:
        """Build a nested :class:`ProxmoxOrchestrator` rooted on
        *hypervisor*.

        The outer orchestrator (typically libvirt) has just booted a
        VM that's been provisioned with PVE via
        :class:`~testrange.vms.builders.proxmox_answer.ProxmoxAnswerBuilder`.
        That gives us a reachable IP, a ``root@pam`` account whose
        password matches the outer credential we seeded into
        ``answer.toml``, and ``pveproxy`` listening on 8006.  This
        method packages those into a fresh ``ProxmoxOrchestrator``
        that the outer orchestrator's ExitStack will enter, at which
        point the inner orchestrator authenticates against the PVE
        REST API and goes through its own normal provisioning of
        ``hypervisor.networks`` and ``hypervisor.vms``.

        Auth flow: prefers an explicit token if one is set on a
        :class:`~testrange.credentials.Credential` (via the
        ``ssh_key`` slot used as a generic secret carrier — kept
        lightweight to avoid a credential schema change just for
        nested PVE), otherwise falls back to the root credential's
        plaintext password.  ``verify_ssl=False`` because PVE ships a
        self-signed cert by default — flipping to ``True`` is the
        operator's call once they've replaced the cert.

        The resulting orchestrator is **not yet entered** — the outer
        orchestrator manages that lifecycle via :class:`ExitStack`.

        :param hypervisor: The just-booted PVE hypervisor VM.  Must
            have a static IP (see :class:`vNIC`) and a
            ``root@pam``-shaped credential whose password matches
            the one the unattended installer set.
        :param outer: The outer orchestrator that booted
            ``hypervisor``; used to source the shared cache root.
        :returns: A configured (not yet entered) inner orchestrator.
        :raises OrchestratorError: If the hypervisor's communicator
            has no resolvable host, or no usable root credential is
            present.
        """
        if not hypervisor.users:
            raise OrchestratorError(
                f"Hypervisor VM {hypervisor.name!r} has no users — "
                "ProxmoxOrchestrator.root_on_vm needs at least one "
                "Credential to authenticate against PVE."
            )
        # PVE's unattended installer creates ``root@pam`` with the
        # ``root-password`` value from answer.toml.  Pick the
        # credential whose username starts with ``"root"`` (handles
        # both ``"root"`` and ``"root@pam"``) so we hit the right
        # account regardless of how the user spelled it.
        root_cred = next(
            (c for c in hypervisor.users if c.username.startswith("root")),
            hypervisor.users[0],
        )
        if not root_cred.password:
            raise OrchestratorError(
                f"Hypervisor VM {hypervisor.name!r}: root credential "
                "has no password.  PVE's REST ticket auth needs the "
                "plaintext password the unattended installer set "
                "into ``answer.toml``."
            )

        # The hypervisor VM's live communicator stores its reachable
        # host.  Both SSHCommunicator and (eventually) any future
        # transport expose ``_host``; we only support the SSH /
        # static-IP case here because the nested PVE REST endpoint
        # is on the same IP.
        comm = hypervisor._require_communicator()
        host = getattr(comm, "_host", None)
        if not host:
            raise OrchestratorError(
                f"Hypervisor VM {hypervisor.name!r}: communicator "
                "has no resolvable host.  Nested Proxmox requires "
                "communicator='ssh' + a static IP "
                "(vNIC('Net', ip='10.x.x.x'))."
            )

        # Reuse the outer cache root so artefacts stay in one place
        # — same convention as the libvirt backend's root_on_vm.
        outer_cache_root: Path | None = None
        outer_cache = getattr(outer, "_cache", None)
        if outer_cache is not None:
            outer_cache_root = outer_cache.root

        # PVE's pveproxy.service depends on pve-cluster + pvedaemon
        # and reaches active state noticeably later than sshd on a
        # freshly-installed VM (see ``examples/nested_proxmox_*``'s
        # ``_wait_for_pveproxy`` helper).  Without this wait, the
        # inner orchestrator's ``__enter__`` races pveproxy startup
        # and intermittently fails with "Connection refused".  Doing
        # the wait here keeps ``__enter__`` simple — by the time
        # the ExitStack enters the returned orchestrator, the API
        # is ready.
        #
        # The dnsmasq install + repo-swap that used to run here is
        # now baked into the cached PVE template by
        # :meth:`ProxmoxAnswerBuilder.post_install_hook`, fired in
        # the install phase on the bare-metal install network (always
        # ``internet=True``).  The cache snapshot therefore contains
        # everything the inner orchestrator needs — meaning the
        # hypervisor's run-phase network can be ``internet=False``
        # without breaking nested provisioning.
        cls._wait_for_pveproxy(hypervisor)

        return cls(
            host=host,
            user="root@pam",
            password=root_cred.password,
            verify_ssl=False,
            networks=hypervisor.networks,  # pyright: ignore[reportArgumentType]
            vms=hypervisor.vms,  # pyright: ignore[reportArgumentType]
            cache_root=outer_cache_root,
        )

    # ``_PVE_BOOTSTRAP_SCRIPT`` and ``_bootstrap_pve_node`` previously
    # lived here — the bootstrap (apt install dnsmasq, repo swap) was
    # SSH-run from this orchestrator on a fresh PVE hypervisor *after*
    # the VM had been swapped to its final user-declared run network.
    # That broke any topology where the run network has
    # ``internet=False``: apt-get update couldn't reach the public
    # mirror.  The bootstrap now lives in
    # :meth:`ProxmoxAnswerBuilder.post_install_hook` and runs in the
    # install phase on the bare-metal install network (always
    # ``internet=True``).  Result: the cached PVE template is fully
    # bootstrapped, and run-phase network internet state becomes
    # irrelevant to nested provisioning.

    @staticmethod
    def _wait_for_pveproxy(
        hypervisor: AbstractHypervisor,
        timeout_s: float = 120.0,
    ) -> None:
        """Poll ``systemctl is-active pveproxy`` on the hypervisor
        until active or *timeout_s* elapses.

        Used by :meth:`root_on_vm` to bridge the gap between sshd
        readiness (which the outer orchestrator's communicator wait
        already keys on) and PVE REST API readiness.

        :raises OrchestratorError: If pveproxy doesn't reach active
            within the timeout.
        """
        import time

        deadline = time.monotonic() + timeout_s
        last_stderr = b""
        while time.monotonic() < deadline:
            r = hypervisor.exec(["systemctl", "is-active", "pveproxy"])
            # ``systemctl is-active`` outputs one of ``active`` /
            # ``inactive`` / ``activating`` / ``failed``.  Substring
            # match (``b"active" in r.stdout``) is a footgun:
            # ``inactive`` and ``activating`` both contain ``active``
            # and would silently pass.  Compare against the trimmed
            # exact word.
            if r.exit_code == 0 and r.stdout.strip() == b"active":
                return
            last_stderr = r.stderr
            time.sleep(2)
        raise OrchestratorError(
            f"pveproxy on hypervisor {hypervisor.name!r} did not "
            f"reach active within {timeout_s:.0f}s; last stderr: "
            f"{last_stderr!r}"
        )
