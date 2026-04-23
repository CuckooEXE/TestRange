"""pip (PyPI) packages."""

from __future__ import annotations

from testrange.packages.base import AbstractPackage

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


__all__ = ["Pip"]
