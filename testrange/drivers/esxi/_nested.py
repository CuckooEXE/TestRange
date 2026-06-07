"""Inner-binding helpers for nesting an ESXi hypervisor (ADR-0021, ORCH-32).

A nested ESXi host is reached over pyVmomi (vSphere SOAP) from the orchestrator:
the inner :class:`~testrange.drivers.esxi.driver.ESXiDriver` dials the running
guest's discovered address with the root password the guest's
:class:`~testrange.builders.ESXiKickstartBuilder` baked at install. These helpers
synthesize that in-process :class:`ESXiProfile` and verify the vSphere API answers
inside the guest before the inner orchestrator recurses.

Unlike the libvirt inner binding (``qemu+ssh`` + a key file,
:mod:`testrange.drivers.libvirt._nested`), the ESXi binding is password-based
pyVmomi; inner-VM reach uses the ESXi driver's own SSH ``guest_gateway``, so there
is no key file to materialize. The orchestrator (``nested_phase``) owns the
recursion; this module only constructs the ESXi-specific pieces.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.esxi._client import EsxiClient, EsxiConn
from testrange.drivers.esxi._profile import ESXiProfile
from testrange.exceptions import DriverError, OrchestratorError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from testrange.devices.network.base import StaticAddr

_log = get_logger(__name__)


def inner_esxi_profile(
    host: str,
    password: str,
    *,
    user: str = "root",
    datastore: str = "datastore1",
    port: int = 443,
    uplinks: Mapping[str, str] | None = None,
    uplink_addrs: Mapping[str, StaticAddr] | None = None,
) -> ESXiProfile:
    """Construct the in-process :class:`ESXiProfile` for the inner binding.

    ``host`` is the running guest's discovered address; ``password`` is the root
    password the guest's ESXiKickstartBuilder baked. ``uplinks`` resolves the
    *inner* plan's ``Switch.uplink`` logical names — carried through so the inner
    cache-only run accepts the inner build switch's uplink at preflight even
    though no build (hence no uplink realization) happens on L1.
    """
    return ESXiProfile(
        host=host,
        user=user,
        password=password,
        datastore=datastore,
        port=port,
        uplinks=dict(uplinks or {}),
        uplink_addrs=dict(uplink_addrs or {}),
    )


def wait_esxi_ready(
    host: str,
    user: str,
    password: str,
    *,
    port: int = 443,
    timeout: float,
    poll: float = 5.0,
) -> None:
    """Block until the guest ESXi answers the vSphere API (the nested readiness gate).

    The guest reboots into ESXi after the kickstart install and hostd takes a
    while to start serving SOAP, so the outer run-phase readiness (SSH up) can
    pass well before the API is live. Poll ``SmartConnect`` (verify off) via a
    throwaway :class:`EsxiClient` until it connects + retrieves content, then
    disconnect. A guest whose API never comes up fails loud (the inner pyVmomi
    bind would otherwise error opaquely).
    """
    deadline = time.monotonic() + timeout
    conn = EsxiConn(host=host, user=user, password=password, port=port)
    last = ""
    while time.monotonic() < deadline:
        client = EsxiClient(conn)
        try:
            client.connect()
        except DriverError as e:
            last = str(e)
            time.sleep(poll)
            continue
        client.close()
        _log.info("guest ESXi vSphere API ready at %s", host)
        return
    raise OrchestratorError(
        f"guest ESXi at {host} not ready within {timeout:.0f}s (vSphere connect failed): {last!r}"
    )


__all__ = ["inner_esxi_profile", "wait_esxi_ready"]
