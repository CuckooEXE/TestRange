"""GuestHypervisor — a VMRecipe that is also a host running an inner plan (CORE-38).

A nested hypervisor wears two hats at once. To the outer (L0) host it is an
ordinary VM: it carries the same ``spec`` / ``builder`` / ``communicator`` as any
:class:`~testrange.vms.recipe.VMRecipe`, so the build, run, and communicator-bind
phases handle it with no special casing. To its inner (L1) plan it is a
Hypervisor: the added ``inner`` field is the L1 topology (networks/pools/vms) the
orchestrator brings up *against the running guest* (ADR-0021).

Because it subclasses :class:`VMRecipe`, the only code that needs to know about
nesting is ``orchestrator.nested_phase``, which selects the entries to recurse
into with ``isinstance(vm, GuestHypervisor)``. Everything else treats it as a VM.

The :meth:`libvirt` classmethod is the ergonomic front door: it fills the
qemu/libvirt-stack :class:`~testrange.builders.CloudInitBuilder`, an
``SSHCommunicator`` for the admin user, and an inner ``LibvirtHypervisor`` (the
existing scheme marker, reused as the inner topology container — installing
libvirtd into the guest pins the inner backend to libvirt), so the common case
needs no hand-written package list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from testrange.vms.recipe import VMRecipe

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.cache.entry import CacheEntry
    from testrange.credentials.posix import PosixCred
    from testrange.devices.pool.base import StoragePool
    from testrange.hypervisor import Hypervisor
    from testrange.networks.base import Switch
    from testrange.packages.base import Package
    from testrange.vms.spec import VMSpec

# The hypervisor stack the .libvirt() sugar installs into the guest: qemu +
# libvirt. (DHCP/DNS for the inner plan is served by the inner Sidecar VM, which
# ships its own dnsmasq — the libvirt driver emits networks with no <dhcp>, so
# the guest host runs no dnsmasq of its own.) Kept as names, not Apt() instances,
# so the module imports nothing at load time.
_LIBVIRT_APT_STACK = (
    "qemu-system-x86",
    "qemu-utils",
    "libvirt-daemon-system",
    "libvirt-clients",
)


@dataclass(frozen=True)
class GuestHypervisor(VMRecipe):
    """A :class:`VMRecipe` that also hosts an inner :class:`Hypervisor` plan.

    ``inner`` is the L1 topology brought up recursively against this guest once
    its hypervisor stack is live (ADR-0021). The inner backend is libvirt-only in
    v1 (enforced at construction); it is *synthesized at run time* from this
    running guest (``qemu+ssh`` to its discovered address), not bound via
    ``--profile``.
    """

    inner: Hypervisor

    def __post_init__(self) -> None:
        # Fail at the trust boundary, not deep in nested_phase: the inner backend
        # is libvirt-only in v1 (ADR-0021), so a non-libvirt inner Hypervisor —
        # which the ``Hypervisor`` annotation still type-checks — must be rejected
        # here at construction rather than after the outer guest is already up.
        # Lazy import keeps this module a leaf (no vms -> drivers edge at load).
        from testrange.drivers.libvirt import LibvirtHypervisor

        if not isinstance(self.inner, LibvirtHypervisor):
            raise TypeError(
                f"GuestHypervisor.inner must be a LibvirtHypervisor (the inner "
                f"backend is libvirt-only in v1, ADR-0021); got "
                f"{type(self.inner).__name__}"
            )

    @classmethod
    def libvirt(
        cls,
        *,
        spec: VMSpec,
        admin: PosixCred,
        networks: Sequence[Switch] = (),
        pools: Sequence[StoragePool] = (),
        vms: Sequence[VMRecipe] = (),
        build_switch: Switch | None = None,
        base: CacheEntry | None = None,
        extra_packages: Sequence[Package] = (),
        post_install_commands: Sequence[str] = (),
    ) -> GuestHypervisor:
        """Build a libvirt-backed nested host with the stack pre-filled.

        ``admin`` is the privileged credential the inner ``qemu+ssh`` binding and
        the outer SSH login share (its baked key reaches both). ``base`` is the
        guest's OS image, defaulting to ``debian-13`` when omitted. ``networks`` /
        ``pools`` / ``vms`` / ``build_switch`` are the *inner* (L1) topology;
        ``extra_packages`` and ``post_install_commands`` append to the baked-in
        libvirt stack and bring-up commands.
        """
        # Lazy imports keep this module a leaf (no vms -> drivers/builders edge at
        # load time) and follow the optional-dependency idiom for the driver.
        # CacheEntry is imported here too (not as a signature default) so the
        # default base needs no load-time import — hence the None sentinel above.
        from testrange.builders import CloudInitBuilder
        from testrange.cache import CacheEntry
        from testrange.communicators import SSHCommunicator
        from testrange.drivers.libvirt import LibvirtHypervisor
        from testrange.packages import Apt

        packages: list[Package] = [Apt(name) for name in _LIBVIRT_APT_STACK]
        packages.extend(extra_packages)
        post = (
            f"usermod -aG libvirt,kvm {admin.username}",
            "systemctl enable --now libvirtd",
            *post_install_commands,
        )
        builder = CloudInitBuilder(
            base=base if base is not None else CacheEntry("debian-13"),
            credentials=[admin],
            packages=packages,
            post_install_commands=post,
        )
        inner = LibvirtHypervisor(
            networks=networks, pools=pools, vms=vms, build_switch=build_switch
        )
        return cls(
            spec=spec,
            builder=builder,
            communicator=SSHCommunicator(admin.username),
            inner=inner,
        )


def reject_unsupported_nesting(hypervisor: Hypervisor) -> None:
    """Reject depth-2+ nesting — TestRange supports a single level (ADR-0021).

    A :class:`GuestHypervisor` whose inner plan *itself* contains a
    ``GuestHypervisor`` nests two levels deep. Its disks build fine (the build
    recursion is depth-agnostic), but the inner run cannot reach the L2 guest
    over ``qemu+ssh`` — the reachability wall documented in CI-8. Refuse it
    loudly here, before any backend work, rather than build three disk sets and
    then time out opaquely deep in the inner bring-up.
    """
    for vm in hypervisor.vms:
        if not isinstance(vm, GuestHypervisor):
            continue
        deeper = [v.name for v in vm.inner.vms if isinstance(v, GuestHypervisor)]
        if deeper:
            raise ValueError(
                f"nested host {vm.name!r} hosts further nested hypervisor(s) {deeper}: "
                f"TestRange supports single-level nesting only (ADR-0021)"
            )


__all__ = ["GuestHypervisor", "reject_unsupported_nesting"]
