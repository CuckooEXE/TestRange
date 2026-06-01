"""Inner-binding helpers for nested virtualization (BACKEND-10, ADR-0021).

A nested libvirt host is reached over ``qemu+ssh`` from the orchestrator: the
inner :class:`~testrange.drivers.libvirt.driver.LibvirtDriver` dials the running
guest's discovered address using the SSH key the guest's ``CloudInitBuilder``
already baked. These helpers build that connection (URI + profile) and verify
libvirtd is live inside the guest before the inner orchestrator recurses.

The inner binding is **synthesized at run time** from the running guest — there
is no TOML ``--profile`` for an inner plan — which is what makes nesting
automatic. The orchestrator (``nested_phase``) owns the key-file lifetime and
the recursion; this module only constructs the libvirt-specific pieces.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.libvirt._profile import LibvirtProfile
from testrange.exceptions import OrchestratorError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from testrange.guest_io import GuestExec

_log = get_logger(__name__)


def inner_ssh_uri(host: str, user: str, *, keyfile: str) -> str:
    """A ``qemu+ssh`` libvirt URI for a TestRange-built guest hypervisor.

    ``no_verify=1`` skips host-key verification (the guest is ephemeral and its
    host key is not pre-known); ``sshauth=privkey`` + ``keyfile`` pin the
    connection to the baked key rather than an agent or password. The query is
    url-encoded so an absolute ``keyfile`` path survives intact.
    """
    query = urllib.parse.urlencode({"keyfile": keyfile, "no_verify": "1", "sshauth": "privkey"})
    return f"qemu+ssh://{user}@{host}/system?{query}"


def inner_libvirt_profile(
    host: str,
    user: str,
    *,
    keyfile: str,
    uplinks: Mapping[str, str] | None = None,
) -> LibvirtProfile:
    """Construct the in-process :class:`LibvirtProfile` for the inner binding.

    ``uplinks`` resolves the *inner* plan's ``Switch.uplink`` logical names to
    bridges on the guest hypervisor (provisioned by the guest's builder, NET-17).
    """
    return LibvirtProfile(
        uri=inner_ssh_uri(host, user, keyfile=keyfile), uplinks=dict(uplinks or {})
    )


def wait_libvirtd_ready(execute: GuestExec, *, timeout: float, poll: float = 2.0) -> None:
    """Block until libvirtd answers *inside the guest* (the nested readiness gate).

    Runs ``virsh -c qemu:///system list --all`` over the guest's bound
    communicator until it exits 0. ``post_install`` enables libvirtd and joins
    the admin user to the ``libvirt`` group, but cloud-init may still be settling
    when the outer run-phase readiness check passes, so this polls. A guest whose
    libvirtd never comes up fails loud (the inner ``qemu+ssh`` connect would
    otherwise hang or error opaquely).
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        r = execute(("virsh", "-c", "qemu:///system", "list", "--all"), timeout=15.0)
        if r.ok:
            _log.info("guest libvirtd ready")
            return
        last = r.stderr.decode("utf-8", "replace").strip()
        time.sleep(poll)
    raise OrchestratorError(
        f"guest libvirtd not ready within {timeout:.0f}s (virsh list failed): {last!r}"
    )


__all__ = ["inner_libvirt_profile", "inner_ssh_uri", "wait_libvirtd_ready"]
