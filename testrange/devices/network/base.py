"""Network interface (NIC) attached to a VM, and its address modes."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class DHCPAddr:
    """Run-phase DHCP: the NIC requests a lease at boot.

    Carries no fields — where the lease comes from is the link's business,
    not the NIC's. On a Switch with ``dhcp=True`` the per-Switch sidecar
    serves it; on a Switch *without* managed DHCP this still renders
    ``dhcp4: true`` on the assumption that something on the segment answers.
    An out-of-band DHCP server is a legitimate topology: the Switch ``dhcp``
    flag describes only whether *TestRange's* sidecar serves leases, not what
    is on the wire, so this is intentionally not policed.
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

    Frozen and hashable (``dns`` normalizes to a tuple) so a NIC carrying one
    flows through :meth:`CloudInitBuilder.config_hash`.
    """

    addr: str
    gw: str | None = None
    dns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
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

    ``network`` references a :class:`~testrange.networks.Network` by name
    (declared on the Hypervisor).

    ``addr`` is the NIC's run-phase address mode:

    - ``None`` (the default) — the NIC exists but is left **unconfigured**: no
      address, no DHCP. The guest OS decides (link-local, its own client, or
      nothing). Renders ``dhcp4: false``.
    - :class:`DHCPAddr` — request a lease at boot.
    - :class:`StaticAddr` — a static address (with prefix/gateway/DNS either
      dictated or derived from the owning Switch).

    Plan-wide validation (CIDR membership, gateway collision, DHCP-pool
    collision, duplicates across VMs) lives in
    :mod:`testrange.networks.validate` and runs at Hypervisor construction.
    """

    network: str
    addr: DHCPAddr | StaticAddr | None = None

    def __post_init__(self) -> None:
        if not self.network:
            raise ValueError("NetworkIface.network must be a non-empty string")
        # User-facing trust boundary: reject anything that isn't an address mode
        # (mypy enforces this for typed callers; this catches dynamic misuse).
        if self.addr is not None and not isinstance(self.addr, DHCPAddr | StaticAddr):
            raise TypeError(
                "NetworkIface.addr must be DHCPAddr, StaticAddr, or None; "
                f"got {type(self.addr).__name__}"
            )
