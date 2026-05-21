"""Shared validation for user-supplied identifiers.

VM, Network, and Switch names flow verbatim into two parsers that this
package does **not** escape at the render site:

- ``dnsmasq.conf`` — ``host-record=<vm>.<net>,<ip>``, ``dhcp-host=``,
  ``domain=<net>,<cidr>`` (see :mod:`testrange.networks.sidecar`). A name
  containing ``,`` ``=`` ``#`` or a newline breaks the line or injects a
  directive.
- libvirt domain/snapshot XML (see :mod:`testrange.drivers.libvirt`). A
  name containing ``<`` ``>`` ``&`` ``"`` ``'`` breaks the document or
  injects elements.

These constraints are libvirt-specific (dnsmasq + libvirt XML), so this is
applied at the ``LibvirtHypervisor`` boundary — NOT on the backend-agnostic
``Network`` / ``Switch`` / ``VMSpec`` value objects, which only enforce a
non-empty name. A future backend (ESXi, Proxmox, ...) applies its own
naming rules at its own hypervisor boundary. The allowed set mirrors
libvirt's own resource-name rule (``[A-Za-z0-9_.+-]``) minus ``+`` (not a
DNS-label character).

A leading underscore is permitted because the orchestrator synthesizes
internal names under a ``__`` prefix (``__install``, ``__uplink__*``,
``__sidecar_*``). That prefix is reserved against *user* names separately at
the ``LibvirtHypervisor`` boundary, not in this character-safety check.
"""

from __future__ import annotations

import re

_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def validate_name(value: str, kind: str) -> str:
    """Return ``value`` unchanged, or raise ``ValueError`` if unsafe.

    ``kind`` names the field for the error message (e.g. ``"Network.name"``).
    """
    if not value:
        raise ValueError(f"{kind} must be a non-empty string")
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError(
            f"{kind} {value!r} has illegal characters: allowed are letters, "
            "digits, '_', '.', '-', starting with a letter or digit. These "
            "names are interpolated into dnsmasq.conf and libvirt XML."
        )
    return value


__all__ = ["validate_name"]
