# ADR-0012: Build success is an explicit serial-console result token, not power-off

Status: Accepted
Date: 2026-05-24

Amends [ADR-0010](0010-build-run-split.md) (the build phase's success
signal). Relates to [ADR-0008](0008-driver-abc-multi-backend.md) (adds a
hypervisor-level driver capability).

## Context

The build phase (ADR-0010) provisions a guest, captures its disks into the
cache, and tears the VM down. Until now it keyed success on a single
out-of-band bit: `CloudInitBuilder` appended `poweroff` to `runcmd`, and the
orchestrator polled driver-level power state until `shutoff` (or the build
timeout). That bit is both lossy and *wrong*:

- It cannot distinguish **succeeded** from **a command failed but the guest
  powered off anyway**. cloud-init `runcmd` ran under `sh` with no `set -e`,
  and package installs ran via cloud-init's `packages:` directive (which logs
  failures but does not abort), so a failed `apt-get update` still reached
  `poweroff`. The orchestrator then **cached a corrupt post-install disk
  silently** — the worst possible failure mode for a cache.
- A failure that wedges the guest before it powers off costs the full build
  timeout *and* yields **no diagnostic output**.

Two hard constraints on any replacement:

1. **Builder-agnostic.** cloud-init is one of several builders; the contract
   must also fit ESXi Kickstart (`%post`) and Windows Unattended
   (`SetupComplete.cmd`). It lives *above* the Builder ABC.
2. **Must not require a native guest agent.** Guests such as OpenBSD ship no
   QGA / VMware-Tools (`native_guest_*` absent — ADR-0008 §3). The result
   channel cannot depend on the agent.

## Decision

**The guest reports an explicit, structured build result over a serial
console; the orchestrator reads that sink, treats the positive token as the
*only* success signal, and raises a typed error otherwise.**

A 16550 UART is the most portable virtual device there is: every target guest
OS writes to it (Linux `ttyS0`, the BSDs `com0`, Windows `COM1`) and it is a
property of the *virtual hardware*, not of any in-guest agent — so it
satisfies constraint 2. It is universal, so the builder emits to the serial
console only and a per-backend driver capability hides the host-side read.

### 1. The result protocol (builder-emitted, on the console)

Provisioning runs **fail-fast** and emits framed records that survive
interleaving with boot chatter and tolerate binary payloads (base64):

```
TESTRANGE-RESULT: ok
# --- or ---
TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"
TESTRANGE-LOG-BEGIN
<base64 of the relevant log — the failing command's output, or a
 /var/log/cloud-init-output.log tail>
TESTRANGE-LOG-END
```

Success is the explicit `ok` token. A guest that powers off *without*
emitting `ok` is a failure (crashed mid-provision) — this kills the
silent-corrupt-cache bug. On failure the guest emits the `fail` record + framed
log, then powers off promptly, so the failure path costs
`boot + time-to-failing-command`, not the full build timeout.

### 2. Builder responsibility (per-dialect, uniform contract)

The Builder ABC gains the obligation (documented above the ABC, since each
concrete renders its own dialect) to produce provisioning that (a) runs
fail-fast, (b) emits the `TESTRANGE-RESULT:` record + framed log to the
console, (c) powers off. `CloudInitBuilder` wraps all provisioning — apt, pip,
and `post_install_commands` — in one `bash -c` script under `set -eE` + an
`ERR` trap that frames the failing command (`$BASH_COMMAND`), rc, and a base64
log tail onto `/dev/ttyS0`; on success it `sync`s, emits `ok`, and powers off.
**Package installs moved out of cloud-init's `packages:` directive into the
trapped script** precisely so a package failure is caught fail-fast rather
than logged-and-ignored.

### 3. Orchestrator responsibility

`wait_for_shutoff` is replaced by `wait_for_build_result(ctx, backend, vm)`:
open the build-result sink right after `start_vm`, live-tail it, and
short-circuit the moment a record arrives. `ok` → proceed to capture; `fail` /
powered-off-without-token → raise `BuildFailedError(vm, rc, cmd, log)` with the
decoded log; the build timeout reverts to a watchdog for a true wedge only
(`BuildTimeoutError`). Only on `ok` does the phase capture the disk, so a
corrupt disk is never cached. The record parser is backend-independent — every
backend's sink delivers the same serial bytes; only the transport differs.

### 4. Driver capability (hypervisor-level, not agent-level)

A new optional accessor on `HypervisorDriver` —
`read_build_result_sink(backend_name)` — returns a `Generator[bytes, None,
None]` the orchestrator tails. It yields console bytes as they arrive and MUST
yield a `b""` heartbeat periodically so the orchestrator can enforce its own
timeout without being held hostage by a silent guest; iteration ends when the
console closes. The orchestrator wraps it in `contextlib.closing`, so a
transport the driver opened (a Proxmox `vncwebsocket`, a libvirt pty) is
released by the generator's `finally` even when the loop breaks early on a
record — no bespoke context-manager type. This is distinct from
`native_guest_*`; absence of a guest agent does not affect it. The default
raises `DriverError` ("no build-result sink") so a backend that cannot verify
a build fails loud rather than caching an unverified disk. `MockDriver` is the
reference sink (canned `ok` by default; injectable `fail` / wedge for tests).

The orchestrator also mirrors each console line to a dedicated
`…build_phase.console` logger at DEBUG as it streams (skipping the protocol's
own framing), so a build's provisioning is watchable live with `--log-level
debug`.

## Consequences

- The silent-corrupt-cache bug is closed: a failed build raises with the
  failing command + its log instead of caching a half-provisioned disk.
- The fast-fail path no longer pays the full build timeout; the timeout is now
  a genuine watchdog.
- `config_hash` shifts (rendered `user-data` changed), invalidating
  pre-existing build-cache entries once — a one-time rebuild, by design.
- The build VM's virtual hardware must carry a serial UART; wiring it is a
  per-driver concern (the sink reads the host side of the same port). The
  Proxmox concrete (`serial0` over `termproxy`→`vncwebsocket`, **landed in
  PVE-17**) carries a second sanctioned transport exception beyond SFTP —
  amended into ADR-0008 §6. It needs password-ticket auth (termproxy rejects
  API tokens) and `websocket-client` in the `[proxmox]` extra. The protocol,
  builder contract, orchestrator flow, and mock backing here are
  backend-independent and landed first against the mock.
