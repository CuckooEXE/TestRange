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
- **NOTED — `tools/build-sidecar-image/build.sh` lists the same dead package.**
  The Alpine sidecar image's `apk add` list includes `open-vm-tools-plugins-all`,
  which also does not exist in Alpine (the real packages are `open-vm-tools` +
  `open-vm-tools-guestinfo`). The *cached* sidecar image works (it predates the
  bad line or apk tolerated it), so a fresh `build.sh` run is needed to confirm —
  left as a fix-when-rebuilding note rather than a blind, unverifiable change.

## The remaining blocker — nested-ESXi environmental limitation (M4)

The `esxi/devices.py` cert cannot reach its test assertions on a **nested** ESXi
node because the on-node Debian build never gets network. Root-caused
exhaustively (the chain of red herrings is itself the finding):

1. apt fails with `Temporary failure resolving 'deb.debian.org'` — looks like DNS.
2. The build **sidecar** resolves `deb.debian.org` perfectly (verified via
   guest-ops: `127.0.0.1`, its own eth0 `10.97.99.1`, and the upstream chain all
   answer; NAT masquerade + the L0 egress chain all work). So egress is **not**
   the problem.
3. The build **VM** has only `lo` — `ip addr` shows no `ens160` address,
   `ping 10.97.99.1` is "Network is unreachable". So it is not DNS, it is **no
   network at all**.
4. `networkd` *matched* `ens160` (correct MAC, DNS `10.97.99.1` configured) but
   the link never activated (`Current Scopes: none`, no carrier).
5. pyVmomi shows the build VM's **virtual NIC is `connected=False,
   status=unrecoverableError`** — ESXi could not connect it.
6. It is **not** a MAC duplicate (all MACs distinct) and **not** the NIC model
   (vmxnet3 *and* e1000e both fail). The discriminator: every NIC on an
   **uplink-less standard vSwitch** (`trs-*`, the isolated guest/build segment,
   `uplinks=NONE`) gets `unrecoverableError`, while NICs on an **uplinked**
   vSwitch (`tru-*`, enslaving `vmnic1`) connect `status=ok`.

**Conclusion: on ESXi-nested-on-libvirt, a VM NIC cannot connect to a standard
vSwitch that has no physical uplink.** TestRange's `_net` realizes each isolated
guest segment as exactly such an uplink-less vSwitch (the per-Switch sidecar
bridges it to the real uplink via NAT — ADR-0013/0025), so the build VM's NIC on
that segment never comes up and the on-node build has no network.

This is a property of the nested environment, **not** a TestRange code defect —
on real ESXi hardware an uplink-less vSwitch is a standard, working internal-only
switch. It is exactly the "ESXi-as-a-guest build phase is finicky" reason ESXI-16
was shelved and why the ESXi backend is certified on a **raw ESXi host** (REL-11),
not nested. A nested-only workaround (a dead-end dummy uplink per isolated
vSwitch, needing an extra vmnic) would pollute the driver for real hosts and is
out of scope.

**What WAS proven live on the nested node** (everything up to that NIC connect):
pyVmomi control plane, preflight, vSwitch + uplink-vSwitch (`vmnic1`) + portgroup
creation, datastore pool, qcow2→vmdk conversion + datastore upload, VM +
sidecar `CreateVM_Task`, the datastore-file serial sink, the build sidecar's full
DHCP/DNS/NAT stack, and guest-ops to the sidecar. The ESXi *driver* pipeline is
sound; only the nested vSwitch L2 falls short.

### Disk-bus feature — LIVE-VERIFIED (M1, the cert's actual subject)

`tests/plans/esxi/devices.py` asserts the **run-phase** disk-bus mapping, which
is independent of the (build-blocked) network: the `buses` VM has no NIC. So it
was verified directly against the leaked node, side-stepping the build — drive
the ESXi driver to create a VM from the pre-built Alpine **sidecar** image as the
OS disk (boots on ESXi via SCSI, ships open-vm-tools) plus three `ESXiHardDrive`s
(`scsi`/`sata`/`nvme`), boot it, and read `/sys/block` over VMware-Tools guest-ops
(`~/Desktop/TestRange-Adhoc/diskbus-verify.py`). Result:

```
guest block devices: [..loop.., nvme0n1, sda, sdb, sdc]
  /dev/sd*   = ['sda', 'sdb', 'sdc']   (OS-scsi + scsi + sata)   -> exactly 3 ✓
  /dev/nvme* = ['nvme0n1']             (the nvme data disk)      -> exactly 1 ✓
DISK-BUS LIVE VERIFY: PASS ✓
```

That is precisely `esxi/devices.py`'s two assertions
(`scsi_and_sata_disks_present_as_sd` → 3 on `/dev/sd*`; `nvme_disk_presents_as_nvme`
→ 1 on `/dev/nvme*`). The M1 driver feature (`create_vm` reading
`ESXiHardDrive.bus` off `spec.data_drives` and wiring LsiLogic/AHCI/NVMe
controllers) is correct **live on real ESXi managed objects**, not just against
the unit fakes. Only the cert's Debian-build prerequisite is blocked by the
nested-vSwitch limit above.
