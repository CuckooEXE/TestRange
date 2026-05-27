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
- ``.3``-``.9`` are reserved for future infra.
- ``.10``-``.99`` (``DHCP_RANGE_LO``..``DHCP_RANGE_HI``) is the DHCP
  lease pool, leaving ``.100``-``.254`` as one contiguous block of
  user-static space.
"""

from __future__ import annotations

SIDECAR_OFFSET = 1
MGMT_OFFSET = 2

DHCP_RANGE_LO = 10
DHCP_RANGE_HI = 99

USER_STATIC_LO = 100
USER_STATIC_HI = 254

SIDECAR_CACHE_NAME = "testrange-sidecar"

__all__ = [
    "DHCP_RANGE_HI",
    "DHCP_RANGE_LO",
    "MGMT_OFFSET",
    "SIDECAR_CACHE_NAME",
    "SIDECAR_OFFSET",
    "USER_STATIC_HI",
    "USER_STATIC_LO",
]
