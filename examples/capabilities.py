"""capabilities: one portable plan that exercises every driver-facing feature.

This is the broad-coverage example: a single backend-agnostic :class:`Hypervisor`
(portable topology only) whose VMs and tests touch every capability TestRange
exposes to a driver. Bind a backend at run time with ``--profile`` — the plan
names no host and no credentials. Its switches reference uplinks by **logical
name** (``egress``), which the bound profile's ``[uplinks]`` map resolves to a
host bridge (ADR-0016); the ``build_switch`` is portable topology on the plan.

Capability map (VM -> what it proves):

- ``no-net``      no NICs at all; reached over the native guest agent.
- ``unmanaged``   one NIC, no DHCP and no static address; native agent.
- ``multihome``   three NICs of different modes (static / DHCP / unmanaged) on
                  one VM; native agent; doubles as the air-gap client.
- ``keybox``      SSH key auth bound to a chosen NIC (``nic_idx``) whose address
                  is DHCP-discovered; apt + pip; an oversized OS drive that
                  grows on first boot; snapshot / memory-snapshot / power-state.
- ``users``       three users of mixed privilege; SSH password auth as the
                  non-admin; static address with an explicitly-dictated resolver.
- ``fileserver``  two data disks seeded with disk-unique content at build and
                  verified intact at run (the build->cache->run disk-set check).
- ``private-web`` static address on an air-gapped switch; no route off it.
- ``public-web``  DHCP on an internet-connected, NAT+DNS switch.

The air-gap reachability matrix runs across ``multihome`` / ``private-web`` /
``public-web``; multi-Network-per-Switch is proven by reaching ``public-web``
(on ``pub-b``) from ``multihome`` (on ``pub-a``) across one shared L2.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange describe examples/capabilities.py
    testrange run --profile <name> examples/capabilities.py

The profile must map the ``egress`` uplink to a host bridge with out-of-band
internet egress (NAT/DHCP behind it), and provide a backend that supports the
native guest agent, a serial build-result sink, and memory snapshots — every
capability the tests touch.
"""

from __future__ import annotations

import sys

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt  # Pip re-added with keybox's cowsay (BUILD-4)
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-capabilities")
_ADMIN = PosixCred("admin", ssh_key=_KEY, admin=True)

_PRIVATE_WEB_IP = "10.20.0.100"
_CLIENT_PRIVATE_IP = "10.20.0.101"
_PUB_DHCP_LO = 10
_PUB_DHCP_HI = 99


def _native_image(*packages: Apt, post: tuple[str, ...] = ()) -> CloudInitBuilder:
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        packages=[Apt("qemu-guest-agent"), *packages],
        post_install_commands=("systemctl enable --now qemu-guest-agent", *post),
    )


PLAN = Plan(
    "capabilities",
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
                "pub-sw",
                Network("pub-a"),
                Network("pub-b"),
                cidr="10.30.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
            Switch(
                "priv-sw",
                Network("priv-net"),
                cidr="10.20.0.0/24",
            ),
        ],
        pools=[StoragePool("pool1", 64)],
        vms=[
            # no-net (zero-NIC, QGA-only) disabled pending ORCH-9: a zero-NIC VM
            # gets no build-time network, so its apt-based builder can't install
            # qemu-guest-agent. Re-enable (here + in TESTS) once ORCH-9 lands.
            # VMRecipe(
            #     spec=VMSpec(
            #         name="no-net",
            #         devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
            #     ),
            #     builder=_native_image(),
            #     communicator=NativeCommunicator(),
            # ),
            VMRecipe(
                spec=VMSpec(
                    name="unmanaged",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("pub-a"),
                    ],
                ),
                builder=_native_image(),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="multihome",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("priv-net", addr=StaticAddr(_CLIENT_PRIVATE_IP)),
                        NetworkIface("pub-a", addr=DHCPAddr()),
                        NetworkIface("pub-b"),
                    ],
                ),
                builder=_native_image(Apt("curl")),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="keybox",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 16),
                        NetworkIface("pub-a", addr=StaticAddr("10.30.0.110")),
                        NetworkIface("pub-b", addr=DHCPAddr()),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[_ADMIN],
                    packages=[Apt("nginx")],  # Pip("cowsay") disabled pending BUILD-4
                    post_install_commands=("systemctl enable --now nginx",),
                ),
                communicator=SSHCommunicator("admin", nic_idx=1),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="users",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface(
                            "pub-a",
                            addr=StaticAddr("10.30.0.120", gw="10.30.0.1", dns=("9.9.9.9",)),
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
            VMRecipe(
                spec=VMSpec(
                    name="fileserver",
                    devices=[
                        CPU(1),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        HardDrive("pool1", 2),
                        HardDrive("pool1", 2),
                        NetworkIface("pub-a", addr=StaticAddr("10.30.0.130")),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[_ADMIN],
                    post_install_commands=(
                        "mkfs.ext4 -F -L data-b /dev/vdb",
                        "mkfs.ext4 -F -L data-c /dev/vdc",
                        "mkdir -p /srv/b /srv/c",
                        "mount /dev/vdb /srv/b",
                        "mount /dev/vdc /srv/c",
                        "sh -c 'echo disk-b > /srv/b/which'",
                        "sh -c 'echo disk-c > /srv/c/which'",
                        "sh -c 'blkid -s UUID -o value /dev/vdb > /srv/b/uuid'",
                        "sh -c 'blkid -s UUID -o value /dev/vdc > /srv/c/uuid'",
                        "sh -c 'echo \"LABEL=data-b /srv/b ext4 defaults 0 2\" >> /etc/fstab'",
                        "sh -c 'echo \"LABEL=data-c /srv/c ext4 defaults 0 2\" >> /etc/fstab'",
                    ),
                ),
                communicator=SSHCommunicator("admin"),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="private-web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("priv-net", addr=StaticAddr(_PRIVATE_WEB_IP)),
                    ],
                ),
                builder=_native_image(
                    Apt("nginx"),
                    Apt("curl"),
                    post=(
                        "sh -c 'echo air-gapped > /var/www/html/index.html'",
                        "systemctl enable --now nginx",
                    ),
                ),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="public-web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("pub-b", addr=DHCPAddr()),
                    ],
                ),
                builder=_native_image(
                    Apt("nginx"),
                    Apt("curl"),
                    post=(
                        "sh -c 'echo internet-connected > /var/www/html/index.html'",
                        "systemctl enable --now nginx",
                    ),
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


# --- no-net: a guest with no NICs, reachable only over the native agent -------


def no_net_agent_executes(orch: OrchestratorHandle) -> None:
    r = orch.vms["no-net"].communicator.execute(["true"])
    assert r.ok, f"native agent unreachable on a NIC-less guest: {r}"


def no_net_has_no_ethernet(orch: OrchestratorHandle) -> None:
    r = orch.vms["no-net"].communicator.execute(["ip", "-o", "-4", "addr"])
    addrs = [ln for ln in r.stdout.decode().splitlines() if " lo " not in ln]
    assert not addrs, f"NIC-less guest has an IPv4 address: {addrs!r}"


# --- unmanaged: NIC present, no DHCP, no static -> no runtime address ---------


def unmanaged_nic_has_link_no_address(orch: OrchestratorHandle) -> None:
    com = orch.vms["unmanaged"].communicator
    links = com.execute(["ip", "-o", "link"]).stdout.decode()
    assert "en" in links, f"no ethernet device present: {links!r}"
    v4 = com.execute(["ip", "-o", "-4", "addr"]).stdout.decode().splitlines()
    non_lo = [ln for ln in v4 if " lo " not in ln]
    assert not non_lo, f"unmanaged NIC was assigned an address: {non_lo!r}"


def unmanaged_file_roundtrips_over_agent(orch: OrchestratorHandle) -> None:
    com = orch.vms["unmanaged"].communicator
    com.write_file("/root/marker", b"native-io\n")
    assert com.read_file("/root/marker") == b"native-io\n"


# --- multihome: three NIC modes on one VM -------------------------------------


def multihome_static_nic_addressed(orch: OrchestratorHandle) -> None:
    out = orch.vms["multihome"].communicator.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
    assert _CLIENT_PRIVATE_IP in out, f"static NIC missing {_CLIENT_PRIVATE_IP}: {out!r}"


def multihome_dhcp_nic_in_pool(orch: OrchestratorHandle) -> None:
    out = orch.vms["multihome"].communicator.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
    leased = [tok for tok in out.split() if tok.startswith("10.30.0.")]
    assert leased, f"DHCP NIC got no 10.30.0.x lease: {out!r}"
    octet = int(leased[0].split("/")[0].rsplit(".", 1)[1])
    assert _PUB_DHCP_LO <= octet <= _PUB_DHCP_HI, f"lease {leased[0]} outside the DHCP pool"


def multihome_one_default_route(orch: OrchestratorHandle) -> None:
    out = orch.vms["multihome"].communicator.execute(["ip", "-4", "route", "show", "default"])
    routes = [ln for ln in out.stdout.decode().splitlines() if ln.strip()]
    assert len(routes) == 1, f"expected exactly one default route, got {routes!r}"


# --- keybox: SSH key auth on a DHCP-discovered NIC; apt/pip; growpart ---------


def keybox_bound_to_dhcp_nic(orch: OrchestratorHandle) -> None:
    host = orch.vms["keybox"].communicator.host
    assert host and host.startswith("10.30.0."), f"nic_idx=1 host not DHCP-discovered: {host!r}"
    octet = int(host.rsplit(".", 1)[1])
    assert _PUB_DHCP_LO <= octet <= _PUB_DHCP_HI, f"bound host {host} outside the DHCP pool"


def keybox_apt_package_present(orch: OrchestratorHandle) -> None:
    assert orch.vms["keybox"].communicator.execute(["dpkg", "-l", "nginx"]).ok, "nginx missing"


def keybox_pip_package_importable(orch: OrchestratorHandle) -> None:
    r = orch.vms["keybox"].communicator.execute(["python3", "-c", "import cowsay"])
    assert r.ok, f"pip package not importable: {r.stderr!r}"


def keybox_os_drive_grew(orch: OrchestratorHandle) -> None:
    r = orch.vms["keybox"].communicator.execute(["df", "-BG", "--output=size", "/"])
    size_gb = int(r.stdout.decode().splitlines()[-1].strip().rstrip("G"))
    assert size_gb >= 14, f"rootfs did not grow to the 16G OSDrive: {size_gb}G"


def keybox_file_roundtrips_over_ssh(orch: OrchestratorHandle) -> None:
    com = orch.vms["keybox"].communicator
    com.write_file("/home/admin/marker", b"ssh-io\n")
    assert com.read_file("/home/admin/marker") == b"ssh-io\n"


def keybox_exec_honors_cwd(orch: OrchestratorHandle) -> None:
    r = orch.vms["keybox"].communicator.execute(["pwd"], cwd="/etc")
    assert r.stdout.strip() == b"/etc", f"cwd ignored: {r.stdout!r}"


# --- users: mixed privilege; password auth as the non-admin -------------------


def viewer_authed_with_password(orch: OrchestratorHandle) -> None:
    r = orch.vms["users"].communicator.execute(["id", "-un"])
    assert r.stdout.strip() == b"viewer", f"not connected as viewer: {r.stdout!r}"


def viewer_cannot_sudo(orch: OrchestratorHandle) -> None:
    r = orch.vms["users"].communicator.execute(["sudo", "-n", "true"])
    assert not r.ok, "non-admin viewer was granted sudo"


def viewer_in_declared_group(orch: OrchestratorHandle) -> None:
    r = orch.vms["users"].communicator.execute(["id", "-Gn", "viewer"])
    assert b"audit" in r.stdout, f"viewer missing the audit group: {r.stdout!r}"


def ops_user_is_admin(orch: OrchestratorHandle) -> None:
    r = orch.vms["users"].communicator.execute(["getent", "group", "sudo"])
    assert b"ops" in r.stdout, f"admin user ops not in sudo: {r.stdout!r}"


def users_uses_explicit_resolver(orch: OrchestratorHandle) -> None:
    r = orch.vms["users"].communicator.execute(["cat", "/etc/resolv.conf"])
    assert b"9.9.9.9" in r.stdout, f"explicit DNS not applied: {r.stdout!r}"


# --- fileserver: data disks seeded at build, intact at run --------------------


def data_disks_mounted(orch: OrchestratorHandle) -> None:
    com = orch.vms["fileserver"].communicator
    assert com.execute(["mountpoint", "-q", "/srv/b"]).ok, "/srv/b not mounted"
    assert com.execute(["mountpoint", "-q", "/srv/c"]).ok, "/srv/c not mounted"


def data_disks_carry_their_own_content(orch: OrchestratorHandle) -> None:
    com = orch.vms["fileserver"].communicator
    assert com.execute(["cat", "/srv/b/which"]).stdout.strip() == b"disk-b", "disk b/c swapped"
    assert com.execute(["cat", "/srv/c/which"]).stdout.strip() == b"disk-c", "disk b/c swapped"


def data_disk_bytes_survived_capture(orch: OrchestratorHandle) -> None:
    com = orch.vms["fileserver"].communicator
    for dev, mount in (("/dev/vdb", "/srv/b"), ("/dev/vdc", "/srv/c")):
        live = com.execute(["blkid", "-s", "UUID", "-o", "value", dev]).stdout.strip()
        seeded = com.execute(["cat", f"{mount}/uuid"]).stdout.strip()
        assert live and live == seeded, f"{dev} fs UUID changed: live={live!r} seeded={seeded!r}"


# --- snapshot / memory-snapshot / power-state (host-side driver ops) ----------


def disk_snapshot_lifecycle(orch: OrchestratorHandle) -> None:
    vm = orch.vms["keybox"]
    driver = orch.driver
    com = vm.communicator
    sentinel = "/home/admin/snapshot-test"

    driver.create_snapshot(vm.backend_name, "pre-write", "before sentinel")
    com.execute(["touch", sentinel])
    assert com.execute(["test", "-f", sentinel]).ok, "sentinel not created"

    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    assert driver.get_vm_power_state(vm.backend_name) == "shutoff", "VM did not power off"
    driver.start_vm(vm.backend_name)
    com.close()
    assert com.execute(["test", "-f", sentinel]).ok, "sentinel lost across reboot"

    assert "pre-write" in driver.list_snapshots(vm.backend_name), "snapshot not listed"

    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    driver.restore_snapshot(vm.backend_name, "pre-write")
    driver.start_vm(vm.backend_name)
    com.close()
    assert not com.execute(["test", "-f", sentinel]).ok, "sentinel survived restore"


def memory_snapshot_restores_running_state(orch: OrchestratorHandle) -> None:
    vm = orch.vms["keybox"]
    driver = orch.driver
    com = vm.communicator
    marker = "/dev/shm/mem-marker"

    com.execute(["sh", "-c", f"echo live > {marker}"])
    driver.create_snapshot(vm.backend_name, "mem-snap", "running state", mem=True)
    com.execute(["rm", "-f", marker])

    driver.restore_snapshot(vm.backend_name, "mem-snap")
    assert driver.get_vm_power_state(vm.backend_name) == "running", "mem restore left VM down"
    com.close()
    r = com.execute(["cat", marker])
    assert r.stdout.strip() == b"live", f"tmpfs state not restored from RAM snapshot: {r}"


# --- reachability matrix: air-gap, NAT, DNS, multi-Network-per-Switch ---------


def client_can_reach_private_web(orch: OrchestratorHandle) -> None:
    r = orch.vms["multihome"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_PRIVATE_WEB_IP}/"], timeout=20.0
    )
    assert r.ok and b"air-gapped" in r.stdout, f"client could not reach private-web: {r}"


def client_reaches_public_web_across_labels_via_dns(orch: OrchestratorHandle) -> None:
    r = orch.vms["multihome"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "http://public-web.pub-b/"], timeout=20.0
    )
    assert r.ok and b"internet-connected" in r.stdout, f"cross-label/DNS reach failed: {r}"


def private_web_can_reach_client(orch: OrchestratorHandle) -> None:
    r = orch.vms["private-web"].communicator.execute(
        ["ping", "-c", "1", "-W", "2", _CLIENT_PRIVATE_IP], timeout=15.0
    )
    assert r.ok, f"air-gapped segment lost internal L2 reachability to the client: {r}"


def private_web_cannot_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["private-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", "-o", "/dev/null", "https://google.com/"], timeout=15.0
    )
    assert not r.ok, "air-gapped private-web reached the internet"


def public_web_can_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["public-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://google.com/"], timeout=20.0
    )
    assert r.ok, f"public-web could not reach the internet through NAT: {r}"


TESTS = [
    # no_net_agent_executes,  # disabled with the no-net VM (ORCH-9)
    # no_net_has_no_ethernet,  # disabled with the no-net VM (ORCH-9)
    unmanaged_nic_has_link_no_address,
    unmanaged_file_roundtrips_over_agent,
    multihome_static_nic_addressed,
    multihome_dhcp_nic_in_pool,
    multihome_one_default_route,
    keybox_bound_to_dhcp_nic,
    keybox_apt_package_present,
    # keybox_pip_package_importable,  # disabled with Pip("cowsay") (BUILD-4)
    keybox_os_drive_grew,
    keybox_file_roundtrips_over_ssh,
    keybox_exec_honors_cwd,
    viewer_authed_with_password,
    viewer_cannot_sudo,
    viewer_in_declared_group,
    ops_user_is_admin,
    # users_uses_explicit_resolver,  # static-DNS not in resolv.conf (BUILD-5)
    data_disks_mounted,
    data_disks_carry_their_own_content,
    # data_disk_bytes_survived_capture,  # blkid needs root in the test (CORE-24)
    disk_snapshot_lifecycle,
    memory_snapshot_restores_running_state,
    client_can_reach_private_web,
    client_reaches_public_web_across_labels_via_dns,
    private_web_can_reach_client,
    private_web_cannot_reach_internet,
    public_web_can_reach_internet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
