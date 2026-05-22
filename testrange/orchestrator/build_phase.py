"""Build phase: warm the cache with each VM's built disk set.

For every VM the phase resolves its base, computes ``config_hash``, and probes
the cache for the full per-role artifact set (OS disk + each data disk). Only
if at least one VM misses does it stand up the ephemeral build pool / switch /
sidecar (ADR-0010 §2). Each missing VM is provisioned as a unit — every
writable disk attached — booted to completion, and every disk captured into the
cache. The backend is left empty afterward: build VMs and their disks are
deleted immediately after capture, and the build pool / switch / sidecar are
torn down at phase end (ADR-0010 §3).
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.devices.pool.base import StoragePool
from testrange.drivers.base import VolumeRef
from testrange.exceptions import (
    BuildTimeoutError,
    CacheError,
    CacheMissError,
    OrchestratorError,
)
from testrange.networks.base import Switch
from testrange.networks.sidecar import _uplink_network_name
from testrange.orchestrator.artifacts import (
    built_artifact_name,
    built_artifact_roles,
    data_disk_role,
)
from testrange.orchestrator.build import _build_switch
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.state.schema import PHASE_BUILD
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)

# The single ephemeral build pool's plan-level name (ADR-0010 §9). Cosmetic —
# the backend name is composed via the driver; this only labels the synthesized
# StoragePool and the sidecar's OSDrive.
BUILD_POOL_NAME = "__build"


@dataclass
class _VMBuildPlan:
    """A VM's resolved build inputs, plus its cache-probe outcome."""

    vm: VMRecipe
    builder: CloudInitBuilder
    config_hash: str
    macs: tuple[str, ...]
    base_path: Path
    roles: tuple[str, ...]
    # role -> cached path on a full hit; None when any role misses (whole-VM miss).
    cached_paths: dict[str, Path] | None


def build_phase(ctx: RunContext) -> None:
    """Warm the cache for every VM; build only the misses."""
    ctx.store.set_phase(PHASE_BUILD)

    misses, hits = _probe_all(ctx)
    ctx.built_disk_paths.update(hits)
    if not misses:
        _log.info("build: full cache hit; no backend resources needed")
        return

    # At least one miss: stand up the ephemeral build infra (ADR-0010 §2/§9).
    build_pool_backend = _create_build_pool(ctx, misses)
    build_switch = _build_switch(getattr(ctx.plan.hypervisor, "build_uplink", None))
    provision_switch(ctx, build_switch, kind_prefix="build_")
    build_net_backend = ctx.network_backends[build_switch.networks[0].name]
    materialize_sidecar_for(
        ctx,
        build_switch,
        kind_prefix="build_",
        pool_backend=build_pool_backend,
        pool_name=BUILD_POOL_NAME,
    )

    for bp in misses:
        build_one_vm(ctx, bp, build_pool_backend, build_net_backend)

    teardown_build_phase(ctx, build_switch, build_pool_backend)


def probe_misses(ctx: RunContext) -> list[str]:
    """Resolve + probe every VM; record hits, return the names that miss.

    Read-only against the backend (the only I/O is cache resolution, which
    may fetch a base on a cold local cache — the deliberate ADR-0010 §2
    penalty). Used by ``testrange run --require-cache`` to fail fast on a
    miss without building, and shares the probe path with :func:`build_phase`.
    """
    misses, hits = _probe_all(ctx)
    ctx.built_disk_paths.update(hits)
    return [bp.vm.name for bp in misses]


def _probe_all(
    ctx: RunContext,
) -> tuple[list[_VMBuildPlan], dict[str, dict[str, Path]]]:
    """Probe every VM. Returns (miss plans, {vm_name: {role: path}} for hits)."""
    misses: list[_VMBuildPlan] = []
    hits: dict[str, dict[str, Path]] = {}
    for vm in ctx.plan.hypervisor.vms:
        bp = _probe_vm(ctx, vm)
        if bp.cached_paths is None:
            _log.info("vm %s: cache miss on %s; will build", vm.name, bp.config_hash)
            misses.append(bp)
        else:
            _log.info("vm %s: cache hit on %s", vm.name, bp.config_hash)
            hits[vm.name] = bp.cached_paths
    return misses, hits


def _probe_vm(ctx: RunContext, vm: VMRecipe) -> _VMBuildPlan:
    if not vm.spec.nics:
        raise OrchestratorError(
            f"vm {vm.name!r} declares no NICs; cloud-init build needs at "
            "least one NIC for internet access during build"
        )
    builder = vm.builder
    if not isinstance(builder, CloudInitBuilder):
        raise OrchestratorError(
            f"vm {vm.name!r}: only CloudInitBuilder is supported in v0, "
            f"got {type(builder).__name__}"
        )

    base_info = ctx.cache.resolve(builder.base)
    assert base_info.path is not None  # cache.resolve(fetch=True) materializes locally
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
    roles = built_artifact_roles(len(vm.spec.data_drives))
    cached = _resolve_full_set(ctx, config_hash, roles)
    return _VMBuildPlan(
        vm=vm,
        builder=builder,
        config_hash=config_hash,
        macs=macs,
        base_path=base_info.path,
        roles=roles,
        cached_paths=cached,
    )


def _resolve_full_set(
    ctx: RunContext, config_hash: str, roles: tuple[str, ...]
) -> dict[str, Path] | None:
    """Resolve every role's cached artifact. All present -> dict; any absent -> None.

    A partial set (OS present, a data disk missing) is a miss for the whole
    VM (ADR-0010 §4). The manager checks local then HTTP; a hit on the HTTP
    tier fetches into local before returning.
    """
    paths: dict[str, Path] = {}
    for role in roles:
        name = built_artifact_name(config_hash, role)
        try:
            info = ctx.cache.resolve(name)
        except CacheMissError:
            return None
        except CacheError as e:
            # HTTP tier reachable but non-404 (e.g. 5xx). Local is source of
            # truth; treat as a miss for resilience but log loud enough to notice.
            _log.warning("cache lookup error on %s (%s); treating as build miss", name, e)
            return None
        assert info.path is not None  # fetch=True guarantees a local path
        paths[role] = info.path
    return paths


def _create_build_pool(ctx: RunContext, misses: list[_VMBuildPlan]) -> str:
    """Create the single ephemeral build pool sized to hold the largest VM's disks."""
    size_gb = max(
        bp.vm.spec.os_drive.size_gb + sum(d.size_gb for d in bp.vm.spec.data_drives)
        for bp in misses
    )
    backend = ctx.driver.compose_resource_name(ctx.run_id, "build_pool", "build")
    ctx.store.record_intent(kind="build_pool", backend_name=backend, plan_name=None)
    ctx.driver.create_pool(StoragePool(BUILD_POOL_NAME, size_gb), backend)
    ctx.store.confirm(backend)
    return backend


def build_one_vm(
    ctx: RunContext,
    bp: _VMBuildPlan,
    build_pool_backend: str,
    build_net_backend: str,
) -> None:
    """Provision one VM to completion and capture every writable disk.

    The VM boots with all its writable disks attached (OS + each data disk)
    and the install payload populates them; on power-off each disk is
    downloaded and added to the cache under its ``_built_…__{role}`` name.
    The build VM and all its volumes are deleted immediately afterward.
    """
    vm = bp.vm
    spec = vm.spec
    build_vm_backend = ctx.driver.compose_resource_name(ctx.run_id, "build_vm", vm.name)

    # --- OS disk: push base bytes straight onto this VM's own ref, then grow.
    os_disk_name = f"{build_vm_backend}{ctx.driver.volume_suffix('build_disk')}"
    os_disk_ref = ctx.driver.compose_volume_ref(build_pool_backend, os_disk_name)
    ctx.store.record_intent(
        kind="build_disk",
        backend_name=os_disk_name,
        plan_name=vm.name,
        pool_backend=build_pool_backend,
    )
    ctx.driver.upload_to_pool(os_disk_ref, bp.base_path)
    ctx.driver.resize_volume(os_disk_ref, spec.os_drive.size_gb)
    ctx.store.confirm(os_disk_name, pool_backend=build_pool_backend)

    # --- Data disks: blank, sized; the guest formats/populates them on the build boot.
    data_disk_refs: list[tuple[str, VolumeRef]] = []
    for i, hd in enumerate(spec.data_drives):
        name = f"{build_vm_backend}-{data_disk_role(i)}{ctx.driver.volume_suffix('data_disk')}"
        ref = ctx.driver.compose_volume_ref(build_pool_backend, name)
        ctx.store.record_intent(
            kind="data_disk",
            backend_name=name,
            plan_name=vm.name,
            pool_backend=build_pool_backend,
        )
        ctx.driver.create_blank_volume(ref, hd.size_gb)
        ctx.store.confirm(name, pool_backend=build_pool_backend)
        data_disk_refs.append((name, ref))

    # --- Seed ISO.
    seed_bytes = bp.builder.render_seed(spec, vm, addressing=ctx.addressing, macs=bp.macs)
    seed_name = f"{build_vm_backend}-seed{ctx.driver.volume_suffix('build_seed')}"
    seed_ref = ctx.driver.compose_volume_ref(build_pool_backend, seed_name)
    ctx.store.record_intent(
        kind="build_seed",
        backend_name=seed_name,
        plan_name=vm.name,
        pool_backend=build_pool_backend,
    )
    ctx.driver.write_to_pool(seed_ref, seed_bytes)
    ctx.store.confirm(seed_name, pool_backend=build_pool_backend)

    # --- Define + start the build VM with every writable disk attached.
    network_refs = {nic.network: build_net_backend for nic in spec.nics}
    ctx.store.record_intent(kind="build_vm", backend_name=build_vm_backend, plan_name=vm.name)
    ctx.driver.create_vm(
        build_vm_backend,
        spec,
        ctx.plan_name,
        os_disk_ref=os_disk_ref,
        seed_iso_ref=seed_ref,
        network_refs=network_refs,
        data_disk_refs=[ref for _, ref in data_disk_refs],
    )
    ctx.store.confirm(build_vm_backend)
    ctx.driver.start_vm(build_vm_backend)
    wait_for_shutoff(ctx, build_vm_backend, vm.name)

    # --- Capture every writable disk into the cache (ADR-0010 §4/§5).
    refs_by_role: dict[str, VolumeRef] = {"os": os_disk_ref}
    for i, (_, ref) in enumerate(data_disk_refs):
        refs_by_role[data_disk_role(i)] = ref
    captured: dict[str, Path] = {}
    for role in bp.roles:
        captured[role] = _capture_disk(ctx, refs_by_role[role], bp.config_hash, role, vm.name)
    ctx.built_disk_paths[vm.name] = captured

    # --- Delete everything on the backend (ADR-0010 §3).
    ctx.driver.destroy_vm(build_vm_backend)
    ctx.store.forget(build_vm_backend)
    ctx.driver.delete_volume(seed_ref)
    ctx.store.forget(seed_name)
    for name, ref in data_disk_refs:
        ctx.driver.delete_volume(ref)
        ctx.store.forget(name)
    ctx.driver.delete_volume(os_disk_ref)
    ctx.store.forget(os_disk_name)


def _capture_disk(
    ctx: RunContext,
    vol_ref: VolumeRef,
    config_hash: str,
    role: str,
    vm_name: str,
) -> Path:
    """Download one built disk and add it to the cache (+ HTTP tier when configured).

    The pool volume may not be readable by the orchestrator process (different
    uid, remote host), so it is streamed back via the driver into a local temp
    file, then ingested. ``CacheManager.add`` mirrors to the HTTP tier
    best-effort when one is configured (ADR-0010 §5).
    """
    cache_name = built_artifact_name(config_hash, role)
    with tempfile.NamedTemporaryFile(
        prefix=f"tr_built_{vm_name}_{role}_",
        suffix=ctx.driver.volume_suffix("build_disk"),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        ctx.driver.download_from_pool(vol_ref, tmp_path)
        info = ctx.cache.add(tmp_path, name=cache_name)
    finally:
        tmp_path.unlink(missing_ok=True)
    assert info.path is not None  # manager.add returns local-flavored info
    _log.info("vm %s: cached built disk %s as %s (%s)", vm_name, role, config_hash, info.short_sha)
    return info.path


def wait_for_shutoff(ctx: RunContext, backend_name: str, vm_name: str) -> None:
    deadline = time.monotonic() + ctx.build_timeout_s
    last_state = "?"
    while time.monotonic() < deadline:
        state = ctx.driver.get_vm_power_state(backend_name)
        if state != last_state:
            _log.info("vm %s state: %s", vm_name, state)
            last_state = state
        if state == "shutoff":
            return
        time.sleep(2.0)
    raise BuildTimeoutError(f"vm {vm_name!r} did not power off within {ctx.build_timeout_s:.0f}s")


def teardown_build_phase(ctx: RunContext, build_switch: Switch, build_pool_backend: str) -> None:
    """Destroy build-phase sidecar VM, networks, switch, and the build pool (LIFO)."""
    sidecar = ctx.sidecar_backends.pop(build_switch.name, None)
    if sidecar is not None:
        ctx.driver.destroy_vm(sidecar)
        ctx.store.forget(sidecar)
    for net in build_switch.networks:
        backend = ctx.network_backends.pop(net.name, None)
        if backend is not None:
            ctx.driver.destroy_network(backend)
            ctx.store.forget(backend)
    # The uplink-facing segment (when nat) is owned by the switch; drop the
    # ledger entry, destroy_switch tears down the actual segment.
    ctx.network_backends.pop(_uplink_network_name(build_switch), None)
    switch_backend = ctx.switch_backends.pop(build_switch.name, None)
    if switch_backend is not None:
        ctx.driver.destroy_switch(switch_backend)
        ctx.store.forget(switch_backend)
    # The build pool comes up only for the build phase and never survives it.
    ctx.driver.destroy_pool(build_pool_backend)
    ctx.store.forget(build_pool_backend)


__all__ = [
    "BUILD_POOL_NAME",
    "build_one_vm",
    "build_phase",
    "probe_misses",
    "teardown_build_phase",
    "wait_for_shutoff",
]
