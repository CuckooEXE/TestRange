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

# The hypervisor stack the .libvirt() sugar installs into the guest. qemu +
# libvirt + dnsmasq (the sidecar's DHCP/DNS server runs inside the inner plan).
# Kept as names, not Apt() instances, so the module imports nothing at load time.
_LIBVIRT_APT_STACK = (
    "qemu-system-x86",
    "qemu-utils",
    "libvirt-daemon-system",
    "libvirt-clients",
    "dnsmasq",
)


@dataclass(frozen=True)
class GuestHypervisor(VMRecipe):
    """A :class:`VMRecipe` that also hosts an inner :class:`Hypervisor` plan.

    ``inner`` is the L1 topology brought up recursively against this guest once
    its hypervisor stack is live (ADR-0021). The inner plan is a normal portable
    Hypervisor; its backend is *synthesized at run time* from this running guest
    (``qemu+ssh`` to its discovered address), not bound via ``--profile``.
    """

    # Required: VMRecipe's fields carry no defaults, so a trailing field without
    # one is valid and stays mandatory.
    inner: Hypervisor

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
        the outer SSH login share (its baked key reaches both). ``networks`` /
        ``pools`` / ``vms`` / ``build_switch`` are the *inner* (L1) topology;
        ``extra_packages`` and ``post_install_commands`` append to the baked-in
        libvirt stack and bring-up commands.
        """
        # Lazy imports keep this module a leaf (no vms -> drivers/builders edge at
        # load time) and follow the optional-dependency idiom for the driver.
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


__all__ = ["GuestHypervisor"]
