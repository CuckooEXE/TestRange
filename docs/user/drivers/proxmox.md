# Proxmox VE

## About

The Proxmox driver runs a portable testrange plan against a single-node Proxmox
VE host.

Install the extra:

```sh
pip install -e '.[proxmox]'
```

Everything PVE exposes over REST goes through `proxmoxer`. Two things can't ride
REST and use sanctioned side channels ([ADR-0008](../../adr/0008-driver-abc-multi-backend.md) §6):
volume bytes move over SSH/SFTP (REST has no byte-egress, and its upload
endpoint 501s on large images), and the serial build-result sink is read over
`termproxy`→`vncwebsocket` ([ADR-0012](../../adr/0012-serial-build-result.md)).
No `subprocess`/`qemu-img` — disk sizing is REST-native.

## Support level

Driver primitives — connect, SDN switches, streamed volume I/O, VM lifecycle,
snapshots, and QGA exec — are live-proven against a single-node PVE 9.x host. The
full `tests/plans/` end-to-end sweep is tracked under the 1.0.0 validation epic
(REL).

| Capability | Status |
| --- | --- |
| `tests/plans/` generic + `proxmox/` sweep (live, single-node) | in progress (REL) |
| Integration wiring | `pytest -m proxmox` → driver-primitive tests in `test_proxmox.py` (connect/SDN/storage/VM/QGA) |
| Block-storage StoragePools (lvm/zfs/ceph) | not supported (PVE-33) |
| Multi-node clusters | not supported — single-node only (PVE-31) |
| QGA chunked guest-file-write (>~45 KB single write) | deferred (PVE-45) |
| Nested-PVE installer-origin build smoke | environment-blocked (BUILD-13) |

Run the live driver-primitive suite against a host by exporting its coordinates
(it drives the driver directly, so it takes host env vars rather than a profile):

```sh
export TESTRANGE_PVE_HOST=10.0.0.5
export TESTRANGE_PVE_PASSWORD='Target123!'
export TESTRANGE_PVE_BASE_QCOW2=/path/to/debian-13.qcow2   # disk/VM tests only
pytest -m proxmox tests/integration/test_proxmox.py
```

Run the portable corpus end-to-end against the profile:

```sh
for p in tests/plans/generic/*.py tests/plans/proxmox/*.py; do
    testrange run --profile pve "$p" || break
done
```

## Connection profile

A portable plan binds to a host at run time via a connection profile
([ADR-0015](../../adr/0015-backend-binding.md)). Copy the example, drop in your
host and password, and select it by name:

```sh
cp examples/connect.toml.example connect.toml   # gitignored — it holds a password
testrange run --profile pve tests/plans/generic/lifecycle.py
```

The profile table:

```toml
[pve]
driver = "proxmox"          # required
host = "10.0.0.5"
user = "root@pam"           # optional; a bare "root" takes the @pam realm
password = "Target123!"
port = 8006                 # optional
verify_ssl = false          # optional (PVE ships a self-signed cert)
node = ""                   # optional; "" auto-detects the single node
backing_storage = "local"   # optional; the dir/nfs storage volumes land in

# SSH carries volume bytes only; it defaults to the API user/password.
# ssh_user = "root"
# ssh_password = "..."
# ssh_port = 22
```

`user` defaults to `root@pam` (a bare `root` is normalised to the `@pam` realm),
`node` auto-detects when the host has exactly one node, `backing_storage`
defaults to `local`, and SSH reuses the API credentials. So
`host` + `password` is the whole common case.

## Egress

A plan's `Switch(uplink="<name>")` resolves through the profile's
`[pve.uplinks]` map to a host bridge ([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)).
Egress is **out-of-band**: TestRange attaches a NAT sidecar to the named bridge
but never provisions it. An unmapped name fails at preflight.

```toml
[pve.uplinks]
egress = "vmbr0"            # string form: the bridge has DHCP/DNS behind it
```

Two value forms — a plain string when the bridge already has DHCP/DNS behind it,
or a table giving the sidecar a static address when the bridge host-NATs but runs
no DHCP/DNS server. See [Out-of-band egress](out-of-band-egress.md) for the
host-NAT bridge recipe and [Networking modes](networking-modes.md) for the full
`Switch` flag surface.

## Prerequisites

- **Storage with the `import` content type.** The driver imports OS disks and
  stages volumes through a `dir`/`nfs` storage's content directory, which must
  have the `import` content type enabled. Missing it surfaces at preflight as
  `proxmox-import-content-missing`. Enable it under *Datacenter → Storage →
  &lt;storage&gt; → Content*.
- **`dir`/`nfs` storage only.** Volume bytes ride SFTP into the storage content
  directory, so the backing storage must expose one. Block stores (lvm/zfs/ceph)
  have no content dir and are **not yet supported** (PVE-33) — the driver fails
  loud rather than silently mis-importing.
- **`xorriso` on the orchestrator host**, *only* for installer-origin builders
  (`ProxmoxAnswerBuilder`): it prepares the answer-seeded installer ISO
  ([ADR-0022](../../adr/0022-xorriso-installer-iso-prep.md)). `apt install
  xorriso` / `dnf install xorriso` / `brew install xorriso`. Not needed for
  cloud-init / image-origin builds.

## `mgmt` semantics (option B)

`Switch(mgmt=True)` gives the **hypervisor host** an L2 presence at `.2` on the
switch's first network ([ADR-0009](../../adr/0009-mgmt-switch-semantics.md),
ratified as option B): guests reach the host and vice-versa. On PVE this is a
per-switch SDN vnet whose subnet gateway is the host's `.2`. `.2` is a
**hypervisor-local** reachability guarantee — it is *not* promised reachable
from a remote test runner.
