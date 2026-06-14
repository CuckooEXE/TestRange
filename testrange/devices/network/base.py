"""Network interface (NIC) attached to a VM, and its address modes."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from testrange.devices.base import Device
from testrange.handles import NetworkHandle


@dataclass(frozen=True)
class DHCPAddr:
    """Run-phase DHCP: the NIC requests a lease at boot.

    Carries no fields — where the lease comes from is the link's business, not
    the NIC's. Always renders ``dhcp4: true`` on the assumption that something on
    the segment answers, whether or not the owning Switch serves leases itself.
    """


@dataclass(frozen=True)
class StaticAddr:
    """A static run-phase address, with optionally-dictated prefix/route/DNS.

    ``addr`` is the host address, either bare (``"192.168.52.3"``) or in CIDR
    notation carrying an explicit prefix (``"192.168.52.3/24"``).

    Every field follows one resolution rule: **if listed, use it; otherwise
    derive it from the owning Switch; raise only if it is underivable.**

    - **prefix** — taken from ``addr`` when written (``/24``); otherwise derived
      from the Switch's CIDR (:attr:`NetworkAddressing.prefix_len`). Underivable
      (a bare ``addr`` on a network with no Switch addressing) raises at render:
      a static address with no netmask cannot be configured.
    - **gw** — used when given; else the Switch-derived gateway (the sidecar
      when ``nat`` is on); else ``None`` => *no default route*. Absence is a
      valid, fully-derived result (an isolated segment), **not** an error.
    - **dns** — used when given; else the Switch-derived resolver (the sidecar
      when ``dns`` is on); else *no* ``nameservers`` stanza. Like ``gw``,
      absence is valid, not an error.

    Frozen and hashable (``dns`` normalizes to a tuple).
    """

    addr: str
    gw: str | None = None
    dns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.dns, str):
            # A str is itself an iterable of characters, so `dns="8.8.8.8"` would
            # silently normalize to ('8','.','8',…) and then fail validation with
            # the misleading "entry '8' is not a valid IPv4". Reject it at the
            # boundary with an actionable message instead (CORE-95).
            raise ValueError(
                "StaticAddr.dns must be an iterable of IPs, not a single string; "
                f"wrap it in a tuple: dns=({self.dns!r},)"
            )
        if not isinstance(self.dns, tuple):
            # Accept any iterable of str at the user boundary; store a tuple.
            object.__setattr__(self, "dns", tuple(self.dns))
        try:
            ipaddress.IPv4Interface(self.addr)
        except ValueError as e:
            raise ValueError(f"StaticAddr.addr is not a valid IPv4 address: {self.addr!r}") from e
        if self.gw is not None:
            try:
                ipaddress.IPv4Address(self.gw)
            except ValueError as e:
                raise ValueError(f"StaticAddr.gw is not a valid IPv4 address: {self.gw!r}") from e
        for entry in self.dns:
            try:
                ipaddress.IPv4Address(entry)
            except ValueError as e:
                raise ValueError(
                    f"StaticAddr.dns entry is not a valid IPv4 address: {entry!r}"
                ) from e

    @property
    def host(self) -> str:
        """The host address with no prefix — for SSH targets and DNS records."""
        return str(ipaddress.IPv4Interface(self.addr).ip)

    def cidr(self, derived_prefix_len: int | None) -> str:
        """``host/prefix`` for netplan ``addresses:``.

        An explicit prefix in ``addr`` wins; otherwise ``derived_prefix_len``
        (the Switch's) is used. Raises when neither is available — a static
        address needs a netmask.
        """
        iface = ipaddress.IPv4Interface(self.addr)
        if "/" in self.addr:
            return iface.with_prefixlen
        if derived_prefix_len is None:
            raise ValueError(
                f"StaticAddr({self.addr!r}) has no prefix and none could be "
                f"derived from the Switch; a static address needs a netmask"
            )
        return f"{iface.ip}/{derived_prefix_len}"


@dataclass(frozen=True)
class NetworkIface(Device):
    """Generic NIC.

    ``network`` is a :class:`~testrange.handles.NetworkHandle` — the typed
    reference from ``hyp.networks["name"]`` (registered by ``hyp.add_switch``)
    — never a bare string, so a NIC on an undeclared network cannot be
    expressed (ADR-0030).

    ``addr`` is the NIC's run-phase address mode:

    - ``None`` (the default) — the NIC exists but is left **unconfigured**: no
      address, no DHCP. The guest OS decides (link-local, its own client, or
      nothing).
    - :class:`DHCPAddr` — request a lease at boot.
    - :class:`StaticAddr` — a static address (with prefix/gateway/DNS either
      dictated or derived from the owning Switch).

    Plan-wide validation (CIDR membership, gateway collision, DHCP-pool
    collision, duplicates across VMs) lives in
    :mod:`testrange.networks.validate` and runs when ``Plan(...)`` freezes the
    graph.
    """

    network: NetworkHandle
    addr: DHCPAddr | StaticAddr | None = None

    def __post_init__(self) -> None:
        # User-facing trust boundary, same rationale as _Disk.pool.
        if not isinstance(self.network, NetworkHandle):
            raise TypeError(
                "NetworkIface.network must be a NetworkHandle from "
                f"hyp.networks['name'], got {type(self.network).__name__}"
            )
        # User-facing trust boundary: reject anything that isn't an address mode
        # (mypy enforces this for typed callers; this catches dynamic misuse).
        if self.addr is not None and not isinstance(self.addr, DHCPAddr | StaticAddr):
            raise TypeError(
                "NetworkIface.addr must be DHCPAddr, StaticAddr, or None; "
                f"got {type(self.addr).__name__}"
            )
