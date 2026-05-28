"""Connection profiles — the local TOML file a dev points ``--connect`` at (CORE-9).

A connection profile supplies the *backend* a portable plan runs against: the
driver scheme, the connection (host/user/password/port/...), and the
environment knobs (build egress, backing storage, node) that are a binding
concern rather than portable topology. Keeping these in a local file — not the
committed plan — lets one plan run unmodified against any backend, and keeps
backend addresses out of the test.

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
``.gitignore`` (CORE-12) keeps a real profile out of git. There is no
env/file-indirection layer.

Format (parsed with stdlib :mod:`tomllib`, no new dependency)::

    driver = "proxmox"          # required: dispatches to the registered subclass
    host = "10.0.0.5"           # backend-specific keys follow
    user = "root@pam"
    password = "Target123!"
    ...

    [build_switch]              # optional: managed build-internet egress, common
    uplink = "vmbr9"            # host interface to SNAT the build network out of
    cidr = "10.10.10.0/24"      # optional internal build subnet

The ``[build_switch]`` table is the one keyset *every* backend understands; it
maps to a :class:`~testrange.networks.base.ManagedBuildSwitch` (ADR-0014). A
bring-your-own plain ``Switch`` egress path is not expressible here by design;
declare it by *pinning* the plan to a concrete ``*Hypervisor`` with a
``build_switch=Switch(...)`` instead.
"""

from __future__ import annotations

import tomllib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from testrange.exceptions import ProfileError
from testrange.networks.base import ManagedBuildSwitch

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.base import HypervisorDriver


# scheme -> concrete profile class. Populated by each driver package at import
# time via register_profile(); load_profile dispatches against it. Kept private
# to mirror the driver registry's _BY_SCHEME pattern.
_PROFILE_BY_SCHEME: dict[str, type[BackendProfile]] = {}

# Keys every backend understands at the top level. Concrete subclasses add their
# own via _validate_keys(); unknown keys are rejected for typo protection.
_COMMON_KEYS: frozenset[str] = frozenset({"driver", "build_switch"})
_ALLOWED_BUILD_SWITCH: frozenset[str] = frozenset({"uplink", "cidr"})


class BackendProfile(ABC):
    """A connection profile parsed from TOML; subclassed once per backend.

    Each concrete subclass declares the connection keys IT expects (as its own
    instance attributes / dataclass fields), implements :meth:`_from_table` to
    construct itself from a parsed TOML mapping, and implements
    :meth:`build_driver` to construct the driver bound to that connection.

    The ABC only carries what every backend shares: the optional
    :class:`~testrange.networks.base.ManagedBuildSwitch` driving build-internet
    egress (ADR-0014).
    """

    scheme: ClassVar[str]
    """The short TOML token (``"mock"``, ``"proxmox"``, ``"libvirt"``) the
    profile selects this subclass with. Set on each concrete class."""

    build_switch: ManagedBuildSwitch | None
    """User-declared managed build-egress (ADR-0014); ``None`` = isolated."""

    @classmethod
    @abstractmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        """Construct an instance from a parsed TOML mapping.

        Subclasses validate their backend-specific keys (typically via
        :meth:`_validate_keys`), parse :attr:`build_switch` via
        :meth:`_parse_build_switch`, and return a fully-populated instance.
        ``path`` is supplied for error messages only.
        """

    @abstractmethod
    def build_driver(self) -> HypervisorDriver:
        """Construct the driver bound to this profile's connection."""

    @abstractmethod
    def describe_fields(self) -> Iterable[tuple[str, str]]:
        """Yield ``(label, displayed_value)`` for ``testrange describe``.

        Passwords MUST be masked (``"***set***"`` / ``"(unset)"``): the binding
        print is the thing most likely to land in a report or PR.
        """

    @staticmethod
    def _validate_keys(table: Mapping[str, Any], allowed: Iterable[str], path: Path) -> None:
        """Reject any top-level key not in ``allowed`` or the common set.

        Typo protection — names the offending key(s). Subclasses call this on
        the raw parsed table before pulling values out.
        """
        allowed_set = _COMMON_KEYS | frozenset(allowed)
        unknown = set(table) - allowed_set
        if unknown:
            raise ProfileError(
                f"connection profile {path} has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed_set)}"
            )

    @staticmethod
    def _parse_build_switch(table: Mapping[str, Any], path: Path) -> ManagedBuildSwitch | None:
        """Parse the common ``[build_switch]`` table off ``table``, if present."""
        if "build_switch" not in table:
            return None
        bs = table["build_switch"]
        if not isinstance(bs, dict):
            raise ProfileError(f"connection profile {path}: [build_switch] must be a table")
        unknown = set(bs) - _ALLOWED_BUILD_SWITCH
        if unknown:
            raise ProfileError(
                f"connection profile {path}: [build_switch] has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_BUILD_SWITCH)}"
            )
        uplink = bs.get("uplink")
        if not isinstance(uplink, str) or not uplink:
            raise ProfileError(
                f"connection profile {path}: [build_switch] requires a non-empty 'uplink'"
            )
        try:
            return ManagedBuildSwitch(uplink=uplink, cidr=bs.get("cidr"))
        except ValueError as e:
            raise ProfileError(f"connection profile {path}: invalid [build_switch]: {e}") from e

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


def load_profile(path: Path) -> BackendProfile:
    """Read ``path``, parse TOML, dispatch on ``driver`` to the registered subclass.

    Raises :class:`ProfileError` for a missing/unreadable file, invalid TOML,
    a missing/empty ``driver`` scheme, an unknown scheme (lists the registered
    ones), or any backend-specific validation failure raised inside
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

    driver = data.get("driver")
    if not isinstance(driver, str) or not driver:
        raise ProfileError(f"connection profile {path} requires a non-empty 'driver' scheme")
    profile_cls = _PROFILE_BY_SCHEME.get(driver)
    if profile_cls is None:
        raise ProfileError(
            f"connection profile {path} names unknown driver scheme {driver!r}; "
            f"registered: {sorted(_PROFILE_BY_SCHEME)}"
        )
    return profile_cls._from_table(data, path)


__all__ = ["BackendProfile", "load_profile", "register_profile"]
