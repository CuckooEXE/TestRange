"""Proxmox VE orchestrator (SCAFFOLDING).

.. warning::

   This module is *not yet implemented*.  The class is here so that
   TestRange's backend architecture is exercised by something other
   than libvirt, and so the abstract method signatures have a real
   second subscriber.  Instantiating and entering the orchestrator
   raises :class:`NotImplementedError`.

Target architecture
-------------------

The Proxmox backend will drive a Proxmox VE cluster via:

- the REST API (`proxmoxer <https://pypi.org/project/proxmoxer/>`_
  wraps auth + retries nicely) for the majority of the lifecycle —
  define VM, start, stop, clone, snapshot, SDN vnet create, storage
  uploads;
- fallback shell-outs to ``qm`` / ``pct`` over SSH for the handful of
  storage-pool operations the REST API does not cleanly expose (e.g.
  importing a qcow2 into an LVM-thin pool).

The builder layer (:class:`~testrange.vms.builders.CloudInitBuilder`,
:class:`~testrange.vms.builders.WindowsUnattendedBuilder`,
:class:`~testrange.vms.builders.NoOpBuilder`) is shared with libvirt
— their :class:`~testrange.vms.builders.base.InstallDomain` /
:class:`~testrange.vms.builders.base.RunDomain` outputs are
hypervisor-neutral.  Only the *rendering* into backend-native calls is
different: where libvirt emits domain XML,
:class:`~testrange.backends.proxmox.vm.ProxmoxVM` will translate the
same dataclasses into ``qm create`` / ``POST /api2/json/nodes/{node}/qemu``
parameters.

TODO list for implementation
----------------------------

Roughly in dependency order:

1. Lazy-import ``proxmoxer`` inside :meth:`__enter__`; raise
   :class:`~testrange.exceptions.OrchestratorError` with the pip
   install hint when missing.
2. Authenticate against ``/api2/json/access/ticket`` using the
   ``host`` + API-token / password supplied at construction.
3. Pick a target node and storage pool.  For single-node setups this
   is trivial; for clusters, add ``node=`` / ``storage=`` kwargs.
4. Create SDN vnets via ``/cluster/sdn/vnets`` for each
   :class:`~testrange.networks.base.AbstractVirtualNetwork`; reload
   SDN; reserve IPs via the IPAM endpoint for static-IP
   :class:`~testrange.devices.vNIC` entries.
5. For each VM, delegate to
   :meth:`~testrange.backends.proxmox.vm.ProxmoxVM.build` which
   consumes the builder's :class:`InstallDomain` and:

   a. uploads / clones the required disk images into the storage pool;
   b. creates an ephemeral VMID via ``POST /nodes/{node}/qemu`` with
      the right ``bios``, ``ostype``, ``ide0``, ``net0``, etc.;
   c. starts the domain and polls
      ``/nodes/{node}/qemu/{vmid}/status/current`` until it reports
      ``stopped`` (the autounattend / cloud-init power_state dance
      still works — Proxmox just observes it differently);
   d. snapshots the post-install disk into the TestRange cache.

6. On exit: stop each VM, destroy the ephemeral VMIDs, delete the
   SDN vnets, reload SDN.

Non-goals (for v1 of the Proxmox backend)
-----------------------------------------

- LXC containers — TestRange is VM-focused and LXC has different
  semantics for most features.
- HA failover / live migration — single-node use is the v1 target.
- Alternative communicators — the shipped communicators (SSH, WinRM)
  work against Proxmox guests unchanged.  A
  ``ProxmoxGuestAgentCommunicator`` (going through
  ``/nodes/{node}/qemu/{vmid}/agent``) is a follow-up.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from testrange.orchestrator_base import AbstractOrchestrator

if TYPE_CHECKING:
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM
    from testrange.vms.hypervisor_base import AbstractHypervisor


class ProxmoxOrchestrator(AbstractOrchestrator):
    """Proxmox VE implementation of
    :class:`~testrange.orchestrator_base.AbstractOrchestrator`.

    **NOT YET IMPLEMENTED.**  Constructing an instance succeeds so
    that callers can stash one for future use without crashing, but
    entering the context manager raises
    :class:`NotImplementedError`.

    :param host: Proxmox cluster entry point — a hostname or IP of any
        node in the cluster.
    :param networks: Virtual networks to create as SDN vnets.
    :param vms: VMs to provision.
    :param cache_root: Override the default cache directory.
    :param node: Optional target node name.  Defaults to the node
        reached by *host* if the cluster has more than one.
    :param storage: Optional storage-pool name for disk images.
        Common values: ``"local-lvm"``, ``"local-zfs"``, ``"ceph"``.
    :param token: Proxmox API token ID + secret, or a ``(user, pw)``
        tuple.  Exact shape to be decided during implementation.
    """

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
        self._networks = list(networks) if networks else []
        self._vm_list = list(vms) if vms else []
        self._cache_root = cache_root
        self._cache_url = cache
        self._cache_verify = cache_verify
        self._node = node
        self._storage = storage
        self._token = token
        self.vms = {}

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

    def __enter__(self) -> AbstractOrchestrator:
        # TODO: lazy import proxmoxer here; raise OrchestratorError
        # with a clear "pip install testrange[proxmox]" message if
        # the client library is unavailable.
        # TODO: authenticate via POST /api2/json/access/ticket using
        # self._token (or user+password fallback).
        # TODO: resolve self._node / self._storage defaults.
        # TODO: create SDN vnets for every AbstractVirtualNetwork in
        # self._networks, via POST /cluster/sdn/vnets + reload SDN.
        # TODO: for each VM, call vm.build(self, cache, run, ...) and
        # then vm.start_run(self, ...); populate self.vms.
        raise NotImplementedError(
            "ProxmoxOrchestrator is not yet implemented.  "
            "Contribute the REST calls + SDN work at "
            "testrange.backends.proxmox, or use the libvirt "
            "Orchestrator in the meantime."
        )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # TODO: stop VMs via POST /nodes/{node}/qemu/{vmid}/status/stop.
        # TODO: delete VMIDs via DELETE /nodes/{node}/qemu/{vmid}.
        # TODO: delete SDN vnets; reload SDN.
        # TODO: close the proxmoxer client.
        raise NotImplementedError(
            "ProxmoxOrchestrator teardown is not yet implemented."
        )

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
