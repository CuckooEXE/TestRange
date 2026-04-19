"""Drop a custom service config from the host into the VM and verify it applied.

A common integration-test pattern: the base VM is built once with a
default package install (``nginx``), and per-test configuration is
uploaded at runtime.  This means the expensive part (installing
packages) stays in the cache, but the tests themselves can vary the
config freely without busting the cache hash.

The flow here:

1. Upload a custom ``index.html`` via :meth:`VM.write_text`.
2. Upload a custom nginx site config via :meth:`VM.upload`.
3. Reload nginx, curl localhost, assert the custom content comes back.

Run with::

    testrange run examples/service_config.py:gen_tests
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    VirtualNetworkRef,
    run_tests,
    vCPU,
)


def deploy_and_verify(orch: Orchestrator) -> None:
    web = orch.vms["web"]

    # Build the custom web root.
    web.exec(["mkdir", "-p", "/srv/custom"]).check()
    web.write_text("/srv/custom/index.html", "<h1>custom content</h1>\n")

    # Ship the site config from the host.
    with tempfile.TemporaryDirectory() as tmp:
        host_conf = Path(tmp) / "custom.conf"
        host_conf.write_text(
            "server {\n"
            "    listen 8080 default_server;\n"
            "    root /srv/custom;\n"
            "    index index.html;\n"
            "}\n"
        )
        web.upload(host_conf, "/etc/nginx/sites-enabled/custom.conf")

    # Remove the stock default so our server is the one on 8080.
    web.exec(["rm", "-f", "/etc/nginx/sites-enabled/default"]).check()
    web.exec(["systemctl", "reload", "nginx"]).check()

    r = web.exec(["curl", "-sSf", "http://127.0.0.1:8080/"])
    r.check()
    assert b"custom content" in r.stdout, r.stdout


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.15.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="web",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        pkgs=[Apt("nginx"), Apt("curl")],
                        post_install_cmds=["systemctl enable --now nginx"],
                        devices=[
                            vCPU(2),
                            Memory(1),
                            HardDrive(20),  # 20 GiB OS disk
                            VirtualNetworkRef("Net"),
                        ],
                    ),
                ],
            ),
            deploy_and_verify,
            name="service-config",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
