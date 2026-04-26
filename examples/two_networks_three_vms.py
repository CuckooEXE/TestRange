"""Fully-featured TestRange example.

Provisions **two networks** and **three VMs**, then verifies connectivity:

Networks
--------
- ``Internet`` — NAT forwarding enabled, DHCP, dnsmasq DNS.
- ``Private`` — fully isolated (no forwarding), DHCP disabled (all static),
  dnsmasq DNS.

VMs
---
- ``webpublic`` (on ``Internet`` only)
    nginx serving ``<h1>Public webserver</h1>``.  DHCP address.
- ``client`` (dual-homed: ``Internet`` + ``Private``)
    ``curl`` installed.  DHCP address on ``Internet``; static
    ``10.42.2.10`` on ``Private``.
- ``webprivate`` (on ``Private`` only)
    nginx serving ``<h1>Private webserver</h1>``.  Static ``10.42.2.20``.

Checks performed from inside ``client``
---------------------------------------
1. Outbound internet:          ``curl https://www.google.com/``
   — proves NAT + DHCP-assigned DNS work on ``Internet``.
2. Public peer by FQDN:        ``curl http://webpublic.Internet/``
   — proves the ``Internet`` network's dnsmasq resolves
   ``<vmname>.<networkname>``.  The network name is used as the TLD so
   it's obvious which logical network a hostname belongs to.
3. Private peer by IP:         ``curl http://10.42.2.20/``
   — proves layer-3 reachability on the isolated ``Private`` network.

(The private webserver is addressed by IP because when a VM has multiple
interfaces and only one gets DNS via DHCP, guest resolvers are unreliable
about which network's nameserver wins.  Static addressing sidesteps that
entirely and makes the topology assertion crisp.)

Running
-------
Via the CLI::

    testrange run examples/two_networks_three_vms.py:gen_tests

Or directly::

    python3 examples/two_networks_three_vms.py
"""

from __future__ import annotations

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)


def verify_network_topology(orchestrator: Orchestrator) -> None:
    """Run three connectivity checks from inside ``client``.

    Each assertion includes the captured stderr on failure so the CLI
    traceback shows which leg of the topology broke.
    """
    client = orchestrator.vms["client"]

    # 1. Outbound internet — proves NAT via the Internet network.
    result = client.exec(
        ["curl", "-sSf", "--max-time", "15", "https://www.google.com/"],
        timeout=30,
    )
    print(result.stdout_text)
    assert result.exit_code == 0, (
        f"client -> internet FAILED (exit {result.exit_code}): "
        f"{result.stderr_text.strip()}"
    )

    # 2. Public peer by FQDN — proves Internet/dnsmasq resolves
    #    ``webpublic.Internet``. The network name acts as the TLD, so
    #    the address spells out which logical net the name belongs to.
    result = client.exec(
        ["curl", "-sSf", "--max-time", "10", "http://webpublic.Internet/"],
        timeout=20,
    )
    assert result.exit_code == 0, (
        f"client -> webpublic.Internet FAILED (exit {result.exit_code}): "
        f"{result.stderr_text.strip()}"
    )
    print(result.stdout_text)
    assert b"Public webserver" in result.stdout, (
        f"unexpected response from webpublic.Internet: {result.stdout!r}"
    )

    # 3. Private peer by IP — proves L3 reachability on the isolated network.
    result = client.exec(
        ["curl", "-sSf", "--max-time", "10", "http://10.42.2.20/"],
        timeout=20,
    )
    assert result.exit_code == 0, (
        f"client -> webprivate FAILED (exit {result.exit_code}): "
        f"{result.stderr_text.strip()}"
    )
    print(result.stdout_text)
    assert b"Private webserver" in result.stdout, (
        f"unexpected response from webprivate: {result.stdout!r}"
    )


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        name="Internet",
                        subnet="10.42.1.0/24",
                        dhcp=True,
                        internet=True,
                        dns=True,
                    ),
                    VirtualNetwork(
                        name="Private",
                        subnet="10.42.2.0/24",
                        dhcp=False,
                        internet=False,
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="webpublic",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential(username="root", password="testrange")],
                        pkgs=[Apt("nginx")],
                        post_install_cmds=[
                            "rm -f /var/www/html/index.nginx-debian.html",
                            "echo '<h1>Public webserver</h1>' > /var/www/html/index.html",
                            "systemctl reload nginx",
                        ],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(20),
                            vNIC("Internet"),
                        ],
                    ),
                    VM(
                        name="client",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential(username="root", password="testrange")],
                        pkgs=[Apt("curl")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Internet"),
                            vNIC("Private", ip="10.42.2.10"),
                        ],
                    ),
                    VM(
                        name="webprivate",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential(username="root", password="testrange")],
                        pkgs=[Apt("nginx")],
                        post_install_cmds=[
                            "rm -f /var/www/html/index.nginx-debian.html",
                            "echo '<h1>Private webserver</h1>' > /var/www/html/index.html",
                            "systemctl reload nginx",
                        ],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(20),
                            vNIC("Private", ip="10.42.2.20"),
                        ],
                    ),
                ],
            ),
            verify_network_topology,
            name="two-networks-three-vms",
        ),
    ]


if __name__ == "__main__":
    import sys

    results = run_tests(gen_tests())
    sys.exit(0 if all(r.passed for r in results) else 1)
