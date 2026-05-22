# ADR-0007: `config_hash` is a pure, deterministic cache key

Status: Accepted
Date: 2026-05-19

Extended by [ADR-0010](0010-build-run-split.md): the key now identifies the
whole *disk set* a build produces, not one disk. The OS-drive `size_gb` and
each data disk's declaration (count + `size_gb`, in spec order) fold into the
hash, since they change the artifacts the build emits.

## Context

The expensive part of bringing up a range is the build pass — booting a
VM, running cloud-init / package installs, powering off. testrange caches
the resulting built disks so that an unchanged VM declaration skips
the build pass on the next run. That cache needs a key.

The key must satisfy two properties that are in tension with the rest of
the system:

- **Stable across runs.** The same VM declaration must produce the *same*
  key every run, or the cache never hits. This rules out anything tied to
  a particular run: `run_id`, wall-clock time, randomness.
- **Sensitive to everything that changes the disk.** If the rendered
  install payload, the base image, or the run-phase MACs change, the disk
  is different and the key must change too — otherwise we serve a stale
  disk that no longer matches the declaration.

The MAC point is subtle: the orchestrator assigns stable MACs at run phase
(see [ADR-0006](0006-driver-stable-mac.md)), and a builder may bake
positional, match-by-MAC netplan into the install payload. So the MACs the
*run* VM will get are an input to the *install* artifact, and therefore to
its cache key.

## Decision

`Builder.config_hash(spec, recipe, *, addressing, base_sha, macs) -> str`
is defined on the `Builder` ABC as a **pure function**:

- It MUST NOT depend on `run_id`, clocks, or any non-deterministic input.
- The same `(spec, recipe, addressing, base_sha, macs)` MUST yield the same
  16-char hex hash, every time.
- `macs` (one per NIC, in spec order) is folded in so builders that bake
  positional NIC config into the payload key the cache on the stable MACs
  the orchestrator will assign — keeping the key sensitive to a change that
  would otherwise silently invalidate the cached disk.

The deterministic-keypair derivation in `SSHKey.generate` (seed =
`sha256(comment)`) exists precisely so that a rendered seed embedding an
SSH public key stays byte-stable across runs and does not perturb this hash.

## Consequences

- A builder author must treat `config_hash` as a contract: any input that
  affects the rendered install payload has to be folded into the hash, or
  the cache will serve a disk that no longer matches the declaration.
- Conversely, nothing run-specific may leak in, or the cache never hits and
  every run pays the install cost.
- The function is trivially testable: call it twice with the same inputs and
  assert equality; vary one input and assert the hash moves.
