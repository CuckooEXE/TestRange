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

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from testrange._logging import get_logger, log_duration
from testrange.backends.proxmox.network import (
    ProxmoxVirtualNetwork,
    _mac_for_vm_network,
)
from testrange.backends.proxmox.vm import ProxmoxVM
from testrange.cache import CacheManager
from testrange.exceptions import NetworkError, OrchestratorError
from testrange.orchestrator_base import AbstractOrchestrator

if TYPE_CHECKING:
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM
    from testrange.vms.generic import GenericVM
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


def _promote_to_proxmox(vm: ProxmoxVM | "GenericVM") -> ProxmoxVM:
    """Convert a backend-agnostic :class:`GenericVM` to the
    proxmox backend's concrete :class:`ProxmoxVM`.

    Field-for-field translation since GenericVM exists exactly to
    be pluggable into any backend.  An already-ProxmoxVM input
    passes through unchanged.  Symmetric with
    :func:`testrange.backends.libvirt.orchestrator._promote_to_libvirt`.
    """
    from testrange.vms.generic import GenericVM as _GenericVM
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

    # Narrow the abstract ``list[AbstractVirtualNetwork]`` to our
    # concrete subclass so calls to ``bind_run`` / ``register_vm`` /
    # ``backend_name`` type-check without a per-call cast.  The
    # ``pyright: ignore`` matches the libvirt backend's convention —
    # ``list`` is invariant, so the narrow is technically a violation
    # of the LSP variance rule, but it's intentional and safe
    # (mixing backend types in one orchestrator is a user error
    # caught at construction).
    _networks: list[ProxmoxVirtualNetwork]  # type: ignore[assignment] # pyright: ignore[reportIncompatibleVariableOverride]
    _started_networks: list[ProxmoxVirtualNetwork]
    _provisioned_vms: list[ProxmoxVM]

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[AbstractVirtualNetwork] | None = None,
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
        super().__init__(
            host=host, networks=networks, vms=vms, cache_root=cache_root,
            cache=cache, cache_verify=cache_verify,
            storage_backend=storage_backend,  # type: ignore[arg-type]
        )
        # Proxmox doesn't yet honour storage_backend (the orchestrator
        # is a stub).  Stash it for forward-compatibility so the
        # contract test passes today and the wiring follows when the
        # PVE REST integration lands.
        self._storage_backend_override = storage_backend
        self._host = host
        self._port = port
        # Narrow the abstract list back to our concrete subclass —
        # mixing backend types in one orchestrator is a user error
        # we don't try to handle gracefully.  Same convention as
        # the libvirt backend (see orchestrator.py:272).
        self._networks = cast(  # pyright: ignore[reportIncompatibleVariableOverride]
            "list[ProxmoxVirtualNetwork]",
            list(networks) if networks else [],
        )
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
        self._run_id: str | None = None
        self._started_networks = []
        self._provisioned_vms = []
        # CacheManager creation involves filesystem mutation
        # (mkdir/chmod on the cache root); defer it to __enter__ so
        # cheap construction patterns — CLI URL dispatch, tests
        # constructing instances for spec inspection — don't trip
        # on filesystem permissions.
        self._cache: CacheManager | None = None

        # CacheManager construction mirrors LibvirtOrchestrator's
        # wiring so the cross-backend cache.backend_name invariant
        # holds even though the rest of this orchestrator is still a
        # stub.  Without it, the contract test in
        # tests/test_backend_contract.py::TestScenarioConstructionContract
        # catches the missing setup.
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
        self._ensure_sdn_zone()
        _log.info(
            "PVE ready: node=%s storage=%s zone=%s",
            self._node, self._storage, self._zone,
        )

        # Run setup — every entry generates a fresh ID so concurrent
        # runs against the same PVE namespace cleanly.  RunDir
        # (scratch space for install overlays / seed ISOs) is
        # deferred until VM provisioning lands; for now the run ID
        # is enough to bind networks against.
        self._run_id = uuid.uuid4().hex
        _log.info("run id: %s", self._run_id[:8])

        try:
            self._setup_vm_networks()
            self._start_networks()
            self._warn_if_unroutable()
            self._provision_vms()
        except Exception:
            # Roll back in reverse provisioning order so we don't
            # leak any SDN / VMID state into the user's exception.
            self._teardown_vms()
            self._teardown_networks()
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
                self._teardown_networks()
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning("unexpected error during PVE teardown: %s", exc)
        finally:
            self._client = None
            self._run_id = None

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

        try:
            from proxmoxer import ProxmoxAPI  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise OrchestratorError(
                "ProxmoxOrchestrator.cleanup needs the ``proxmoxer`` "
                "Python package — install with "
                "``pip install testrange[proxmox]``."
            ) from exc

        client_kwargs = self._resolve_client_kwargs()
        try:
            client = ProxmoxAPI(self._host, **client_kwargs)
        except Exception as exc:
            raise OrchestratorError(
                f"cleanup: cannot connect to PVE at {self._host!r}: {exc}"
            ) from exc

        # Resolve node — re-runs the same logic __enter__ uses so
        # cleanup picks the same node the original run did.
        try:
            nodes = list(client.nodes.get())
        except Exception as exc:
            raise OrchestratorError(
                f"cleanup: cannot list PVE nodes: {exc}"
            ) from exc
        try:
            self._resolve_node(nodes)
        except OrchestratorError:
            raise
        node = self._node
        assert node is not None  # _resolve_node sets it or raises

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
        for net in self._networks:
            net.bind_run(self._run_id)
            net.start(self)
            self._started_networks.append(net)
            _log.debug(
                "started network %r (backend=%s)",
                net.name, net.backend_name(),
            )

    def _check_network_reachable(
        self, net: ProxmoxVirtualNetwork,
    ) -> bool:
        """Check whether the test runner has a route into *net*'s subnet.

        SDN vnets live on a bridge inside the PVE host.  The test
        runner needs an IP route through the PVE node to reach VM
        IPs on that subnet — without it, the SSH-readiness wait in
        :meth:`ProxmoxVM.start_run` will time out at 300s with no
        useful diagnostic.

        We can't TCP-probe a VM that doesn't exist yet, and the
        gateway IP isn't a host (just the PVE-side bridge).  But the
        kernel knows whether *anything* on the subnet is routable:
        ``ip route get`` returns a "via … dev …" line for a routable
        target and exits non-zero otherwise.  We pick a host inside
        the subnet (any will do — the kernel doesn't ARP) and ask.
        """
        import subprocess

        # Pick the gateway address — guaranteed to be in-subnet and
        # not equal to the test runner's own address.
        target = net.gateway_ip
        try:
            result = subprocess.run(
                ["ip", "-4", "route", "get", target],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Either ``ip`` isn't on PATH or the kernel hung — give
            # the user the benefit of the doubt and skip the warning.
            return True
        if result.returncode != 0:
            return False
        # ``ip route get`` returns 0 even when the route is via the
        # default gateway, which would not actually reach the SDN
        # subnet (the default GW has no route for it).  Look for a
        # ``via <pve-host>`` clause to confirm we'd egress through
        # the PVE node.
        return f"via {self._host}" in result.stdout

    def _warn_if_unroutable(self) -> None:
        """Log a clear ``ip route add ...`` hint for any SDN subnet
        that isn't reachable from the test runner.

        Called after networks come up but before VMs build, so the
        warning lands in the user's logs *before* a 300-second SSH
        timeout would.  Doesn't raise — the hint is advisory; some
        topologies may route correctly via mechanisms we can't
        detect here.
        """
        for net in self._started_networks:
            if self._check_network_reachable(net):
                continue
            _log.warning(
                "SDN subnet %s (gateway %s) is not reachable from "
                "this host — VM SSH attach will likely time out.",
                net.subnet, net.gateway_ip,
            )
            _log.warning(
                "Add a route through the PVE node, e.g.: "
                "sudo ip route add %s via %s",
                net.subnet, self._host,
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

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    def _setup_vm_networks(self) -> None:
        """Register each VM's static IPs against its networks.

        v1 PVE backend requires every :class:`vNIC` to
        carry a static ``ip=`` — DHCP discovery is a future slice.
        We fail loud here rather than later in
        :meth:`AbstractVM._make_communicator` so users see the cause
        immediately.
        """
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
                if not ref.ip:
                    raise NetworkError(
                        f"VM {vm.name!r}: vNIC "
                        f"{ref.ref!r} has no static ``ip=`` — "
                        "the Proxmox backend doesn't support DHCP "
                        "discovery yet."
                    )
                net.register_vm(vm.name, ref.ip)

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
            nameserver = net.gateway_ip if net.dns else ""
            cidr = f"{ref.ip}/{net.prefix_len}" if ref.ip else ""
            mac_ip_pairs.append((mac, cidr, gateway, nameserver))
        return network_entries, mac_ip_pairs

    def _provision_vms(self) -> None:
        """Build + start every configured VM.

        Each VM gets its first NIC's network as the install-phase
        attachment — the Proxmox backend doesn't have a separate
        install network the way the libvirt backend does, since
        cloud-init can run against the same vnet the test phase
        will use.  Tracks successfully-started VMs in
        ``_provisioned_vms`` so a partial failure rolls back only
        what got created.
        """
        # __enter__ sets _cache before _provision_vms runs; the assert
        # narrows the Optional for pyright.
        assert self._cache is not None, "cache must be initialised"
        cache = self._cache
        installed_disks: dict[str, str] = {}
        for vm in self._vm_list:
            assert isinstance(vm, ProxmoxVM)
            network_entries, _ = self._vm_network_refs(vm)
            if not network_entries:
                raise NetworkError(
                    f"VM {vm.name!r}: no network refs — Proxmox VMs "
                    "need at least one vNIC."
                )
            install_net_name, install_mac = network_entries[0]

            vm.set_client(self._client)
            with log_duration(_log, f"build VM {vm.name!r}"):
                installed_disks[vm.name] = vm.build(
                    context=self,
                    cache=cache,
                    run=None,  # type: ignore[arg-type]
                    install_network_name=install_net_name,
                    install_network_mac=install_mac,
                )
            self._provisioned_vms.append(vm)

        for vm in self._vm_list:
            assert isinstance(vm, ProxmoxVM)
            network_entries, mac_ip_pairs = self._vm_network_refs(vm)
            with log_duration(_log, f"start VM {vm.name!r}"):
                vm.start_run(
                    context=self,
                    run=None,  # type: ignore[arg-type]
                    installed_disk=installed_disks[vm.name],
                    network_entries=network_entries,
                    mac_ip_pairs=mac_ip_pairs,
                )
            self.vms[vm.name] = vm

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
