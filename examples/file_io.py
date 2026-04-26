"""End-to-end demonstration of the VM file helpers.

Exercises all four ergonomic helpers on :class:`~testrange.vms.base.AbstractVM`:

- ``write_text`` / ``read_text`` — string round-trip
- ``upload`` — host file → VM path
- ``download`` — VM path → host file (auto-creates parent directory)

Also shows the raw ``get_file`` / ``put_file`` primitives that the
helpers wrap.

Run with::

    testrange run examples/file_io.py:gen_tests
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from testrange import (
    VM,
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


def file_io(orch: Orchestrator) -> None:
    vm = orch.vms["worker"]

    # --- Text round-trip -------------------------------------------------
    vm.write_text("/root/greeting.txt", "hello from the host\n")
    echoed = vm.read_text("/root/greeting.txt")
    assert echoed == "hello from the host\n", echoed

    # --- Raw bytes round-trip --------------------------------------------
    payload = bytes(range(256))  # every byte value once
    vm.put_file("/root/bytes.bin", payload)
    assert vm.get_file("/root/bytes.bin") == payload

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # --- upload: host path → VM ---------------------------------------
        src = tmp_path / "uploaded.conf"
        src.write_text("key = value\n")
        vm.upload(src, "/etc/mock-app.conf")
        assert vm.read_text("/etc/mock-app.conf") == "key = value\n"

        # --- download: VM path → host (auto-mkdir) ------------------------
        os_release_host = tmp_path / "capture" / "os-release"
        returned = vm.download("/etc/os-release", os_release_host)
        assert returned == os_release_host
        assert os_release_host.parent.is_dir()  # auto-created
        assert b"Debian" in os_release_host.read_bytes()


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.11.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="worker",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),  # 10 GiB OS disk
                            vNIC("Net"),
                        ],
                    ),
                ],
            ),
            file_io,
            name="file-io",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
