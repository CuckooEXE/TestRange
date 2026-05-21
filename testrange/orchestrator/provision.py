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
from testrange.networks.base import Network, Switch
from testrange.networks.sidecar import (
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


def mgmt_cidr(switch: Switch) -> str:
    return f"{switch.mgmt_ip}/{switch.network.prefixlen}"


def make_bridge(
    ctx: RunContext,
    switch: Switch,
    *,
    suffix: str,
    uplink: str | None,
    mgmt_cidr: str | None,
    kind: str,
) -> str:
    bridge_name = ctx.driver.compose_bridge_name(ctx.run_id, f"{switch.name}-{suffix}")
    ctx.store.record_intent(kind=kind, backend_name=bridge_name, plan_name=switch.name)
    if uplink is None:
        ctx.driver.create_isolated_bridge(bridge_name, mgmt_cidr=mgmt_cidr)
    else:
        ctx.driver.create_bridge(uplink, bridge_name, mgmt_cidr=mgmt_cidr)
    ctx.store.confirm(bridge_name)
    if suffix == "iso":
        ctx.switch_bridge[switch.name] = bridge_name
    else:
        ctx.switch_uplink_bridge[switch.name] = bridge_name
    return bridge_name


def provision_switch(ctx: RunContext, switch: Switch, *, kind_prefix: str = "") -> None:
    """Stand up the bridges + libvirt network(s) for one Switch.

    Topology cases:

    - ``uplink and not nat`` → one uplink bridge (enslaves the physical
      NIC; assigns ``.2`` if ``mgmt``). The libvirt network references
      this bridge by name.
    - ``uplink and nat`` → TWO bridges. An isolated switch bridge
      (assigns ``.2`` if ``mgmt``) holding guests + sidecar's eth0,
      plus a separate uplink bridge enslaving the physical NIC for
      the sidecar's eth1. A hidden ``__uplink__<switch>`` libvirt
      network exposes the uplink bridge to the sidecar VM.
    - ``mgmt`` or ``needs_sidecar`` without uplink → one isolated
      bridge (with ``.2`` if ``mgmt``).
    - bare → no testrange bridge; libvirt's default bridge.

    Records every created bridge / network in state for LIFO teardown.
    Network backend names are stashed in ``ctx.network_backends``.
    """
    bridge_name: str | None = None
    uplink_bridge_name: str | None = None
    if switch.uplink is not None and switch.nat:
        bridge_name = make_bridge(
            ctx,
            switch,
            suffix="iso",
            uplink=None,
            mgmt_cidr=mgmt_cidr(switch) if switch.mgmt else None,
            kind=f"{kind_prefix}bridge",
        )
        uplink_bridge_name = make_bridge(
            ctx,
            switch,
            suffix="upl",
            uplink=switch.uplink,
            mgmt_cidr=None,
            kind=f"{kind_prefix}bridge",
        )
    elif switch.uplink is not None:
        bridge_name = make_bridge(
            ctx,
            switch,
            suffix="upl",
            uplink=switch.uplink,
            mgmt_cidr=mgmt_cidr(switch) if switch.mgmt else None,
            kind=f"{kind_prefix}bridge",
        )
    elif switch.mgmt or switch.needs_sidecar:
        bridge_name = make_bridge(
            ctx,
            switch,
            suffix="iso",
            uplink=None,
            mgmt_cidr=mgmt_cidr(switch) if switch.mgmt else None,
            kind=f"{kind_prefix}bridge",
        )

    for net in switch.networks:
        backend = ctx.driver.compose_resource_name(ctx.run_id, f"{kind_prefix}network", net.name)
        ctx.store.record_intent(
            kind=f"{kind_prefix}network",
            backend_name=backend,
            plan_name=net.name,
        )
        ctx.driver.create_network(net, switch, backend, bridge_name=bridge_name)
        ctx.store.confirm(backend)
        ctx.network_backends[net.name] = backend

    if uplink_bridge_name is not None:
        uplink_net_name = f"__uplink__{switch.name}"
        uplink_backend = ctx.driver.compose_resource_name(
            ctx.run_id, f"{kind_prefix}network", uplink_net_name
        )
        # Synthetic Switch: exposes the uplink bridge as a libvirt network
        # so the sidecar's eth1 can attach. The renderer takes the
        # bridge-mode branch on `uplink is not None` and references
        # `uplink_bridge_name` directly; the cidr is a shim to satisfy
        # Switch's strict-form validator and is otherwise unused.
        uplink_switch = Switch(
            f"__uplink__{switch.name}",
            Network(uplink_net_name),
            cidr=switch.cidr,
            uplink=switch.uplink,
        )
        ctx.store.record_intent(
            kind=f"{kind_prefix}network",
            backend_name=uplink_backend,
            plan_name=uplink_net_name,
        )
        ctx.driver.create_network(
            Network(uplink_net_name),
            uplink_switch,
            uplink_backend,
            bridge_name=uplink_bridge_name,
        )
        ctx.store.confirm(uplink_backend)
        ctx.network_backends[uplink_net_name] = uplink_backend


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
    "make_bridge",
    "materialize_sidecar_for",
    "mgmt_cidr",
    "provision_switch",
]
