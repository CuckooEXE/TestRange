# ADR-0009: `Switch(mgmt=True)` semantics across backends

Status: Accepted — option (B) (ratified 2026-05-31, see Ratification addendum)
Date: 2026-05-21

## Context

`Switch(mgmt=True)` is documented (`networks/base.py`) as putting "a host
adapter at `.2` on the Switch's subnet — just an adapter, no NAT, no
forwarding, no router semantics." The examples use it as the channel by which
the host and guests reach each other out of band: `network_modes.py` asserts
`mgmt-vm` can ping `10.51.0.2`, and `hello_world.py` / `private_public.py` lean
on the same `.2` adapter.

The flag was co-designed with the original local-libvirt backend, where it is a
clean fit and where the orchestrator runs *on* the hypervisor host. The ABC was
since reshaped for four backends (ADR-0008), and the libvirt driver was deleted;
`MockDriver` is the only concrete backend today and it merely *records* the
flag — **no backend actually realizes a mgmt host adapter**. Before any real
backend claims `mgmt` support we need to pin down what `.2` means off the local
libvirt box.

### The construct is realizable everywhere

Every target backend has a "give the host an L2 adapter with an IP on this
switch" primitive, so `mgmt` is not blocked by the ABC or the recent refactor —
`mgmt` rides on the `Switch` value object, is handed whole to `create_switch`,
and there is no orchestrator-side L2 realization to get in the way:

| Backend | mgmt `.2` realized as | Fit |
|---|---|---|
| libvirt (local KVM) | IP on the Linux bridge device itself | Native — the original model |
| Proxmox VE | IP on the `vmbr` / SDN vnet on the PVE node | Single-node only; not a first-class proxmoxer call (ifupdown / `interfaces`) |
| ESXi (standalone) | VMkernel adapter (vmk) on the port-group | Clean, first-class |
| vCenter (vSphere) | vmk on a host; DVS spans hosts | Construct exists, but "which host owns `.2`?" |
| Hyper-V | host vNIC `vEthernet (switch)` from an Internal/External VMSwitch | First-class standalone; host vNIC may need an explicit VLAN tag |

### The two real frictions are semantic, not structural

1. **Locality — "which host?"** `mgmt` presumes a single host that gets `.2`.
   True for local libvirt, standalone ESXi, standalone Hyper-V, single-node
   Proxmox. Ambiguous for vCenter + DVS, Proxmox clusters, and any
   SDN/distributed fabric where the VM's host is not pinned — a single `.2`
   vmk/vNIC lives on exactly one host. A driver would have to pin the mgmt
   adapter to the node running the workloads (or pin VM placement).

2. **Reachability — "whose host?" (the reason mgmt exists).** The examples use
   `.2` so the *orchestrator/test runner* can reach guests (and vice versa).
   That holds only while the orchestrator runs on the hypervisor host. For a
   remote ESXi / vCenter / Proxmox / Hyper-V, the host adapter is on the
   *hypervisor*, not the orchestrator box — `.2` becomes reachable-by-the-
   hypervisor, not reachable-by-the-test-runner. `mgmt`-as-host-adapter and
   `mgmt`-as-orchestrator-reachable are the same thing only on-box.

## Decision (proposed — to be ratified)

Pick what `.2` promises before any remote backend honors `mgmt`. Two coherent
options:

- **(A) `mgmt` means "the orchestrator can reach guests here."** Then it is only
  honest on a co-located orchestrator. A remote driver should *reject*
  `mgmt=True` in preflight rather than provision an adapter the runner cannot
  reach. Off-box guest reachability becomes a separate, explicit concern.

- **(B) `mgmt` means "the hypervisor host has an L2 presence at `.2`."** Then it
  ports to every backend (with the locality caveat resolved by pinning), but the
  examples' reachability assertions are a local-only guarantee and must be
  documented as such — `.2` is not promised reachable from the test runner.

Either way, the choice changes the **preflight contract** for every remote
driver, which is why it is recorded here rather than decided ad hoc per backend.

### Interim gate (already in effect)

> **Amended 2026-05-27 (CORE-16) — see Addendum:** the `native_capability_findings`
> cross-reference below is obsolete (that helper was removed), and findings no
> longer carry a severity.

Until this ADR is ratified, `mgmt=True` fails loud at preflight:
`mgmt_unsupported_findings` (shared, in `testrange/preflight.py`, alongside
`native_capability_findings`) emits one error-level `PreflightFinding` per
offending Switch; `MockDriver.preflight` calls it, and the run aborts with
`PreflightError` (`orchestrator/runtime.py`). A backend that grows real,
specified `mgmt` support drops the call from its `preflight`.

## Consequences

- `mgmt` is a no-op-but-loud flag for now: plans that set it are rejected early
  with a fix hint pointing here, instead of silently provisioning nothing (or,
  worse, an unreachable adapter) at run time.
- The shipped examples that previously set `mgmt=True` (`hello_world.py`,
  `private_public.py`, `network_modes.py`) would otherwise **fail preflight**,
  so they have been updated: each `mgmt=True` is commented out with a pointer to
  this ADR, demonstrating the wiring without tripping the interim gate. They
  return to `mgmt=True` once this ADR is ratified and a backend realizes it.
- Ratifying (A) vs (B) is a prerequisite for the Proxmox driver (and any other
  remote backend) to claim mgmt support; the gate stays until then.

## Addendum — 2026-05-27 (CORE-16)

The interim gate is **still in effect**; `mgmt_unsupported_findings` is
unchanged. Two wording corrections to the paragraph above:

- The sibling helper `native_capability_findings` it cites was removed (see the
  ADR-0008 addendum). `mgmt_unsupported_findings` is now the lone shared
  topology gate in `testrange/preflight.py`; `managed_build_egress_findings`
  moved onto the driver ABC (ADR-0014) and was subsequently **removed entirely**
  when ADR-0016 superseded ADR-0014 (egress is out-of-band — there is nothing to
  gate).
- `PreflightFinding` no longer carries a severity — the error/warning tier was
  dropped, since preflight only ever emitted blockers. "one error-level
  `PreflightFinding` per offending Switch" now reads simply "one
  `PreflightFinding` per offending Switch," and `bool(report)` is true iff there
  are no findings at all.

## Ratification — 2026-05-31 (PVE-43)

**Decision: option (B).** `Switch(mgmt=True)` means *the hypervisor host has an
L2 presence at `.2`* on the Switch's subnet — a plain host adapter, no NAT, no
forwarding, no router semantics (the sidecar still owns `.1`). Guests on the
switch and the hypervisor host reach each other over `.2`.

**Reachability is a hypervisor-local guarantee, not a test-runner one.** `.2`
lives on the *hypervisor*; it is promised reachable from guests on the switch
and from a co-located orchestrator, but **not** from a remote test runner. A
plan that needs the runner to reach guests must use the guests' own
static/DHCP addresses (over a routed/uplinked switch), not `mgmt`. This is the
honest reading of friction #2 above and it does **not** change the preflight
contract: realizing `mgmt` is now legal on every backend, so no driver rejects
it — the locality/reachability caveats are documentation, not a gate.

**Locality (friction #1) is resolved by node-pinning, single-node only.** The
`.2` adapter is realized on the node that runs the workloads. Multi-node
Proxmox clusters and vCenter+DVS — where "which host owns `.2`?" is genuinely
ambiguous — remain out of scope (PVE-31); a driver targeting them must pin VM
placement before claiming `mgmt`.

**Per-backend realization (the "drops the call" path):**

- **libvirt** — host IP on the network's bridge via `<ip address=.2>`
  (`drivers/libvirt/_net.py`); the gate call was already dropped. *Realized.*
- **Proxmox VE** — an SDN **subnet** on the per-Switch vnet carrying
  `gateway = <.2>` (`POST /cluster/sdn/vnets/{vnet}/subnets`,
  `{subnet=<cidr>, type=subnet, gateway=<.2>}`), which PVE plumbs onto the
  vnet bridge on the (single, pinned) node. No SNAT and no DHCP IPAM on the
  subnet, so it is a pure host adapter that coexists with the sidecar at `.1`.
  `drivers/proxmox/_sdn.py`; the `mgmt_unsupported_findings` call is dropped
  from `ProxmoxDriver.preflight` (PVE-44). *Realized (single-node).*
- **ESXi / vCenter / Hyper-V** — unrealized; they keep calling
  `mgmt_unsupported_findings` until they grow real support.

**The shared gate stays** in `testrange/preflight.py` for backends that have
*not* realized `mgmt` (today: the test-only `MockDriver`, and future ESXi /
Hyper-V drivers). It is no longer "the interim gate until ratification" — it is
the standing per-backend opt-out for unrealized support.
