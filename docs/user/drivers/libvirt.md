# libvirt

The libvirt driver runs a portable testrange plan against a local (or remote
`qemu+ssh`) libvirt/KVM host. It is the **certified reference backend**
([ADR-0019](../../adr/0019-libvirt-reference-backend.md)) — the real backend the
`tests/plans/` corpus and the `pytest -m libvirt` integration suite are held
against, and the worked example every other driver is built to match.

Install the extra:

```sh
pip install -e '.[libvirt]'
```

The only dependency is `libvirt-python`; it imports lazily (on connect), so the
rest of the CLI works without it. Everything goes through the libvirt API — the
driver never shells out to `virsh`/`qemu-img` for control-plane work. Volume
bytes move over the libvirt **stream API** (full-content qcow2, no backing
chains), and L2 fabric is built by the **libvirt daemon** via the network API
(no host netlink, no `CAP_NET_ADMIN`).

## Connecting

A portable plan binds to a host at run time via a connection profile
([ADR-0015](../../adr/0015-backend-binding.md)). A stock local host needs no
knobs — `qemu:///system` is reachable by any member of the `libvirt` group:

```sh
cp examples/connect.toml.example connect.toml   # gitignored
testrange run --profile libvirt-local tests/plans/generic/lifecycle.py
```

The profile table:

```toml
[libvirt-local]
driver = "libvirt"          # required
# uri = "qemu:///system"    # optional; the default. qemu+ssh://user@host/system for remote
```

`uri` defaults to `qemu:///system`. Per-run storage pools are driver-created, so
there is no `backing_storage`/`backing_pool` knob — libvirt makes a directory
pool per run and tears it down on cleanup.

## Prerequisites

- **`libvirt`-group membership, no root.** The whole flow runs unprivileged:
  membership in the `libvirt` group is what lets the driver reach
  `qemu:///system`, and the daemon does the privileged work (bridge creation,
  `dnsmasq`, NAT rules). You do not need `sudo`.
- **`libvirt-python`** (`pip install -e '.[libvirt]'`) and a running `libvirtd`
  with the QEMU/KVM driver.
- **`xorriso` on the orchestrator host**, *only* for installer-origin builders
  (`ProxmoxAnswerBuilder`, `ESXiKickstartBuilder`): it prepares the answer-seeded
  installer ISO ([ADR-0022](../../adr/0022-xorriso-installer-iso-prep.md)).
  `apt install xorriso` / `dnf install xorriso` / `brew install xorriso`. Not
  needed for cloud-init / image-origin builds.

## Named uplinks

A plan's `Switch(uplink="<name>")` resolves through the profile's
`[libvirt-local.uplinks]` map to a host bridge
([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)). Egress is
**out-of-band**: TestRange attaches the sidecar's `eth1` to the named bridge but
never provisions it. An unmapped name fails at preflight.

```toml
[libvirt-local.uplinks]
egress = "tr-egress"        # a dedicated libvirt NAT network, NOT default/virbr0
```

The recommended egress bridge is a dedicated `tr-egress` NAT network you define
once with `virsh net-define` — libvirt itself runs the `dnsmasq` lease and the
`MASQUERADE` rules, so nothing else needs standing up. See
[Out-of-band egress](out-of-band-egress.md) for the full `tr-egress` recipe
(local and `qemu+ssh` remote) and [Networking modes](networking-modes.md) for
the `Switch` flag surface.

## `mgmt` semantics (option B)

`Switch(mgmt=True)` gives the **hypervisor host** an L2 presence at `.2` on the
switch's first network ([ADR-0009](../../adr/0009-mgmt-switch-semantics.md),
ratified as option B): guests reach the host and vice-versa. On libvirt this is
the host IP on the network's bridge device itself (`<ip address>` at `.2`,
`drivers/libvirt/_net.py`) — the native fit the flag was originally designed
for. `.2` is a **hypervisor-local** reachability guarantee; on a remote
`qemu+ssh` host it is *not* promised reachable from the test runner.

## Certification status

| Capability | Status |
| --- | --- |
| `tests/plans/` generic + `libvirt/` sweep (local `qemu:///system`) | **certified** — reference backend (ADR-0019) |
| Integration suite | `pytest -m libvirt` → `tests/integration/test_libvirt.py` (live, local `qemu:///system`) |
| Remote `qemu+ssh://` | the **remote** daemon builds the bridge + `dnsmasq`/NAT (BACKEND-5); off-box guest reachability via an `SSHJumpGateway` ([ADR-0020](../../adr/0020-guest-gateway-abstraction.md)) is in progress (BACKEND-11) |
| UEFI firmware (OVMF) | certified |

Run the live integration suite against the local host (no root — just `libvirt`
group):

```sh
pytest -m libvirt tests/integration/test_libvirt.py
```

Certify the backend end-to-end by running the portable corpus against the
profile:

```sh
for p in tests/plans/generic/*.py tests/plans/libvirt/*.py; do
    testrange run --profile libvirt-local "$p" || break
done
```
