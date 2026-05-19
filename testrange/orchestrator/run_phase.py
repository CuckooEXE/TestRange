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
from testrange.communicators.qga import QGACommunicator
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.posix import PosixCred
from testrange.exceptions import BuildNotReadyError, OrchestratorError
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.provision import (
    ensure_base_in_pool,
    materialize_sidecar_for,
    provision_switch,
)
from testrange.state.schema import PHASE_RUN
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)


def run_phase(ctx: RunContext) -> None:
    ctx.store.set_phase(PHASE_RUN)
    hyp = ctx.plan.hypervisor

    for switch in hyp.networks:
        provision_switch(ctx, switch)
        materialize_sidecar_for(ctx, switch)

    for vm in hyp.vms:
        pool_backend = ctx.pool_backends[vm.spec.os_drive.pool]
        run_disk_name = f"{vm.name}{ctx.driver.volume_suffix('run_disk')}"
        run_disk_ref = ctx.driver.compose_volume_ref(pool_backend, run_disk_name)
        ctx.store.record_intent(
            kind="run_disk",
            backend_name=run_disk_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        base_ref = ensure_base_in_pool(
            ctx, pool_backend, ctx.post_install_paths[vm.name]
        )
        ctx.driver.create_disk_from_base(run_disk_ref, base_ref)
        ctx.store.confirm(run_disk_name, pool_backend=pool_backend)

        vm_backend = ctx.driver.compose_resource_name(ctx.run_id, "vm", vm.name)
        ctx.store.record_intent(
            kind="vm",
            backend_name=vm_backend,
            plan_name=vm.name,
        )
        ctx.driver.create_vm(
            vm_backend,
            vm.spec,
            ctx.plan_name,
            os_disk_ref=run_disk_ref,
            seed_iso_ref=None,
            network_refs=ctx.network_backends,
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
            ip = discover_ip(ctx, vm)
            cred = lookup_credential(vm)
            comm.bind(host=ip, credential=cred)
            _log.info("vm %s: bound SSHCommunicator at %s", vm.name, ip)
        elif isinstance(comm, QGACommunicator):
            backend = ctx.driver.compose_resource_name(ctx.run_id, "vm", vm.name)
            comm.bind(
                execute=ctx.driver.native_guest_execute(backend),
                read_file=ctx.driver.native_guest_read_file(backend),
                write_file=ctx.driver.native_guest_write_file(backend),
            )
            _log.info("vm %s: bound QGACommunicator via %s", vm.name, backend)
        else:
            _log.debug(
                "vm %s: communicator %s not bindable; skipping",
                vm.name,
                type(comm).__name__,
            )


def wait_builder_ready(ctx: RunContext) -> None:
    """Drive each builder's readiness check via the bound communicator.

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


def discover_ip(ctx: RunContext, vm: VMRecipe) -> str:
    """Resolve the IPv4 address of the VM's **first** declared NIC.

    Static (``nic.ipv4 is not None``): return the declared address
    directly — the staged run-phase netplan applies it on the first
    run-phase boot.

    DHCP: poll the driver for the lease keyed on the stable MAC derived
    from ``(plan_name, vm_name, nic_idx=0)`` until ``lease_timeout_s``
    elapses. Raises :class:`OrchestratorError` on timeout.
    """
    if not vm.spec.nics:
        raise OrchestratorError(f"vm {vm.name!r}: no NICs; cannot resolve an address")
    first_nic = vm.spec.nics[0]
    if first_nic.ipv4 is not None:
        return first_nic.ipv4
    net_backend = ctx.network_backends[first_nic.network]
    mac = ctx.driver.compose_mac(ctx.plan_name, vm.name, 0)
    deadline = time.monotonic() + ctx.lease_timeout_s
    while time.monotonic() < deadline:
        ip = ctx.driver.get_lease_ip(net_backend, mac)
        if ip:
            return ip
        time.sleep(2.0)
    raise OrchestratorError(
        f"vm {vm.name!r} did not acquire a DHCP lease on "
        f"{first_nic.network!r} within {ctx.lease_timeout_s:.0f}s"
    )


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
    "bind_communicators",
    "discover_ip",
    "lookup_credential",
    "run_phase",
    "wait_builder_ready",
]
