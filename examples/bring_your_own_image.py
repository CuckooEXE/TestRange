"""Bring Your Own Image (BYOI): boot a pre-built qcow2 without cloud-init.

The normal TestRange flow downloads a cloud base image and runs a
cloud-init install phase to inject users/packages/post-install commands.
With ``builder=NoOpBuilder()`` that flow is skipped entirely: you hand
over a qcow2 produced elsewhere (Packer, Buildroot, a manual golden
build) and the orchestrator just creates an overlay and boots it.

Because the no-op builder has no install phase:

- ``pkgs`` and ``post_install_cmds`` are silently ignored — install
  that stuff into the image yourself.
- ``users=[...]`` is *informational*: testrange treats the credentials
  as already present in the image and hands them to whichever
  communicator you select.
- The communicator is still your choice: ``communicator="ssh"`` uses the
  first static IP declared on a ``vNIC``; the default
  ``"guest-agent"`` requires you to have installed ``qemu-guest-agent``
  in your image.

The priming helper below fakes "Packer built the image" by running the
standard testrange install path **once** (via the default
:class:`CloudInitBuilder`) and saving the resulting disk.  In a real
project, replace it with your actual Packer / buildkite /
artifact-fetch step — testrange only needs a qcow2 on disk.

Run with::

    testrange run examples/bring_your_own_image.py:gen_tests
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    NoOpBuilder,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)

_GOLDEN_DIR = Path("/var/tmp/testrange-byoi")
"""Where the baked image lives.  Any directory outside ``$HOME`` works;
see the installation docs for the permission rules on custom paths."""

_GOLDEN_IMAGE = _GOLDEN_DIR / "debian-12-byoi.qcow2"
"""Final baked image the BYOI test boots."""

_BYOI_USER = Credential(
    username="deploy",
    password="byoi-demo",
    sudo=True,
)
"""Credential that must already exist inside the golden image."""


def _bake_golden_image() -> Path:
    """Produce a golden qcow2 with ``deploy`` preconfigured.

    First call runs the standard cloud-init install path to plant the
    user + sshd + qemu-guest-agent, caches the resulting disk, and
    copies it to :data:`_GOLDEN_IMAGE`.  Subsequent calls short-circuit
    on the file's existence.

    In production this would be ``packer build`` (or similar).
    """
    if _GOLDEN_IMAGE.exists():
        return _GOLDEN_IMAGE
    _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    print(f"baking golden image at {_GOLDEN_IMAGE} (first run only)...")
    # Run a one-off install test that produces the image we need.  The
    # orchestrator caches the post-install disk under
    # ``<cache>/vms/<hash>.qcow2``; we capture that path and copy it.
    from testrange.cache import CacheManager

    bake_vm = VM(
        name="byoi-bake",
        iso=(
            "https://cloud.debian.org/images/cloud/bookworm/"
            "latest/debian-12-generic-amd64.qcow2"
        ),
        users=[_BYOI_USER, Credential("root", "byoi-demo")],
        devices=[
            vCPU(1),
            Memory(1),
            HardDrive(10),
            vNIC("BakeNet"),
        ],
    )

    def _bake(orch: Orchestrator) -> None:
        # Verify the VM booted; nothing else to do — we just need the
        # cached disk as a side effect.
        assert orch.vms["byoi-bake"].exec(["true"]).exit_code == 0

    test = Test(
        Orchestrator(
            networks=[
                VirtualNetwork("BakeNet", "10.41.0.0/24", internet=True),
            ],
            vms=[bake_vm],
        ),
        _bake,
        name="byoi-bake",
    )
    result = test.run()
    if not result.passed:
        raise RuntimeError(f"baking golden image failed: {result}")

    # Pull the cached post-install disk out of the cache.  The builder
    # owns the cache-key formula now — ask it for the hash.
    cache = CacheManager()
    h = bake_vm.builder.cache_key(bake_vm)
    cached = cache.get_vm(h)
    if cached is None:
        raise RuntimeError(
            f"expected cached disk for hash {h[:12]}, got nothing"
        )
    tmp = _GOLDEN_IMAGE.with_suffix(".qcow2.part")
    shutil.copyfile(cached, tmp)
    tmp.rename(_GOLDEN_IMAGE)

    # Sanity check.
    info = subprocess.run(
        ["qemu-img", "info", str(_GOLDEN_IMAGE)],
        check=True, capture_output=True, text=True,
    )
    assert "file format: qcow2" in info.stdout, info.stdout
    return _GOLDEN_IMAGE


# Prime at import time so ``gen_tests`` has a path to hand VM(iso=...).
GOLDEN = _bake_golden_image()


def byoi_smoke(orch: Orchestrator) -> None:
    vm = orch.vms["byoi"]

    # Communicator is SSH; the Credential above must already exist in
    # the golden image (it does — the baking step created it).
    assert vm.hostname() != ""
    release = vm.read_text("/etc/os-release")
    assert "Debian" in release, release

    # sudo access is a property of the image, not something cloud-init
    # reconfigured at run time — prove we can still use it.
    result = vm.exec(["sudo", "-n", "whoami"])
    result.check()
    assert result.stdout_text.strip() == "root"


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "BYOINet",
                        "10.40.0.0/24",
                        dhcp=False,
                        internet=True,
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="byoi",
                        iso=str(GOLDEN),
                        builder=NoOpBuilder(),  # no install phase
                        communicator="ssh",
                        users=[_BYOI_USER],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("BYOINet", ip="10.40.0.10"),
                        ],
                    ),
                ],
            ),
            byoi_smoke,
            name="bring-your-own-image",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
