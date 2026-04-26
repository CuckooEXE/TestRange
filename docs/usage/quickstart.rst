Quickstart
==========

This guide walks through a complete test definition: two VMs on separate
networks, one with internet access and one isolated.

.. code-block:: python

    from testrange import (
        Test, Orchestrator, VM, VirtualNetwork,
        Credential, Apt, Pip, vCPU, Memory, vNIC, HardDrive,
        run_tests,
    )

    # ---------------------------------------------------------------------------
    # Define the test function
    # ---------------------------------------------------------------------------

    def my_test(orchestrator: Orchestrator) -> None:
        # Access VMs by name via the dict
        online_vm = orchestrator.vms["OnlineVM"]
        offline_vm = orchestrator.vms["OfflineVM"]

        # hostname() runs `hostname` inside the VM via the QEMU guest agent
        assert online_vm.hostname() == "OnlineVM"
        assert offline_vm.hostname() == "OfflineVM"

        # exec() runs an arbitrary command and returns (exit_code, stdout, stderr)
        result = online_vm.exec(["systemctl", "is-active", "nginx"])
        assert result.exit_code == 0, result.stderr_text

        # Low-level file I/O — raw bytes
        os_release = offline_vm.get_file("/etc/os-release")
        assert b"Debian" in os_release

        # Text helpers — UTF-8 by default
        motd = offline_vm.read_text("/etc/motd")
        offline_vm.write_text("/tmp/hello.txt", "Hello from TestRange!\n")

        # Host ↔ VM file copies
        offline_vm.download("/var/log/dpkg.log", "/tmp/dpkg.log")
        offline_vm.upload("./myapp.conf", "/etc/myapp.conf")

        # Tests pass by default — use assert to signal failures


    # ---------------------------------------------------------------------------
    # Define the test configuration
    # ---------------------------------------------------------------------------

    SSH_KEY = "ssh-rsa AAAA... your-key-here"  # optional

    tests = [
        Test(
            Orchestrator(
                host="localhost",   # or "user@remote-host" for SSH
                networks=[
                    VirtualNetwork(
                        name="NetA",
                        subnet="10.0.50.0/24",
                        dhcp=True,
                        internet=True,    # VMs can reach the internet
                        dns=True,         # "OnlineVM.NetA" resolves (FQDN only, not "OnlineVM")
                    ),
                    VirtualNetwork(
                        name="OfflineNet",
                        subnet="10.0.100.0/24",
                        dhcp=True,
                        internet=False,   # fully isolated
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="OnlineVM",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[
                            Credential("root", "Password123!", ssh_key=SSH_KEY),
                            Credential("admin", "Password123!", sudo=True),
                        ],
                        pkgs=[
                            Apt("nginx"),
                            Pip("requests"),
                        ],
                        post_install_cmds=["systemctl enable --now nginx"],
                        devices=[
                            vCPU(2),
                            Memory(2),
                            vNIC("NetA"),
                            HardDrive(20),   # 20 GiB OS disk
                        ],
                    ),
                    VM(
                        name="OfflineVM",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[
                            Credential("root", "Password123!"),
                            Credential("user1", "Password123!", sudo=True),
                        ],
                        pkgs=[Apt("curl")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            vNIC("OfflineNet", ip="10.0.100.55"),
                            HardDrive(10),   # 10 GiB OS disk
                        ],
                    ),
                ],
            ),
            my_test,
            name="two-vm-smoke-test",
        ),
    ]


    # ---------------------------------------------------------------------------
    # Run the tests
    # ---------------------------------------------------------------------------

    if __name__ == "__main__":
        results = run_tests(tests)

Run from the command line::

    # Run the tests (INFO-level logs to stderr, VM output to stdout)
    testrange run my_tests.py:tests

    # Preview the topology without booting anything
    testrange describe my_tests.py:tests

    # Inspect the disk-image cache
    testrange cache-list

    # Reset cached VM snapshots (base images are preserved)
    testrange cache-clear --yes

    # Bump verbosity when debugging a hang
    testrange run my_tests.py:tests --log-level DEBUG

    # Run four tests in parallel (each must declare its own VirtualNetwork
    # subnets; install-phase subnets are serialised automatically)
    testrange run my_tests.py:tests -j 4


While a run is in flight you'll see a timeline like this on stderr::

    INFO  testrange.backends.libvirt.orchestrator: provisioning run a9c6d044: 3 VM(s), 2 network(s)
    INFO  testrange.backends.libvirt.orchestrator: install phase for 3 VM(s) ...
    INFO  testrange.backends.libvirt.vm: VM 'webpublic' install cache hit (8a14e6a8cb0b) — skipping install phase
    INFO  testrange.backends.libvirt.orchestrator: start test network 'Internet' done in 0.1s
    INFO  testrange.backends.libvirt.orchestrator: start VM 'webpublic' ...
    INFO  testrange.backends.libvirt.vm: wait for guest agent on 'webpublic' done in 9.0s
    INFO  testrange.backends.libvirt.orchestrator: all VMs ready; handing off to test function

Every long-running step is bracketed with its elapsed time so
you can tell whether a slow run is blocked on downloads, cloud-init,
or the guest agent.


How It Works
------------

1. **First run** — TestRange downloads the Debian 12 cloud image, boots it
   with cloud-init to install packages and create users, then powers it off and
   caches the resulting disk image under ``/var/tmp/testrange/<user>/vms/``.

2. **Subsequent runs** — TestRange creates a thin qcow2 overlay over the cached
   image.  The VM boots in seconds directly into the post-install state.

3. **Communication** — All VM interaction (``exec``, ``get_file``, ``put_file``,
   ``hostname``) goes through the QEMU Guest Agent over a ``virtio-serial``
   channel.  No network port is exposed to the host.

4. **Network isolation** — VMs on networks with ``internet=False`` are on a
   libvirt "isolated" network with no forwarding rules.  They have no path to
   the host or internet during test execution.


Images
------

Pass ``iso=`` an absolute local path to a ``.qcow2`` / ``.img`` file,
or an ``https://`` URL pointing at an upstream cloud image (Debian,
Ubuntu, Fedora, Rocky, AlmaLinux, CentOS Stream, Alpine, Arch — any
distro that ships a cloud-init-ready image will work).

See :doc:`vms` for a short list of frequently-used upstream URLs.
Downloads are cached under ``<cache_root>/images/`` keyed by URL,
so the same image is only fetched once across test runs.
