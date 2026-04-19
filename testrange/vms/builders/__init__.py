"""Provisioning strategies for :class:`~testrange.backends.libvirt.VM`.

Each concrete :class:`Builder` encodes one way of getting from a
user-supplied ``iso=`` to a runnable disk image:

- :class:`CloudInitBuilder` — boot a Linux cloud image under a NoCloud
  seed ISO and let cloud-init customise it.
- :class:`WindowsUnattendedBuilder` — boot a Windows installer with an
  autounattend seed and let Setup + FirstLogonCommands run to
  completion.
- :class:`NoOpBuilder` — no install phase; the user's qcow2 is already
  ready.

Subclass :class:`Builder` to support a new provisioning pipeline
(preseed, Kickstart, Ignition, sysprep'd Windows, …) and pass the
instance as ``builder=...`` to :class:`~testrange.backends.libvirt.VM`.

Auto-selection
--------------

When a VM is constructed without an explicit ``builder=``, the VM
class walks the :data:`BUILDER_REGISTRY` looking for the first
``(predicate, factory)`` pair whose ``predicate(iso)`` returns
``True``, and calls ``factory()`` to produce the builder.  The
shipped registry matches Windows install ISOs
(:func:`~testrange.vms.images.is_windows_image`) and falls through to
:class:`CloudInitBuilder` for everything else.

Third-party code extends the registry via :func:`register_builder`::

    from testrange.vms.builders import register_builder, Builder

    class DebianPreseedBuilder(Builder):
        ...

    def is_debian_installer_iso(iso: str) -> bool:
        return iso.endswith(".iso") and "debian" in iso.lower()

    register_builder(is_debian_installer_iso, DebianPreseedBuilder)

Earlier entries take priority — the first matching predicate wins.
The default Windows entry is inserted at index 0 so shipped
predicates don't shadow user registrations unless the user asks for
that behaviour explicitly.
"""

from collections.abc import Callable

from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.builders.cloud_init import (
    CloudInitBuilder,
    write_seed_iso,
)
from testrange.vms.builders.noop import NoOpBuilder
from testrange.vms.builders.unattend import (
    WindowsUnattendedBuilder,
    write_autounattend_iso,
)
from testrange.vms.images import is_windows_image

_BuilderFactory = Callable[[], Builder]
_BuilderPredicate = Callable[[str], bool]

BUILDER_REGISTRY: list[tuple[_BuilderPredicate, _BuilderFactory]] = [
    (is_windows_image, WindowsUnattendedBuilder),
]
"""Ordered list of ``(predicate, factory)`` pairs used for auto-selection.

Walked front-to-back by :func:`auto_select_builder`; the first match
wins.  Insert new entries with :func:`register_builder`.
"""

_DEFAULT_BUILDER_FACTORY: _BuilderFactory = CloudInitBuilder
"""Factory used when no registry entry matches the ``iso=`` string."""


def register_builder(
    predicate: _BuilderPredicate,
    factory: _BuilderFactory,
    *,
    prepend: bool = True,
) -> None:
    """Register a ``(predicate, factory)`` pair for builder auto-selection.

    :param predicate: Callable taking the VM's ``iso=`` string and
        returning ``True`` when *factory* should produce the builder.
    :param factory: Zero-arg callable returning a :class:`Builder`
        instance.  Typically a :class:`Builder` subclass directly.
    :param prepend: If ``True`` (default), the pair wins over existing
        entries.  Set to ``False`` to register as a fallback —
        matched only when all earlier predicates return ``False``.
    """
    entry = (predicate, factory)
    if prepend:
        BUILDER_REGISTRY.insert(0, entry)
    else:
        BUILDER_REGISTRY.append(entry)


def auto_select_builder(iso: str) -> Builder:
    """Walk :data:`BUILDER_REGISTRY` and return the first matching builder.

    Falls back to :class:`CloudInitBuilder` when no registered
    predicate matches (Linux cloud images are the most common case,
    and every ``.qcow2`` / ``.img`` / ``https://`` input lands there
    by default).
    """
    for predicate, factory in BUILDER_REGISTRY:
        if predicate(iso):
            return factory()
    return _DEFAULT_BUILDER_FACTORY()


__all__ = [
    "Builder",
    "InstallDomain",
    "RunDomain",
    "CloudInitBuilder",
    "WindowsUnattendedBuilder",
    "NoOpBuilder",
    "write_seed_iso",
    "write_autounattend_iso",
    "BUILDER_REGISTRY",
    "register_builder",
    "auto_select_builder",
]
