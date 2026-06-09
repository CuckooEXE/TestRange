"""generic/users_credentials: identity, auth modes, and the privilege boundary.

WHAT: provisions one guest reached over **SSH key** auth and another reached
over **SSH password** auth. The password guest carries three users of mixed
privilege (a password root, a key-based admin, a non-admin viewer in a custom
group) and a static NIC with an explicitly-dictated resolver. The tests assert
each auth mode connects as the right identity, that the non-admin cannot sudo,
that declared group membership took, and that the explicit DNS resolver is live.

WHY: credential rendering is a build-time seam that silently drifts — a key that
never lands in ``authorized_keys``, a password user who is accidentally granted
sudo, a declared resolver that the builder writes to netplan but never reaches
``/etc/resolv.conf``. The privilege boundary in particular is a security
property a range author relies on; it must hold on every backend.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/users_credentials.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-creds")

PLAN = Plan(
    "users-credentials",
    Hypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "lab",
                Network("lab-net"),
                cidr="10.40.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="keybox",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("lab-net", addr=DHCPAddr()),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
                ),
                communicator=SSHCommunicator("admin"),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="pwbox",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface(
                            "lab-net",
                            addr=StaticAddr("10.40.0.120", gw="10.40.0.1", dns=("9.9.9.9",)),
                        ),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred("root", password="root"),
                        PosixCred("ops", ssh_key=_KEY, admin=True),
                        PosixCred("viewer", password="viewer-pw", groups=("audit",)),
                    ],
                ),
                communicator=SSHCommunicator("viewer"),
            ),
        ],
    ),
)


def key_auth_connects_as_admin(orch: OrchestratorHandle) -> None:
    r = orch.vms["keybox"].communicator.execute(["id", "-un"])
    assert r.stdout.strip() == b"admin", f"SSH key auth did not connect as admin: {r.stdout!r}"


def password_auth_connects_as_viewer(orch: OrchestratorHandle) -> None:
    r = orch.vms["pwbox"].communicator.execute(["id", "-un"])
    assert r.stdout.strip() == b"viewer", (
        f"SSH password auth did not connect as viewer: {r.stdout!r}"
    )


def viewer_cannot_sudo(orch: OrchestratorHandle) -> None:
    r = orch.vms["pwbox"].communicator.execute(["sudo", "-n", "true"])
    assert not r.ok, "non-admin viewer was granted sudo"


def viewer_in_declared_group(orch: OrchestratorHandle) -> None:
    r = orch.vms["pwbox"].communicator.execute(["id", "-Gn", "viewer"])
    assert b"audit" in r.stdout, f"viewer missing the declared audit group: {r.stdout!r}"


def ops_user_is_admin(orch: OrchestratorHandle) -> None:
    r = orch.vms["pwbox"].communicator.execute(["getent", "group", "sudo"])
    assert b"ops" in r.stdout, f"admin user ops not in sudo: {r.stdout!r}"


def explicit_resolver_applied(orch: OrchestratorHandle) -> None:
    r = orch.vms["pwbox"].communicator.execute(["resolvectl", "status"])
    assert b"9.9.9.9" in r.stdout, f"explicit DNS resolver not applied: {r.stdout!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    key_auth_connects_as_admin,
    password_auth_connects_as_viewer,
    viewer_cannot_sudo,
    viewer_in_declared_group,
    ops_user_is_admin,
    explicit_resolver_applied,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
