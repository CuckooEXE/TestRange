# Proxmox end-to-end certification — findings log

Live certification of the Proxmox **builder** (`ProxmoxAnswerBuilder`) and
**driver** (`testrange.drivers.proxmox`) against a real PVE node, per REL-12 /
REL-15 / BUILD-13 (tracked as PVE-57 / PVE-58).

**Method.** The node is stood up by `examples/pve_node.py` —
`testrange run --profile libvirt-local examples/pve_node.py` — which installs PVE
9.x as a libvirt guest (installer-origin, UEFI/q35), brings the run boot up on a
host-reachable static management address (`10.50.0.100`), and `leak()`s it so it
survives as the driver's certification target. The driver is then certified by
looping the corpus against it:

```sh
for p in tests/plans/generic/*.py tests/plans/proxmox/*.py; do
    testrange run --profile pve-live "$p" || echo "FAIL: $p"
done
```

`pve-live` is the profile in `connect.toml` bound to the leaked node.

Building the host with the *builder* and certifying the *driver* against it are
two independent stovepipes (a builder proof and a driver proof), not a
GuestHypervisor self-reference — so this is non-circular, and it exercises both
halves at once.

---

## Findings

Each finding: symptom → root cause → fix → ticket. Severity: **blocker**
(cert can't proceed), **bug** (a discrepancy from the contract), **nit**.

### F1 — `ProxmoxAnswerBuilder` first-boot cannot configure PVE storage — **bug (builder integration)** — FIXED

- **Symptom.** A `pvesm set local --content …` placed in `post_install_commands`
  failed the build: `update storage failed: cfs-lock 'file-storage_cfg' error:
  pve cluster filesystem not online` (rc 2), aborting the otherwise-successful
  install (the installer selected `/dev/vda`, partitioned, installed PVE; the
  first-boot network-flip DHCP'd fine).
- **Root cause.** The PVE first-boot hook (`proxmox-first-boot`, ordered
  `network-online`) runs before `pve-cluster`/pmxcfs has mounted `/etc/pve`, so
  any `pvesm`/`pvesh` storage-config write (which takes a cfs lock) fails. This
  is intrinsic to *any* PVE config write from the first-boot oneshot, not
  specific to storage.
- **Fix.** Don't configure PVE cluster state from first-boot. `examples/pve_node.py`
  moves the `local`-storage widening to the **run phase** over SSH (a TEST step
  that waits for `pvesm status` to answer, then sets), where PVE is fully booted
  and pmxcfs is online. First-boot keeps only pmxcfs-independent work (the UEFI
  removable-media fallback).
- **Ticket.** PVE-57. (Plan-level; no driver/builder code change — the builder's
  `post_install_commands` contract is "shell run in first-boot," and first-boot
  pmxcfs unavailability is a PVE fact a plan author must work around. Considered
  documenting it on the builder; see PVE-57 notes.)

### F2 — date-scoped backend names collide across same-day runs — **nit (env / single-instance boundary)** — worked around

- **Symptom.** With a leaked lab from another same-day run present, the run phase
  failed two ways in succession: `pool 'tr-pool-20260609-pool1' already exists`,
  then (after renaming the pool) `Network is already in use by interface virbr2`
  — a leftover network already held the node's `10.50.0.0/24` mgmt subnet.
- **Root cause.** `libvirt._naming.compose_resource_name` scopes backend names by
  `run_id[:8]` — the **date** — so two same-day plans declaring the same pool /
  switch / network name produce the *same* libvirt object, and same-CIDR
  `mgmt=True` switches fight for the host `.2` adapter. This is the ADR-0018
  single-instance boundary (one run per profile is supported); the collisions
  only appear when that's violated by concurrent leaked labs.
- **Fix.** `examples/pve_node.py` uses unique resource names (`pvebuild`,
  `pvemgmt`, `pvepool`) and a private mgmt subnet (`10.55.0.0/24`) so the lab is
  robust against other same-day leaked runs sharing the host. Not a driver bug —
  date-scoping is the intended single-instance design; noted for lab authors who
  run multiple leaked hypervisors side by side.

### F3 — libvirt UEFI domains enable Secure Boot, blocking captured-disk run boot — **bug (libvirt driver)** — FIXED

- **Symptom.** The PVE node built and captured fine, but the run-phase UEFI boot
  never came up: serial showed `error: prohibited by secure boot policy`, then
  `wait_communicators_ready` timed out (`SSH connect to <node>:22 ... Unable to
  connect`).
- **Root cause.** `libvirt._vm._os_xml` emitted `<os firmware='efi'>` with no
  Secure-Boot feature, so libvirt's auto-descriptor selected a **Secure-Boot**
  OVMF with pre-enrolled MS keys. A TestRange UEFI VM boots a *captured*
  installer-built disk with *fresh* per-domain EFI vars (no NVRAM boot entry
  survives capture) and relies on the removable-media fallback
  `\EFI\BOOT\BOOTX64.EFI` (`grubx64.efi`, unsigned for the MS chain) — which
  SB-OVMF rejects.
- **Fix.** `_os_xml` now emits `<firmware><feature enabled='no'
  name='secure-boot'/></firmware>` for UEFI domains, selecting a non-SB OVMF.
  Signed images still boot (SB-off is permissive), so the cloud-image UEFI path
  is unaffected. New unit test `test_uefi_os_uses_q35_efi_with_secure_boot_disabled`.
- **Ticket.** PVE-57. Cross-stovepipe (a libvirt-driver fix surfaced by the PVE
  builder's UEFI requirement); stays inside the libvirt driver.

### F4 — node `/etc/resolv.conf` retains the build sidecar's DNS — **nit (builder)** — not blocking

- **Symptom.** On the run-booted node, `/etc/resolv.conf` reads `nameserver
  10.97.99.1` / `search pvebuild-net` — the *build* switch's sidecar, gone at run.
- **Root cause.** The first-boot network-flip DHCPs off the build sidecar for apt
  and that writes `/etc/resolv.conf`, which is captured into the image; the run
  boot's static (`dns = <run sidecar>`) is applied via `/etc/network/interfaces`
  but doesn't necessarily rewrite the captured `resolv.conf`.
- **Impact.** None observed — name resolution + egress work on the node (PVE
  9.2.2; `ping 8.8.8.8` + `download.proxmox.com` resolve/connect over IPv4), and
  inner cert guests resolve via their own SDN sidecar, independent of the node's
  `resolv.conf`. Logged for builder hygiene; fix candidate: have the first-boot
  footer restore a run-appropriate `resolv.conf` (or `rm` it so the run boot
  regenerates it) before `sync`.

<!-- Driver discrepancies from the cert-corpus sweep get appended below. -->
