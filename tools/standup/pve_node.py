"""pve_node: stand up a Proxmox VE node as a libvirt guest, then leak it.

This is the live-certification vehicle for :class:`ProxmoxAnswerBuilder` (the
Proxmox *builder*) and — by leaving the node running — the host the Proxmox
*driver* is certified against (``tests/plans/proxmox`` + ``tests/plans/generic``).
Building the host with the builder and then certifying the *driver* against it
is two independent stovepipes (a builder proof and a driver proof), not a
GuestHypervisor self-reference.

Portable plan — it pins no backend. Bind libvirt at run time:

    testrange run --profile libvirt-local tools/standup/pve_node.py

It installs PVE 9.x installer-origin (blank ``vda`` + the prepared installer ISO
+ a ``PROXMOX-AIS`` answer seed, build-result over the serial console), reboots
into the installed system (first-boot provisions + powers off), and brings the
run-phase boot up on a static management address the libvirt host can reach. A
single ``leak`` test calls ``orch.leak()`` so the node SURVIVES ``testrange
run`` — point a ``proxmox`` profile at ``MGMT_ADDR`` (PVE API :8006, SSH :22) to
drive it as a backend.

Why each knob is load-bearing:

- ``firmware="uefi"`` — q35 + OVMF. The PVE installer is validated under UEFI,
  and q35 is what makes the in-guest NIC name ``enp1s0`` — the builder's
  ``network_interface`` default, baked into ``answer.toml``'s
  ``filter.ID_NET_NAME``. Under i440fx/BIOS the install-time NIC names differ
  and the static-network match misses. The run phase recreates the VM with
  *fresh* OVMF nvram (the build's NVRAM boot entry is not captured with the
  disk), so a post-install step ensures the removable-media fallback
  ``\\EFI\\BOOT\\BOOTX64.EFI`` exists for OVMF to boot the captured disk.
- run switch ``mgmt=True`` — puts the libvirt host at ``.2`` on the node's
  segment so the host (and the orchestrator's SSH ``wait_ready``) can reach the
  node's static ``.100``. Without it the node installs green but is unreachable.
- run switch ``uplink="egress"`` + NAT ``Sidecar`` — the node egresses to the
  internet (apt during build, and as a hypervisor for its own guests later).
- ``CPU(nested=True)`` — exposes ``vmx``/``svm`` so the node can run KVM guests
  (the whole point of certifying it as a backend).

Prerequisites:

    testrange cache add /path/to/proxmox-ve_9.2-1.iso --name pve-iso
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 \\
        --name testrange-sidecar
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import ProxmoxAnswerBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

# Deterministic key (insecure-by-design, lab only) — baked into the node's root
# authorized_keys via answer.toml and used by the SSHCommunicator.
_ROOT_KEY = SSHKey.generate(comment="testrange-pve-node")

# The node's run-phase management address. It lives in the user-static band
# (.100-.254) of the run switch CIDR so it never collides with the sidecar's
# DHCP pool (.10-.99); the libvirt host sits at .2 (mgmt=True) on the same
# segment and reaches it here. A proxmox profile points at this for API + SSH.
MGMT_CIDR = "10.55.0.0/24"
MGMT_ADDR = "10.55.0.100"
# Root login for the node — also what a `proxmox` profile uses for API + SSH
# auth. Inlined in the PosixCred below (lab credential, not a security boundary).

hyp = Hypervisor(
    build_switch=Switch(
        # Unique build-switch name (not "build") to avoid the date-scoped
        # backend-name collision with other same-day runs' build switches.
        "pvebuild",
        Network("pvebuild-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

hyp.add_pool(StoragePool("pvepool", 128))

hyp.add_switch(
    Switch(
        # Resource names are unique (not "mgmt"/"pool1") so they never
        # collide with other same-day runs: the libvirt driver scopes
        # backend names by run_id[:8] (the date), so two same-day plans
        # declaring the same pool/switch name would stomp each other
        # (ADR-0018 is single-instance; this keeps the lab robust anyway).
        "pvemgmt",
        Network("pvemgmt-net"),
        cidr=MGMT_CIDR,
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

hyp.add_vm(
    VMRecipe(
        spec=VMSpec(
            name="pve1",
            firmware="uefi",
            devices=[
                CPU(6, nested=True),
                Memory(10240),
                OSDrive(hyp.pools["pvepool"], 100),
                NetworkIface(hyp.networks["pvemgmt-net"], addr=StaticAddr(f"{MGMT_ADDR}/24")),
            ],
        ),
        builder=ProxmoxAnswerBuilder(
            installer_iso=CacheEntry("pve-iso"),
            credentials=[
                PosixCred("root", password="tr-proxmox-lab", ssh_key=_ROOT_KEY),
            ],
            # Storage-widening (`local` -> images/import) is NOT done here: the
            # PVE cluster filesystem (pmxcfs, /etc/pve) isn't online during the
            # first-boot oneshot, so `pvesm set` fails ("cfs-lock ... not online").
            # It's done at run phase over SSH instead (see the TESTS below),
            # where PVE is fully booted. First-boot only needs the pmxcfs-
            # independent UEFI fallback.
            post_install_commands=(
                # UEFI run-boot robustness: the run phase boots fresh OVMF nvram,
                # so ensure the removable-media fallback exists for the captured
                # disk (no NVRAM boot entry survives the capture). Tolerant of the
                # /EFI/{proxmox,debian} layout split; no-op if grubx64.efi is absent.
                "src=$(find /boot/efi/EFI -name grubx64.efi 2>/dev/null | head -1); "
                'if [ -n "$src" ]; then install -D "$src" /boot/efi/EFI/BOOT/BOOTX64.EFI; fi',
            ),
        ),
        communicator=SSHCommunicator("root"),
    )
)

PLAN = Plan("pve-node", hyp)


def node_is_proxmox_ve(orch: OrchestratorHandle) -> None:
    r = orch.vms["pve1"].communicator.execute(["pveversion"])
    assert r.exit_code == 0, f"pveversion failed: {r.stderr!r}"
    assert b"pve-manager" in r.stdout, f"unexpected pveversion: {r.stdout!r}"


def prepare_local_storage_for_images(orch: OrchestratorHandle) -> None:
    # Make the node usable as a testrange backend: widen the default `local`
    # (dir) storage to hold VM `images` + the `import` staging the Proxmox
    # driver's import-from path uses. Run-phase (not first-boot) because pmxcfs
    # must be online; wait for it, then set, idempotently.
    r = orch.vms["pve1"].communicator.execute(
        [
            "bash",
            "-c",
            "for i in $(seq 1 60); do pvesm status >/dev/null 2>&1 && break; sleep 2; done; "
            "pvesm set local --content backup,iso,vztmpl,images,import,rootdir,snippets",
        ],
        timeout=180.0,
    )
    assert r.exit_code == 0, f"widening `local` storage failed: {r.stderr!r}"


def local_storage_accepts_images(orch: OrchestratorHandle) -> None:
    # Confirm `local` now carries the `images` content type the Proxmox driver
    # needs for VM disks.
    r = orch.vms["pve1"].communicator.execute(["pvesm", "status", "--content", "images"])
    assert r.exit_code == 0, f"pvesm status failed: {r.stderr!r}"
    assert b"local" in r.stdout, f"`local` is not an images store: {r.stdout!r}"


def leak_node_for_certification(orch: OrchestratorHandle) -> None:
    # Retain the running node as the Proxmox driver's certification target.
    # Tear down later with `testrange cleanup <run-id>` or `virsh`.
    orch.leak()


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    node_is_proxmox_ve,
    prepare_local_storage_for_images,
    local_storage_accepts_images,
    leak_node_for_certification,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
