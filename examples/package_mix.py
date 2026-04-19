"""Mixing native (Apt) and non-native (Pip) packages in one VM.

Cloud-init installs ``nginx`` via the native ``packages:`` path and
``requests`` via a ``runcmd`` pip install; this example verifies
both ended up on the image and that the post-install command to
enable nginx ran.

The build hash folds in the full package list, so this VM gets its
own cache entry distinct from the other examples.

Run with::

    testrange run examples/package_mix.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Pip,
    Test,
    VirtualNetwork,
    VirtualNetworkRef,
    run_tests,
    vCPU,
)


def package_mix(orch: Orchestrator) -> None:
    vm = orch.vms["appbox"]

    # nginx came in via the native apt path + post_install_cmds.
    active = vm.exec(["systemctl", "is-active", "nginx"]).check()
    assert active.stdout_text.strip() == "active"

    # requests came in via the pip runcmd path.
    import_test = vm.exec(["python3", "-c", "import requests; print(requests.__version__)"])
    import_test.check()
    assert import_test.stdout_text.strip()  # some version string

    # Default nginx index page — proves the server is listening locally.
    curl = vm.exec(["curl", "-sSf", "http://127.0.0.1/"])
    curl.check()
    assert b"nginx" in curl.stdout.lower() or b"welcome" in curl.stdout.lower()


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.12.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="appbox",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        pkgs=[
                            Apt("nginx"),
                            Apt("python3-pip"),
                            Pip("requests"),
                        ],
                        post_install_cmds=["systemctl enable --now nginx"],
                        devices=[
                            vCPU(2),
                            Memory(2),
                            HardDrive(20),  # 20 GiB OS disk
                            VirtualNetworkRef("Net"),
                        ],
                    ),
                ],
            ),
            package_mix,
            name="package-mix",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
