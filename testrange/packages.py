"""Package manager definitions for VM provisioning.

Provides an abstract base class and concrete implementations for the
supported package managers: APT, DNF, Homebrew, pip, and winget.
"""

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
        :class:`~testrange.vms.builders.CloudInitBuilder` — the flag
        lives on the builder because it applies to the whole install
        phase, not a single package.
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


_BREW_INSTALL_CMD = (
    'NONINTERACTIVE=1 su -s /bin/bash -c'
    ' \'$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\''
    ' {user}'
)
"""Shell command template that installs Homebrew non-interactively.

The ``{user}`` placeholder is substituted with the non-root username at
provisioning time.
"""

_BREW_PKG_CMD = "su -s /bin/bash -c 'brew install {pkg}' {user}"
"""Shell command template for installing a single Homebrew formula.

Placeholders: ``{pkg}`` — formula name; ``{user}`` — non-root username.
"""


class Homebrew(AbstractPackage):
    """Install a package via Homebrew.

    .. note::
        Homebrew refuses to run as ``root``.  At least one non-root user must
        be present in the VM's credential list.

    :param name: The Homebrew formula or cask name (e.g. ``'hello'``, ``'wget'``).

    Example::

        Homebrew("hello")
        Homebrew("gh")
    """

    @property
    def package_manager(self) -> str:
        """Return ``'brew'``.

        :returns: The string ``'brew'``.
        """
        return "brew"

    def native_package_name(self) -> None:
        """Return ``None`` — Homebrew packages are installed via :meth:`install_commands`.

        :returns: ``None``.
        """
        return None

    def install_commands(self) -> list[str]:
        """Return the shell command template that installs this formula.

        The ``{brew_user}`` placeholder must be substituted by the caller before
        execution.

        :returns: List of shell command templates.
        """
        return [_BREW_PKG_CMD.format(pkg=self.name, user="{brew_user}")]

    @staticmethod
    def install_homebrew_command() -> str:
        """Return the shell command template that installs Homebrew itself.

        :returns: Shell command template with a ``{user}`` placeholder.
        """
        return _BREW_INSTALL_CMD


# Shell snippet that extracts the hostname from pip's configured
# ``global.index-url`` at install time (pip.conf or ``PIP_INDEX_URL`` env).
# Used by ``Pip(insecure=True)`` so users with a custom mirror don't have to
# re-specify the URL — we just trust whatever pip is already pointed at.
# The ``|| true`` swallows a ``pip3 config get`` failure (no index configured)
# so the caller's ``set -e`` script keeps going with just the default hosts.
_PIP_EXTRACT_INDEX_HOST = (
    "$(pip3 config get global.index-url 2>/dev/null "
    r"| sed -nE 's,^https?://([^/:]+).*,\1,p' || true)"
)


class Pip(AbstractPackage):
    """Install a Python package via ``pip3``.

    :param name: The PyPI package name (e.g. ``'requests'``, ``'flask'``).
    :param user_install: If ``True``, passes ``--user`` to ``pip3 install``
        so the package is installed for the first non-root user rather than
        system-wide.  Defaults to ``False`` (system-wide install).
    :param insecure: If ``True``, pip is told to trust the host of its
        configured index URL (and the default PyPI hosts) so a private
        mirror whose CA isn't in the VM's trust store still installs.
        Defaults to ``False``.

    Example::

        Pip("requests")
        Pip("flask", user_install=True)
        Pip("numpy", insecure=True)  # Private mirror configured in pip.conf
    """

    user_install: bool
    """If ``True``, passes ``--user`` to ``pip3 install`` for a per-user install."""

    insecure: bool
    """If ``True``, pip is invoked with ``--trusted-host`` covering the configured mirror."""

    def __init__(
        self,
        name: str,
        user_install: bool = False,
        insecure: bool = False,
    ) -> None:
        super().__init__(name)
        self.user_install = user_install
        self.insecure = insecure

    @property
    def package_manager(self) -> str:
        """Return ``'pip'``.

        :returns: The string ``'pip'``.
        """
        return "pip"

    def native_package_name(self) -> None:
        """Return ``None`` — pip packages are installed via :meth:`install_commands`.

        :returns: ``None``.
        """
        return None

    def install_commands(self) -> list[str]:
        """Return the shell command that installs this package via pip3.

        :returns: A one-element list containing the install command.
        """
        user_flag = " --user" if self.user_install else ""
        if self.insecure:
            return [
                f'_tr_pip_host="{_PIP_EXTRACT_INDEX_HOST}"; '
                f"pip3 install{user_flag} "
                '${_tr_pip_host:+--trusted-host "$_tr_pip_host"} '
                "--trusted-host pypi.org --trusted-host files.pythonhosted.org "
                f"{self.name}"
            ]
        return [f"pip3 install{user_flag} {self.name}"]

    def __repr__(self) -> str:
        extras = []
        if self.user_install:
            extras.append("user_install=True")
        if self.insecure:
            extras.append("insecure=True")
        if extras:
            return f"Pip({self.name!r}, {', '.join(extras)})"
        return f"Pip({self.name!r})"


class Winget(AbstractPackage):
    """Install a package via Windows Package Manager (``winget``).

    :param name: The winget package ID (e.g. ``'Git.Git'``,
        ``'Microsoft.VisualStudioCode'``).

    Example::

        Winget("Git.Git")
        Winget("Notepad++.Notepad++")
    """

    @property
    def package_manager(self) -> str:
        """Return ``'winget'``.

        :returns: The string ``'winget'``.
        """
        return "winget"

    def native_package_name(self) -> None:
        """Return ``None`` — winget packages are installed via :meth:`install_commands`.

        :returns: ``None``.
        """
        return None

    def install_commands(self) -> list[str]:
        """Return the PowerShell command that installs this package.

        :returns: A one-element list containing the winget install command.
        """
        return [
            f"winget install --id {self.name} --silent --accept-package-agreements"
            " --accept-source-agreements"
        ]


__all__ = [
    "AbstractPackage",
    "Apt",
    "Dnf",
    "Homebrew",
    "Pip",
    "Winget",
]
