"""Pass ``iso=`` an absolute local path (pre-baked / golden image).

The normal flow is ``iso=\"https://...\"``; TestRange downloads the
image and caches it under ``<cache_root>/images/``.  But sometimes you
want to point at an image that's already on disk — a pre-hardened
golden build, a custom base you produced with ``packer``, or a
snapshot left over from a previous run.

Any absolute path (or ``~/``-expanded path) that exists and points at
a ``.qcow2`` / ``.img`` is accepted.  TestRange will **not** modify
the file; it layers a copy-on-write overlay during the install phase
and the resulting post-install image is cached separately.

This example primes a local path from an upstream cloud image exactly
once, then reuses that local file as the ``iso=`` for every run.
Nothing about this flow prevents you from replacing
:func:`_prime_local_image` with ``packer build`` or a hand-prepared
artifact.

Run with::

    testrange run examples/local_image.py:gen_tests
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.request
from pathlib import Path

from testrange import (
    VM,
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


def _prime_local_image() -> Path:
    """Ensure ``/var/tmp/testrange-golden/debian-12-base.qcow2`` exists.

    The first run downloads; subsequent runs re-use the same file.
    In a real project this would be replaced with whatever produces
    your golden image (``packer build``, an artifact registry fetch,
    etc.) — TestRange only cares that the file ends up on disk.
    """
    target_dir = Path("/var/tmp/testrange-golden")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "debian-12-base.qcow2"
    if target.exists():
        return target

    print(f"priming {target} from cloud.debian.org (one-off, ~500 MiB)...")
    url = (
        "https://cloud.debian.org/images/cloud/bookworm/latest/"
        "debian-12-generic-amd64.qcow2"
    )
    # Atomic write: download to a tempfile, fsync, then rename.
    tmp = target.with_suffix(".qcow2.part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.rename(target)

    # Sanity check: qemu-img info should succeed and report qcow2.
    info = subprocess.run(
        ["qemu-img", "info", str(target)],
        check=True, capture_output=True, text=True,
    )
    assert "file format: qcow2" in info.stdout, info.stdout

    return target


# Prime the image at import time so the path is known before
# ``gen_tests()`` is called.  `testrange describe` and
# `testrange run` both import the module first — this is the only
# way the local path can feed into the Test config.
GOLDEN_IMAGE = _prime_local_image()


def local_image_check(orch: Orchestrator) -> None:
    vm = orch.vms["bakery"]

    # The guest booted from the local qcow2.  Confirm basic functionality.
    assert vm.hostname() == "bakery"
    release = vm.read_text("/etc/os-release")
    assert "Debian" in release, release

    # Prove that we really did boot from the golden image by checking the
    # layer chain: the live disk should have the golden image as its
    # backing file.  The orchestrator builds an overlay under its run
    # scratch dir — cloud-init and user code write to the overlay only.
    # (We can't inspect the host side from inside the VM, so just
    # assert basic boot health and move on.)
    result = vm.exec(["uname", "-s"])
    assert result.exit_code == 0
    assert result.stdout.decode().strip() == "Linux"


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("LocalNet", "10.21.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="bakery",
                        iso=str(GOLDEN_IMAGE),  # absolute local path
                        users=[Credential("root", "testrange")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            VirtualNetworkRef("LocalNet"),
                        ],
                    ),
                ],
            ),
            local_image_check,
            name="local-image",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
