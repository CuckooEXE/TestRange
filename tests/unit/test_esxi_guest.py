"""ESXI-5: VMware Tools guest-ops credential gate (``_auth``).

VMware Tools authenticates against the guest OS on every call, so each guest-op
binds a per-call username+password credential (CORE-60, ADR-0008). The two
rejection paths are pure logic (the client/SOAP plane is never touched), so they
need no live ESXi — mirroring test_libvirt_guest / test_proxmox_guest.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from testrange.credentials.posix import PosixCred
from testrange.drivers.esxi import _guest
from testrange.exceptions import GuestAgentError
from testrange.utils import SSHKey


def test_auth_requires_a_credential() -> None:
    with pytest.raises(GuestAgentError, match="require a guest credential"):
        _guest._auth(cast(Any, None), None, "tr-vm-x")


def test_auth_rejects_key_only_credential() -> None:
    # A key-only PosixCred has no password; VMware Tools can't use an SSH key,
    # so guest-ops must reject it loudly rather than build a passwordless auth.
    key_only = PosixCred("admin", ssh_key=SSHKey.generate(comment="t"))
    with pytest.raises(GuestAgentError, match="key-only credentials are not usable"):
        _guest._auth(cast(Any, None), key_only, "tr-vm-x")
