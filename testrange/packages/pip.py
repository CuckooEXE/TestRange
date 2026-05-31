"""Pip — Python package installed during the install phase."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.packages.base import Package


@dataclass(frozen=True)
class Pip(Package):
    """A Python package installed via pip during the install phase.

    ``insecure=True`` passes ``--trusted-host pypi.org --trusted-host
    files.pythonhosted.org`` to pip so this package can install from a
    misconfigured / proxied / air-gapped index that the host can't TLS-verify.
    Per-package so a single insecure dep doesn't bypass verification for the
    rest of the install set.
    """

    insecure: bool = False
