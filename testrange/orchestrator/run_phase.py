"""Run phase: materialize user switches + VMs, bind communicators, wait ready.

Provisions each user Switch, creates each run VM from its cached post-install
disk, then binds each VM's communicator and drives the builder readiness check.
Communicator dispatch is the sanctioned trust boundary between the user's Plan
and the orchestrator.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.communicators.native import NativeCommunicator
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.devices.network import DHCPAddr, StaticAddr
from testrange.devices.pool.base import StoragePool
from testrange.exceptions import (
    BuildNotReadyError,
    CommunicatorError,
    GuestAgentError,
    OrchestratorError,
)
from testrange.networks._addressing_consts import SIDECAR_CRED
from testrange.networks.base import Switch
from testrange.networks.sidecar import LEASEFILE, SIDECAR_DNSMASQ_CONF, parse_dnsmasq_leases
from testrange.orchestrator._parallel import parallel_map
from testrange.orchestrator.artifacts import data_disk_role
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.dashboard_state import VMStage
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.state.schema import PHASE_RUN
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)


def _guard_vm(ctx: RunContext, vm: VMRecipe, step: Callable[[RunContext, VMRecipe], None]) -> None:
    """Run a per-VM bring-up ``step``; tag the VM FAILED in the dashboard on error.

    ``parallel_map`` is fail-fast and re-raises one worker's exception; marking
    the VM here gives the dashboard the actual culprit (with the message) before
    the run unwinds. The exception still propagates, so teardown runs unchanged.
    """
    try:
        step(ctx, vm)
    except Exception as e:
        ctx.dashboard.set_vm_stage(vm.name, VMStage.FAILED, detail=str(e))
        raise


def run_phase(ctx: RunContext) -> None:
    ctx.store.set_phase(PHASE_RUN)
    hyp = ctx.plan.hypervisor

    # The independent units of each step run on a bounded thread pool, driving
    # the one shared, thread-safe driver connection concurrently (ADR-0023): the
    # disk uploads are blocking I/O that overlaps for real. Each step is a
    # barrier — pools exist before switches read pool_backends, switches+sidecars
    # exist before VMs wire to network_backends, and every sidecar is serving
    # before any user VM boots (ADR-0010 §8). ``ctx.store`` and the ledger dicts
    # are guarded; the slow backend calls run unlocked.
    #
    # The build phase tears down its own ephemeral pool and no longer leaves the
    # user's declared pools behind (ADR-0010 §9), so the run phase owns creating
    # them before any disk is pushed or sidecar materialized.
    parallel_map(lambda pool: _create_pool(ctx, pool), hyp.pools, jobs=ctx.jobs)
    parallel_map(
        lambda switch: _provision_switch_with_sidecar(ctx, switch), hyp.networks, jobs=ctx.jobs
    )
    wait_sidecars_ready(ctx)
    parallel_map(lambda vm: _guard_vm(ctx, vm, _bring_up_vm), hyp.vms, jobs=ctx.jobs)


def _create_pool(ctx: RunContext, pool: StoragePool) -> None:
    backend = ctx.driver.compose_resource_name(ctx.run_id, "pool", pool.name)
    ctx.store.record_intent(kind="pool", backend_name=backend, plan_name=pool.name)
    ctx.driver.create_pool(pool, backend)
    ctx.store.confirm(backend)
    with ctx.ledger_lock:
        ctx.pool_backends[pool.name] = backend


def _provision_switch_with_sidecar(ctx: RunContext, switch: Switch) -> None:
    # One switch's fabric + its sidecar are a single unit: the sidecar reads this
    # switch's own network_backends, so they must be ordered within the switch.
    # Different switches are independent.
    provision_switch(ctx, switch)
    materialize_sidecar_for(ctx, switch)


def _bring_up_vm(ctx: RunContext, vm: VMRecipe) -> None:
    ctx.dashboard.set_vm_stage(vm.name, VMStage.PROVISIONING)
    drv = ctx.driver
    pool_backend = ctx.pool_backends[vm.spec.os_drive.pool]
    built = ctx.built_disk_paths[vm.name]  # {role: cached path}
    vm_backend = drv.compose_resource_name(ctx.run_id, "vm", vm.name)

    # OS disk: push the cached built OS bytes straight onto this VM's own
    # ref — no shared base, no clone (ADR-0010 §3). The captured disk is
    # already full-size, so run VMs need neither a seed nor a resize (§6).
    os_disk_name = f"{vm_backend}{drv.volume_suffix('run_disk')}"
    os_disk_ref = drv.compose_volume_ref(pool_backend, os_disk_name)
    ctx.store.record_intent(
        kind="run_disk",
        backend_name=os_disk_name,
        plan_name=vm.name,
        pool_backend=pool_backend,
    )
    drv.upload_to_pool(os_disk_ref, built["os"])
    ctx.store.confirm(os_disk_name, pool_backend=pool_backend)

    # Data disks: push each cached built data disk onto its own ref.
    data_disk_refs = []
    for i, _hd in enumerate(vm.spec.data_drives):
        role = data_disk_role(i)
        name = f"{vm_backend}-{role}{drv.volume_suffix('data_disk')}"
        ref = drv.compose_volume_ref(pool_backend, name)
        ctx.store.record_intent(
            kind="data_disk",
            backend_name=name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        drv.upload_to_pool(ref, built[role])
        ctx.store.confirm(name, pool_backend=pool_backend)
        data_disk_refs.append(ref)

    ctx.store.record_intent(kind="vm", backend_name=vm_backend, plan_name=vm.name)
    drv.create_vm(
        vm_backend,
        vm.spec,
        ctx.plan_name,
        os_disk_ref=os_disk_ref,
        seed_iso_ref=None,
        network_refs=ctx.network_backends,
        data_disk_refs=data_disk_refs,
    )
    ctx.store.confirm(vm_backend)
    drv.start_vm(vm_backend)
    ctx.dashboard.set_vm_stage(vm.name, VMStage.BOOTING)


def bind_communicators(ctx: RunContext) -> None:
    """Bind each VM's communicator at run-phase bring-up.

    Each Communicator declares its own ``bind`` signature; the orchestrator
    dispatches by communicator type and hands each one the inputs it needs.
    Transport-specific state (IPs, callables) lives on the bound
    communicator, not on VMHandle. The ``isinstance`` ladder is the
    sanctioned trust boundary between the user's Plan and dispatch.
    """
    # Independent per VM; the slow part is the SSH path's DHCP-lease poll in
    # discover_ip, overlapped across VMs (ADR-0023). Each VM binds its own
    # communicator object — no shared mutable state — and native lease reads are
    # serialized at the driver's call lock.
    parallel_map(
        lambda vm: _guard_vm(ctx, vm, _bind_one_communicator),
        ctx.plan.hypervisor.vms,
        jobs=ctx.jobs,
    )


def _bind_one_communicator(ctx: RunContext, vm: VMRecipe) -> None:
    ctx.dashboard.set_vm_stage(vm.name, VMStage.BINDING)
    comm = vm.communicator
    if isinstance(comm, SSHCommunicator):
        ip = discover_ip(ctx, vm, comm.nic_idx)
        cred = lookup_credential(vm)
        # The driver decides whether guests are directly routable (gateway None)
        # or only reachable through an off-box gateway (a remote hypervisor); the
        # orchestrator brokers it onto the SSH transport (ADR-0021).
        gateway = ctx.driver.guest_gateway()
        comm.bind(host=ip, credential=cred, gateway=gateway)
        _log.info(
            "vm %s: bound SSHCommunicator at %s%s",
            vm.name,
            ip,
            " via gateway" if gateway is not None else "",
        )
    elif isinstance(comm, NativeCommunicator):
        backend = ctx.driver.compose_resource_name(ctx.run_id, "vm", vm.name)
        # Per-call guest login for credential-requiring native channels (VMware
        # Tools / Hyper-V); QGA ignores it (CORE-60, ADR-0008). Resolved from the
        # VM's builder, or None when the builder bakes no credential.
        native_cred = native_guest_credential(vm)
        comm.bind(
            execute=ctx.driver.native_guest_execute(backend, credential=native_cred),
            read_file=ctx.driver.native_guest_read_file(backend, credential=native_cred),
            write_file=ctx.driver.native_guest_write_file(backend, credential=native_cred),
        )
        _log.info("vm %s: bound NativeCommunicator via %s", vm.name, backend)
    else:
        _log.debug(
            "vm %s: communicator %s not bindable; skipping",
            vm.name,
            type(comm).__name__,
        )


def wait_sidecars_ready(ctx: RunContext) -> None:
    """Block until every materialized sidecar is serving (ADR-0010 §8).

    A sidecar is **ready** when its native guest agent answers *and* the
    orchestrator can read back the dnsmasq config it delivered — proof the
    sidecar has booted, the agent is up, and the config has been applied, so
    DHCP/DNS/NAT is live before the first user VM starts. Sidecars are driven
    only through the driver's native guest channel; the orchestrator never
    routes IP traffic to one. A sidecar whose agent never answers fails loud.
    """
    parallel_map(
        lambda item: _wait_one_sidecar_ready(ctx, item[0], item[1]),
        list(ctx.sidecar_backends.items()),
        jobs=ctx.jobs,
    )


def _wait_one_sidecar_ready(ctx: RunContext, switch_name: str, sidecar_backend: str) -> None:
    read_file = ctx.driver.native_guest_read_file(sidecar_backend, credential=SIDECAR_CRED)
    deadline = time.monotonic() + ctx.sidecar_ready_timeout_s
    last_err: GuestAgentError | None = None
    while time.monotonic() < deadline:
        try:
            data = read_file(SIDECAR_DNSMASQ_CONF)
        except GuestAgentError as e:
            last_err = e  # agent not up yet
        else:
            if data:  # agent answered and the delivered config is present
                _log.info("sidecar for switch %s ready", switch_name)
                return
        time.sleep(2.0)
    detail = f": {last_err}" if last_err is not None else ""
    raise OrchestratorError(
        f"sidecar for switch {switch_name!r} not ready within "
        f"{ctx.sidecar_ready_timeout_s:.0f}s (native guest agent unreachable or "
        f"config not applied){detail}"
    )


def wait_communicators_ready(ctx: RunContext) -> None:
    """Wait until each user VM's bound communicator can execute a command.

    At run-phase boot the native guest agent (or SSH) comes up a few seconds
    *after* the VM powers on. The first real exec — the builder's readiness
    probe (``await_guest_readiness``) — must not race that, or it hits e.g. PVE's
    ``QEMU guest agent is not running``. This is the user-VM analogue of
    :func:`wait_sidecars_ready`: poll a trivial exec until the communicator
    answers, then hand off. A VM whose communicator never answers fails loud.
    """
    parallel_map(
        lambda vm: _guard_vm(ctx, vm, _wait_one_communicator_ready),
        ctx.plan.hypervisor.vms,
        jobs=ctx.jobs,
    )


def _wait_one_communicator_ready(ctx: RunContext, vm: VMRecipe) -> None:
    deadline = time.monotonic() + ctx.agent_ready_timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            vm.communicator.execute(("true",), timeout=10.0)
        except (GuestAgentError, CommunicatorError) as e:
            last_err = e  # agent / SSH not up yet
        else:
            _log.info("communicator for vm %s ready", vm.name)
            return
        time.sleep(2.0)
    detail = f": {last_err}" if last_err is not None else ""
    raise OrchestratorError(
        f"vm {vm.name!r} communicator not ready within "
        f"{ctx.agent_ready_timeout_s:.0f}s (native guest agent or SSH unreachable){detail}"
    )


def await_guest_readiness(ctx: RunContext) -> None:
    """Drive each builder's readiness check via the bound communicator.

    Run-phase gate (not the build phase): once a guest is up and its
    communicator is bound, block until it passes its builder's readiness check.

    The builder runs its own readiness command through the injected
    ``execute`` callable (``vm.communicator.execute`` — whatever the
    communicator is) and raises :class:`BuildNotReadyError` itself.
    Builders never see a Communicator type; the orchestrator only
    brokers the callable and tags failures with the VM name.
    """
    parallel_map(
        lambda vm: _guard_vm(ctx, vm, _await_one_guest_readiness),
        ctx.plan.hypervisor.vms,
        jobs=ctx.jobs,
    )


def _await_one_guest_readiness(ctx: RunContext, vm: VMRecipe) -> None:
    try:
        vm.builder.wait_ready(vm.spec, vm, vm.communicator.execute)
    except BuildNotReadyError as e:
        raise BuildNotReadyError(f"vm {vm.name!r}: {e}") from e
    # Final gate cleared: the guest is up and answering its readiness probe.
    ctx.dashboard.set_vm_stage(vm.name, VMStage.READY)


def discover_ip(ctx: RunContext, vm: VMRecipe, nic_idx: int | None = None) -> str:
    """Resolve the IPv4 address the orchestrator should SSH to.

    ``nic_idx`` (from ``SSHCommunicator(nic_idx=)``) selects the NIC by its
    position in the VM's device list — the same index the MAC and staged
    netplan already key on, and the only thing that disambiguates multiple
    NICs on one network. When ``None``, the first *addressed* NIC is used
    (NICs with ``addr=None`` are unconfigured and skipped).

    :class:`StaticAddr`: return the declared host address directly — the
    staged run-phase netplan applies it on the first run-phase boot.

    :class:`DHCPAddr`: the per-Switch sidecar — not the hypervisor — serves
    DHCP, so the lease lives in the sidecar's dnsmasq lease file. Poll that file
    over the native guest agent for the lease keyed on the stable MAC derived
    from
    ``(plan_name, vm_name, nic_idx)`` until ``lease_timeout_s`` elapses. The
    orchestrator brokers: it combines the driver's guest-file read transport
    with the sidecar's lease-file path and parser. The sidecar-readiness gate
    (:func:`wait_sidecars_ready`) has already confirmed the agent is up, so the
    ``GuestAgentError`` catch below is now only a guard against a transient
    blip while the lease itself is being written — not the agent-up race.

    Raises :class:`OrchestratorError` when the chosen NIC has no address (or no
    NIC does), when ``nic_idx`` is out of range, or on DHCP-lease timeout.
    """
    nics = vm.spec.nics
    if nic_idx is not None:
        if not 0 <= nic_idx < len(nics):
            raise OrchestratorError(
                f"vm {vm.name!r}: nic_idx={nic_idx} out of range (VM has {len(nics)} NIC(s))"
            )
        nic = nics[nic_idx]
        if nic.addr is None:
            raise OrchestratorError(f"vm {vm.name!r}: nic_idx={nic_idx} has no address to SSH to")
    else:
        addressed = [(i, n) for i, n in enumerate(nics) if n.addr is not None]
        if not addressed:
            raise OrchestratorError(
                f"vm {vm.name!r}: no NIC has an address (all unconfigured); "
                f"cannot resolve an address"
            )
        nic_idx, nic = addressed[0]
    if isinstance(nic.addr, StaticAddr):
        return nic.addr.host
    return _wait_for_dhcp_lease(ctx, vm.name, nic.network, nic_idx)


def _wait_for_dhcp_lease(ctx: RunContext, vm_name: str, network: str, nic_idx: int) -> str:
    """Poll the per-Switch sidecar's dnsmasq lease file until the NIC's MAC leases.

    The lease lives in the sidecar (not the hypervisor), keyed on the stable MAC
    derived from ``(plan_name, vm_name, nic_idx)``. The orchestrator brokers the
    driver's guest-file read of the sidecar against the dnsmasq lease path +
    parser. Raises :class:`OrchestratorError` on a sidecar-less switch or timeout.
    """
    switch = _switch_for_network(ctx, network)
    sidecar_backend = ctx.sidecar_backends.get(switch.name)
    if sidecar_backend is None:
        raise OrchestratorError(
            f"vm {vm_name!r}: DHCP NIC on {network!r} but switch "
            f"{switch.name!r} has no sidecar lease file to poll"
        )
    mac = ctx.driver.compose_mac(ctx.plan_name, vm_name, nic_idx).lower()
    read_leasefile = ctx.driver.native_guest_read_file(sidecar_backend, credential=SIDECAR_CRED)
    deadline = time.monotonic() + ctx.lease_timeout_s
    while time.monotonic() < deadline:
        try:
            raw = read_leasefile(LEASEFILE)
        except GuestAgentError:
            raw = b""  # sidecar agent not up yet, or no lease file written yet
        ip = parse_dnsmasq_leases(raw.decode("utf-8", "replace")).get(mac)
        if ip:
            return ip
        time.sleep(2.0)
    raise OrchestratorError(
        f"vm {vm_name!r} did not acquire a DHCP lease on "
        f"{network!r} within {ctx.lease_timeout_s:.0f}s"
    )


def wait_dhcp_leases(ctx: RunContext) -> None:
    """Gate the run phase until every DHCP NIC on every VM has leased.

    The SSH bind path already blocks on its target NIC's lease (``discover_ip``),
    but a :class:`NativeCommunicator` VM binds the instant its guest agent
    answers — seconds *before* the guest's DHCP client finishes — while a static
    NIC on the same guest comes up at boot. Without this gate a plan can read
    ``ip addr`` and see the static address but no lease yet (REL-24). Waiting here
    closes that race for every communicator type. Backend-agnostic: it polls the
    per-switch sidecar dnsmasq lease file, never the guest.
    """

    def _wait_one(vm: VMRecipe) -> None:
        for idx, nic in enumerate(vm.spec.nics):
            if isinstance(nic.addr, DHCPAddr):
                _wait_for_dhcp_lease(ctx, vm.name, nic.network, idx)

    parallel_map(_wait_one, ctx.plan.hypervisor.vms, jobs=ctx.jobs)


def _switch_for_network(ctx: RunContext, network_name: str) -> Switch:
    """The Switch that owns ``network_name`` in the plan."""
    switches: tuple[Switch, ...] = ctx.plan.hypervisor.all_switches
    for sw in switches:
        if any(n.name == network_name for n in sw.networks):
            return sw
    raise OrchestratorError(f"network {network_name!r} is not owned by any switch")


def lookup_credential(vm: VMRecipe) -> PosixCred:
    """The PosixCred an SSHCommunicator authenticates with, from the builder's
    baked credentials. Builder-agnostic: every Builder exposes the same
    credentials contract via :meth:`Builder.find_credential`, so the run phase
    resolves an SSH login without knowing the builder type (CloudInit, Proxmox,
    ESXi). The installer-origin builders need this — they reach the run phase the
    same way cloud-init does."""
    builder = vm.builder
    if not isinstance(vm.communicator, SSHCommunicator):
        raise OrchestratorError(f"vm {vm.name!r}: communicator is not SSHCommunicator")
    cred = builder.find_credential(vm.communicator.username)
    if cred is None:
        usernames = [c.username for c in builder.credentials]
        raise OrchestratorError(
            f"vm {vm.name!r}: SSHCommunicator({vm.communicator.username!r}) "
            f"has no matching credential in builder.credentials; "
            f"declared: {usernames}"
        )
    if not isinstance(cred, PosixCred):
        raise OrchestratorError(
            f"vm {vm.name!r}: credential for {vm.communicator.username!r} "
            f"is not a PosixCred (got {type(cred).__name__})"
        )
    return cred


def native_guest_credential(vm: VMRecipe) -> Credential | None:
    """The guest OS login a native-agent channel authenticates with, or ``None``.

    QGA-style agents (libvirt/Proxmox) authenticate at the channel and need none
    — ``None`` is correct and the driver ignores it. VMware Tools / Hyper-V
    guest-ops require a per-call guest credential (CORE-60); the orchestrator
    sources it from the VM's builder: the admin credential if one is marked, else
    the sole declared credential, else ``None`` (a credential-requiring backend
    then fails loud rather than guessing). A non-``CloudInitBuilder`` carries no
    credentials here, so it resolves to ``None``.
    """
    builder = vm.builder
    if not isinstance(builder, CloudInitBuilder):
        return None
    creds = builder.credentials
    admins = [c for c in creds if c.admin]
    if admins:
        return admins[0]
    return creds[0] if len(creds) == 1 else None


__all__ = [
    "await_guest_readiness",
    "bind_communicators",
    "discover_ip",
    "lookup_credential",
    "native_guest_credential",
    "run_phase",
    "wait_communicators_ready",
    "wait_dhcp_leases",
    "wait_sidecars_ready",
]
