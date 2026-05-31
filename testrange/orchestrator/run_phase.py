"""Run phase: materialize user switches + VMs, bind communicators, wait ready.

Provisions each user Switch, creates each run VM from its cached post-install
disk, then binds each VM's communicator and drives the builder readiness check.
Communicator dispatch is the sanctioned trust boundary between the user's Plan
and the orchestrator.
"""

from __future__ import annotations

import time

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.communicators.native import NativeCommunicator
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.posix import PosixCred
from testrange.devices.network import StaticAddr
from testrange.exceptions import (
    BuildNotReadyError,
    CommunicatorError,
    GuestAgentError,
    OrchestratorError,
)
from testrange.networks.base import Switch
from testrange.networks.sidecar import LEASEFILE, SIDECAR_DNSMASQ_CONF, parse_dnsmasq_leases
from testrange.orchestrator.artifacts import data_disk_role
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.state.schema import PHASE_RUN
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)


def run_phase(ctx: RunContext) -> None:
    ctx.store.set_phase(PHASE_RUN)
    hyp = ctx.plan.hypervisor

    # The build phase tears down its own ephemeral pool and no longer leaves the
    # user's declared pools behind (ADR-0010 §9), so the run phase owns creating
    # them before any disk is pushed or sidecar materialized.
    for pool in hyp.pools:
        backend = ctx.driver.compose_resource_name(ctx.run_id, "pool", pool.name)
        ctx.store.record_intent(kind="pool", backend_name=backend, plan_name=pool.name)
        ctx.driver.create_pool(pool, backend)
        ctx.store.confirm(backend)
        ctx.pool_backends[pool.name] = backend

    for switch in hyp.networks:
        provision_switch(ctx, switch)
        materialize_sidecar_for(ctx, switch)

    # Block until every sidecar is serving before any user VM boots, so a guest
    # can never ask for a DHCP lease the sidecar isn't ready to hand out yet
    # (ADR-0010 §8).
    wait_sidecars_ready(ctx)

    for vm in hyp.vms:
        pool_backend = ctx.pool_backends[vm.spec.os_drive.pool]
        built = ctx.built_disk_paths[vm.name]  # {role: cached path}
        vm_backend = ctx.driver.compose_resource_name(ctx.run_id, "vm", vm.name)

        # OS disk: push the cached built OS bytes straight onto this VM's own
        # ref — no shared base, no clone (ADR-0010 §3). The captured disk is
        # already full-size, so run VMs need neither a seed nor a resize (§6).
        os_disk_name = f"{vm_backend}{ctx.driver.volume_suffix('run_disk')}"
        os_disk_ref = ctx.driver.compose_volume_ref(pool_backend, os_disk_name)
        ctx.store.record_intent(
            kind="run_disk",
            backend_name=os_disk_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        ctx.driver.upload_to_pool(os_disk_ref, built["os"])
        ctx.store.confirm(os_disk_name, pool_backend=pool_backend)

        # Data disks: push each cached built data disk onto its own ref.
        data_disk_refs = []
        for i, _hd in enumerate(vm.spec.data_drives):
            role = data_disk_role(i)
            name = f"{vm_backend}-{role}{ctx.driver.volume_suffix('data_disk')}"
            ref = ctx.driver.compose_volume_ref(pool_backend, name)
            ctx.store.record_intent(
                kind="data_disk",
                backend_name=name,
                plan_name=vm.name,
                pool_backend=pool_backend,
            )
            ctx.driver.upload_to_pool(ref, built[role])
            ctx.store.confirm(name, pool_backend=pool_backend)
            data_disk_refs.append(ref)

        ctx.store.record_intent(kind="vm", backend_name=vm_backend, plan_name=vm.name)
        ctx.driver.create_vm(
            vm_backend,
            vm.spec,
            ctx.plan_name,
            os_disk_ref=os_disk_ref,
            seed_iso_ref=None,
            network_refs=ctx.network_backends,
            data_disk_refs=data_disk_refs,
        )
        ctx.store.confirm(vm_backend)
        ctx.driver.start_vm(vm_backend)


def bind_communicators(ctx: RunContext) -> None:
    """Bind each VM's communicator at run-phase bring-up.

    Each Communicator declares its own ``bind`` signature; the orchestrator
    dispatches by communicator type and hands each one the inputs it needs.
    Transport-specific state (IPs, callables) lives on the bound
    communicator, not on VMHandle. The ``isinstance`` ladder is the
    sanctioned trust boundary between the user's Plan and dispatch.
    """
    for vm in ctx.plan.hypervisor.vms:
        comm = vm.communicator
        if isinstance(comm, SSHCommunicator):
            ip = discover_ip(ctx, vm, comm.nic_idx)
            cred = lookup_credential(vm)
            # The driver decides whether guests are directly routable (gateway
            # None) or only reachable through an off-box gateway (a remote
            # hypervisor); the orchestrator brokers it onto the SSH transport.
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
            comm.bind(
                execute=ctx.driver.native_guest_execute(backend),
                read_file=ctx.driver.native_guest_read_file(backend),
                write_file=ctx.driver.native_guest_write_file(backend),
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
    for switch_name, sidecar_backend in ctx.sidecar_backends.items():
        _wait_one_sidecar_ready(ctx, switch_name, sidecar_backend)


def _wait_one_sidecar_ready(ctx: RunContext, switch_name: str, sidecar_backend: str) -> None:
    read_file = ctx.driver.native_guest_read_file(sidecar_backend)
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
    for vm in ctx.plan.hypervisor.vms:
        _wait_one_communicator_ready(ctx, vm)


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
    for vm in ctx.plan.hypervisor.vms:
        try:
            vm.builder.wait_ready(vm.spec, vm, vm.communicator.execute)
        except BuildNotReadyError as e:
            raise BuildNotReadyError(f"vm {vm.name!r}: {e}") from e


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

    switch = _switch_for_network(ctx, nic.network)
    sidecar_backend = ctx.sidecar_backends.get(switch.name)
    if sidecar_backend is None:
        raise OrchestratorError(
            f"vm {vm.name!r}: DHCP NIC on {nic.network!r} but switch "
            f"{switch.name!r} has no sidecar lease file to poll"
        )
    mac = ctx.driver.compose_mac(ctx.plan_name, vm.name, nic_idx).lower()
    read_leasefile = ctx.driver.native_guest_read_file(sidecar_backend)
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
        f"vm {vm.name!r} did not acquire a DHCP lease on "
        f"{nic.network!r} within {ctx.lease_timeout_s:.0f}s"
    )


def _switch_for_network(ctx: RunContext, network_name: str) -> Switch:
    """The Switch that owns ``network_name`` in the plan."""
    switches: tuple[Switch, ...] = ctx.plan.hypervisor.all_switches
    for sw in switches:
        if any(n.name == network_name for n in sw.networks):
            return sw
    raise OrchestratorError(f"network {network_name!r} is not owned by any switch")


def lookup_credential(vm: VMRecipe) -> PosixCred:
    builder = vm.builder
    if not isinstance(builder, CloudInitBuilder):
        raise OrchestratorError(f"vm {vm.name!r}: only CloudInitBuilder is supported in v0")
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


__all__ = [
    "await_guest_readiness",
    "bind_communicators",
    "discover_ip",
    "lookup_credential",
    "run_phase",
    "wait_communicators_ready",
    "wait_sidecars_ready",
]
