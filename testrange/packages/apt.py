"""APT (Debian/Ubuntu) packages."""

from __future__ import annotations

from testrange.packages.base import AbstractPackage


class Apt(AbstractPackage):
    """Install a package via ``apt-get`` on Debian/Ubuntu VMs.

    :param name: The APT package name (e.g. ``'nginx'``, ``'docker.io'``).

    Example::

        Apt("nginx")
        Apt("docker.io")

    .. note::

        APT's TLS trust config is process-wide for the install boot.  To
        install from a mirror whose CA isn't on the VM, pass
        ``apt_insecure=True`` to
        :class:`~testrange.vms.builders.CloudInitBuilder` (Linux cloud
        images) or
        :class:`~testrange.vms.builders.ProxmoxAnswerBuilder` (PVE
        Hypervisor VMs) — the flag lives on the builder because it
        applies to the whole install phase, not a single package.
    """

    @property
    def package_manager(self) -> str:
        """Return ``'apt'``.

        :returns: The string ``'apt'``.
        """
        return "apt"

    def native_package_name(self) -> str:
        """Return the APT package name.

        :returns: The package name string.
        """
        return self.name

    def install_commands(self) -> list[str]:
        """Return an empty list — APT packages are handled natively by the provisioner.

        :returns: An empty list.
        """
        return []


__all__ = ["Apt"]
