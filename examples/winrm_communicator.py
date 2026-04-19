"""End-to-end Windows test: install a Windows ISO, then talk over WinRM.

TestRange's Linux flow ships a cloud image that's already installed and
layers cloud-init on top.  Windows cloud images don't exist, so the
orchestrator instead boots the installer from the ISO, feeds it an
autounattend answer file, lets Setup run to completion, and caches the
resulting disk.  Subsequent runs overlay the cache and take seconds.

Prerequisites (once per host):

- A Windows 10/11 install ISO on disk.  TestRange stages it into
  ``<cache_root>/images/iso-<sha>.iso`` on first use.
- ``libvirt`` + ``qemu`` with OVMF (UEFI firmware).  The orchestrator
  points at ``/usr/share/OVMF/OVMF_CODE_4M.fd`` and
  ``/usr/share/OVMF/OVMF_VARS_4M.fd``; on Debian/Ubuntu these come from
  the ``ovmf`` package.
- Network egress on first run so ``virtio-win.iso`` can be downloaded
  from ``fedorapeople.org`` (about 800 MiB).  Cached forever after that.
- ``pywinrm`` installed: ``pip install testrange[winrm]``.

The first run takes 15-30 minutes (Windows Setup is not fast).  After
the post-install image lands in the cache, subsequent runs take
roughly as long as booting the VM + WinRM handshake — a couple of
minutes.

Set ``TESTRANGE_WIN_ISO`` to the absolute path of your install ISO and
run with::

    TESTRANGE_WIN_ISO=/srv/iso/Win10_21H1_English_x64.iso \\
        testrange run examples/winrm_communicator.py:gen_tests
"""

from __future__ import annotations

import os
import sys

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


def _require_iso() -> str:
    """Return the Windows ISO path or exit with a helpful message."""
    iso = os.environ.get("TESTRANGE_WIN_ISO")
    if iso and os.path.isfile(iso):
        return iso
    print(
        "error: set TESTRANGE_WIN_ISO to an absolute path pointing at a "
        "Windows 10 / 11 install ISO.  TestRange does not ship one — "
        "Microsoft does not publish stable download URLs.",
        file=sys.stderr,
    )
    sys.exit(2)


_ISO_PATH = _require_iso()
"""Evaluated at import time so ``testrange describe`` surfaces missing ISOs
before spinning anything up."""


def windows_smoke(orch: Orchestrator) -> None:
    vm = orch.vms["winbox"]

    # Hostname round-trips through the QEMU guest agent MSI installed
    # during the autounattend FirstLogonCommands.  The sanity check
    # that Windows actually finished setup and the tools are wired up.
    hn = vm.hostname()
    assert hn.upper() == "WINBOX", f"unexpected hostname: {hn!r}"

    # exec() → cmd.exe → WinRM.  ``ver`` prints the Windows version
    # string on a new line.
    ver = vm.exec(["cmd", "/c", "ver"])
    ver.check()
    ver_line = ver.stdout.decode(errors="replace").strip()
    assert "Windows" in ver_line or "Microsoft" in ver_line, ver_line

    # Prove the user accounts from the autounattend landed in the SAM.
    # ``net user <name>`` exits 0 for known accounts and 2 for unknown.
    for user in ("Administrator", "deploy"):
        r = vm.exec(["net", "user", user])
        r.check()

    # File round-trip via WinRM's SFTP-equivalent: base64-chunked
    # PowerShell writes.  1 KiB is enough to cross the small-frame /
    # large-frame boundary in ``put_file``.
    payload = bytes(range(256)) * 4  # 1 KiB, every byte value represented
    vm.put_file("C:\\\\Windows\\\\Temp\\\\canary.bin", payload)
    assert vm.get_file("C:\\\\Windows\\\\Temp\\\\canary.bin") == payload


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "WinNet",
                        "10.60.0.0/24",
                        dhcp=True,
                        internet=True,  # Windows Update / optional egress
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="winbox",
                        iso=_ISO_PATH,  # stages into cache if needed
                        users=[
                            # The root credential sets the built-in
                            # Administrator password (by convention in
                            # WindowsUnattendBuilder).
                            Credential("root", "TR-Admin!2026"),
                            Credential(
                                "deploy", "TR-Deploy!2026", sudo=True
                            ),
                        ],
                        devices=[
                            vCPU(2),
                            Memory(4),
                            HardDrive(40),
                            # Static IP so the WinRM communicator can
                            # find the VM without relying on DHCP lease
                            # discovery (not yet implemented).
                            VirtualNetworkRef("WinNet", ip="10.60.0.10"),
                        ],
                        # communicator= defaults to "winrm" for Windows
                        # ISOs; passed explicitly here for documentation.
                        communicator="winrm",
                    ),
                ],
            ),
            windows_smoke,
            name="windows-winrm-e2e",
        ),
    ]


if __name__ == "__main__":
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
