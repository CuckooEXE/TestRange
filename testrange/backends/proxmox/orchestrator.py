"""Proxmox VE orchestrator.

Authenticates against the PVE REST API (via the ``proxmoxer``
package), resolves a target node + image-capable storage pool, and
ensures TestRange's SDN simple-zone exists.  VM and network lifecycle
are still in progress — the orchestrator currently exits without
provisioning any VMs.

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

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._logging import get_logger
from testrange.exceptions import OrchestratorError
from testrange.orchestrator_base import AbstractOrchestrator

if TYPE_CHECKING:
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM
    from testrange.vms.hypervisor_base import AbstractHypervisor

_log = get_logger(__name__)

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


class ProxmoxOrchestrator(AbstractOrchestrator):
    """Proxmox VE implementation of
    :class:`~testrange.orchestrator_base.AbstractOrchestrator`.

    :param host: PVE node hostname or IP.  A single node is fine; for
        a cluster, point at any node and pass ``node=`` to pick the
        target.
    :param networks: Virtual networks to create as SDN vnets.
    :param vms: VMs to provision (lifecycle still in progress — see
        the module docstring for the implementation roadmap).
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

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[AbstractVirtualNetwork] | None = None,
        vms: Sequence[AbstractVM] | None = None,
        cache_root: Path | None = None,
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
        super().__init__(
            host=host, networks=networks, vms=vms, cache_root=cache_root,
        )
        self._host = host
        self._port = port
        self._networks = list(networks) if networks else []
        self._vm_list = list(vms) if vms else []
        self._cache_root = cache_root
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
        if isinstance(token, dict):
            if not self._user and token.get("user"):
                self._user = token["user"]
            if not self._password and token.get("password"):
                self._password = token["password"]
            self._legacy_token = token.get("token")
        else:
            self._legacy_token = None

        self._client: Any = None
        self.vms = {}
        self._run = None

    @classmethod
    def backend_type(cls) -> str:
        """Return ``"proxmox"``."""
        return "proxmox"

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
        self._ensure_sdn_zone()
        _log.info(
            "PVE ready: node=%s storage=%s zone=%s",
            self._node, self._storage, self._zone,
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close the PVE client.

        Future slices will tear down VMs and SDN vnets before this; for
        now the orchestrator owns no provisioned resources, so the
        teardown is a single client-handle release.  Honours
        :meth:`leak` by short-circuiting any future cleanup the same
        way the libvirt backend does.
        """
        if self._leaked:
            _log.info(
                "leak() set — leaving Proxmox resources in place; "
                "client handle released",
            )
            self._client = None
            return None
        self._client = None
        return None

    def keep_alive_hints(self) -> list[str]:
        """Return cleanup commands for resources left behind by
        :meth:`leak`.

        At the current implementation slice no provisioned resources
        survive ``__exit__``, so the hint list is empty.  Future
        slices populate it with ``pvesh`` snippets for the leaked
        VMIDs and SDN vnets.
        """
        return []

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

    def _ensure_sdn_zone(self) -> None:
        """Create our SDN simple-zone if it doesn't already exist.

        TestRange parks every vnet under one zone so concurrent runs
        only have to namespace by vnet name.  Idempotent: a no-op if
        the zone already exists.
        """
        zones = self._client.cluster.sdn.zones.get()
        if any(z.get("zone") == self._zone for z in zones):
            return
        _log.info("creating SDN simple-zone %s", self._zone)
        self._client.cluster.sdn.zones.post(type="simple", zone=self._zone)
        # ``cluster/sdn`` accepts an empty PUT to apply pending config.
        # Without it, the zone exists in the "pending" state and isn't
        # usable for vnets yet.
        self._client.cluster.sdn.put()

    @classmethod
    def root_on_vm(
        cls,
        hypervisor: AbstractHypervisor,
        outer: AbstractOrchestrator,
    ) -> AbstractOrchestrator:
        """Not yet implemented.

        Nested Proxmox-in-libvirt will:

        1. Obtain an API token for the inner cluster by POSTing to
           ``/api2/json/access/ticket`` with credentials injected by
           the Proxmox ISO unattended installer.
        2. Construct a fresh :class:`ProxmoxOrchestrator` pointing at
           ``https://<hypervisor-ip>:8006`` with the new token.
        3. Return it so the outer orchestrator can enter it via
           :class:`ExitStack`.

        Step (1) needs an unattended Proxmox installer (a dedicated
        :class:`~testrange.vms.builders.base.Builder` subclass) that
        pre-seeds the cluster's root password and enables HTTPS.
        That's scheduled as its own track.
        """
        del hypervisor, outer
        raise NotImplementedError(
            "ProxmoxOrchestrator.root_on_vm is not yet implemented. "
            "Nested Proxmox-in-libvirt needs an unattended Proxmox "
            "installer (tracked separately).  Use "
            "LibvirtOrchestrator for nested libvirt-in-libvirt in the "
            "meantime."
        )
