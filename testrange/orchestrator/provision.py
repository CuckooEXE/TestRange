"""Shared provisioning substrate: switches, sidecars, NIC MACs.

These helpers are used by both the build phase and the run phase. Each
takes a :class:`RunContext` explicitly and brokers between Plan-time data and
the driver/cache/state store.

Every disk reaches the backend by host->pool upload (``upload_to_pool``);
there is no pool->pool copy and no shared base (ADR-0010 §3).
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from testrange.builders.sidecar_iso import build_sidecar_config_iso
from testrange.cache.entry import CacheEntry
from testrange.exceptions import OrchestratorError
from testrange.networks._addressing_consts import SIDECAR_CACHE_NAME
from testrange.networks.base import Switch
from testrange.networks.sidecar import (
    _uplink_network_name,
    render_dnsmasq_conf,
    render_nftables_ruleset,
    render_sidecar_interfaces,
    render_sysctl_conf,
    sidecar_nic_specs,
)
from testrange.orchestrator.build import _sidecar_spec
from testrange.orchestrator.context import RunContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from testrange.devices.network import StaticAddr


def _effective_switch(switch: Switch, uplink_addrs: Mapping[str, StaticAddr]) -> Switch:
    """Inject the profile's static uplink address (NET-8) into a Switch's sidecar.

    The plan stays portable: a host-NAT'd uplink's static MASQUERADE-NIC address
    (which won't DHCP) lives in the bound profile's ``[uplinks]`` table, not the
    Switch. The orchestrator is the only thing that sees both, so it brokers the
    merge here. A no-op unless the Switch has a ``nat`` sidecar with no address of
    its own (a plan-set ``addr`` wins) and the profile maps its uplink name.
    """
    sc = switch.sidecar
    if sc is None or not sc.nat or sc.addr is not None or switch.uplink is None:
        return switch
    addr = uplink_addrs.get(switch.uplink)
    if addr is None:
        return switch
    return Switch(
        switch.name,
        *switch.networks,
        cidr=switch.cidr,
        uplink=switch.uplink,
        mgmt=switch.mgmt,
        sidecar=replace(sc, addr=addr),
    )


def mac_for(ctx: RunContext, vm_name: str, idx: int) -> str:
    return ctx.driver.compose_mac(ctx.plan_name, vm_name, idx)


def provision_switch(
    ctx: RunContext,
    switch: Switch,
    *,
    kind_prefix: str = "",
) -> None:
    """Realize one Switch and its Network(s) via the driver.

    The driver owns all L2 topology — the orchestrator names no bridges. It
    calls :meth:`HypervisorDriver.create_switch` (the driver decides bridge vs
    vSwitch vs vmbr vs VMSwitch, resolves the logical ``uplink`` name against the
    profile's ``[uplinks]`` map, assigns the ``mgmt`` adapter, and — for a
    ``uplink+nat`` Switch — provisions the uplink-facing segment the sidecar's
    ``eth1`` rides), then attaches each Network with
    :meth:`HypervisorDriver.create_network`.

    Records the switch + each network in state for LIFO teardown. Network
    backend names (including the driver-owned uplink segment, keyed under the
    synthetic ``__uplink__<switch>`` name) are stashed in
    ``ctx.network_backends`` for the sidecar and VM NIC wiring.

    The run phase provisions independent switches concurrently on the shared,
    thread-safe driver (ADR-0023); the ledger-dict writes take ``ctx.ledger_lock``
    since concurrent switches share those dicts.
    """
    drv = ctx.driver
    switch_backend = drv.compose_resource_name(ctx.run_id, f"{kind_prefix}switch", switch.name)
    ctx.store.record_intent(
        kind=f"{kind_prefix}switch",
        backend_name=switch_backend,
        plan_name=switch.name,
    )
    uplink_net_backend = drv.create_switch(switch, switch_backend)
    ctx.store.confirm(switch_backend)
    with ctx.ledger_lock:
        ctx.switch_backends[switch.name] = switch_backend

    for net in switch.networks:
        backend = drv.compose_resource_name(ctx.run_id, f"{kind_prefix}network", net.name)
        ctx.store.record_intent(
            kind=f"{kind_prefix}network",
            backend_name=backend,
            plan_name=net.name,
        )
        drv.create_network(net, switch, backend, switch_backend_name=switch_backend)
        ctx.store.confirm(backend)
        with ctx.ledger_lock:
            ctx.network_backends[net.name] = backend

    if uplink_net_backend is not None:
        # The uplink-facing segment is owned by the switch (created inside
        # create_switch, torn down by destroy_switch). Expose it under the
        # synthetic uplink network name so the sidecar's eth1 can attach; it
        # is not separately recorded in state.
        with ctx.ledger_lock:
            ctx.network_backends[_uplink_network_name(switch)] = uplink_net_backend


def materialize_sidecar_for(
    ctx: RunContext,
    switch: Switch,
    *,
    kind_prefix: str = "",
    pool_backend: str | None = None,
    pool_name: str | None = None,
) -> None:
    """Bring up the per-Switch sidecar VM (DHCP/DNS/NAT services).

    By default the sidecar lands in the user's first declared pool (the
    run-phase home). The build phase passes an explicit ``pool_backend`` +
    ``pool_name`` so the sidecar lives in the ephemeral build pool instead —
    the build phase no longer creates the user's pools (ADR-0010 §9).

    The run phase materializes independent sidecars concurrently on the shared,
    thread-safe driver (ADR-0023).
    """
    if not switch.needs_sidecar:
        return
    drv = ctx.driver
    # NET-8: merge any profile-supplied static uplink address into the sidecar
    # before it is specced/rendered, so a host-NAT'd uplink egresses + resolves.
    switch = _effective_switch(switch, ctx.resolved.uplink_addrs)
    if pool_backend is None:
        if not ctx.plan.hypervisor.pools:
            raise OrchestratorError(
                f"switch {switch.name!r} needs a sidecar but the plan has no pools"
            )
        pool_name = ctx.plan.hypervisor.pools[0].name
        pool_backend = ctx.pool_backends[pool_name]
    assert pool_name is not None, "pool_name must accompany an explicit pool_backend"
    sidecar_spec = _sidecar_spec(switch, pool_name)
    sidecar_vm_backend = drv.compose_resource_name(
        ctx.run_id, f"{kind_prefix}sidecar_vm", switch.name
    )

    # 1. Sidecar's OS disk: push the cached Alpine image straight onto the
    # sidecar's own ref — no shared base, no clone (ADR-0010 §3). Sidecars
    # carry no data disks.
    sidecar_disk_name = f"{sidecar_vm_backend}{drv.volume_suffix('sidecar_disk')}"
    sidecar_disk_ref = drv.compose_volume_ref(pool_backend, sidecar_disk_name)
    base_info = ctx.cache.resolve(CacheEntry(SIDECAR_CACHE_NAME))
    assert base_info.path is not None
    ctx.store.record_intent(
        kind="sidecar_disk",
        backend_name=sidecar_disk_name,
        plan_name=switch.name,
        pool_backend=pool_backend,
    )
    drv.upload_to_pool(sidecar_disk_ref, base_info.path)
    ctx.store.confirm(sidecar_disk_name, pool_backend=pool_backend)

    # 2. Per-run config ISO: dnsmasq.conf + interfaces + nftables + sysctl.
    sidecar_cfg_name = f"{sidecar_vm_backend}-cfg{drv.volume_suffix('sidecar_config')}"
    sidecar_cfg_ref = drv.compose_volume_ref(pool_backend, sidecar_cfg_name)
    iso_bytes = build_sidecar_config_iso(
        dnsmasq_conf=render_dnsmasq_conf(switch, ctx.plan.hypervisor.vms, partial(mac_for, ctx)),
        interfaces=render_sidecar_interfaces(switch),
        nftables_ruleset=render_nftables_ruleset(switch),
        sysctl_conf=render_sysctl_conf(switch),
    )
    ctx.store.record_intent(
        kind="sidecar_config",
        backend_name=sidecar_cfg_name,
        plan_name=switch.name,
        pool_backend=pool_backend,
    )
    drv.write_to_pool(sidecar_cfg_ref, iso_bytes)
    ctx.store.confirm(sidecar_cfg_name, pool_backend=pool_backend)

    # 3. Define + start the sidecar VM. NIC0 sits on the switch network;
    # for `nat`, NIC1 sits on the hidden __uplink__ network.
    nic_specs = sidecar_nic_specs(switch)
    network_refs = {name: ctx.network_backends[name] for (name, _ip) in nic_specs}
    ctx.store.record_intent(
        kind="sidecar_vm",
        backend_name=sidecar_vm_backend,
        plan_name=switch.name,
    )
    drv.create_vm(
        sidecar_vm_backend,
        sidecar_spec,
        ctx.plan_name,
        os_disk_ref=sidecar_disk_ref,
        seed_iso_ref=sidecar_cfg_ref,
        network_refs=network_refs,
    )
    ctx.store.confirm(sidecar_vm_backend)
    drv.start_vm(sidecar_vm_backend)
    with ctx.ledger_lock:
        ctx.sidecar_backends[switch.name] = sidecar_vm_backend


__all__ = [
    "mac_for",
    "materialize_sidecar_for",
    "provision_switch",
]
