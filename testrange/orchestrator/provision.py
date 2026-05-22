"""Shared provisioning substrate: switches, bridges, sidecars, base images.

These helpers are used by both the install phase and the run phase. Each
takes a :class:`RunContext` explicitly and brokers between Plan-time data and
the driver/cache/state store.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from testrange.builders.sidecar_iso import build_sidecar_config_iso
from testrange.cache.entry import CacheEntry
from testrange.drivers.base import VolumeRef
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
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.install import _sidecar_spec


def ensure_base_in_pool(ctx: RunContext, pool_backend: str, source_path: Path) -> VolumeRef:
    """Upload a host-side base image into the pool, idempotent per run.

    Returns the in-pool path. The volume name is derived from the cache
    file's stem (a content sha), so multiple VMs sharing a base share
    the in-pool upload too.
    """
    vol_name = f"tr_base_{source_path.stem}{ctx.driver.volume_suffix('base_image')}"
    target_ref = ctx.driver.compose_volume_ref(pool_backend, vol_name)
    key = (pool_backend, vol_name)
    if key in ctx.uploaded_bases:
        return ctx.driver.upload_to_pool(target_ref, source_path)
    ctx.store.record_intent(
        kind="base_image",
        backend_name=vol_name,
        plan_name=None,
        pool_backend=pool_backend,
    )
    ctx.driver.upload_to_pool(target_ref, source_path)
    ctx.store.confirm(vol_name, pool_backend=pool_backend)
    ctx.uploaded_bases.add(key)
    return target_ref


def mac_for(ctx: RunContext, vm_name: str, idx: int) -> str:
    return ctx.driver.compose_mac(ctx.plan_name, vm_name, idx)


def provision_switch(ctx: RunContext, switch: Switch, *, kind_prefix: str = "") -> None:
    """Realize one Switch and its Network(s) via the driver.

    The driver owns all L2 topology — the orchestrator names no bridges. It
    calls :meth:`HypervisorDriver.create_switch` (the driver decides bridge vs
    vSwitch vs vmbr vs VMSwitch, assigns the ``mgmt`` adapter, and — for a
    ``uplink+nat`` Switch — provisions the uplink-facing segment the sidecar's
    ``eth1`` rides), then attaches each Network with
    :meth:`HypervisorDriver.create_network`.

    Records the switch + each network in state for LIFO teardown. Network
    backend names (including the driver-owned uplink segment, keyed under the
    synthetic ``__uplink__<switch>`` name) are stashed in
    ``ctx.network_backends`` for the sidecar and VM NIC wiring.
    """
    switch_backend = ctx.driver.compose_resource_name(
        ctx.run_id, f"{kind_prefix}switch", switch.name
    )
    ctx.store.record_intent(
        kind=f"{kind_prefix}switch",
        backend_name=switch_backend,
        plan_name=switch.name,
    )
    uplink_net_backend = ctx.driver.create_switch(switch, switch_backend)
    ctx.store.confirm(switch_backend)
    ctx.switch_backends[switch.name] = switch_backend

    for net in switch.networks:
        backend = ctx.driver.compose_resource_name(ctx.run_id, f"{kind_prefix}network", net.name)
        ctx.store.record_intent(
            kind=f"{kind_prefix}network",
            backend_name=backend,
            plan_name=net.name,
        )
        ctx.driver.create_network(net, switch, backend, switch_backend_name=switch_backend)
        ctx.store.confirm(backend)
        ctx.network_backends[net.name] = backend

    if uplink_net_backend is not None:
        # The uplink-facing segment is owned by the switch (created inside
        # create_switch, torn down by destroy_switch). Expose it under the
        # synthetic uplink network name so the sidecar's eth1 can attach; it
        # is not separately recorded in state.
        ctx.network_backends[_uplink_network_name(switch)] = uplink_net_backend


def materialize_sidecar_for(ctx: RunContext, switch: Switch, *, kind_prefix: str = "") -> None:
    if not switch.needs_sidecar:
        return
    if not ctx.plan.hypervisor.pools:
        raise OrchestratorError(f"switch {switch.name!r} needs a sidecar but the plan has no pools")
    pool_name = ctx.plan.hypervisor.pools[0].name
    pool_backend = ctx.pool_backends[pool_name]
    sidecar_spec = _sidecar_spec(switch, pool_name)
    sidecar_vm_backend = ctx.driver.compose_resource_name(
        ctx.run_id, f"{kind_prefix}sidecar_vm", switch.name
    )

    # 1. Sidecar's overlay disk (cached Alpine image as base).
    sidecar_disk_name = f"{sidecar_vm_backend}{ctx.driver.volume_suffix('sidecar_disk')}"
    sidecar_disk_ref = ctx.driver.compose_volume_ref(pool_backend, sidecar_disk_name)
    base_info = ctx.cache.resolve(CacheEntry(SIDECAR_CACHE_NAME))
    assert base_info.path is not None
    base_ref = ensure_base_in_pool(ctx, pool_backend, base_info.path)
    ctx.store.record_intent(
        kind="sidecar_disk",
        backend_name=sidecar_disk_name,
        plan_name=switch.name,
        pool_backend=pool_backend,
    )
    ctx.driver.create_disk_from_base(sidecar_disk_ref, base_ref)
    ctx.store.confirm(sidecar_disk_name, pool_backend=pool_backend)

    # 2. Per-run config ISO: dnsmasq.conf + interfaces + nftables + sysctl.
    sidecar_cfg_name = f"{sidecar_vm_backend}-cfg{ctx.driver.volume_suffix('sidecar_config')}"
    sidecar_cfg_ref = ctx.driver.compose_volume_ref(pool_backend, sidecar_cfg_name)
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
    ctx.driver.write_to_pool(sidecar_cfg_ref, iso_bytes)
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
    ctx.driver.create_vm(
        sidecar_vm_backend,
        sidecar_spec,
        ctx.plan_name,
        os_disk_ref=sidecar_disk_ref,
        seed_iso_ref=sidecar_cfg_ref,
        network_refs=network_refs,
    )
    ctx.store.confirm(sidecar_vm_backend)
    ctx.driver.start_vm(sidecar_vm_backend)
    ctx.sidecar_backends[switch.name] = sidecar_vm_backend


__all__ = [
    "ensure_base_in_pool",
    "mac_for",
    "materialize_sidecar_for",
    "provision_switch",
]
