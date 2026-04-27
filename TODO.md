# TODO

Open follow-ups, code smells, and known limits.  Add new ones at the
bottom of the relevant section so existing items keep their numbering.
Each entry has:

- **Where:** file:line (or area) the smell lives in
- **Why it bothers us:** what's wrong, what fails, who notices
- **Sketch of a fix:** rough shape, not a contract — pick a different
  approach if a better one comes up

Things you've already shipped don't go here; the changelog is the
source of truth for what landed.

---

## Proxmox backend

### 1. Hardcoded install-vnet subnet — no concurrency

**Where:** `testrange/backends/proxmox/orchestrator.py` —
`_PROXMOX_INSTALL_SUBNET = "192.168.230.0/24"`.

**Why it bothers us:** two test processes pointed at the same
PVE node + zone would both try to create an install vnet on the
same subnet (and possibly the same SDN vnet name).  The libvirt
backend has a 15-subnet pool + cross-process file lock for
exactly this case; the proxmox backend is single-orchestrator-
per-PVE-zone today.  Documented in the constant's docstring but
it's a real concurrency limit — first to land in CI matters.

**Sketch:** define a pool (`192.168.230.0/24`–`192.168.239.0/24`
or similar) + a PVE-side lock keyed off `(host, zone)`.  Pick at
`__enter__` time; release on teardown.  The PVE-side lock can
piggy-back on the SDN zone (`tr-lock-<run-id>` vnet existence)
since per-host file locks don't work for a remote PVE.

### 2. Hardcoded public DNS in install-phase cloud-init seed

**Where:** `testrange/backends/proxmox/vm.py` —
`_PUBLIC_DNS = "1.1.1.1"`.

**Why it bothers us:** PVE SDN simple-zones don't ship a
per-bridge DNS resolver (libvirt's dnsmasq pattern doesn't
apply).  The install seed has to point cloud-init at a working
resolver or apt times out on `deb.debian.org`.  We hardcode
`1.1.1.1`.  Three concrete consequences:

1. Air-gapped or DNS-restricted environments silently fail
   (corporate firewalls that block outbound 53, captive portals).
2. Sovereignty / policy concerns — some shops can't legally
   route DNS to Cloudflare.
3. There's no user knob to override it.

There's also a related latent bug: `_vm_network_refs` for run-phase
NICs sets `nameserver = net.gateway_ip if net.dns else ""` — but
PVE doesn't run a resolver on the gateway, so a run-phase guest's
`/etc/resolv.conf` ends up pointing at a dead address whenever a
test sets `dns=True` on a `ProxmoxVirtualNetwork`.  The example
sidesteps this by using IPs in its assertions; a test that uses
names at run time will silently fail.

**Sketch:**
- Short term: add `install_dns="1.1.1.1"` kwarg to
  `ProxmoxOrchestrator.__init__`, plumb to `_PUBLIC_DNS`'s usage
  site.  Solves (1)–(3) without changing the architecture.
- Run phase: do the same fallback in `_vm_network_refs` instead
  of pretending the gateway is a resolver.
- Long term: actually run a dnsmasq somewhere PVE-side (host or
  one-off LXC inside the PVE node) so cross-VM `<vm>.<net>` name
  resolution works the same way it does on libvirt.  Restores
  feature parity — tests written against libvirt that use names
  would then run unchanged on PVE.

### 3. `RunDir` constructed only as an `id` carrier

**Where:** `testrange/backends/proxmox/orchestrator.py` —
`__enter__` constructs `RunDir(LocalStorageBackend(...))` purely
because `ProxmoxVM.build()`'s signature requires a `RunDir` for
`run.run_id`.

**Why it bothers us:** the proxmox backend uploads disks via PVE
REST and never touches the local scratch path.  A real
filesystem directory is created and never written to.  It's
cosmetic waste and conceptually misleading — "this orchestrator
needs a local filesystem" is false.

**Sketch:** decouple `run_id` from `RunDir`.  Introduce a `Run`
struct with just an ID, make `RunDir` the libvirt-side
extension that adds filesystem ops on top.  `vm.build()` /
`vm.start_run()` take the smaller `Run` interface; libvirt's
implementation casts to `RunDir` to use the FS bits.

### 4. `_await_upload_upid` defensive non-UPID handling

**Where:** `testrange/backends/proxmox/vm.py` —
`_await_upload_upid` treats any response that doesn't start
with `UPID:` as "synchronous, nothing to wait for".

**Why it bothers us:** PVE + proxmoxer responses for
`upload.create()` aren't 100% guaranteed across versions.  If
PVE ever returns a different async-ID shape, we'd silently race
the underlying write and the original upload-vs-attach class of
bugs comes back.  No live observation of this happening today,
just defensive interpretation that's narrower than ideal.

**Sketch:** add an explicit allowlist of known synchronous
response shapes (empty, dict-with-no-UPID, etc.) and raise on
anything outside it.  Forces a real diagnosis if PVE introduces
a new response shape rather than silently masking it.

### 5. `inst<run_id[:4]>` install-vnet name is unstable across runs

**Where:** `testrange/backends/proxmox/orchestrator.py` —
`_create_install_network` uses `name="install"` which renders
to `inst<run_id[:4]>` via
`ProxmoxVirtualNetwork.backend_name()`.

**Why it bothers us:** a crashed run leaves `inst<run_id[:4]>`
behind on PVE.  `testrange cleanup MODULE RUN_ID` doesn't sweep
it because `cleanup()` only walks user-declared networks.  An
operator has to `pvesh delete /cluster/sdn/vnets/inst<id>` by
hand.

**Sketch:** extend `ProxmoxOrchestrator.cleanup()` to also
look for and remove `inst<run_id[:4]>`-shaped vnets in the
zone.  Same shape as the other per-run resources it already
sweeps.

---

## Examples

### 6. `examples/nested_proxmox_public_private.py` uses libvirt's `Hypervisor`

**Where:** the example imports `Hypervisor` from
`testrange.backends.libvirt`.

**Why it bothers us:** libvirt's `Hypervisor` class auto-injects
`libvirt-daemon-system`, `qemu-kvm`, `qemu-utils` packages into
the spec + the `systemctl enable libvirtd` post_install_cmds.
`ProxmoxAnswerBuilder` ignores `pkgs=` / `post_install_cmds=`
(PVE installs from `answer.toml`), so those entries don't
actually run — but they DO get baked into the spec's cache hash.
The displayed hash is misleading and the spec carries dead data.

**Sketch:** introduce `ProxmoxHypervisor` in
`testrange/backends/libvirt/` (or a backend-neutral
`Hypervisor` factory that picks the right shim based on the
inner orchestrator type).  No package injection; the PVE
installer is the whole install phase.

### 7. Inner-orchestrator state isn't swept by `testrange cleanup`

**Where:** `testrange/orchestrator_base.py` — `cleanup()` is
per-orchestrator and doesn't recurse into Hypervisor VMs'
inner orchestrators.

**Why it bothers us:** if a nested run dies between inner
provisioning and outer teardown, the inner orchestrator's
PVE-side resources (VMIDs, vnets, install seeds) are left
behind.  `testrange cleanup MODULE RUN_ID` against the outer
factory only handles the outer libvirt resources.  The PVE-side
state has to be cleaned by hand from the PVE node.

**Sketch:** per-VM `cleanup_inner()` hook that
`AbstractHypervisor` overrides — when run for a given
``run_id``, walk the inner orchestrator's expected resource
names and delete them.  Requires the inner-orchestrator class
to know its own naming conventions, which it already does for
its own `cleanup()` impl.

---

## Cross-cutting

### 8. SDN routing warning is informational, not actionable

**Where:** `testrange/backends/proxmox/orchestrator.py` —
`_warn_if_unroutable` logs ``Add a route through the PVE node,
e.g.: sudo ip route add 10.42.0.0/24 via 10.0.0.10`` but doesn't
add the route.

**Why it bothers us:** the user has to copy-paste a sudo command
manually for SSH-based communicators to reach inner SDN-VM IPs.
The guest-agent communicator sidesteps the whole routing problem
(traffic hops through PVE REST), so the warning only matters when
``communicator='ssh'`` is selected — but it always fires.

**Sketch:**
- Suppress the warning when every VM that lives on the
  unroutable subnet uses ``communicator='guest-agent'`` (no
  SSH attempt → no route needed).
- For the SSH case, optionally have the orchestrator add the
  route via a privileged subprocess at ``__enter__`` and remove
  it at ``__exit__``.  Behind a flag — most CI environments
  don't have passwordless sudo.

### 9. Memory preflight is libvirt-only

**Where:**
`testrange/backends/libvirt/_preflight.py` — `check_memory()`
sums declared `Memory(...)` allocations against host RAM.
Proxmox doesn't have an equivalent.

**Why it bothers us:** running against a remote PVE node, the
orchestrator has no idea whether the declared VMs will fit on
the target host's RAM until PVE refuses the start.  Failure
mode is "VM start fails partway through ``__enter__``" instead
of "preflight catches it before any state changes".

**Sketch:** add a backend-neutral preflight hook on
`AbstractOrchestrator`; libvirt's existing one moves into its
backend.  Proxmox queries `/nodes/{node}/status` for total + used
memory and runs the same arithmetic.

### 10. Module of test_orchestrator.py has a flaky memory-preflight test

**Where:**
`tests/test_orchestrator.py::TestCleanupStaleInstallNetworks::test_runs_before_install_network_start`.

**Why it bothers us:** failing intermittently with
``TypeError: unsupported format string passed to MagicMock.__format__``
in `_preflight.py:163`.  The test wires a `MagicMock` for the
declared-memory dict; the format string `:.2f` doesn't accept it.
Pre-existing — was failing before this branch's work — but
the failure looks like a real regression in CI logs every time it
trips.

**Sketch:** the test should pass real `Memory(...)` instances
(or a dict of `{name: float}`) instead of a bare MagicMock.  One-
line fix, just hasn't been done.
