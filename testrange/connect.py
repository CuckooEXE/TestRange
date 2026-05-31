"""Connection profiles — the local TOML file a dev points ``--profile`` at.

A connection profile supplies the *backend* a portable plan runs against: the
driver scheme, the connection (host/user/password/port/...), and the named
**uplinks** (logical-name → host-iface) the plan's switches resolve against
(ADR-0016). Keeping these in a local file — not the committed plan — lets one
plan run unmodified against any backend, and keeps backend addresses and host
interface names out of the test.

One file holds **many** profiles, one per top-level table (ADR-0016)::

    [myProxmox]                 # profile name; selected with --profile myProxmox
    driver = "proxmox"          # required: dispatches to the registered subclass
    host = "10.0.0.5"           # backend-specific keys follow
    user = "root@pam"
    password = "Target123!"

    [myProxmox.uplinks]         # optional: logical-name -> host iface
    lab_net  = "vmbr3"          # string form: just the bridge (sidecar DHCPs on it)

    [myProxmox.uplinks.egress]  # table form (NET-8): bridge + static sidecar
    bridge       = "vmbr9"      # addressing, for a host-NAT'd uplink that won't
    sidecar_addr = "10.10.10.2/24"  # DHCP the sidecar — gives its MASQUERADE NIC a
    gateway      = "10.10.10.1"      # source IP + an explicit upstream resolver.
    dns          = ["1.1.1.1"]

    [myLibvirtServer]           # a second profile; libvirt's localhost default
    driver = "libvirt"

The CLI ``--profile`` grammar is ``[<file>:]<name>`` with a default file of
``connect.toml`` (``--profile myProxmox`` → ``./connect.toml``;
``--profile other.toml:myProxmox`` → ``./other.toml``); :func:`load_profile`
takes the resolved ``(path, name)``.

This module is **backend-agnostic**: it defines the :class:`BackendProfile` ABC
and the :func:`load_profile` dispatch, but knows nothing about which keys any
particular backend expects. Each backend ships its own concrete subclass
(``ProxmoxProfile`` / ``LibvirtProfile`` / ``MockProfile``) that declares the
keys IT consumes, self-registers under :attr:`BackendProfile.scheme`, and builds
its own driver via :meth:`BackendProfile.build_driver`. Adding a backend means
landing one more subclass — no edits here.

Secrets policy (deliberately simple): passwords live **inline** in the TOML as
plain ``password`` / ``ssh_password`` strings. TestRange backends are
firewalled lab environments, so a credential in a local file is acceptable;
``.gitignore`` keeps a real profile out of git. There is no env/file-indirection
layer.
"""

from __future__ import annotations

import tomllib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from testrange.devices.network import StaticAddr
from testrange.exceptions import ProfileError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.base import HypervisorDriver


# scheme -> concrete profile class. Populated by each driver package at import
# time via register_profile(); load_profile dispatches against it. Kept private
# to mirror the driver registry's _BY_SCHEME pattern.
_PROFILE_BY_SCHEME: dict[str, type[BackendProfile]] = {}

# Keys every profile understands. Concrete subclasses add their own via
# _validate_keys(); unknown keys are rejected for typo protection.
_COMMON_KEYS: frozenset[str] = frozenset({"driver", "uplinks"})


class BackendProfile(ABC):
    """A connection profile parsed from one TOML table; subclassed per backend.

    Each concrete subclass declares the connection keys IT expects (as its own
    instance attributes / dataclass fields), implements :meth:`_from_table` to
    construct itself from a parsed TOML mapping, and implements
    :meth:`build_driver` to construct the driver bound to that connection (passing
    :attr:`uplinks` into it so the driver can resolve logical uplink names).

    The ABC only carries what every backend shares: the :attr:`uplinks` map.
    """

    scheme: ClassVar[str]
    """The short TOML token (``"mock"``, ``"proxmox"``, ``"libvirt"``) a profile's
    ``driver`` key selects this subclass with. Set on each concrete class."""

    uplinks: Mapping[str, str]
    """Logical-name → host-iface map (ADR-0016). A plan's ``Switch.uplink`` is a
    key here; the driver resolves it. May be empty (no uplinks declared)."""

    uplink_addrs: Mapping[str, StaticAddr]
    """Optional per-uplink static addressing for the sidecar's MASQUERADE NIC
    (``eth1``), keyed by the same logical name (NET-8). Set only for uplinks
    declared in the *table* form (``bridge`` + ``sidecar_addr``/``gateway``/
    ``dns``). The orchestrator injects it into the Switch's sidecar so a
    host-NAT'd uplink that won't DHCP the sidecar still gets a source IP for the
    MASQUERADE and an explicit upstream resolver. May be empty."""

    @classmethod
    @abstractmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        """Construct an instance from one parsed profile table.

        Subclasses validate their backend-specific keys (typically via
        :meth:`_validate_keys`), parse :attr:`uplinks` via :meth:`_parse_uplinks`,
        and return a fully-populated instance. ``path`` is supplied for error
        messages only.
        """

    @abstractmethod
    def build_driver(self) -> HypervisorDriver:
        """Construct the driver bound to this profile's connection + uplinks."""

    @abstractmethod
    def describe_fields(self) -> Iterable[tuple[str, str]]:
        """Yield ``(label, displayed_value)`` for ``testrange describe``.

        Passwords MUST be masked (``"***set***"`` / ``"(unset)"``): the binding
        print is the thing most likely to land in a report or PR.
        """

    @staticmethod
    def _validate_keys(table: Mapping[str, Any], allowed: Iterable[str], path: Path) -> None:
        """Reject any key not in ``allowed`` or the common set.

        Typo protection — names the offending key(s). Subclasses call this on
        the profile table before pulling values out.
        """
        allowed_set = _COMMON_KEYS | frozenset(allowed)
        unknown = set(table) - allowed_set
        if unknown:
            raise ProfileError(
                f"connection profile {path} has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed_set)}"
            )

    @staticmethod
    def _parse_uplinks(
        table: Mapping[str, Any], path: Path
    ) -> tuple[dict[str, str], dict[str, StaticAddr]]:
        """Parse the common ``[<profile>.uplinks]`` sub-table, if present.

        Returns ``({}, {})`` when absent. Each value is one of two forms:

        - **string** — ``egress = "vmbr9"``: the host iface, no sidecar addressing
          (the sidecar's uplink NIC DHCPs from the upstream LAN).
        - **table** — ``egress = { bridge = "vmbr9", sidecar_addr = "10.10.10.2/24",
          gateway = "10.10.10.1", dns = ["1.1.1.1"] }`` (NET-8): the bridge plus a
          static address for the sidecar's MASQUERADE NIC, for a host-NAT'd uplink
          that won't DHCP the sidecar. ``sidecar_addr`` must carry a prefix.

        Anything else is a typo worth failing loud over.
        """
        if "uplinks" not in table:
            return {}, {}
        raw = table["uplinks"]
        if not isinstance(raw, dict):
            raise ProfileError(f"connection profile {path}: [uplinks] must be a table")
        uplinks: dict[str, str] = {}
        addrs: dict[str, StaticAddr] = {}
        for name, val in raw.items():
            if isinstance(val, str):
                if not val:
                    raise ProfileError(
                        f"connection profile {path}: uplink {name!r} must map to a "
                        f"non-empty host-interface string; got {val!r}"
                    )
                uplinks[name] = val
            elif isinstance(val, dict):
                uplinks[name], addr = BackendProfile._parse_uplink_table(name, val, path)
                if addr is not None:
                    addrs[name] = addr
            else:
                raise ProfileError(
                    f"connection profile {path}: uplink {name!r} must be a host-interface "
                    f"string or a table with a 'bridge' key; got {val!r}"
                )
        return uplinks, addrs

    @staticmethod
    def _parse_uplink_table(
        name: str, val: Mapping[str, Any], path: Path
    ) -> tuple[str, StaticAddr | None]:
        """Parse one table-form uplink → ``(bridge, sidecar_addr_or_None)``."""
        allowed = {"bridge", "sidecar_addr", "gateway", "dns"}
        unknown = set(val) - allowed
        if unknown:
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} has unknown key(s) "
                f"{sorted(unknown)}; allowed: {sorted(allowed)}"
            )
        bridge = val.get("bridge")
        if not isinstance(bridge, str) or not bridge:
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} (table form) requires a "
                f"non-empty 'bridge' string; got {bridge!r}"
            )
        sidecar_addr = val.get("sidecar_addr")
        if sidecar_addr is None:
            return bridge, None
        if not isinstance(sidecar_addr, str) or "/" not in sidecar_addr:
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} sidecar_addr must be a CIDR "
                f"with an explicit prefix (e.g. '10.10.10.2/24'); got {sidecar_addr!r}"
            )
        gateway = val.get("gateway")
        if gateway is not None and not isinstance(gateway, str):
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} gateway must be a string"
            )
        dns = val.get("dns", [])
        if not isinstance(dns, list) or not all(isinstance(d, str) for d in dns):
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} dns must be a list of strings"
            )
        try:
            return bridge, StaticAddr(sidecar_addr, gw=gateway, dns=tuple(dns))
        except ValueError as e:
            raise ProfileError(
                f"connection profile {path}: uplink {name!r} addressing is invalid: {e}"
            ) from e

    @staticmethod
    def _mask_password(password: str | None) -> str:
        """Render a password for :meth:`describe_fields` without leaking it."""
        return "***set***" if password else "(unset)"


def register_profile(profile_cls: type[BackendProfile]) -> None:
    """Register a concrete :class:`BackendProfile` under its :attr:`scheme`.

    Driver packages call this at module import time; the symmetric driver-side
    side-effect is :func:`testrange.drivers._registry.register`. Re-registering
    the same scheme replaces the prior class (matches the driver registry).
    """
    _PROFILE_BY_SCHEME[profile_cls.scheme] = profile_cls


def load_profile(path: Path, name: str) -> BackendProfile:
    """Read ``path``, select the ``[name]`` table, dispatch on its ``driver`` key.

    Raises :class:`ProfileError` for a missing/unreadable file, invalid TOML, a
    missing/empty profile name, a profile that isn't a table, a missing/empty
    ``driver`` scheme, an unknown scheme (lists the registered ones), or any
    backend-specific validation failure raised inside
    :meth:`BackendProfile._from_table`.
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ProfileError(f"connection profile not found: {path}") from e
    except OSError as e:
        raise ProfileError(f"cannot read connection profile {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"connection profile {path} is not valid TOML: {e}") from e

    if name not in data:
        available = sorted(k for k, v in data.items() if isinstance(v, dict))
        raise ProfileError(
            f"connection profile {path} has no profile named {name!r}; available: {available}"
        )
    table = data[name]
    if not isinstance(table, dict):
        raise ProfileError(
            f"connection profile {path}: {name!r} is not a profile table "
            f"(expected [{name}] with a driver = ... key)"
        )

    driver = table.get("driver")
    if not isinstance(driver, str) or not driver:
        raise ProfileError(
            f"connection profile {path} profile {name!r} requires a non-empty 'driver' scheme"
        )
    profile_cls = _PROFILE_BY_SCHEME.get(driver)
    if profile_cls is None:
        raise ProfileError(
            f"connection profile {path} profile {name!r} names unknown driver scheme "
            f"{driver!r}; registered: {sorted(_PROFILE_BY_SCHEME)}"
        )
    return profile_cls._from_table(table, path)


__all__ = ["BackendProfile", "load_profile", "register_profile"]
