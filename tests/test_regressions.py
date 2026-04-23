"""Regression tests guarding against previously-fixed bugs.

Each test here is tied to a specific bug / design decision that was
fixed or settled during development.  If any of these tests starts
failing, the corresponding regression has returned and must be fixed.

Tests whose coverage is a strict subset of a feature-test file's
assertions live there, not here (avoids drift between duplicates).
"""

from __future__ import annotations

import pytest

from testrange import VM, Credential
from testrange.backends.libvirt.network import VirtualNetwork, _mac_for_vm_network
from testrange.packages import Apt, Homebrew
from testrange.vms.builders.cloud_init import (
    CloudInitBuilder,
    _native_packages,
    _runcmd_entries,
)

pytestmark = pytest.mark.regression


def _vm(
    users: list[Credential],
    pkgs: list | None = None,
    post: list[str] | None = None,
) -> VM:
    """Build a Linux VM spec for builder-invariant regression checks."""
    return VM(
        name="vm",
        iso="https://example.com/debian.qcow2",
        users=users,
        pkgs=pkgs or [],
        post_install_cmds=post or [],
    )


def test_mac_addresses_use_qemu_oui() -> None:
    """MAC addresses must start with the QEMU OUI prefix ``52:54:00``."""
    assert _mac_for_vm_network("x", "y").startswith("52:54:00:")


def test_brew_packages_require_non_root_user() -> None:
    """Homebrew refuses to run as root; we must validate at config-gen time."""
    from testrange.exceptions import CloudInitError
    with pytest.raises(CloudInitError):
        _runcmd_entries(
            [Homebrew("gh")],
            [Credential("root", "pw")],
            [],
            ["qemu-guest-agent"],
        )


def test_brew_isinstance_not_string_compare() -> None:
    """The `brew_pkgs` filter uses `isinstance`, not string comparison on
    `.package_manager` — without the isinstance path, the type checker
    couldn't narrow `list[AbstractPackage]` to `list[Homebrew]`.

    Exercising the happy path proves the isinstance filter still catches
    Homebrew subclasses (and not false positives from other packages).
    """
    cmds = "\n".join(_runcmd_entries(
        [Apt("nginx"), Homebrew("gh")],
        [Credential("alice", "pw")],
        [],
        ["nginx", "qemu-guest-agent"],
    ))
    assert "brew install gh" in cmds
    # Apt shouldn't be picked up by the Homebrew handler
    assert "brew install nginx" not in cmds


def test_qemu_guest_agent_always_present() -> None:
    """`qemu-guest-agent` MUST appear in the install package list — it's the
    only way the orchestrator can talk to the booted VM (over the virtio
    channel) when `communicator="guest-agent"` is in use."""
    assert "qemu-guest-agent" in _native_packages([])


def test_cloud_init_builder_is_stateless() -> None:
    """Regression for the post-refactor invariant: one CloudInitBuilder
    instance can serve many distinct VM specs without carrying state
    between calls."""
    b = CloudInitBuilder()
    vm_a = VM(name="alpha", iso="https://e/x.qcow2", users=[Credential("root", "pw")])
    vm_b = VM(name="bravo", iso="https://e/x.qcow2", users=[Credential("root", "pw")])
    ud_a = b.install_user_data(vm_a)
    ud_b = b.install_user_data(vm_b)
    # Each rendering reflects its own VM, not a sticky first one.
    assert "hostname: alpha" in ud_a
    assert "hostname: bravo" in ud_b
    assert "alpha" not in ud_b
    assert "bravo" not in ud_a


def test_libvirt_network_name_length_limit() -> None:
    """libvirt network names are capped at ~15 chars; if the generated
    name ever exceeds that, libvirt rejects the network silently."""
    net = VirtualNetwork("ExcessivelyLongNetworkNameThatShouldTruncate", "10.0.0.0/24")
    net.bind_run("abcd1234efgh5678")
    assert len(net.backend_name()) <= 15
    assert len(net.bridge_name()) <= 15


def test_package_install_commands_are_list_not_string() -> None:
    """Regression: all `install_commands()` implementations return a list
    (even for a single command). `runcmd` extension assumes this."""
    for pkg in [Apt("a"), Homebrew("h")]:
        assert isinstance(pkg.install_commands(), list)


def test_bind_run_clears_prior_vm_entries() -> None:
    """Regression: ``VirtualNetwork.stop()`` does not reset run state, so
    re-using the same instance across orchestrator entries would otherwise
    pile duplicate DHCP reservations into ``_vm_entries``.  ``bind_run``
    treats each call as a fresh run and clears the accumulated state."""
    net = VirtualNetwork("N", "10.0.0.0/24")
    net.bind_run("run-one")
    net.register_vm("web", "10.0.0.5")
    assert len(net._vm_entries) == 1

    # A second run starts — bind_run should drop the run-one registrations.
    net.bind_run("run-two")
    assert net._vm_entries == []
    net.register_vm("web", "10.0.0.5")
    assert len(net._vm_entries) == 1
