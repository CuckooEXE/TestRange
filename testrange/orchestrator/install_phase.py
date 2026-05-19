"""Install phase: build (or cache-hit) each VM's post-install disk.

Brings up a transient sidecar-served install network, runs each VM's
cloud-init install to completion, snapshots the resulting disk into the
cache, then tears the install-phase resources down LIFO.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.exceptions import (
    CacheError,
    CacheMissError,
    InstallTimeoutError,
    OrchestratorError,
)
from testrange.networks.base import Switch
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.install import _install_switch
from testrange.orchestrator.provision import (
    ensure_base_in_pool,
    materialize_sidecar_for,
    provision_switch,
)
from testrange.state.schema import PHASE_INSTALL
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)


def install_phase(ctx: RunContext) -> None:
    ctx.store.set_phase(PHASE_INSTALL)
    hyp = ctx.plan.hypervisor

    # 1. Create user pools first (install overlays live there).
    for pool in hyp.pools:
        backend = ctx.driver.compose_resource_name(ctx.run_id, "pool", pool.name)
        ctx.store.record_intent(kind="pool", backend_name=backend, plan_name=pool.name)
        ctx.driver.create_pool(pool, backend)
        ctx.store.confirm(backend)
        ctx.pool_backends[pool.name] = backend

    # 2. Transient install Switch — sidecar-served DHCP+DNS+NAT so install
    # VMs can reach the internet for apt/pip. The Hypervisor's
    # `install_uplink` carries the physical NIC for upstream egress.
    install_switch = _install_switch(getattr(hyp, "install_uplink", None))
    provision_switch(ctx, install_switch, kind_prefix="install_")
    install_net_backend = ctx.network_backends[install_switch.networks[0].name]
    materialize_sidecar_for(ctx, install_switch, kind_prefix="install_")

    # 3. Per VM: cache hit -> skip; cache miss -> build install VM.
    for vm in hyp.vms:
        install_one_vm(ctx, vm, install_net_backend)

    # 4. Tear down the install-phase resources LIFO. Run phase materializes
    # user Switches independently — no install-phase state bleeds through.
    teardown_install_phase(ctx, install_switch)


def install_one_vm(ctx: RunContext, vm: VMRecipe, install_net_backend: str) -> None:
    if not vm.spec.nics:
        raise OrchestratorError(
            f"vm {vm.name!r} declares no NICs; cloud-init install needs at "
            "least one NIC for internet access during install"
        )

    builder = vm.builder
    if not isinstance(builder, CloudInitBuilder):
        raise OrchestratorError(
            f"vm {vm.name!r}: only CloudInitBuilder is supported in v0, "
            f"got {type(builder).__name__}"
        )

    base_info = ctx.cache.resolve(builder.base)
    macs = tuple(
        ctx.driver.compose_mac(ctx.plan_name, vm.name, i) for i in range(len(vm.spec.nics))
    )
    config_hash = builder.config_hash(
        vm.spec,
        vm,
        addressing=ctx.addressing,
        base_sha=base_info.sha256,
        macs=macs,
    )
    post_install_name = f"_post_install_{config_hash}"

    # Cache hit? Manager checks local then HTTP (if configured); a hit
    # on the HTTP tier triggers a fetch into local before returning.
    try:
        cached = ctx.cache.resolve(post_install_name)
        assert cached.path is not None  # fetch=True guarantees this
        ctx.post_install_paths[vm.name] = cached.path
        _log.info("vm %s: cache hit on %s", vm.name, config_hash)
        return
    except CacheMissError:
        _log.info("vm %s: cache miss on %s; building install VM", vm.name, config_hash)
    except CacheError as e:
        # HTTP tier reachable but reported a non-404 error (e.g. 5xx).
        # Treat as a miss for resilience — local is source of truth —
        # but log loud enough to be noticed in CI.
        _log.warning(
            "vm %s: cache lookup error on %s (%s); building install VM",
            vm.name,
            config_hash,
            e,
        )

    pool_backend = ctx.pool_backends[vm.spec.os_drive.pool]
    install_vm_backend = ctx.driver.compose_resource_name(ctx.run_id, "install_vm", vm.name)
    install_disk_name = f"{install_vm_backend}{ctx.driver.volume_suffix('install_disk')}"
    install_seed_name = f"{install_vm_backend}-seed{ctx.driver.volume_suffix('install_seed')}"
    install_disk_ref = ctx.driver.compose_volume_ref(pool_backend, install_disk_name)
    install_seed_ref = ctx.driver.compose_volume_ref(pool_backend, install_seed_name)

    # Create install overlay
    ctx.store.record_intent(
        kind="install_disk",
        backend_name=install_disk_name,
        plan_name=vm.name,
        pool_backend=pool_backend,
    )
    assert base_info.path is not None  # cache.resolve(fetch=True) materializes locally
    base_ref = ensure_base_in_pool(ctx, pool_backend, base_info.path)
    ctx.driver.create_disk_from_base(install_disk_ref, base_ref)
    ctx.store.confirm(install_disk_name, pool_backend=pool_backend)

    # Render + write seed
    seed_bytes = builder.render_seed(vm.spec, vm, addressing=ctx.addressing, macs=macs)
    ctx.store.record_intent(
        kind="install_seed",
        backend_name=install_seed_name,
        plan_name=vm.name,
        pool_backend=pool_backend,
    )
    ctx.driver.write_to_pool(install_seed_ref, seed_bytes)
    ctx.store.confirm(install_seed_name, pool_backend=pool_backend)

    # Define + start install VM with ALL NICs on the install network
    install_network_refs = {nic.network: install_net_backend for nic in vm.spec.nics}
    ctx.store.record_intent(
        kind="install_vm",
        backend_name=install_vm_backend,
        plan_name=vm.name,
    )
    ctx.driver.create_vm(
        install_vm_backend,
        vm.spec,
        ctx.plan_name,
        os_disk_ref=install_disk_ref,
        seed_iso_ref=install_seed_ref,
        network_refs=install_network_refs,
    )
    ctx.store.confirm(install_vm_backend)
    ctx.driver.start_vm(install_vm_backend)

    # Poll for shutoff (the install runcmd ends with `poweroff`).
    wait_for_shutoff(ctx, install_vm_backend, vm.name)

    # Snapshot the post-install disk into the cache. The pool volume is
    # not necessarily readable by the orchestrator process — drivers may
    # run the hypervisor under their own service account or on a remote
    # host — so we stream it back via the driver, into a local temp
    # file, then ingest from there.
    with tempfile.NamedTemporaryFile(
        prefix=f"tr_post_install_{vm.name}_",
        suffix=ctx.driver.volume_suffix("install_disk"),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        ctx.driver.download_from_pool(install_disk_ref, tmp_path)
        info = ctx.cache.add(tmp_path, name=post_install_name)
    finally:
        tmp_path.unlink(missing_ok=True)
    assert info.path is not None  # manager.add returns local-flavored info
    ctx.post_install_paths[vm.name] = info.path
    _log.info(
        "vm %s: cached post-install disk as %s (%s)",
        vm.name,
        config_hash,
        info.short_sha,
    )

    # Tear down install resources (transient; remove from state.json).
    ctx.driver.destroy_vm(install_vm_backend)
    ctx.store.forget(install_vm_backend)
    ctx.driver.delete_volume(install_seed_ref)
    ctx.store.forget(install_seed_name)
    ctx.driver.delete_volume(install_disk_ref)
    ctx.store.forget(install_disk_name)


def wait_for_shutoff(ctx: RunContext, backend_name: str, vm_name: str) -> None:
    deadline = time.monotonic() + ctx.install_timeout_s
    last_state = "?"
    while time.monotonic() < deadline:
        state = ctx.driver.get_vm_power_state(backend_name)
        if state != last_state:
            _log.info("vm %s state: %s", vm_name, state)
            last_state = state
        if state == "shutoff":
            return
        time.sleep(2.0)
    raise InstallTimeoutError(
        f"vm {vm_name!r} did not power off within {ctx.install_timeout_s:.0f}s"
    )


def teardown_install_phase(ctx: RunContext, install_switch: Switch) -> None:
    """Destroy install-phase sidecar VM, networks, bridges (LIFO)."""
    sidecar = ctx.sidecar_backends.pop(install_switch.name, None)
    if sidecar is not None:
        ctx.driver.destroy_vm(sidecar)
        ctx.store.forget(sidecar)
    for net in install_switch.networks:
        backend = ctx.network_backends.pop(net.name, None)
        if backend is not None:
            ctx.driver.destroy_network(backend)
            ctx.store.forget(backend)
    uplink_net_name = f"__uplink__{install_switch.name}"
    uplink_backend = ctx.network_backends.pop(uplink_net_name, None)
    if uplink_backend is not None:
        ctx.driver.destroy_network(uplink_backend)
        ctx.store.forget(uplink_backend)
    uplink_bridge = ctx.switch_uplink_bridge.pop(install_switch.name, None)
    if uplink_bridge is not None:
        ctx.driver.destroy_bridge(uplink_bridge)
        ctx.store.forget(uplink_bridge)
    switch_bridge = ctx.switch_bridge.pop(install_switch.name, None)
    if switch_bridge is not None:
        ctx.driver.destroy_bridge(switch_bridge)
        ctx.store.forget(switch_bridge)


__all__ = [
    "install_one_vm",
    "install_phase",
    "teardown_install_phase",
    "wait_for_shutoff",
]
