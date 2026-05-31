"""Shared subnet-addressing constants.

Single source of truth for the reserved-address layout and the DHCP pool
bounds. Read by :mod:`testrange.networks.validate` (which rejects static
IPs that collide with reserved slots or the pool), by
:mod:`testrange.networks.sidecar` (which renders the dnsmasq
``dhcp-range`` and the sidecar's interfaces), and by
:mod:`testrange.networks.base` (the ``Switch.sidecar_ip`` / ``mgmt_ip``
properties) — keeping them in lockstep instead of each hard-coding its
own copy.

Offsets are added to the Switch's network address (``switch.cidr``):

- ``SIDECAR_OFFSET`` (``.1``) — the sidecar VM's static address, present
  whenever a Switch has ``dhcp``, ``dns``, or ``nat``. The sidecar IS
  the gateway when ``nat=True``.
- ``MGMT_OFFSET`` (``.2``) — the mgmt host adapter's pinned address when
  a Switch has ``mgmt=True``. Not configurable.
- ``BUILD_NIC_OFFSET`` (``.3``) — the dedicated build NIC's static address on
  the build switch (ADR-0017). The first of the ``.3``-``.9`` infra range; a
  serial build uses this one fixed slot, a parallel build (ORCH-4) allocates a
  distinct slot per in-flight VM from the same range.
- ``.3``-``.9`` are reserved for infra (the build NIC plus future use).
- ``.10``-``.99`` (``DHCP_RANGE_LO``..``DHCP_RANGE_HI``) is the DHCP
  lease pool, leaving ``.100``-``.254`` as one contiguous block of
  user-static space.
"""

from __future__ import annotations

SIDECAR_OFFSET = 1
MGMT_OFFSET = 2
BUILD_NIC_OFFSET = 3

DHCP_RANGE_LO = 10
DHCP_RANGE_HI = 99

USER_STATIC_LO = 100
USER_STATIC_HI = 254

SIDECAR_CACHE_NAME = "testrange-sidecar"

__all__ = [
    "BUILD_NIC_OFFSET",
    "DHCP_RANGE_HI",
    "DHCP_RANGE_LO",
    "MGMT_OFFSET",
    "SIDECAR_CACHE_NAME",
    "SIDECAR_OFFSET",
    "USER_STATIC_HI",
    "USER_STATIC_LO",
]
