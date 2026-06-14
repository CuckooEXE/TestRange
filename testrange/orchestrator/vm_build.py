"""Per-VM disk-set build machinery (the *materialize* half of a VM node).

For one VM this resolves its OS-disk origin, computes ``config_hash``, probes
the cache for the full per-role artifact set (OS disk + each data disk), and —
on a miss — provisions the VM as a unit on the shared ephemeral build infra,
boots it to completion, and captures every writable disk into the cache. The
backend is left empty afterward: build VMs and their disks are deleted
immediately after capture (ADR-0010 §3).

The ephemeral build pool / switch / sidecar are shared by every building VM
(ADR-0010 §2/§9) and live in ``ctx.build_infra``: the first cache-missing VM
node creates them (:func:`ensure_build_infra`, lock-guarded), the executor
tears them down after the materialize walk (:func:`teardown_build_infra`).
The wave orchestration itself lives in ``orchestrator/executor.py`` (DAG-6);
this module is the per-VM mechanics it dispatches.
"""

from __future__ import annotations

import base64
import binascii
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
from testrange.builders.base import Builder, NativeAgentProvision
from testrange.cache.entry import CacheEntry
from testrange.communicators import NativeCommunicator
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
from testrange.orchestrator.artifacts import (
    built_artifact_name,
    built_artifact_roles,
    data_disk_role,
)
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.dashboard_state import VMStage
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)

# Build VMs' serial console output is streamed here, one line per record, as it
# arrives — run with ``--verbose`` to watch a build provision live in the tail.
# A dedicated child logger so it routes independently of the module's own INFO
# progress lines: it is pinned above the operator log level (CORE-50) so a plain
# ``--log-level debug`` run does not dump this raw-guest-output firehose through
# the stderr handler; only the ``--verbose`` live tail lowers it to DEBUG.
_console = get_logger(f"{__name__}.console")

# The single ephemeral build pool's plan-level name (ADR-0010 §9). Cosmetic —
# the backend name is composed via the driver; this only labels the synthesized
# StoragePool and the sidecar's OSDrive.
BUILD_POOL_NAME = "__build"


@dataclass
class VMBuildProbe:
    """A VM's resolved build inputs, plus its cache-probe outcome.

    Produced once per VM node by the executor's serial key walk
    (``VMNode.cache_key`` -> :func:`probe_vm`) and stored in
    ``ctx.vm_probes``, so the cache key and the seed render see identical
    inputs on every path that consumes them.
    """

    vm: VMRecipe
    builder: Builder
    config_hash: str
    macs: tuple[str, ...]
    build_nic: BuildNic
    # CORE-90: the backend's native-agent install recipe, resolved once at probe
    # time so config_hash (the cache key) and render_seed see the identical value.
    # None unless the VM uses a NativeCommunicator.
    native_agent: NativeAgentProvision | None
    # Whether this VM's OS disk comes from an installer boot medium (blank OS
    # disk the installer partitions) instead of an image base (upload+grow).
    installer_origin: bool
    # Image-origin: the local path of the OS base to upload+grow. Installer-
    # origin: None (the install medium is ``boot_media_path`` instead). Only
    # populated when the probe materialized what it resolved (paths_resolved).
    base_path: Path | None
    boot_media_path: Path | None
    roles: tuple[str, ...]
    # role -> cached path on a full hit; None when any role misses (whole-VM
    # miss). A metadata-only probe (paths_resolved=False) reports a full hit as
    # an EMPTY dict — presence is known, local paths are not.
    cached_paths: dict[str, Path] | None
    # False for a metadata-only probe (ctx.probe_fetch=False, the read-only
    # inspection path): origin shas are resolved but nothing is materialized,
    # so this probe can answer hit/miss questions and never feed a build.
    paths_resolved: bool = True

    def __post_init__(self) -> None:
        # Exactly one OS-disk origin: an image base (upload+grow) XOR an installer
        # boot medium (blank disk the installer partitions). A plan with *both*
        # set would silently drop the boot medium and one with *neither* would
        # yield a blank, unbootable disk. probe_vm constructs these mutually
        # exclusive; this backstops a future edit (or a test) that doesn't.
        # Path presence is only checkable when the probe materialized them.
        if self.paths_resolved:
            if self.installer_origin and self.boot_media_path is None:
                raise OrchestratorError(
                    f"vm {self.vm.name!r}: installer-origin VMBuildProbe has no boot medium"
                )
            if not self.installer_origin and self.base_path is None:
                raise OrchestratorError(
                    f"vm {self.vm.name!r}: image-origin VMBuildProbe has no base path"
                )
        if self.base_path is not None and self.boot_media_path is not None:
            raise OrchestratorError(
                f"vm {self.vm.name!r}: VMBuildProbe needs exactly one OS-disk origin "
                f"(base_path xor boot_media_path); got both"
            )


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


def build_nic_for(ctx: GraphContext, build_switch: Switch, vm: VMRecipe, vm_index: int) -> BuildNic:
    """Synthesize the dedicated build NIC for one VM (ADR-0017).

    One transient NIC on the build switch, statically addressed. The host offset
    is :func:`_build_ip_offset` of the VM's plan position, so concurrent build
    VMs (ORCH-4) get distinct, deterministic addresses. When the build switch is
    ``nat`` the address derives its gateway/DNS from the sidecar at ``.1``, so
    the build boot egresses for ``apt``/``pip``.

    MAC selection (ESXI-18): an image-origin build gets the reserved-slot MAC
    (:data:`BUILD_NIC_NIC_IDX`), disjoint from the VM's declared NICs. An
    INSTALLER-origin build instead wears the MAC of the VM's first declared NIC:
    an installed OS can pin its first-boot identity to the install-time MAC and
    keep it (ESXi creates ``vmk0`` with it and restores it from ``esx.conf`` on
    every later boot), while the run phase recreates the VM with the declared
    NICs and polls the sidecar lease file for the *declared idx-0* MAC — so the
    install must happen under the identity the node wakes up with. Safe because
    a build VM carries ONLY the build NIC (the declared NICs are replaced, not
    joined), and the build/run switches are distinct L2s. Falls back to the
    reserved slot when the spec declares no NICs (nothing will poll a lease).
    """
    network = build_switch.networks[0]
    build_ip = str(build_switch.network.network_address + _build_ip_offset(vm_index))
    installer_origin = vm.builder.os_disk_base() is None
    mac_idx = 0 if installer_origin and vm.spec.nics else BUILD_NIC_NIC_IDX
    return BuildNic(
        mac=ctx.driver.compose_mac(ctx.plan_name, vm.name, mac_idx),
        network=network.name,
        addr=StaticAddr(build_ip),
        addressing=NetworkAddressing.from_switch(build_switch),
    )


def resolve_sidecar_sha(ctx: GraphContext) -> str:
    """The build sidecar image's content sha — a build input for every VM (CI-1).

    ``fetch=False`` keeps this to a metadata read (the bytes are only needed if
    a VM actually builds).
    """
    return ctx.cache.resolve(CacheEntry(SIDECAR_CACHE_NAME), fetch=False).sha256


def probe_vm(
    ctx: GraphContext, vm: VMRecipe, vm_index: int, sidecar_sha: str, build_switch: Switch
) -> VMBuildProbe:
    """Resolve one VM's build inputs and probe the cache for its disk set.

    Read-only against the backend (the only I/O is cache resolution, which may
    fetch a base on a cold local cache — the deliberate ADR-0010 §2 penalty;
    ``ctx.probe_fetch=False`` suppresses even that, for the inspection path).
    Called from the executor's *serial* key walk: VMs commonly share one base,
    so a serial walk fetches it once and the rest hit it.
    """
    builder = vm.builder
    base = builder.os_disk_base()
    fetch = ctx.probe_fetch
    # OS-disk origin: image-based (a base CacheEntry to upload+grow) or
    # installer-based (no base; the builder supplies the boot medium and the
    # orchestrator materializes a blank OS disk — BUILD-1, ADR-0010 §6). Exactly
    # one origin sha feeds the cache key via ``base_sha``: the base image's, or
    # the install medium's (a different installer ISO must invalidate the cache
    # just as a different base would).
    base_path: Path | None = None
    boot_media_path: Path | None = None
    if base is not None:
        base_info = ctx.cache.resolve(base, fetch=fetch)
        if fetch:
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
        media_info = ctx.cache.resolve(boot_media, fetch=fetch)
        if fetch:
            assert media_info.path is not None
            boot_media_path = media_info.path
        origin_sha = media_info.sha256
    macs = tuple(
        ctx.driver.compose_mac(ctx.plan_name, vm.name, i) for i in range(len(vm.spec.nics))
    )
    build_nic = build_nic_for(ctx, build_switch, vm, vm_index)
    # CORE-90: a NativeCommunicator VM needs the backend's native agent in the
    # guest. The orchestrator — the only component that knows both the driver
    # (agent identity) and the communicator (agent wanted) — brokers the driver's
    # provision recipe into the builder; an SSH-only VM gets None and nothing is
    # injected. Resolved here so config_hash and render_seed use the same value.
    native_agent = (
        ctx.driver.native_agent_provision()
        if isinstance(vm.communicator, NativeCommunicator)
        else None
    )
    config_hash = builder.config_hash(
        vm.spec,
        vm,
        addressing=ctx.addressing,
        base_sha=origin_sha,
        sidecar_sha=sidecar_sha,
        macs=macs,
        build_nic=build_nic,
        native_agent=native_agent,
    )
    roles = built_artifact_roles(len(vm.spec.data_drives))
    cached = resolve_full_set(ctx, config_hash, roles, fetch=fetch)
    return VMBuildProbe(
        vm=vm,
        builder=builder,
        config_hash=config_hash,
        macs=macs,
        build_nic=build_nic,
        native_agent=native_agent,
        installer_origin=base is None,
        base_path=base_path,
        boot_media_path=boot_media_path,
        roles=roles,
        cached_paths=cached,
        paths_resolved=fetch,
    )


def resolve_full_set(
    ctx: GraphContext, config_hash: str, roles: tuple[str, ...], *, fetch: bool = True
) -> dict[str, Path] | None:
    """Resolve every role's cached artifact. All present -> dict; any absent -> None.

    A partial set (OS present, a data disk missing) is a miss for the whole
    VM (ADR-0010 §4). The manager checks local then HTTP; a hit on the HTTP
    tier fetches into local before returning. Under ``fetch=False`` (the
    read-only inspection path) presence is checked from metadata alone and a
    full hit is reported as an EMPTY dict — non-``None`` means present, and no
    multi-GB artifact moves.
    """
    paths: dict[str, Path] = {}
    for role in roles:
        name = built_artifact_name(config_hash, role)
        try:
            info = ctx.cache.resolve(name, fetch=fetch)
        except CacheMissError:
            return None
        except CacheError as e:
            # HTTP tier reachable but non-404 (e.g. 5xx). Local is source of
            # truth; treat as a miss for resilience but log loud enough to notice.
            _log.warning("cache lookup error on %s (%s); treating as build miss", name, e)
            return None
        if fetch:
            assert info.path is not None  # fetch=True guarantees a local path
            paths[role] = info.path
    return paths


def ensure_build_infra(ctx: GraphContext) -> tuple[str, str]:
    """Create the shared ephemeral build infra on first use; return its refs.

    Returns ``(build_pool_backend, build_net_backend)``. The first
    cache-missing VM node creates the pool + switch + sidecar under
    ``ctx.build_infra.lock`` (VM nodes materialize concurrently); subsequent
    callers get the existing refs. The pool is sized to the largest missing
    VM's disk set — the misses are known up front from the executor's key
    walk (``ctx.vm_probes``). Torn down by :func:`teardown_build_infra` after
    the materialize walk.
    """
    infra = ctx.build_infra
    with infra.lock:
        if infra.active:
            assert infra.pool_backend is not None and infra.net_backend is not None
            return infra.pool_backend, infra.net_backend
        misses = [p for p in ctx.vm_probes.values() if p.cached_paths is None]
        if not misses:
            raise OrchestratorError(
                "ensure_build_infra called with no cache-missing VM in the probe "
                "ledger; a cache-hit VM never needs the build infra"
            )
        pool_backend = _create_build_pool(ctx, misses)
        build_switch = resolve_build_switch(ctx.plan.hypervisor.build_switch)
        provision_switch(ctx, build_switch, kind_prefix="build_")
        net_backend = ctx.network_backends[build_switch.networks[0].name]
        materialize_sidecar_for(
            ctx,
            build_switch,
            kind_prefix="build_",
            pool_backend=pool_backend,
            pool_name=BUILD_POOL_NAME,
        )
        infra.pool_backend = pool_backend
        infra.net_backend = net_backend
        infra.build_switch = build_switch
        infra.active = True
        return pool_backend, net_backend


def teardown_build_infra(ctx: GraphContext) -> None:
    """Destroy the build sidecar VM, networks, switch, and pool (LIFO).

    No-op unless :func:`ensure_build_infra` ran. Called by the executor at the
    end of the materialize walk — the infra never survives the build
    (ADR-0010 §3).
    """
    infra = ctx.build_infra
    with infra.lock:
        if not infra.active:
            return
        build_switch = infra.build_switch
        pool_backend = infra.pool_backend
        assert build_switch is not None and pool_backend is not None
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
        # The build pool comes up only for the build and never survives it.
        ctx.driver.destroy_pool(pool_backend)
        ctx.store.forget(pool_backend)
        infra.active = False
        infra.pool_backend = None
        infra.net_backend = None
        infra.build_switch = None


def _create_build_pool(ctx: GraphContext, misses: list[VMBuildProbe]) -> str:
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
    ctx: GraphContext,
    bp: VMBuildProbe,
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
    if not bp.paths_resolved:
        raise OrchestratorError(
            f"vm {vm.name!r}: cannot build from a metadata-only probe "
            "(ctx.probe_fetch=False is the read-only inspection path)"
        )
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
        spec,
        vm,
        addressing=ctx.addressing,
        macs=bp.macs,
        build_nic=bp.build_nic,
        native_agent=bp.native_agent,
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
    # not abort the remaining VMs in the materialize wave or skip the infra
    # teardown (which reclaims the build pool/switch/sidecar). A delete that
    # fails leaves its resource recorded in state.json, so the
    # record-before-create ledger (ADR-0003) still drives teardown/cleanup to
    # retry it.
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
    ctx: GraphContext,
    kind: str,
    backend_name: str,
    delete: Callable[[], None],
) -> None:
    """Run one post-capture backend delete without letting a single failure
    abort the build.

    On success the resource is forgotten from ``state.json``; on failure it is
    logged and *left recorded*, so teardown/cleanup reverses it later. Never
    raises — the caller is mid-walk over multiple VMs and the infra teardown
    must still be reached.
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
    ctx: GraphContext,
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


# The RFC 4648 base64 alphabet (no padding) — used to salvage a log block that
# shared the guest's serial console with kernel/boot chatter.
_B64_ALPHABET = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")

# How much raw serial tail to surface when a build dies without any framed
# result at all — a bounded window so a runaway console can't flood the error.
_FALLBACK_TAIL_BYTES = 4096


def _decode_b64_tolerant(raw: bytes) -> bytes:
    """Base64-decode a serial-carried log block, tolerating interleaved noise.

    The failure log rides the guest's *shared* serial console: kernel/boot
    chatter can land mid-block and ``poweroff -f`` can sever it mid-quantum. A
    strict ``b64decode`` raises on either, and the old fallback then dumped the
    raw base64 to the operator's console — the blob this fixes (BUILD-23). Keep
    only base64-alphabet bytes (dropping CR/LF wrapping, ``=`` padding, and any
    interleaved log lines), re-pad to a 4-byte boundary, and decode, so the
    operator always gets readable log text rather than a base64 blob. A block
    with interleaved chatter decodes lossily (garbage past the intrusion), but
    the common case — clean tail, only line-wrapping/truncation — is exact.
    """
    compact = bytes(b for b in raw if b in _B64_ALPHABET)
    rem = len(compact) % 4
    if rem == 1:
        compact = compact[:-1]  # a lone trailing char can't form a quantum
    elif rem:
        compact += b"=" * (4 - rem)
    if not compact:
        return b""
    try:
        return base64.b64decode(compact)
    except (binascii.Error, ValueError):  # pragma: no cover - alphabet+repad keeps this unreachable
        return b""


def _extract_log(segment: bytes) -> tuple[bytes, bool]:
    """Decode the framed ``TESTRANGE-LOG-BEGIN``/``END`` base64 block.

    Returns ``(decoded_bytes, complete)``. ``complete`` is ``False`` until both
    markers are present so the caller can wait for the rest. The decode is
    tolerant (:func:`_decode_b64_tolerant`) so a block corrupted by chatter on
    the shared serial yields readable text, never a raw base64 blob (BUILD-23).
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
    return _decode_b64_tolerant(segment[content_start + 1 : end]), True


def _fallback_log(buffer: bytes) -> bytes:
    """Best-effort readable log when the guest died without a framed result.

    If a ``TESTRANGE-LOG-BEGIN`` block reached the wire — even without its
    closing ``…-END`` (a ``poweroff -f`` mid-block) — decode it so the operator
    sees log text, not a base64 blob (BUILD-23). Otherwise hand back a bounded
    raw tail of the console.
    """
    begin = buffer.find(_LOG_BEGIN)
    if begin != -1:
        content_start = buffer.find(b"\n", begin)
        if content_start != -1:
            end = buffer.find(_LOG_END, content_start)
            decoded = _decode_b64_tolerant(
                buffer[content_start + 1 : end if end != -1 else len(buffer)]
            )
            if decoded:
                return decoded
    return buffer[-_FALLBACK_TAIL_BYTES:]


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
    ctx: GraphContext, backend_name: str, vm_name: str, *, driver: HypervisorDriver | None = None
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

    Console output is mirrored to the ``…vm_build.console`` logger at DEBUG
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
        log=final.log if final else _fallback_log(bytes(buffer)),
        detail=None if final else "powered off without reporting a build result",
    )


def wait_for_poweroff(
    ctx: GraphContext, backend_name: str, vm_name: str, *, driver: HypervisorDriver | None = None
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


__all__ = [
    "BUILD_POOL_NAME",
    "BuildResult",
    "VMBuildProbe",
    "build_nic_for",
    "build_one_vm",
    "ensure_build_infra",
    "parse_build_result",
    "probe_vm",
    "resolve_full_set",
    "resolve_sidecar_sha",
    "teardown_build_infra",
    "wait_for_build_result",
    "wait_for_poweroff",
]
