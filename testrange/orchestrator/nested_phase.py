"""Nested phase: recurse the orchestrator into each guest hypervisor (ORCH-20).

After the outer (L0) run phase has brought every VM up, bound its communicator,
and waited for readiness, this phase finds the ``GuestHypervisor`` entries and,
for each, brings up its inner (L1) plan *against the running guest* (ADR-0021):

1. confirm the guest is reachable over SSH and its inner topology is libvirt;
2. wait for libvirtd to answer inside the guest;
3. write the guest admin's baked private key to a temp keyfile and synthesize an
   in-process ``LibvirtProfile`` for ``qemu+ssh`` to the guest's address;
4. enter a full inner :class:`~testrange.orchestrator.runtime.Orchestrator` with
   ``require_cache=True`` — the inner VM disks were already built on L0 (BUILD-14),
   so the inner run is upload-cached-disk-and-boot.

The inner orchestrators are held open for the duration of the outer run (so outer
test code can poke inner VMs through ``orch.nested``) and torn down LIFO before
the outer teardown destroys the guest (:func:`teardown_nested`).

This module is generic, but the *inner backend is libvirt-only* in v1: it imports
the libvirt inner-binding helpers directly and rejects any other inner topology,
mirroring the run phase's "only CloudInitBuilder in v0" narrowing.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.posix import PosixCred
from testrange.drivers.libvirt._nested import inner_libvirt_profile, wait_libvirtd_ready
from testrange.exceptions import OrchestratorError
from testrange.plan import Plan
from testrange.vms.handle import VMHandle
from testrange.vms.nested import GuestHypervisor, reject_unsupported_nesting

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from testrange.drivers.base import HypervisorDriver
    from testrange.orchestrator.context import RunContext
    from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle
    from testrange.utils.sshkey import SSHKey

_log = get_logger(__name__)


@dataclass(frozen=True)
class NestedHandle:
    """Test-code-facing view of one brought-up nested hypervisor.

    ``host`` is the L1 guest itself (a VM on the outer backend); ``inner`` is the
    inner run's :class:`OrchestratorHandle`. The ``vms`` / ``driver`` / ``run_id``
    shortcuts forward to the inner handle so test code reads
    ``orch.nested["host-a"].vms["webapp"]`` and
    ``orch.nested["host-a"].driver`` naturally.
    """

    host: VMHandle
    inner: OrchestratorHandle

    @property
    def vms(self) -> Mapping[str, VMHandle]:
        return self.inner.vms

    @property
    def driver(self) -> HypervisorDriver:
        return self.inner.driver

    @property
    def run_id(self) -> str:
        return self.inner.run_id


@dataclass
class NestedRun:
    """Bookkeeping for one entered inner orchestrator (internal to the phase)."""

    orchestrator: Orchestrator
    handle: NestedHandle
    keyfile: Path


def run_nested_phase(ctx: RunContext) -> tuple[list[NestedRun], dict[str, NestedHandle]]:
    """Bring up every ``GuestHypervisor``'s inner plan; return runs + handle map.

    Enters each inner orchestrator in turn. On any failure the already-entered
    inner orchestrators are torn down (LIFO) and the error propagates, so the
    outer ``__enter__`` never returns with half-built nested state.
    """
    reject_unsupported_nesting(ctx.plan.hypervisor)
    guests = [vm for vm in ctx.plan.hypervisor.vms if isinstance(vm, GuestHypervisor)]
    runs: list[NestedRun] = []
    handles: dict[str, NestedHandle] = {}
    try:
        for guest in guests:
            run = _bring_up_one(ctx, guest)
            runs.append(run)
            handles[guest.name] = run.handle
            _log.info("nested host %r: inner plan up (run_id=%s)", guest.name, run.handle.run_id)
    except Exception:
        teardown_nested(runs)
        raise
    return runs, handles


def _bring_up_one(ctx: RunContext, guest: GuestHypervisor) -> NestedRun:
    # Lazy import breaks the runtime <-> nested_phase cycle (runtime imports this
    # module at load time; this needs Orchestrator only when actually recursing).
    from testrange.orchestrator.runtime import Orchestrator

    comm = guest.communicator
    if not isinstance(comm, SSHCommunicator):
        raise OrchestratorError(
            f"nested host {guest.name!r} needs an SSHCommunicator for the inner "
            f"qemu+ssh binding; got {type(comm).__name__}"
        )
    # (The inner LibvirtHypervisor requirement is enforced at GuestHypervisor
    # construction; see GuestHypervisor.__post_init__.)
    host = comm.host
    if not host:
        raise OrchestratorError(
            f"nested host {guest.name!r}: communicator has no resolved address "
            f"(run phase should have bound it)"
        )
    if comm.gateway is not None:
        # The inner qemu+ssh binding dials comm.host directly from the
        # orchestrator host (no jump). If the L0 guest itself was bound *via* a
        # gateway — a remote L0 whose guests aren't directly routable — that dial
        # can't reach it and would hang opaquely. Fail loud here: nested virt
        # currently requires a directly SSH-reachable L1 guest (local libvirt L0;
        # remote-L0 nesting is deferred, BACKEND-5/BACKEND-11).
        raise OrchestratorError(
            f"nested host {guest.name!r}: the L0 guest is bound via a gateway "
            f"({type(comm.gateway).__name__}), so the inner qemu+ssh binding cannot "
            f"route to it directly; nested virtualization requires a directly "
            f"SSH-reachable L1 guest (local libvirt L0)"
        )
    key = _admin_ssh_key(guest, comm.username)

    # Gate on libvirtd answering inside the guest before we dial qemu+ssh.
    wait_libvirtd_ready(comm.execute, timeout=ctx.agent_ready_timeout_s)

    keyfile = _write_keyfile(key.priv)
    try:
        # Inherit the outer profile's uplink map: the inner plan was built on L0
        # against these same logical names, and its cache-only inner run must
        # accept the inner build switch's uplink at preflight even though that
        # switch is never realized on L1 (no build happens). NET-17 will refine
        # runtime egress to a guest-provisioned bridge.
        profile = inner_libvirt_profile(
            host, comm.username, keyfile=str(keyfile), uplinks=ctx.resolved.uplinks
        )
        inner_plan = Plan(f"{ctx.plan_name}.{guest.name}", guest.inner)
        inner = Orchestrator(
            inner_plan,
            cache_manager=ctx.cache,
            require_cache=True,
            profile=profile,
        )
        inner_handle = inner.__enter__()
    except Exception:
        keyfile.unlink(missing_ok=True)
        raise

    host_handle = VMHandle(
        name=guest.name,
        backend_name=ctx.driver.compose_resource_name(ctx.run_id, "vm", guest.name),
        communicator=comm,
    )
    return NestedRun(
        orchestrator=inner,
        handle=NestedHandle(host=host_handle, inner=inner_handle),
        keyfile=keyfile,
    )


def _admin_ssh_key(guest: GuestHypervisor, username: str) -> SSHKey:
    """The baked SSH key the inner ``qemu+ssh`` binding authenticates with.

    It must be the same key the guest's ``CloudInitBuilder`` put into
    ``authorized_keys`` for ``username``, so the admin credential must be a
    key-bearing :class:`PosixCred`.
    """
    builder = guest.builder
    if not isinstance(builder, CloudInitBuilder):
        raise OrchestratorError(
            f"nested host {guest.name!r}: only CloudInitBuilder is supported "
            f"(got {type(builder).__name__})"
        )
    cred = builder.find_credential(username)
    if cred is None:
        raise OrchestratorError(
            f"nested host {guest.name!r}: builder bakes no credential for admin user "
            f"{username!r} (the inner qemu+ssh binding authenticates as this user)"
        )
    if not isinstance(cred, PosixCred) or cred.ssh_key is None:
        raise OrchestratorError(
            f"nested host {guest.name!r}: admin credential {username!r} must be a "
            f"PosixCred carrying an ssh_key (qemu+ssh authenticates with the baked key)"
        )
    return cred.ssh_key


def _write_keyfile(private_key: str) -> Path:
    """Write a private key to a ``0600`` temp file for the qemu+ssh transport.

    libvirt's ``qemu+ssh`` shells out to ssh and needs a key *file* (paramiko
    takes an in-memory key, but the libvirt transport does not), so the baked key
    is materialized for the lifetime of the inner run and unlinked at teardown.
    """
    fd, name = tempfile.mkstemp(prefix="tr-inner-key-")
    try:
        os.write(fd, private_key.encode("utf-8"))
    finally:
        os.close(fd)
    path = Path(name)
    path.chmod(0o600)
    return path


def teardown_nested(runs: list[NestedRun]) -> None:
    """Tear down entered inner orchestrators LIFO and unlink their keyfiles.

    Best-effort: a failure tearing one inner run down is logged and the rest still
    run (the outer teardown then destroys each guest VM, reclaiming any inner
    resource a failed inner teardown left behind). Never raises.
    """
    for run in reversed(runs):
        try:
            run.orchestrator.__exit__(None, None, None)
        except Exception:
            _log.exception("inner teardown failed for nested host %r", run.handle.host.name)
        finally:
            run.keyfile.unlink(missing_ok=True)


__all__ = ["NestedHandle", "NestedRun", "run_nested_phase", "teardown_nested"]
