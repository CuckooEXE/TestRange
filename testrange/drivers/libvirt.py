"""Libvirt driver.

Phase 0: the ``LibvirtHypervisor`` Plan-time dataclass (the top-level
entry in ``Plan(*hypervisors)``).
Phase 2: ``LibvirtDriver`` runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from testrange.devices.pool.base import StoragePool
from testrange.networks.base import Network, Switch
from testrange.vms.recipe import VMRecipe


@dataclass(frozen=True)
class LibvirtHypervisor:
    """Top-level Plan entry: a libvirt host with its declared topology.

    The driver class is inferred from this type at orchestrator-construction
    time: ``LibvirtHypervisor -> LibvirtDriver(uri=connection)``.
    """

    connection: str
    networks: tuple[Switch, ...]
    pools: tuple[StoragePool, ...]
    vms: tuple[VMRecipe, ...]

    def __init__(
        self,
        *,
        connection: str,
        networks: Sequence[Switch] = (),
        pools: Sequence[StoragePool] = (),
        vms: Sequence[VMRecipe] = (),
    ) -> None:
        if not isinstance(connection, str) or not connection:
            raise ValueError("LibvirtHypervisor.connection must be a non-empty string")
        switches = tuple(networks)
        for s in switches:
            if not isinstance(s, Switch):
                raise TypeError(
                    f"LibvirtHypervisor.networks must contain Switch, got {type(s).__name__}"
                )
        ps = tuple(pools)
        for p in ps:
            if not isinstance(p, StoragePool):
                raise TypeError(
                    f"LibvirtHypervisor.pools must contain StoragePool, got {type(p).__name__}"
                )
        rs = tuple(vms)
        for r in rs:
            if not isinstance(r, VMRecipe):
                raise TypeError(
                    f"LibvirtHypervisor.vms must contain VMRecipe, got {type(r).__name__}"
                )

        # Cross-reference checks
        net_names = {n.name for s in switches for n in s.networks}
        pool_names = {p.name for p in ps}
        vm_names = [r.name for r in rs]
        dup_vms = {n for n in vm_names if vm_names.count(n) > 1}
        if dup_vms:
            raise ValueError(f"LibvirtHypervisor.vms has duplicate names: {sorted(dup_vms)}")
        all_nets = [n.name for s in switches for n in s.networks]
        dup_nets = {n for n in all_nets if all_nets.count(n) > 1}
        if dup_nets:
            raise ValueError(f"LibvirtHypervisor networks have duplicate names: {sorted(dup_nets)}")

        for r in rs:
            for nic in r.spec.nics:
                if nic.network not in net_names:
                    raise ValueError(
                        f"VM {r.name!r} references unknown network {nic.network!r}; "
                        f"declared networks: {sorted(net_names)}"
                    )
            if r.spec.os_drive.pool not in pool_names:
                raise ValueError(
                    f"VM {r.name!r} OSDrive references unknown pool "
                    f"{r.spec.os_drive.pool!r}; declared pools: {sorted(pool_names)}"
                )
            for d in r.spec.data_drives:
                if d.pool not in pool_names:
                    raise ValueError(
                        f"VM {r.name!r} HardDrive references unknown pool "
                        f"{d.pool!r}; declared pools: {sorted(pool_names)}"
                    )

        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "networks", switches)
        object.__setattr__(self, "pools", ps)
        object.__setattr__(self, "vms", rs)

    @property
    def all_networks(self) -> tuple[Network, ...]:
        """Flat list of every Network across all Switches."""
        return tuple(n for s in self.networks for n in s.networks)
