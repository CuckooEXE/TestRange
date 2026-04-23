"""DNF (Fedora / RHEL family) packages."""

from __future__ import annotations

from testrange.packages.base import AbstractPackage


class Dnf(AbstractPackage):
    """Install a package via ``dnf`` on RHEL-family VMs.

    :param name: The DNF package name (e.g. ``'nginx'``, ``'docker-ce'``).

    Example::

        Dnf("nginx")
        Dnf("podman")

    .. note::

        DNF's TLS trust config is process-wide for the install boot.  To
        install from a mirror whose CA isn't on the VM, pass
        ``dnf_insecure=True`` to
        :class:`~testrange.vms.builders.CloudInitBuilder` — the flag
        lives on the builder because it applies to the whole install
        phase, not a single package.
    """

    @property
    def package_manager(self) -> str:
        """Return ``'dnf'``.

        :returns: The string ``'dnf'``.
        """
        return "dnf"

    def native_package_name(self) -> str:
        """Return the DNF package name.

        :returns: The package name string.
        """
        return self.name

    def install_commands(self) -> list[str]:
        """Return an empty list — DNF packages are handled natively by the provisioner.

        :returns: An empty list.
        """
        return []


__all__ = ["Dnf"]
