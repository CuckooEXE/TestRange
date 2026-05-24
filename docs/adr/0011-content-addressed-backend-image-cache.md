# ADR-0011: Content-addressed backend image cache at the driver ABC

Status: **Draft** (not implemented — see [PVE-side cache discussion, 2026-05-23])
Date: 2026-05-23

Amends [ADR-0008](0008-driver-abc-multi-backend.md) (the driver disk surface)
and [ADR-0010](0010-build-run-split.md) (which deliberately deferred
"backend-side reuse of base images"). This ADR is the design for that reuse,
generalized across every planned backend.

## Context

A warm `testrange run` re-uploads the **entire** captured OS disk from the
runner to the backend on every run (`upload_to_pool` → `create_vm import-from`).
On a constrained uplink an 8 GiB disk is ~28 min/run — the dominant cost of the
dev loop. We deferred backend-side reuse in ADR-0010; this is the design.

Two hard constraints from the design discussion (2026-05-23):

1. **The orchestrator must stay dumb** — no `if driver.has_cached_layer`
   branching sprayed through the run/build phases. Cache locality is a *driver*
   concern.
2. **No disk spray** — backend-resident cache artifacts must be deterministically
   named (idempotent, never duplicated), tracked, and GC-able; they must not
   accumulate as orphaned mystery disks.

The current storage surface (`upload_to_pool` / `download_from_pool` /
`create_blank_volume` / `resize_volume` / `delete_volume`, with the orchestrator
threading a `VolumeRef` and registering the staging volume for teardown) is
*storage-centric*: it bakes in a "here vs. a pool to push to" model that is
wrong for a local backend (libvirt: no transfer) and leaky for remote ones (the
orchestrator should not know whether bytes move).

## Decision

Replace the storage-centric surface with an **intent-centric** one: the backend
owns a set of **content-addressed images**, and materializes VM disks from them.

```python
ImageId  = NewType("ImageId", str)   # content identity (config_hash / base sha) — orchestrator-known
ImageRef = NewType("ImageRef", str)  # opaque backend handle — orchestrator never inspects

class HypervisorDriver(ABC):
    @abstractmethod
    def ensure_image(self, image_id: ImageId, fetch: Callable[[], Path]) -> ImageRef:
        """Make `image_id` resident as a reusable image; return its handle.
        Idempotent + content-addressed: a resident image returns its ref WITHOUT
        transfer. `fetch()` (yielding a local path) is called ONLY on a miss."""

    @abstractmethod
    def capture_image(self, vm: VMHandle, image_id: ImageId, sink: Callable[[Path], None]) -> None:
        """Read a built VM's OS disk back out, handing the local bytes to `sink`
        (which stores them in the canonical cache). The build→cache direction."""

    @abstractmethod
    def list_images(self) -> Sequence[ImageId]: ...      # GC inventory
    @abstractmethod
    def evict_image(self, image_id: ImageId) -> None: ...  # GC verb

    # create_vm consumes an ImageRef instead of a freshly-uploaded VolumeRef:
    #   create_vm(..., os_image: ImageRef, data_disks: Sequence[BlankDisk | ImageDisk], ...)
```

This **retires** `upload_to_pool`, `download_from_pool`, `create_blank_volume`,
and `resize_volume`, folding them into `ensure_image` / `capture_image` /
`create_vm`'s disk specs. The ABC gets *smaller*.

### The linchpin: `fetch` / `sink` are lazy callables, not eager bytes

This single choice makes the abstraction hold across the backend spectrum and
keeps the cache stovepipe intact (the driver never imports the cache manager):

- The driver is handed *a way to obtain bytes* (`fetch`) or *a place to put
  them* (`sink`) — the orchestrator/cache layer is the only thing that knows the
  local + HTTP cache tiers.
- **Warm remote hit:** the driver returns the resident ref and never calls
  `fetch()` → the orchestrator does not even materialize the local file.
- **Local backend:** `fetch()` returns the already-local cache path; the driver
  hardlinks/refs it — no copy.

Eager `bytes`/`path` arguments would force local materialization on every run
and defeat both the warm-hit and the local-backend cases.

### The backend spectrum (same 4 methods, no orchestrator branch)

| Backend | `ensure_image` (miss) | warm hit | `create_vm` disk | `capture_image` | `list/evict` |
|---|---|---|---|---|---|
| local libvirt | ref cache file in place / copy into pool | already there | qcow2 backing-file or copy | local copy | enumerate pool |
| remote libvirt (`qemu+ssh`) | `virsh vol-upload` | resident vol | `vol-clone` / backing | `vol-download` | `vol-list` |
| ESXi | upload VMDK to datastore | datastore VMDK | clone VMDK | datastore download | datastore browse |
| Hyper-V | push VHDX over SMB | VHDX present | `New-VHD -ParentPath` | SMB read | dir listing |
| Proxmox | upload → `tr-cache-<id>` content vol | volid present → skip | `import-from` (server-side copy) | SFTP capture | list `tr-cache-*` |

The degenerate local-libvirt case (no transfer anywhere) passing the *same* code
path is the acid test that the seam is in the right place.

## Consequences

- **Orchestrator simplifies.** It stops tracking the staging/import volume
  entirely (images are driver-owned), threads only `ImageId` (= `config_hash`,
  which it already holds), and registers only genuinely-ephemeral run resources
  for teardown. The Option-2 `VolumeRef` re-resolution dance largely dissolves.
- **No spray, structurally.** `ImageId` is content-addressed → `ensure_image` is
  idempotent → re-runs reuse, never duplicate. VM disks and images are distinct
  lifecycle kinds: VM disks are purged with the VM; images are reachable only via
  `list_images` / `evict_image`, so they can't become orphans and GC is a generic
  loop. Crash-safety is unchanged (images aren't run resources).
- **Eviction policy lives in the cross-backend cache layer**, not the drivers —
  `evict_image` stays a dumb verb; the policy (cascade-on-local-eviction /
  LRU-cap / explicit-only) is defined once. (Open: which policy. Leaning
  cascade-on-local-eviction as the default.)
- **Bigger than a patch.** This is the storage half of the ABC reshaped once;
  every future backend then implements it naturally. It revises ADR-0008's disk
  surface and the Option-2 model.

## Alternatives considered

- **PVE templates + linked clones** (and per-backend equivalents): thinner/faster
  but couple the cache to VM lifecycle (cannot delete a base with clones
  outstanding), needing template/clone bookkeeping — the state web we are
  avoiding. A full server-side copy (`import-from`, ~seconds for 8 GiB on local
  disk) is simpler and fast enough given simplicity > speed.
- **Keep the storage-centric surface, add a `has_cached_layer` query the
  orchestrator branches on** — rejected: violates constraint 1, and breaks on the
  local-libvirt degenerate case.

## Status / next

Draft only. Tracked by the BACKEND ticket for the image-cache reshape. Until
implemented, the dev loop uses an out-of-tree monkeypatch harness to skip the
re-upload (not part of the codebase).
