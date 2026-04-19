"""Hypervisor-neutral VM abstractions and install-phase builders.

This package hosts the abstract :class:`AbstractVM`, the image
resolver, and the provisioning strategies under
:mod:`testrange.vms.builders` (:class:`CloudInitBuilder`,
:class:`WindowsUnattendedBuilder`, :class:`NoOpBuilder`).  Concrete VM
classes live under the backend packages — see
:class:`testrange.backends.libvirt.VM` for the default libvirt
implementation.
"""

from testrange.vms.base import AbstractVM
from testrange.vms.builders import (
    Builder,
    CloudInitBuilder,
    InstallDomain,
    NoOpBuilder,
    RunDomain,
    WindowsUnattendedBuilder,
)
from testrange.vms.images import resolve_image

__all__ = [
    "AbstractVM",
    "Builder",
    "InstallDomain",
    "RunDomain",
    "CloudInitBuilder",
    "WindowsUnattendedBuilder",
    "NoOpBuilder",
    "resolve_image",
]
