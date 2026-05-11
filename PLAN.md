I want to build a Python project that will allow users to write declarative Python scripts to create VMs on hypervisors in specific configurations (i.e. network configurations, disk configurations, etc.). Then, a user-specified testing function(s) will run with access to these VMs. It's for things like CI/CD where you want to run your software against specific versions of operating systems networked in different ways.

The objective is to provide as much functionality to the user with _sane defaults_ while exposing a simple, easy to use API. There are multiple long-term goals that need to be kept in-mind while developing so we don't develop something that ends up being incompatible with future versions, but don't need to actually write code for them yet (all maintained in a detailed TODO.md that gets updated (things don't get deleted, just moved to the bottom)).

Example Plan script:

```python

_key = gen_ssh_key()

def hello(orch: OrchestratorHandle):
    # Tests raise on error - they don't return a truthy value
    vm = orch.vms["webserver"] # Gets a VMHandle by name
    p = vm.communicator.exec("uname -a")
    p.wait()
    assert p.stdout == "" # whatever it's supposed to be
    assert "trixie" in vm.communicator.read_file("/etc/hosts").decode().lower()

TESTS = [hello] # `TESTS` var is auto discovered by the runner
PLAN = Plan( # `PLAN` var is auto discovered by the runner
    LibvirtHypervisor(
        connection="qemu://session",
        networks=[
            Switch(
                "switch1",
                Network("networka", "127.31.0.0/24"), Network("networkb", "10.10.10.0/24"),
            ),
            Switch(
                "switch2",
                Network("networkc", "10.1.4.0/24"), Network("networkd", "10.10.11.0/24"),
            ),
        ],
        pools=[
            StoragePool("pool1", 32), # 32 GB, creates these pools on the hypervisor
            StoragePool("pool2", 128), # 128 GB 
        ],
        vms=[
            VM(
                name="webserver",
                devices=[
                    CPU(2), # runtime check only one call to CPU per VM
                    Memory(4096), # runtime check only one call to Memory per VM
                    HardDrive("pool1", 64),
                    LibvirtNetworkIface("networkb", driver="e1000"), # Because we specified the concrete rather than the generic, we can utilize the specific `driver` kwarg that LibvirtNetworkIface exposes
                ]
                builder=CloudInitBuilder(
                    "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2",
                    insecure_apt=True,
                    credentials=[
                        PosixCredential("root", "password"),
                        PosixCredential("myotheruser", "password", sudo=True, pubkey=_key.public),
                    ]
                    packages=[
                        Apt("nginx")
                    ],
                    post_install_commands=(
                        "echo whatever > /whatever",
                    ),
                ),
                communicator=SSHCommunicatorFactory("myotheruser"), # Orchestrator will create an SSHCommunicator for the myotheruser
            )
        ]
    )
)

if __name__ == '__main__':
    sys.exit(0 if all(run_test(t, PLAN) for t in TESTS) else 1)
```

CLI:

```
testrange --verbose --log-level <info/debug/error/warn>
testrange cache list # Lists cache entries
testrange cache add <url> # Add a URL or filepath to the cache
testrange cache del <sha> # Delete an item from the cache
testrange cleanup --all # Cleanup all orphaned resources from all existing statefiles
testrange cleanup <run id> # Cleanup all orphaned resources from a specific run
testrange run examples/hello_world.py
testrange run --leak-on-failure examples/hello_world.py # Don't clean up any resources if there is ANY exception/failure
testrange describe examples/hello_World.py # Completely passive run that pretty-prints the networks, pools, VMs, etc. of a plan
```

File layout:

```
docs/
    user/
    dev/
examples/
    hello_world.py # Simple create Debian VM and prove that we can communicate it
testrange/
    builders/ # Build ISOs of various operating systems
        base.py
        cloudinit.py
    cache/ # Writes to the XDG directories that keep state and local run ISOs and stuff
        http.py
        local.py
    communicators/ # Communicators are objects that allow remote execution to systems. The ABC can be referenced from `.bak/testrange/communicators/base.py`
        base.py
        ssh.py
    credentials/ 
        base.py
    devices/ # Each device type has an ABC and the concretes for specific hypervisors. Each specific hypervisor has special attributes. Generic device for sane defaults per-driver
        cpu/
            base.py
            generic.py
            libvirt.py
        disk/
            base.py
            generic.py
            libvirt.py
        memory/
            base.py
            generic.py
            libvirt.py
        network/
            base.py
            generic.py
            libvirt.py
        pool/
            base.py
            generic.py
            libvirt.py
    drivers/ # Exposes the API to the hypervisors (libvirt api, etc.)
        base.py # <provision/destroy>(resource), etc. Create things, destroy things, start/stop/reset VMs, etc.
        libvirt.py     
    orchestrator/ # Orchestrate the program run: update state, communicate with the driver what needs to be created, etc.
    packages/
        base.py
        apt.py
        pip.py
    state/ # State management
    types/ # Global types
    _log.py # logging
    cli.py # Argparse
    exceptions.py
tests/
    integration/
    unit/
```

Design notes:
    - Things should be built into stovepipes
        - Credentials should have no idea about packages should have no idea about drivers, etc.
        - Orchestrator should be the one reaching into the various types with knowledge of their internals
    - Phases (each phase has detailed state transitions so if a test is interrupted cleanup can go through and destroy orphaned resources)
        - Pre-Flight
            - Orchestrator checks if the hypervisor has enough resources for the plan (through the driver)
        - Install
            - Network (connected to the internet) is created for VMs to be attached
            - VMs are created one-by-one and they go through the builder process (orchestrator orchestrates it)
            - After VMs finish building their disks are exported and sent through the cache
            - VMs are torn down
        - Run
            - Networks are created
            - Storage pools are created
            - VMs are created and attached to where they need to be (with their post-install disks)
        - Test
            - User test functions are created
        - Cleanup
            - If skip cleanup then abort
            - Power off all VMs
            - Teardown vms, networks, storage pools
    - HTTP and local filesystem cache
        - `$XDG_CACHE_HOME` for local cache
        - Reads go through local cache, then HTTP cache on misses
        - Writes go through local cache, then HTTP cache to ensure it's global
        - `--cache <url>` is a global flag to specify the HTTP cache, no cache by default
    - Orchestration is the driver
        - Orchestrator takes a driver

Short-term TODOs:
- DHCP on networks
- DNS on networks
    - VMs are addressable via `<vm name>.<network name>`
- KWARG on Switches to place a management interface on them
    - Allows a VM to communicate to the hypervisor's api for example
- KWARG on switches to allow air-gapped network
    - No internet access on these switches
- Intelligent Cleanup
    - Tear down of old tests via a statefile
    - *ALL* exceptions (raised Python errors from the testrange or other package), CTRL-C, etc. trigger a cleanup (`unless --skip-on-failure` is specified) tearing down all resources
- Detailed statefile
    - A state file in `$XDG_STATE_HOME` per-run
    - Statefile contains all resources and their state (created, destroyed, etc.)
    - Statefile always contains the exact state the orchestration is at

Long-term TODOs:

- Multiple top-level Hypervisors in a plan
    - Create VMs on two different hypervisors for a single plan
- Idempotency and run continuations
    - Interrupted runs can be continued from the CLI with the state file
- Different builders
    - Proxmox
    - ESXi
    - Windows
- Different drivers
    - Proxmox
    - ESXi
    - HyperV
- Nested virtualization
    - Expose some sort of `AbstractVM` specialization that holds a Hypervisor and can host other VMs
    - Orchestration is passed onto these nested Hypervisors so they can stand up everything they need to do





```python
#!/usr/bin/env python3
"""hello_world: one libvirt VM, cloud-init bootstraps SSH + nginx, smoke-test it.

Usage:
    testrange run examples/hello_world.py
    python  examples/hello_world.py        # exits 0/1 on aggregate result

Prerequisites:
- libvirt + KVM at qemu:///system (user in `libvirt` and `kvm` groups)
- A working dnsmasq config (see docs/user-guide/install.md)

The example generates an ephemeral SSH keypair in-process; nothing is
read from `~/.ssh/` or environment variables, and the keypair never
touches the orchestrator host's filesystem.
"""

from __future__ import annotations

import sys

from testrange import (
    Orchestrator,
    Plan,
    SSHCommunicatorFactory,
    VMRecipe,
    VMSpec,
    run_tests,
)
from testrange.builders import CloudInitBuilder, CloudInitBuildSpec
from testrange.credentials import PosixCredentials, gen_ssh_key
from testrange.devices import CPU, HardDrive, Memory, VirtualNetwork, VNic
from testrange.drivers.libvirt import LibvirtDriver
from testrange.packages import Apt



# Ephemeral keypair: pubkey goes into the guest's authorized_keys via
# cloud-init; privkey is handed to paramiko in-memory via the credential.
_KEY = gen_ssh_key(comment="testrange-hello")

PLAN = Plan(
    name="hello",
    networks=[
        VirtualNetwork(
            "net0",
            "172.31.130.0/24",
            dhcp=True,
            dns=True,
            internet=True,
            mgmt=True,
        ),
    ],
    vms=[
        VMRecipe(
            spec=VMSpec(
                name="web",
                devices=[CPU(2), Memory(1024), HardDrive(8), VNic("net0")],
            ),
            builder=CloudInitBuilder(CloudInitBuildSpec(base_disk=UBUNTU_CLOUD)),
            packages=[Apt("nginx")],
            accounts=[
                PosixCredentials(
                    "ubuntu",
                    authorized_keys=_KEY.public_openssh,
                    ssh_private_key=_KEY.private_openssh,
                    admin=True,
                ),
            ],
            communicator_factory=SSHCommunicatorFactory(),
        ),
    ],
)


def cloud_init_finished(orch: Orchestrator) -> None:
    # Cloud-init runs on first boot; subsequent tests may race the package
    # install if we don't gate on its completion.
    orch.vms["web"].execute(["cloud-init", "status", "--wait"], timeout=300.0)


def nginx_is_installed(orch: Orchestrator) -> None:
    result = orch.vms["web"].execute(["dpkg", "-l", "nginx"])
    assert result.exit_code == 0, "nginx package missing"  # type: ignore[attr-defined]


def hostname_matches(orch: Orchestrator) -> None:
    result = orch.vms["web"].execute(["hostname"])
    assert result.stdout_text.strip() == "web", result  # type: ignore[attr-defined]


TESTS = [cloud_init_finished, nginx_is_installed, hostname_matches]


if __name__ == "__main__":
    backend = LibvirtDriver(uri="qemu:///system")
    results = run_tests(TESTS, PLAN, driver=backend)
    for r in results:
        print(r.report_line())
    sys.exit(0 if all(r.passed for r in results) else 1)
```