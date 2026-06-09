# ESXi end-to-end findings (ESXI-20 / REL-14-style)

Discrepancy log from driving the full ESXi vertical on the local box: stand up a
nested ESXi 8.0U3b node on libvirt via `ESXiKickstartBuilder`, leak it, and
certify the ESXi **driver** against it with `tests/plans/`. Each finding is a
bug/surprise with its diagnosis and disposition (fixed here, or filed).

Environment: libvirt L0 (`qemu:///system`), nested KVM on, `tr-egress` libvirt
NAT (192.168.199.0/24) as the out-of-band egress uplink. Standup scaffolding in
`~/Desktop/TestRange-Adhoc/` (operator scratch, not committed): `esxi-standup.py`
(the node plan), `esxi-diag.py` (manual-phase bring-up that leaks on bring-up
failure), `node-ready.py` (post-boot IP discovery + datastore/sshd readiness).

## Builder audit (M1)

- **ESXiKickstartBuilder is complete**, not a stub — every ABC method + the
  build-result contract are honored. **BUILD-15 / BUILD-16 are stale**: the
  control-char rejection and the dropped disk floor they describe are already
  in-code. → move both to Done.
- **FIXED — keyless-root + `enable_ssh=True` footgun.** SSH is ESXi's only
  run-phase channel (no host guest-agent); a root cred with no `ssh_key` + the
  default `enable_ssh=True` bakes an image with no `authorized_keys` and no sshd,
  yet `wait_ready` still probes SSH and hangs the full 300 s. Now a fail-loud
  construction guard. (`builders/esxi.py`)

## Driver gap the cert needs (M1)

- **FIXED — `ESXiHardDrive.bus` was ignored.** `create_vm` hardcoded every data
  disk onto the single LsiLogic SCSI controller, so `sata`/`nvme`/`ide` all
  enumerated as `/dev/sd*` — defeating `tests/plans/esxi/devices.py`. Now
  `create_vm` reads each disk's bus off `spec.data_drives` and attaches it to a
  per-bus controller (LsiLogic / AHCI / NVMe / IDE-201), kept ESXi-stovepiped
  (the `HypervisorDriver.create_vm` ABC carries no bus; the orchestrator is
  untouched). (`drivers/esxi/_vm.py`)

## Standup findings (M3)

- **FIXED — a modest-disk install leaves NO datastore.** ESXi 8 on a 48 GiB disk
  gave `OSDATA = 39.75 GiB` and **no local VMFS datastore** (`host.datastore ==
  []`); on a 96 GiB disk OSDATA ballooned to 87.75 GiB — ESX-OSData expands to
  fill the disk, so the installer's leftover-space VMFS is never created and the
  ESXi driver's `create_pool` (which folds a pool into an existing datastore) has
  nowhere to go. Fix: add `systemMediaSize=min` to the installer kernelopt (caps
  ESX-OSData) and size the node disk with headroom. **Two bugs had to be fixed
  for this to take:** (a) the kernelopt rides the patched BOOT.CFG, not the
  ks.cfg, so it was folded into `config_hash`; (b) `prepare_boot_media` keyed the
  prepared-ISO cache *only* on the kickstart digest, so the kernelopt edit reused
  a stale ISO and `systemMediaSize` never reached the installer (the extracted
  BOOT.CFG proved it absent) — fixed to key on the kernelopt too. **Verified
  live:** an 80 GiB node now installs `OSDATA = 23.75 GiB` + `datastore1 = 47.75
  GiB` (46.3 GiB free > the cert's 32 GiB pool). (`builders/_esxi_prepare.py`,
  `builders/esxi.py`)

- **OPEN (sidestepped) — ESXI-18: vmk0 keeps the install-time build-NIC MAC.**
  Live-confirmed: a freshly-installed node settles at the DCUI on its lab DHCP
  lease, but `vmk0`'s MAC is the **build-NIC** MAC (seen via the DCUI IPv6
  link-local EUI-64 and the lab sidecar lease), **not** the run NIC's MAC the
  orchestrator polls — so `discover_ip` times out (`did not acquire a DHCP lease
  on lab-net`). The shipped builder fix (set `Net.FollowHardwareMac=1` + reboot
  from `local.sh`) **does not take**: `FollowHardwareMac` is consulted only at
  `vmk` *creation*, and a plain reboot **restores** `vmk0` from `esx.conf` with
  its pinned (build) MAC rather than re-creating it — so the flag never moves an
  existing `vmk0`. sshd also stays off (the same `%firstboot`/`local.sh` block
  that should enable it is gated behind the reboot that never achieves boot-2).
  → **ESXI-18 ticket updated with this diagnosis.** A real fix must DELETE +
  re-add `vmk0` (so it adopts the uplink MAC), or set `vmk0`'s MAC explicitly to
  the uplink pNIC's. Sidestepped for the cert: the node is reached at its actual
  DHCP IP via pyVmomi (the cert's primary transport is VMware-Tools/pyVmomi, not
  SSH lease-discovery), and sshd is enabled host-side via pyVmomi
  (`node-ready.py`).

## Cert findings (M4)

Run via `testrange --no-dashboard run --profile connect.toml:esxi-nested <plan>`
against the leaked node (10.50.0.85, datastore1).

- **The full ESXi build pipeline works on the nested node.** `esxi/devices.py`
  drove it end to end: pyVmomi connect → preflight → create the isolated build
  vSwitch + the **uplink vSwitch enslaving vmnic1** → datastore pool → qcow2→vmdk
  convert (`_diskconvert`) → upload to datastore1 → create + boot the build VM +
  sidecar. The disk-bus controllers wire correctly: the run VM enumerated `sda`
  on the LsiLogic controller and `sdb`/`sdc` on the AHCI controller (`sd 2:0:0:0`
  vs `sd 32:0:*` in the guest serial), plus the nvme disk — the M1 disk-bus
  feature, live.
- **Nested NAT egress works (triple-NAT).** The build VM's `apt-get update`
  succeeded through build-VM → on-node NAT sidecar → vmnic1 → L0 egress-uplink
  segment → L0 NAT sidecar → `tr-egress` → internet. This is what env-blocked
  ESXI-11 — the local `tr-egress` path clears it.
- **FIXED — `esxi/devices.py` named a non-existent package.** The build failed
  with `apt-get install -y open-vm-tools-plugins-all exited 100`. That package
  does **not** exist in Debian trixie ("No such package"); the base
  `open-vm-tools` already ships the guest-ops vix plugin
  (`…/plugins/common/libvix.so`), so the plan now installs just `open-vm-tools`.
  The plan was authored for the (shelved) ESXi cert and never run live, so the
  bad name was latent. (`tests/plans/esxi/devices.py`)
