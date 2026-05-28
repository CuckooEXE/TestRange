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
import re
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.entry import CacheEntry
from testrange.devices.pool.base import StoragePool
from testrange.drivers.base import VolumeRef
from testrange.exceptions import (
    BuildFailedError,
    BuildTimeoutError,
    CacheError,
    CacheMissError,
    OrchestratorError,
)
from testrange.networks._addressing_consts import SIDECAR_CACHE_NAME
from testrange.networks.base import Switch
from testrange.networks.sidecar import _uplink_network_name
from testrange.orchestrator.artifacts import (
    built_artifact_name,
    built_artifact_roles,
    data_disk_role,
)
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.state.schema import PHASE_BUILD
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)

# Build VMs' serial console output is streamed here, one line per record, as it
# arrives — run with ``--log-level debug`` to watch a build provision live. A
# dedicated child logger so it can be silenced/routed independently of the
# phase's own INFO progress lines.
_console = get_logger(f"{__name__}.console")

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
    # managed_egress instructs the driver to manufacture + fence the egress
    # segment for a ManagedBuildSwitch (ADR-0014); None for a plain/no build switch.
    build_switch, managed_egress = resolve_build_switch(
        getattr(ctx.plan.hypervisor, "build_switch", None)
    )
    provision_switch(ctx, build_switch, kind_prefix="build_", managed_egress=managed_egress)
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
    # Every build boots on a sidecar-served switch (DHCP/DNS/NAT), so the
    # sidecar image is a build input for every VM (CI-1). Resolve its content
    # sha once — fetch=False keeps this to a metadata read (the bytes are only
    # needed if we actually build) — and fold it into each VM's config_hash.
    sidecar_sha = ctx.cache.resolve(CacheEntry(SIDECAR_CACHE_NAME), fetch=False).sha256
    misses: list[_VMBuildPlan] = []
    hits: dict[str, dict[str, Path]] = {}
    for vm in ctx.plan.hypervisor.vms:
        bp = _probe_vm(ctx, vm, sidecar_sha)
        if bp.cached_paths is None:
            _log.info("vm %s: cache miss on %s; will build", vm.name, bp.config_hash)
            misses.append(bp)
        else:
            _log.info("vm %s: cache hit on %s", vm.name, bp.config_hash)
            hits[vm.name] = bp.cached_paths
    return misses, hits


def _probe_vm(ctx: RunContext, vm: VMRecipe, sidecar_sha: str) -> _VMBuildPlan:
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
        sidecar_sha=sidecar_sha,
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
    # The guest reports an explicit result over the serial console; ``ok`` is
    # the *only* success signal. A ``fail`` record or a power-off without ``ok``
    # raises before capture, so a corrupt disk is never silently cached (ADR §21).
    wait_for_build_result(ctx, build_vm_backend, vm.name)
    # ``ok`` says it succeeded; the guest then runs ``poweroff``. Wait for the VM
    # to actually reach shutoff before reading its disk — otherwise capture races
    # qemu's final writes / file release on a live backend (torn read).
    wait_for_poweroff(ctx, build_vm_backend, vm.name)

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
    # CORE-4: stage the download on the cache filesystem, not the system
    # tempdir — a multi-GiB OS disk ENOSPCs on a small tmpfs /tmp, and a
    # same-fs temp keeps the subsequent cache ingest a cheap intra-fs copy.
    with tempfile.NamedTemporaryFile(
        prefix=f"tr_built_{vm_name}_{role}_",
        suffix=ctx.driver.volume_suffix("build_disk"),
        dir=ctx.cache.staging,
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
    """
    idx = data.find(_RESULT_MARKER)
    if idx == -1:
        return None
    rest = data[idx + len(_RESULT_MARKER) :]
    nl = rest.find(b"\n")
    if nl == -1:
        if not final:
            return None  # the result line itself is not fully arrived yet
        line = rest
    else:
        line = rest[:nl]
    text = line.decode("utf-8", "replace").strip()

    if text.startswith("ok"):
        return BuildResult(ok=True)
    if text.startswith("fail"):
        rc_m = _RC_RE.search(text)
        cmd_m = _CMD_RE.search(text)
        log, complete = _extract_log(data[idx:])
        if not complete and not final:
            return None  # wait for the framed log to finish arriving
        return BuildResult(
            ok=False,
            rc=int(rc_m.group(1)) if rc_m else None,
            cmd=cmd_m.group(1) if cmd_m else None,
            log=log,
        )
    # A complete marker line carrying an unrecognized token: treat as failure
    # rather than hang (we have a whole line: nl != -1, or this is the final
    # pass). Anything that isn't the explicit ``ok`` token is not success.
    return BuildResult(ok=False)


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
            text = line.decode("utf-8", "replace").rstrip("\r")
            if text:
                _console.debug("[%s] %s", self._vm_name, text)


def wait_for_build_result(ctx: RunContext, backend_name: str, vm_name: str) -> None:
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
    """
    deadline = time.monotonic() + ctx.build_timeout_s
    buffer = bytearray()
    streamer = _ConsoleStreamer(vm_name)
    # ``closing`` runs the generator's ``finally`` (releasing the driver's
    # transport) even when we break out early on a record.
    with closing(ctx.driver.read_build_result_sink(backend_name)) as stream:
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


def wait_for_poweroff(ctx: RunContext, backend_name: str, vm_name: str) -> None:
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
    deadline = time.monotonic() + ctx.build_timeout_s
    while time.monotonic() < deadline:
        if ctx.driver.get_vm_power_state(backend_name) == "shutoff":
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
