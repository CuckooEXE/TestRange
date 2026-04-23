"""Abstract base class for package specifications."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AbstractPackage(ABC):
    """Base class for all package specifications.

    Subclass this to support additional package managers.

    :param name: The package name to install.
    """

    name: str
    """The package name to install (e.g. ``'nginx'``, ``'requests'``)."""

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    @abstractmethod
    def package_manager(self) -> str:
        """Short identifier for the package manager.

        :returns: E.g. ``'apt'``, ``'dnf'``, ``'brew'``, ``'pip'``, ``'winget'``.
        """

    @abstractmethod
    def native_package_name(self) -> str | None:
        """Return the package identifier for the provisioner's native package
        installation mechanism, or ``None`` if not supported natively.

        :returns: Package name string, or ``None``.
        """

    @abstractmethod
    def install_commands(self) -> list[str]:
        """Return shell commands that install this package out-of-band.

        Used for package managers not handled natively by the provisioner.
        Return an empty list for packages covered by :meth:`native_package_name`.

        :returns: List of shell command strings.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name!r})"


__all__ = ["AbstractPackage"]
