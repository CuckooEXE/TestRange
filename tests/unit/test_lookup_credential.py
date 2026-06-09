"""run-phase lookup_credential works for any Builder, not just CloudInit.

An SSHCommunicator only needs the matching PosixCred from the builder's baked
credentials; the builder *type* is irrelevant. Regression guard for the
installer-origin run phase (ProxmoxAnswerBuilder, ESXiKickstartBuilder).
"""

from __future__ import annotations

import pytest

from testrange.builders import CloudInitBuilder, ProxmoxAnswerBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.exceptions import OrchestratorError
from testrange.orchestrator.run_phase import lookup_credential
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec


def _spec() -> VMSpec:
    return VMSpec(name="g", devices=[CPU(1), Memory(512), OSDrive("pool1", 8)], firmware="uefi")


def _proxmox_recipe(communicator: object) -> VMRecipe:
    root = PosixCred("root", password="rootpw", ssh_key=SSHKey.generate(comment="t"))
    return VMRecipe(
        spec=_spec(),
        builder=ProxmoxAnswerBuilder(installer_iso=CacheEntry("pve-iso"), credentials=[root]),
        communicator=communicator,  # type: ignore[arg-type]
    )


def test_proxmox_builder_ssh_credential_resolves() -> None:
    cred = lookup_credential(_proxmox_recipe(SSHCommunicator("root")))
    assert isinstance(cred, PosixCred)
    assert cred.username == "root"


def test_cloudinit_builder_still_resolves() -> None:
    recipe = VMRecipe(
        spec=_spec(),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"), credentials=[PosixCred("admin", password="p")]
        ),
        communicator=SSHCommunicator("admin"),
    )
    assert lookup_credential(recipe).username == "admin"


def test_unknown_username_fails_loud() -> None:
    with pytest.raises(OrchestratorError, match="no matching credential"):
        lookup_credential(_proxmox_recipe(SSHCommunicator("nobody")))


def test_non_ssh_communicator_rejected() -> None:
    with pytest.raises(OrchestratorError, match="not SSHCommunicator"):
        lookup_credential(_proxmox_recipe(NativeCommunicator()))
