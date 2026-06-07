# ADR-0021: Nested virtualization via recursive orchestration

Status: Accepted
Date: 2026-05-31

## Context

A TestRange `Plan` describes one `Hypervisor` (portable topology: networks,
pools, VMs) bound to a backend at run time via `--profile`. A `VMRecipe` is a
leaf: `spec` (hardware) + `builder` (how to install) + `communicator` (how to
talk to it). The orchestrator runs a single linear pipeline — preflight → build
→ run → test → teardown — over one driver.

We want a guest that is itself a hypervisor: an L1 host (libvirt) that runs its
own inner plan of L2 guests, brought up *automatically* as part of the outer
run. PLAN.md previously declared nesting "out of scope for v0, designed fresh
when it lands." This is that design.

The constraint that shapes everything: **libvirt is the only backend we can
build today**, and the libvirt driver already emits `<cpu mode='host-passthrough'/>`
(`drivers/libvirt/_vm.py`), so an L1 guest already sees `vmx`/`svm`. The
orchestrator pipeline is driven by an immutable `RunContext` with no global
state, so a second run against a second driver composes cleanly. Nesting is
therefore not a new execution model — it is **the existing pipeline, recursed**.

## Decision

### 1. A nested hypervisor is a `VMRecipe` subclass, in `Hypervisor.vms`

`GuestHypervisor(VMRecipe)` adds one field, `inner: Hypervisor` — the L1 plan
(its own `networks`/`pools`/`vms`). It lives in the existing `Hypervisor.vms`
list (`Sequence[VMRecipe]`). Because it *is* a `VMRecipe`, the build, run, and
communicator-bind phases handle it as an ordinary VM through its shared
`.spec`/`.builder`/`.communicator` surface with **no changes**. The only code
that knows about nesting is the new `nested_phase`, which selects entries with
`isinstance(vm, GuestHypervisor)`. The subtype is the discriminator.

A `.libvirt(...)` classmethod is the ergonomic front door: it fills a
`CloudInitBuilder` with the qemu/libvirt package set, an `SSHCommunicator` for
the admin user, and wraps an inner `LibvirtHypervisor`, so the common case
needs no hand-written package list.

`LibvirtHypervisor` (the existing top-level scheme marker) is reused as the
**inner** topology container: it pins the inner backend to libvirt, which is
correct — we just installed libvirtd into the guest.

### 2. Orchestration recurses against a synthesized inner binding

After the outer (L0) run phase brings the guest-hypervisor VM up and binds its
communicator, `nested_phase` does, per `GuestHypervisor`:

1. **Readiness gate** — wait until libvirtd answers in the guest (`virsh list`
   over the bound communicator), the nested analogue of `await_guest_readiness`.
2. **Synthesize the inner binding** — from the *running* guest: its discovered
   IP (`run_phase.discover_ip`) and the SSH key the `CloudInitBuilder` already
   baked. Build a `LibvirtProfile`/`LibvirtDriver` in-process (no TOML) for
   `qemu+ssh://<admin>@<L1-ip>/system`, with the ssh transport pointed at the
   baked key. **The inner profile is derived from the L1 guest, not supplied by
   `--profile`** — that is what makes nesting automatic.
3. **Recurse** — `inner_plan = Plan("<outer>.<host>", vm.inner)`; enter a full
   inner `Orchestrator(inner_plan, profile=<synthesized>, require_cache=True,
   cache_manager=<the shared one>)`. The inner run reuses the entire pipeline
   unmodified — to it, this is an ordinary remote-libvirt run that happens to be
   talking to a libvirtd living inside one of the outer run's VMs.
4. **Expose** the inner handle as `OrchestratorHandle.nested["<host>"]`, a
   `NestedHandle` wrapping the inner handle (`.vms`/`.driver`/`.run_id`) plus a
   `.host` `VMHandle` for the L1 guest itself.

Recursion is depth-agnostic by construction: an inner plan may itself contain a
`GuestHypervisor`, and the inner `Orchestrator` runs its own `nested_phase`. We
impose no artificial cap (see Consequences for the depth-2 reality).

The inner plan namespace `"<outer>.<host>"` keeps `compose_mac` and cache keys
disjoint from the outer plan's, since both already key on `plan_name`.

### 3. Inner VM disks always build on L0; the inner run is upload-and-boot

Inner VM disks are built during the **outer** build phase, on the **L0**
backend, into the shared `CacheManager`, alongside L0 VMs. The outer build phase
enumerates each `GuestHypervisor`'s inner VMs (flattened, namespaced) and builds
their disk sets there. The inner `Orchestrator` then runs with
`require_cache=True`: its run phase uploads the cached disks into the L1 guest's
libvirt pool and boots — **no nested build boot, no L1 build infrastructure**.

This is a deliberate speed choice. It also dissolves the hardest networking
problem: build-time egress (apt/pip) is served by the L0 build switch/sidecar,
which has real egress. Inner egress is therefore needed *only* if an inner test
wants the internet at run time.

### 4. Egress is the wrapping L0 sidecar — no synthesized egress

The inner plan declares its networking the normal portable way: a `Switch` with
`Sidecar(nat=True)` and `uplink="egress"`. The inner uplink resolves to a bridge
on the guest hypervisor that the `GuestHypervisor` builder provisions. Runtime
egress for an inner VM is then plain chained NAT:

```
inner VM → inner sidecar (NAT) → host-a bridge → wrapping L0 Switch+Sidecar (NAT) → real world
```

There is **no driver-level egress magic** — no synthesized `virbr0` mapping, no
manufactured NAT rules. Egress works because the guest hypervisor is itself a
guest on an L0 network that already has NAT egress, and because we built the
guest hypervisor, so we own the bridge the inner uplink names. The "wrap" is the
ordinary L0 `Switch`+`Sidecar(nat)` the guest hypervisor sits on.

## Consequences

- The execution model is unchanged: nesting reuses preflight/build/run/teardown
  wholesale. The new surface is `GuestHypervisor`, `nested_phase`, the
  programmatic inner `LibvirtProfile`, and `OrchestratorHandle.nested`.
- Inner VM disks build once and cache like any VM; reruns are cache hits.
- Teardown is LIFO: inner `__exit__` first, then the outer teardown destroys the
  L1 guest. Because all inner state lives inside the L1 VM, destroying the guest
  reclaims it — inner teardown is a fast-path/forget for clean state and
  `--leak-on-failure` semantics, not a correctness requirement.
- `CPU(count, nested=True)` is a portable "must run hardware-accelerated VMs"
  knob; libvirt already passes the host CPU through. For a **local** L0 the
  driver's preflight verifies the host has nested KVM enabled and fails loud
  early; for a **remote** L0 the host's sysfs isn't reachable over the libvirt
  API yet (BACKEND-5), so preflight `warning`-logs that it cannot verify nesting
  rather than asserting it — the check is local-L0-only for now.
- The inner `qemu+ssh` binding requires the L1 guest be SSH-reachable *directly*
  from the orchestrator host (true for local libvirt: guests are directly
  routable, `guest_gateway()` is `None`) and the admin user in the
  `libvirt`/`kvm` groups. A guest bound through a gateway (remote L0) is rejected
  loudly — the direct inner dial can't route through the jump.
- **Depth-2 is unsupported, and we reject it loudly** (CI-8). The recursion
  structure permits arbitrary depth and — to my initial surprise — the *build*
  path does too; the wall is **L2 guest reachability** (see findings below). Both
  `build_phase` and `run_nested_phase` now refuse a plan whose nested host itself
  contains a nested host (`reject_unsupported_nesting`), before any backend work,
  rather than building three disk sets and timing out opaquely at L2.

## Depth-2 findings (CI-8, run 2026-05-31)

A doubly-nested plan was run on real libvirt:
`outer (L0) → host-a (L1) → host-b (L2) → leaf (L3)`. Observed, empirically:

- **The build path works at depth 2.** `build_nested_inner_vms` recurses
  correctly: all three disks (host-a, host-b, *and* the L3 leaf) built on L0 and
  cached, under namespaces `depth2`, `depth2.host-a`, `depth2.host-a.host-b`.
  My pre-experiment hypothesis — that an L2 disk would need an L0→L1→L2 upload the
  single-hop `qemu+ssh` binding couldn't do — was **wrong**: because every inner
  disk builds on L0 and the inner run only uploads to its *immediate* host, no
  multi-hop upload is ever attempted.

- **The wall is reachability of the L2 guest.** host-a (L1) came up, the inner
  run connected over `qemu+ssh`, the nested sidecar served DHCP, and host-b (L2)
  booted on host-a and leased `192.168.50.28` on host-a's *internal* network. The
  inner run then bound host-b's `SSHCommunicator` to `192.168.50.28` and the
  bring-up died:

  ```
  OrchestratorError: vm 'host-b' communicator not ready within 120s
    (native guest agent or SSH unreachable):
    SSH connect to 192.168.50.28:22 as admin failed ... timed out
  ```

  The orchestrator has no route to `192.168.50.28` — it lives on a libvirt
  network *inside* host-a. The inner `LibvirtDriver` returns
  `guest_gateway() = None` (it assumes co-located/direct routability, true for L1
  on the mgmt'd lab switch but false for L2), so the `SSHCommunicator` dials the
  address directly and times out. This is precisely the ADR-0020 `GuestGateway`
  gap, un-wired for the nested case.

- **What depth-2 would need (not built):** the inner driver would have to expose
  a `GuestGateway` that SSH-jumps through host-a so the orchestrator reaches L2
  guests (and analogously tunnel the native-agent channel for `NativeCommunicator`
  L2 guests through host-a's control plane). Both are real work in the driver/
  gateway layer; deferred. The single-instance/`run_id[:8]`-by-date naming
  (ADR-0018) is also a latent collision for rapid same-day reruns, surfaced
  incidentally during this work — orthogonal, noted, not addressed here.

### Gateway experiment (BACKEND-11, run 2026-05-31)

We wired the first ingredient — `LibvirtDriver.guest_gateway()` returns an
`SSHJumpGateway` through the `qemu+ssh` host for a remote connection (ADR-0020) —
and re-ran depth-2 to see how far it gets. It is **necessary but not sufficient**;
it advances one hop and reveals a chain:

1. **Jump establishes, final hop fails.** With the gateway, host-b's
   `SSHCommunicator` binds "via gateway" and the SSH jump to host-a is
   established — but the `direct-tcpip` channel to `192.168.50.28:22` fails with
   `ChannelException(2, 'Connect failed')`. host-a's *host OS* has no route to the
   inner network: the inner switch had no `mgmt`, so host-a carries no host
   adapter on it (the ADR-0009/0020 ".2 reach depends on mgmt(B)" caveat,
   recursed).
2. **Adding `mgmt=True` to the inner switch trips a nested-host libvirt gap.** The
   inner sidecar then fails to start on host-a:
   `Failed to create file '/var/lib/libvirt/dnsmasq/virbr1.macs.new': No such
   file or directory` — host-a's libvirt has no dnsmasq state dir for a
   host-IP'd network. A nested host built by `CloudInitBuilder` isn't provisioned
   for managed (host-routed) libvirt networks.
3. **Even past that, the recursive `qemu+ssh` binding wouldn't route.** The
   inner-inner run would `libvirt.open("qemu+ssh://admin@192.168.50.28/…")`, which
   shells out to the system `ssh` from the orchestrator host — the `GuestGateway`
   tunnels paramiko `SSHCommunicator` sockets, **not** libvirt's `qemu+ssh`
   transport, so the L2 control-plane connect has no jump and no route.

Conclusion: a gateway is the right first ingredient (and a real improvement for
any *depth-1* `SSHCommunicator` inner VM — kept as BACKEND-11), but depth-2 needs
all three of (host route into the inner net) + (nested-host libvirt provisioned
for it) + (`qemu+ssh` jump support). Not pursued here — this confirms the
original "don't patch depth-2" call empirically.

## Amendment 2026-06-02: ESXi inner backend (ORCH-32)

The original decision pinned the inner backend to libvirt ("we just installed
libvirtd into the guest"). We now also support a **nested ESXi** inner — an ESXi
node installed unattended on the L0, running an inner plan over pyVmomi. The
motivation is concrete: ESXi certification (`examples/capabilities.py` full
green) was **environment-blocked** on the physical host — its build VMs need
internet egress to `apt`, and a single-public-IP ESXi host offers no VM-egress
path. Decision §3 dissolves exactly that: inner VM disks build on the **libvirt
L0** (which has working NAT egress) and the nested ESXi only ever *boots*
pre-built disks. So a nested ESXi unblocks certification with no change to the
egress model — it is decision §3 applied to a second inner backend.

What changed:

- **The inner-type guard widens.** `GuestHypervisor.__post_init__` accepts
  `LibvirtHypervisor` **or** `ESXiHypervisor`; anything else still fails at
  construction. A `.esxi(...)` front door mirrors `.libvirt(...)`: it fills an
  `ESXiKickstartBuilder` (root credential + optional `license`), an
  `SSHCommunicator("root")`, and an inner `ESXiHypervisor`.
- **The recursion is unchanged; the per-backend pieces move behind a dispatch.**
  `nested_phase._synthesize_inner_binding` switches on the inner `Hypervisor`
  type. The libvirt path is exactly as before (libvirtd readiness gate →
  `qemu+ssh` `LibvirtProfile` + a materialized key file). The ESXi path waits on
  the guest's **vSphere API** (`wait_esxi_ready`, a throwaway `SmartConnect`
  poll — the run-phase SSH readiness can pass before hostd serves SOAP) and
  synthesizes an in-process **`ESXiProfile`** (pyVmomi, password-based, **no key
  file**) from the guest's discovered address + the root password the kickstart
  baked. Inner-VM reach uses the ESXi driver's existing SSH `guest_gateway`.
- **The guest must declare ESXi-compatible hardware.** The L0 is libvirt and
  ESXi has no virtio drivers, so the guest's OS disk must hang off `sata`/`ide`
  and its NIC must be `e1000e` — expressed with the libvirt-concrete device
  types (`LibvirtOSDrive`/`LibvirtNetworkIface`, see ADR-0026). The guest also
  carries `CPU(nested=True)`: the L0's `host-passthrough` exposes VMX so the
  ESXi guest can power on its *own* VMs (L0 KVM → L1 ESXi → L2 guest). The
  existing nested-KVM preflight (`nested-kvm-disabled`) already covers this.

Unchanged: depth-1 only (`reject_unsupported_nesting` still fires); the L1 guest
must be directly reachable (no gateway-bound L0); inner disks build on L0 and the
inner run is `require_cache=True` upload-and-boot.
