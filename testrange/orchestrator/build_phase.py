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

import base64
import binascii
import hashlib
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from testrange._ansi import scrub_terminal_control
from testrange._log import get_logger
from testrange.builders.base import Builder
from testrange.cache.entry import CacheEntry
from testrange.devices.network import StaticAddr
from testrange.devices.pool.base import StoragePool
from testrange.drivers.base import BUILD_NIC_NIC_IDX, HypervisorDriver, VolumeRef
from testrange.exceptions import (
    BuildFailedError,
    BuildTimeoutError,
    CacheError,
    CacheMissError,
    OrchestratorError,
)
from testrange.networks._addressing_consts import (
    BUILD_NIC_OFFSET,
    DHCP_RANGE_LO,
    SIDECAR_CACHE_NAME,
    USER_STATIC_HI,
    USER_STATIC_LO,
)
from testrange.networks.base import BuildNic, NetworkAddressing, Switch
from testrange.networks.sidecar import _uplink_network_name
from testrange.orchestrator._parallel import parallel_map
from testrange.orchestrator.artifacts import (
    built_artifact_name,
    built_artifact_roles,
    data_disk_role,
)
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.dashboard_state import VMStage
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.plan import Plan
from testrange.state.schema import PHASE_BUILD
from testrange.vms.nested import GuestHypervisor, reject_unsupported_nesting
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)

# Build VMs' serial console output is streamed here, one line per record, as it
# arrives — run with ``--verbose`` to watch a build provision live in the tail.
# A dedicated child logger so it routes independently of the phase's own INFO
# progress lines: it is pinned above the operator log level (CORE-50) so a plain
# ``--log-level debug`` run does not dump this raw-guest-output firehose through
# the stderr handler; only the ``--verbose`` live tail lowers it to DEBUG.
_console = get_logger(f"{__name__}.console")

# The single ephemeral build pool's plan-level name (ADR-0010 §9). Cosmetic —
# the backend name is composed via the driver; this only labels the synthesized
# StoragePool and the sidecar's OSDrive.
BUILD_POOL_NAME = "__build"


@dataclass
class _VMBuildPlan:
    """A VM's resolved build inputs, plus its cache-probe outcome."""

    vm: VMRecipe
    builder: Builder
    config_hash: str
    macs: tuple[str, ...]
    build_nic: BuildNic
    # Image-origin: the local path of the OS base to upload+grow. Installer-
    # origin: None (the OS disk is materialized blank; the install medium is
    # ``boot_media_path`` instead).
    base_path: Path | None
    boot_media_path: Path | None
    roles: tuple[str, ...]
    # role -> cached path on a full hit; None when any role misses (whole-VM miss).
    cached_paths: dict[str, Path] | None

    def __post_init__(self) -> None:
        # Exactly one OS-disk origin: an image base (upload+grow) XOR an installer
        # boot medium (blank disk the installer partitions). installer_origin reads
        # base_path alone, so a plan with *both* set would silently drop the boot
        # medium and one with *neither* would yield a blank, unbootable disk.
        # _probe_vm constructs these mutually exclusive; this backstops a future
        # edit (or a test) that doesn't.
        if (self.base_path is None) == (self.boot_media_path is None):
            raise OrchestratorError(
                f"vm {self.vm.name!r}: _VMBuildPlan needs exactly one OS-disk origin "
                f"(base_path xor boot_media_path); got base_path={self.base_path!r}, "
                f"boot_media_path={self.boot_media_path!r}"
            )

    @property
    def installer_origin(self) -> bool:
        return self.base_path is None


# Host offsets a build NIC may take on the build switch, in allocation order:
# the ``.3``-``.9`` infra range first, then ``.100`` upward — skipping the
# sidecar's DHCP pool ``.10``-``.99`` so a static build IP never collides with a
# lease. The build switch carries no user-declared statics (build VMs attach
# only the build NIC), so ``.100``+ is free there.
_BUILD_IP_SLOTS: tuple[int, ...] = (
    *range(BUILD_NIC_OFFSET, DHCP_RANGE_LO),
    *range(USER_STATIC_LO, USER_STATIC_HI + 1),
)


def _build_ip_offset(vm_index: int) -> int:
    """Deterministic per-VM build-switch host offset, keyed on the VM's plan
    position (not scheduling order, since the build IP feeds ``config_hash``)."""
    if vm_index >= len(_BUILD_IP_SLOTS):
        raise OrchestratorError(
            f"build needs a distinct build-switch address per VM, but VM #{vm_index} "
            f"exceeds the {len(_BUILD_IP_SLOTS)} available on the build switch"
        )
    return _BUILD_IP_SLOTS[vm_index]


def _build_nic_for(ctx: RunContext, build_switch: Switch, vm_name: str, vm_index: int) -> BuildNic:
    """Synthesize the dedicated build NIC for one VM (ADR-0017).

    One transient NIC on the build switch, statically addressed with a
    reserved-slot MAC (:data:`BUILD_NIC_NIC_IDX`) disjoint from the VM's declared
    NICs. The host offset is :func:`_build_ip_offset` of the VM's plan position,
    so concurrent build VMs (ORCH-4) get distinct, deterministic addresses. When
    the build switch is ``nat`` the address derives its gateway/DNS from the
    sidecar at ``.1``, so the build boot egresses for ``apt``/``pip``.
    """
    network = build_switch.networks[0]
    build_ip = str(build_switch.network.network_address + _build_ip_offset(vm_index))
    return BuildNic(
        mac=ctx.driver.compose_mac(ctx.plan_name, vm_name, BUILD_NIC_NIC_IDX),
        network=network.name,
        addr=StaticAddr(build_ip),
        addressing=NetworkAddressing.from_switch(build_switch),
    )


def build_phase(ctx: RunContext) -> None:
    """Warm the cache for every VM; build only the misses.

    Nested hypervisors (``GuestHypervisor``) build like any VM here for their own
    L0 disk; their *inner* VM disks are warmed afterward on the same L0 backend
    (:func:`build_nested_inner_vms`, ADR-0021 — "build on L0"), so the later inner
    run is a pure cache hit and needs no nested build boot.
    """
    # Refuse depth-2+ nesting before any backend work (ADR-0021): build recursion
    # is depth-agnostic, but a depth-2 inner run can't be reached, so fail loud
    # rather than build three disk sets and time out opaquely later.
    reject_unsupported_nesting(ctx.plan.hypervisor)
    ctx.store.set_phase(PHASE_BUILD)

    misses, hits = _probe_all(ctx)
    ctx.built_disk_paths.update(hits)
    if not misses:
        _log.info("build: full cache hit; no backend resources needed")
        build_nested_inner_vms(ctx)
        return

    # At least one miss: stand up the ephemeral build infra (ADR-0010 §2/§9).
    build_pool_backend = _create_build_pool(ctx, misses)
    # The build switch is portable topology on the Hypervisor now (ADR-0016);
    # it is realized exactly like a run-phase switch.
    build_switch = resolve_build_switch(ctx.plan.hypervisor.build_switch)
    provision_switch(ctx, build_switch, kind_prefix="build_")
    build_net_backend = ctx.network_backends[build_switch.networks[0].name]
    materialize_sidecar_for(
        ctx,
        build_switch,
        kind_prefix="build_",
        pool_backend=build_pool_backend,
        pool_name=BUILD_POOL_NAME,
    )

    # Build the misses concurrently, driving the one shared, thread-safe driver
    # connection (ADR-0023): the base upload and the multi-GB capture download
    # are blocking I/O that overlaps across VMs. Each build VM has a distinct,
    # deterministic build IP (_build_ip_offset) so they coexist on the one build
    # switch stood up above.
    parallel_map(
        lambda bp: _guard_build(ctx, bp, build_pool_backend, build_net_backend),
        misses,
        jobs=ctx.jobs,
    )

    teardown_build_phase(ctx, build_switch, build_pool_backend)

    build_nested_inner_vms(ctx)


def build_nested_inner_vms(ctx: RunContext) -> None:
    """Warm each nested host's inner VM disks on the **L0** backend (ADR-0021 §3).

    For every ``GuestHypervisor`` in the plan, run the ordinary build phase over
    its inner plan against the *same* (outer/L0) driver and shared cache: the
    inner VMs build with real egress through the L0 build switch, and their disks
    land in the shared cache keyed under the inner plan namespace
    (``"<outer>.<host>"``). The inner run (``nested_phase``, ``require_cache=True``)
    then only uploads those cached disks into the guest's pool and boots.

    Single level only: a guest whose inner plan itself contains a
    ``GuestHypervisor`` is rejected up front by :func:`build_phase`
    (``reject_unsupported_nesting``), so this never recurses past depth 1.
    """
    guests = [vm for vm in ctx.plan.hypervisor.vms if isinstance(vm, GuestHypervisor)]
    for guest in guests:
        inner_plan = Plan(f"{ctx.plan_name}.{guest.name}", guest.inner)
        _log.info(
            "build: warming inner plan %r on L0 for nested host %r", inner_plan.name, guest.name
        )
        build_phase(_inner_build_ctx(ctx, inner_plan))


def _inner_build_ctx(ctx: RunContext, inner_plan: Plan) -> RunContext:
    """A RunContext for building ``inner_plan`` on the outer driver + shared cache.

    Shares the outer driver binding, state store, and cache, but gets a distinct
    run id deterministically derived from the outer run id + inner plan name, so
    its ``run_id``-composed backend resource names don't collide with the outer
    run's.
    """
    inner_run_id = hashlib.sha256(f"{ctx.run_id}/{inner_plan.name}".encode()).hexdigest()
    return RunContext(
        plan=inner_plan,
        resolved=ctx.resolved,
        store=ctx.store,
        cache=ctx.cache,
        run_id=inner_run_id,
        plan_name=inner_plan.name,
        build_timeout_s=ctx.build_timeout_s,
        lease_timeout_s=ctx.lease_timeout_s,
        sidecar_ready_timeout_s=ctx.sidecar_ready_timeout_s,
        agent_ready_timeout_s=ctx.agent_ready_timeout_s,
        addressing={
            n.name: NetworkAddressing.from_switch(s)
            for s in inner_plan.hypervisor.all_switches
            for n in s.networks
        },
        # Share the outer dashboard so a nested host's inner-VM builds report
        # their serial/stage into the same panes (ADR-0029).
        dashboard=ctx.dashboard,
    )


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
    # Every build boots on a sidecar-served switch (DHCP/DNS/NAT), so the
    # sidecar image is a build input for every VM (CI-1). Resolve its content
    # sha once — fetch=False keeps this to a metadata read (the bytes are only
    # needed if we actually build) — and fold it into each VM's config_hash.
    sidecar_sha = ctx.cache.resolve(CacheEntry(SIDECAR_CACHE_NAME), fetch=False).sha256
    # The build switch is portable topology on the Hypervisor (ADR-0016);
    # resolving it is pure, so the probe can synthesize each VM's build NIC
    # (whose MAC + static address now feed config_hash, ADR-0017) without
    # standing any backend resources up.
    build_switch = resolve_build_switch(ctx.plan.hypervisor.build_switch)
    misses: list[_VMBuildPlan] = []
    hits: dict[str, dict[str, Path]] = {}
    # Probe stays serial: VMs commonly share one base, so a serial walk fetches
    # it once into the local cache and the rest hit it — parallel probes would
    # redundantly fetch the same base and race its content-addressed staging.
    for vm_index, vm in enumerate(ctx.plan.hypervisor.vms):
        bp = _probe_vm(ctx, vm, vm_index, sidecar_sha, build_switch)
        if bp.cached_paths is None:
            _log.info("vm %s: cache miss on %s; will build", vm.name, bp.config_hash)
            misses.append(bp)
        else:
            _log.info("vm %s: cache hit on %s", vm.name, bp.config_hash)
            hits[vm.name] = bp.cached_paths
    return misses, hits


def _probe_vm(
    ctx: RunContext, vm: VMRecipe, vm_index: int, sidecar_sha: str, build_switch: Switch
) -> _VMBuildPlan:
    builder = vm.builder
    base = builder.os_disk_base()
    # OS-disk origin: image-based (a base CacheEntry to upload+grow) or
    # installer-based (no base; the builder supplies the boot medium and the
    # orchestrator materializes a blank OS disk — BUILD-1, ADR-0010 §6). Exactly
    # one origin sha feeds the cache key via ``base_sha``: the base image's, or
    # the install medium's (a different installer ISO must invalidate the cache
    # just as a different base would).
    base_path: Path | None = None
    boot_media_path: Path | None = None
    if base is not None:
        base_info = ctx.cache.resolve(base)
        assert base_info.path is not None  # cache.resolve(fetch=True) materializes locally
        base_path = base_info.path
        origin_sha = base_info.sha256
    else:
        boot_media = builder.boot_media()
        if boot_media is None:
            raise OrchestratorError(
                f"vm {vm.name!r}: builder {type(builder).__name__} provides neither an "
                "OS-disk base image (os_disk_base) nor a boot medium (boot_media)"
            )
        media_info = ctx.cache.resolve(boot_media)
        assert media_info.path is not None
        boot_media_path = media_info.path
        origin_sha = media_info.sha256
    macs = tuple(
        ctx.driver.compose_mac(ctx.plan_name, vm.name, i) for i in range(len(vm.spec.nics))
    )
    build_nic = _build_nic_for(ctx, build_switch, vm.name, vm_index)
    config_hash = builder.config_hash(
        vm.spec,
        vm,
        addressing=ctx.addressing,
        base_sha=origin_sha,
        sidecar_sha=sidecar_sha,
        macs=macs,
        build_nic=build_nic,
    )
    roles = built_artifact_roles(len(vm.spec.data_drives))
    cached = _resolve_full_set(ctx, config_hash, roles)
    return _VMBuildPlan(
        vm=vm,
        builder=builder,
        config_hash=config_hash,
        macs=macs,
        build_nic=build_nic,
        base_path=base_path,
        boot_media_path=boot_media_path,
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


def _guard_build(
    ctx: RunContext, bp: _VMBuildPlan, build_pool_backend: str, build_net_backend: str
) -> None:
    """Run one VM's build; tag it FAILED in the dashboard on error, then re-raise.

    ``parallel_map`` is fail-fast — this attributes the failure to the right VM
    (with the message) for the dashboard before the build phase unwinds.
    """
    try:
        build_one_vm(ctx, bp, build_pool_backend, build_net_backend)
    except Exception as e:
        ctx.dashboard.set_vm_stage(bp.vm.name, VMStage.FAILED, detail=str(e))
        raise


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

    Concurrent VM builds (ORCH-4) drive the one shared, thread-safe driver
    connection, so their base uploads and capture downloads overlap.
    ``ctx.store`` and ``ctx.built_disk_paths`` are guarded; the transfers are not.
    """
    drv = ctx.driver
    vm = bp.vm
    spec = vm.spec
    ctx.dashboard.set_vm_stage(vm.name, VMStage.BUILDING)
    build_vm_backend = drv.compose_resource_name(ctx.run_id, "build_vm", vm.name)

    # --- OS disk. Image-origin: push base bytes onto this VM's own ref, then
    # grow. Installer-origin (BUILD-1): materialize a blank disk of the declared
    # size — the installer partitions it, booting the install medium staged
    # below.
    os_disk_name = f"{build_vm_backend}{drv.volume_suffix('build_disk')}"
    os_disk_ref = drv.compose_volume_ref(build_pool_backend, os_disk_name)
    ctx.store.record_intent(
        kind="build_disk",
        backend_name=os_disk_name,
        plan_name=vm.name,
        pool_backend=build_pool_backend,
    )
    if bp.installer_origin:
        drv.create_blank_volume(os_disk_ref, spec.os_drive.size_gb)
    else:
        assert bp.base_path is not None  # image-origin always resolves a base path
        drv.upload_to_pool(os_disk_ref, bp.base_path)
        drv.resize_volume(os_disk_ref, spec.os_drive.size_gb)
    ctx.store.confirm(os_disk_name, pool_backend=build_pool_backend)

    # --- Boot medium (installer-origin only): stage the install ISO onto the
    # build pool and attach it as a bootable CDROM. Ephemeral like the seed —
    # deleted post-capture.
    boot_media_ref: VolumeRef | None = None
    boot_media_name: str | None = None
    if bp.boot_media_path is not None:
        # The builder may transform the resolved medium (e.g. bake the PVE
        # auto-installer activation file + first-boot script into the ISO).
        # Default identity; runs only here, on a build miss.
        prepared_media = bp.builder.prepare_boot_media(bp.boot_media_path)
        boot_media_name = f"{build_vm_backend}-bootmedia{drv.volume_suffix('boot_iso')}"
        boot_media_ref = drv.compose_volume_ref(build_pool_backend, boot_media_name)
        ctx.store.record_intent(
            kind="boot_iso",
            backend_name=boot_media_name,
            plan_name=vm.name,
            pool_backend=build_pool_backend,
        )
        drv.upload_to_pool(boot_media_ref, prepared_media)
        ctx.store.confirm(boot_media_name, pool_backend=build_pool_backend)

    # --- Data disks: blank, sized; the guest formats/populates them on the build boot.
    data_disk_refs: list[tuple[str, VolumeRef]] = []
    for i, hd in enumerate(spec.data_drives):
        name = f"{build_vm_backend}-{data_disk_role(i)}{drv.volume_suffix('data_disk')}"
        ref = drv.compose_volume_ref(build_pool_backend, name)
        ctx.store.record_intent(
            kind="data_disk",
            backend_name=name,
            plan_name=vm.name,
            pool_backend=build_pool_backend,
        )
        drv.create_blank_volume(ref, hd.size_gb)
        ctx.store.confirm(name, pool_backend=build_pool_backend)
        data_disk_refs.append((name, ref))

    # --- Seed ISO (optional: a builder that needs no seed medium returns None).
    seed_ref: VolumeRef | None = None
    seed_name: str | None = None
    seed_bytes = bp.builder.render_seed(
        spec, vm, addressing=ctx.addressing, macs=bp.macs, build_nic=bp.build_nic
    )
    if seed_bytes is not None:
        seed_name = f"{build_vm_backend}-seed{drv.volume_suffix('build_seed')}"
        seed_ref = drv.compose_volume_ref(build_pool_backend, seed_name)
        ctx.store.record_intent(
            kind="build_seed",
            backend_name=seed_name,
            plan_name=vm.name,
            pool_backend=build_pool_backend,
        )
        drv.write_to_pool(seed_ref, seed_bytes)
        ctx.store.confirm(seed_name, pool_backend=build_pool_backend)

    # --- Define + start the build VM with every writable disk attached.
    # ADR-0017: the build VM gets one dedicated build NIC on the build switch,
    # NOT its declared spec.nics — so a zero-NIC VM still builds with network,
    # and a static-NIC VM builds without its unroutable real address. network_refs
    # therefore carries only the build network.
    network_refs = {bp.build_nic.network: build_net_backend}
    ctx.store.record_intent(kind="build_vm", backend_name=build_vm_backend, plan_name=vm.name)
    drv.create_vm(
        build_vm_backend,
        spec,
        ctx.plan_name,
        os_disk_ref=os_disk_ref,
        seed_iso_ref=seed_ref,
        network_refs=network_refs,
        data_disk_refs=[ref for _, ref in data_disk_refs],
        build_nic=bp.build_nic,
        boot_media_ref=boot_media_ref,
    )
    ctx.store.confirm(build_vm_backend)
    drv.start_vm(build_vm_backend)
    # The guest reports an explicit result over the serial console; ``ok`` is
    # the *only* success signal. A ``fail`` record or a power-off without ``ok``
    # raises before capture, so a corrupt disk is never silently cached (ADR §21).
    wait_for_build_result(ctx, build_vm_backend, vm.name, driver=drv)
    # ``ok`` says it succeeded; the guest then runs ``poweroff``. Wait for the VM
    # to actually reach shutoff before reading its disk — otherwise capture races
    # qemu's final writes / file release on a live backend (torn read).
    wait_for_poweroff(ctx, build_vm_backend, vm.name, driver=drv)

    # --- Capture every writable disk into the cache (ADR-0010 §4/§5). Serial
    # within this VM (one worker connection); capture overlaps across VMs.
    refs_by_role: dict[str, VolumeRef] = {"os": os_disk_ref}
    for i, (_, ref) in enumerate(data_disk_refs):
        refs_by_role[data_disk_role(i)] = ref
    captured: dict[str, Path] = {}
    for role in bp.roles:
        captured[role] = _capture_disk(ctx, drv, refs_by_role[role], bp.config_hash, role, vm.name)
    with ctx.ledger_lock:
        ctx.built_disk_paths[vm.name] = captured

    # --- Delete everything on the backend (ADR-0010 §3). Best-effort: the disks
    # are already captured into the cache, so a single flaky backend delete must
    # not abort the remaining VMs in the build loop or skip teardown_build_phase
    # (which reclaims the build pool/switch/sidecar). A delete that fails leaves
    # its resource recorded in state.json, so the record-before-create ledger
    # (ADR-0003) still drives teardown/cleanup to retry it.
    _best_effort_delete(
        ctx, "build_vm", build_vm_backend, partial(drv.destroy_vm, build_vm_backend)
    )
    if seed_ref is not None and seed_name is not None:
        _best_effort_delete(ctx, "volume", seed_name, partial(drv.delete_volume, seed_ref))
    if boot_media_ref is not None and boot_media_name is not None:
        _best_effort_delete(
            ctx, "volume", boot_media_name, partial(drv.delete_volume, boot_media_ref)
        )
    for name, ref in data_disk_refs:
        _best_effort_delete(ctx, "volume", name, partial(drv.delete_volume, ref))
    _best_effort_delete(ctx, "volume", os_disk_name, partial(drv.delete_volume, os_disk_ref))


def _best_effort_delete(
    ctx: RunContext,
    kind: str,
    backend_name: str,
    delete: Callable[[], None],
) -> None:
    """Run one post-capture backend delete without letting a single failure
    abort the build.

    On success the resource is forgotten from ``state.json``; on failure it is
    logged and *left recorded*, so teardown/cleanup reverses it later. Never
    raises — the caller is mid-loop over multiple VMs and must reach
    ``teardown_build_phase`` regardless.
    """
    try:
        delete()
    except Exception as e:
        _log.warning(
            "post-capture delete of %s %s failed (left for teardown): %s",
            kind,
            backend_name,
            e,
        )
        return
    ctx.store.forget(backend_name)


def _capture_disk(
    ctx: RunContext,
    drv: HypervisorDriver,
    vol_ref: VolumeRef,
    config_hash: str,
    role: str,
    vm_name: str,
) -> Path:
    """Download one built disk and add it to the cache (+ HTTP tier when configured).

    The pool volume may not be readable by the orchestrator process (different
    uid, remote host), so it is streamed back via the worker's driver connection
    into a local temp file, then ingested. ``CacheManager.add`` mirrors to the
    HTTP tier best-effort when one is configured (ADR-0010 §5). Distinct VMs'
    captures carry distinct ``config_hash`` content shas, so concurrent
    ``cache.add`` calls never collide on a staging path.
    """
    cache_name = built_artifact_name(config_hash, role)
    # CORE-4: stage the download on the cache filesystem, not the system
    # tempdir — a multi-GiB OS disk ENOSPCs on a small tmpfs /tmp, and a
    # same-fs temp keeps the subsequent cache ingest a cheap intra-fs copy.
    with tempfile.NamedTemporaryFile(
        prefix=f"tr_built_{vm_name}_{role}_",
        suffix=drv.volume_suffix("build_disk"),
        dir=ctx.cache.staging,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        drv.download_from_pool(vol_ref, tmp_path)
        info = ctx.cache.add(tmp_path, name=cache_name)
    finally:
        tmp_path.unlink(missing_ok=True)
    assert info.path is not None  # manager.add returns local-flavored info
    _log.info("vm %s: cached built disk %s as %s (%s)", vm_name, role, config_hash, info.short_sha)
    return info.path


# Build-result protocol (backend-independent).
#
# The builder emits a framed record to the guest serial console; the driver's
# build-result sink streams those bytes back host-side and the orchestrator
# parses them here. The positive ``ok`` token is the *only* success signal —
# a guest that powers off without it crashed mid-provision (ADR §21). This
# parser is intentionally backend-independent: every backend's sink delivers
# the same serial bytes, only the transport differs.
#
#     TESTRANGE-RESULT: ok
#     # --- or ---
#     TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"
#     TESTRANGE-LOG-BEGIN
#     <base64 of the relevant log>
#     TESTRANGE-LOG-END

_RESULT_MARKER = b"TESTRANGE-RESULT:"
_LOG_BEGIN = b"TESTRANGE-LOG-BEGIN"
_LOG_END = b"TESTRANGE-LOG-END"
_RC_RE = re.compile(r"\brc=(\d+)")
_CMD_RE = re.compile(r'\bcmd="(.*)"')


@dataclass(frozen=True)
class BuildResult:
    """A parsed ``TESTRANGE-RESULT:`` record."""

    ok: bool
    rc: int | None = None
    cmd: str | None = None
    log: bytes = b""


def parse_build_result(data: bytes, *, final: bool = False) -> BuildResult | None:
    """Scan accumulated serial bytes for a complete build-result record.

    Returns the parsed :class:`BuildResult` once a *complete* record is
    present, else ``None`` so the caller keeps reading. The record can be
    interleaved with boot chatter — only the framing markers matter.

    ``final=True`` is the end-of-stream pass (the console closed): a ``fail``
    line whose log block never finished is still returned with whatever log
    was captured, rather than discarded, so a guest that died after announcing
    the failure still yields a diagnostic.

    The marker can appear more than once: boot chatter can print the literal
    ``TESTRANGE-RESULT:`` string before the real record (e.g. echoing the
    provisioning script). We therefore scan *every* occurrence rather than
    committing to the first — an earlier marker with a broken/unfinished frame
    or an unrecognized token must not mask a later, complete record (which would
    otherwise hang the build until the watchdog fires).
    """
    offsets: list[int] = []
    pos = 0
    while True:
        idx = data.find(_RESULT_MARKER, pos)
        if idx == -1:
            break
        offsets.append(idx)
        pos = idx + len(_RESULT_MARKER)
    if not offsets:
        return None

    for i, idx in enumerate(offsets):
        is_last = i == len(offsets) - 1
        # Bound each candidate record to the span up to the next marker so a
        # broken earlier frame can't swallow the real record's log block.
        segment = data[idx : (len(data) if is_last else offsets[i + 1])]
        rest = segment[len(_RESULT_MARKER) :]
        nl = rest.find(b"\n")
        if nl == -1:
            # The result line itself has not fully arrived. Only the last marker
            # can be mid-line (anything before it is followed by a later marker).
            if not final:
                continue  # nothing actionable here yet — keep reading
            line = rest
        else:
            line = rest[:nl]
        text = line.decode("utf-8", "replace").strip()
        tag = text.split(maxsplit=1)[0] if text else ""

        if tag == "ok":
            return BuildResult(ok=True)
        if tag == "fail":
            rc_m = _RC_RE.search(text)
            cmd_m = _CMD_RE.search(text)
            log, complete = _extract_log(segment)
            if complete or final:
                return BuildResult(
                    ok=False,
                    rc=int(rc_m.group(1)) if rc_m else None,
                    cmd=cmd_m.group(1) if cmd_m else None,
                    log=log,
                )
            if is_last:
                return None  # the real fail's framed log is still arriving — wait
            continue  # an earlier fail with a never-finished frame is chatter; skip
        # A complete marker line carrying an unrecognized token. Anything that is
        # not the explicit ``ok`` signal is a failure — but an earlier such line
        # may be boot chatter before the real record, so only the *last* marker
        # is treated as the verdict; earlier ones are skipped.
        if is_last:
            # Capture the raw line in the log so the failure is triageable rather
            # than a contextless "build failed" (REL-29).
            return BuildResult(
                ok=False,
                log=f"unrecognized build-result token; raw line: {text!r}".encode(),
            )
    return None


def _extract_log(segment: bytes) -> tuple[bytes, bool]:
    """Decode the framed ``TESTRANGE-LOG-BEGIN``/``END`` base64 block.

    Returns ``(decoded_bytes, complete)``. ``complete`` is ``False`` until
    both markers are present so the caller can wait for the rest.
    """
    begin = segment.find(_LOG_BEGIN)
    if begin == -1:
        return b"", False
    content_start = segment.find(b"\n", begin)
    if content_start == -1:
        return b"", False
    end = segment.find(_LOG_END, content_start)
    if end == -1:
        return b"", False
    raw = segment[content_start + 1 : end]
    try:
        return base64.b64decode(b"".join(raw.split())), True
    except (binascii.Error, ValueError):
        return raw.strip(), True  # not valid base64 — hand back the raw tail


class _ConsoleStreamer:
    """Mirrors a build VM's serial console to the log, one line at a time.

    Fed the growing serial buffer after each chunk; emits each newly-completed
    line to :data:`_console` at DEBUG so a build's provisioning is watchable
    live (``--log-level debug``). Skips the protocol's own framing — the
    ``TESTRANGE-RESULT:`` record line and the base64 ``TESTRANGE-LOG-BEGIN`` /
    ``…-END`` block — so the log shows build chatter, not the wire format (the
    failure log is surfaced *decoded* in :class:`BuildFailedError` regardless).

    Scans only the unseen tail of the buffer each call (no full-buffer copy),
    so feeding it once per chunk stays linear in total console output.
    """

    def __init__(self, vm_name: str) -> None:
        self._vm_name = vm_name
        self._pos = 0  # how far into the buffer we've split lines
        self._in_log_block = False

    def feed(self, buffer: bytearray) -> None:
        while True:
            nl = buffer.find(b"\n", self._pos)
            if nl == -1:
                return
            line = bytes(buffer[self._pos : nl])
            self._pos = nl + 1
            if _LOG_BEGIN in line:
                self._in_log_block = True
                continue
            if _LOG_END in line:
                self._in_log_block = False
                continue
            if self._in_log_block or _RESULT_MARKER in line:
                continue  # framing, not build output
            # Scrub guest terminal control bytes (colour/cursor escapes, embedded
            # \r, C0) so raw boot chatter can't hijack the operator's terminal or
            # garble the log (CORE-6).
            text = scrub_terminal_control(line.decode("utf-8", "replace"))
            if text:
                _console.debug("[%s] %s", self._vm_name, text)


def wait_for_build_result(
    ctx: RunContext, backend_name: str, vm_name: str, *, driver: HypervisorDriver | None = None
) -> None:
    """Live-tail the build VM's serial console until it reports a result.

    Replaces the old power-off-as-success poll. Opens the driver's
    build-result sink right after ``start_vm`` and reads until:

    - an ``ok`` record arrives -> return (the disk is safe to capture);
    - a ``fail`` record arrives -> raise :class:`BuildFailedError` with the
      failing command, rc, and decoded log (real-time fast-fail);
    - the console closes without ``ok`` -> raise :class:`BuildFailedError`
      ("powered off without a result" — the silent-corrupt-cache guard);
    - the build-timeout elapses -> raise :class:`BuildTimeoutError` (the
      watchdog, now only for a true wedge that never reports *and* never
      powers off).

    Console output is mirrored to the ``…build_phase.console`` logger at DEBUG
    as it streams (see :class:`_ConsoleStreamer`), so a build's provisioning is
    watchable live with ``--log-level debug``.

    **Hard dependency on the heartbeat contract.** The deadline is only checked
    between yields from the sink, so the timeout watchdog relies on the driver
    honoring the ``read_build_result_sink`` contract: an idle/blocked sink MUST
    yield ``b""`` at a bounded cadence (including while waiting for the guest's
    console to connect). A driver whose generator blocks ``__next__`` forever
    without heartbeating would hang here — that is a driver bug, not a missing
    guard (a true wall-clock interrupt would need a thread/signal, which ADR-0002
    rules out). The shipped libvirt and proxmox sinks both heartbeat.
    """
    drv = driver or ctx.driver
    deadline = time.monotonic() + ctx.build_timeout_s
    buffer = bytearray()
    streamer = _ConsoleStreamer(vm_name)
    # ``closing`` runs the generator's ``finally`` (releasing the driver's
    # transport) even when we break out early on a record.
    with closing(drv.read_build_result_sink(backend_name)) as stream:
        for chunk in stream:
            # Process the chunk *before* the deadline check: a chunk carrying the
            # result must never be discarded just because it landed at the timeout
            # boundary. The watchdog fires only with no result in hand (a chunk
            # that parsed to nothing, or a b"" heartbeat).
            if chunk:
                buffer.extend(chunk)
                streamer.feed(buffer)  # mirror console lines to the log live
                result = parse_build_result(bytes(buffer))
                if result is not None:
                    _raise_or_return(result, vm_name)
                    return
            if time.monotonic() >= deadline:
                raise BuildTimeoutError(
                    f"vm {vm_name!r} produced no build result within {ctx.build_timeout_s:.0f}s"
                )
    # Stream closed: parse one last time, leniently. No ``ok`` => failure.
    final = parse_build_result(bytes(buffer), final=True)
    if final is not None and final.ok:
        return
    raise BuildFailedError(
        vm_name,
        rc=final.rc if final else None,
        cmd=final.cmd if final else None,
        log=final.log if final else bytes(buffer[-4096:]),
        detail=None if final else "powered off without reporting a build result",
    )


def wait_for_poweroff(
    ctx: RunContext, backend_name: str, vm_name: str, *, driver: HypervisorDriver | None = None
) -> None:
    """Block until the build VM is actually ``shutoff`` (safe to capture).

    The serial ``ok`` token means provisioning *succeeded*; it is emitted just
    before the guest's ``poweroff``, so at that instant the VM is still running
    and the backend still holds its disk image open. Capturing then would SFTP a
    torn read out from under a live qemu (and miss any final shutdown writes).
    This is the short, bounded wait for the guest's own ``poweroff`` to land —
    distinct from :func:`wait_for_build_result`, which decides success/failure.
    A guest that reports ``ok`` but never powers off is a wedge; the build
    timeout catches it.
    """
    drv = driver or ctx.driver
    deadline = time.monotonic() + ctx.build_timeout_s
    while time.monotonic() < deadline:
        if drv.get_vm_power_state(backend_name) == "shutoff":
            return
        time.sleep(2.0)
    raise BuildTimeoutError(
        f"vm {vm_name!r} reported build ok but did not power off within {ctx.build_timeout_s:.0f}s"
    )


def _raise_or_return(result: BuildResult, vm_name: str) -> None:
    """Return on success; raise :class:`BuildFailedError` on a ``fail`` record."""
    if result.ok:
        _log.info("vm %s: build reported success", vm_name)
        return
    raise BuildFailedError(vm_name, rc=result.rc, cmd=result.cmd, log=result.log)


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
    "BuildResult",
    "build_one_vm",
    "build_phase",
    "parse_build_result",
    "probe_misses",
    "teardown_build_phase",
    "wait_for_build_result",
    "wait_for_poweroff",
]
