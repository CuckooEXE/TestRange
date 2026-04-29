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

### 1. `RunDir` constructed only as an `id` carrier

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

### 2. `_await_upload_upid` defensive non-UPID handling

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

### 3. `inst<run_id[:4]>` install-vnet not swept by `testrange cleanup`

**Where:** `testrange/backends/proxmox/orchestrator.py` —
`_create_install_network` produces a vnet whose backend name is
`inst<run_id[:4]>` via `ProxmoxVirtualNetwork.backend_name()`.

**Why it bothers us:** a crashed run leaves `inst<run_id[:4]>`
behind on PVE.  `testrange cleanup MODULE RUN_ID` doesn't sweep
it because `cleanup()` only walks user-declared networks.  An
operator has to `pvesh delete /cluster/sdn/vnets/inst<id>` by
hand.

**Sketch:** extend `ProxmoxOrchestrator.cleanup()` to also
look for and remove `inst<run_id[:4]>`-shaped vnets in the
zone.  Same shape as the other per-run resources it already
sweeps.

### 4. SDN-side dnsmasq for run-phase name resolution

**Where:** `testrange/backends/proxmox/orchestrator.py` —
``_vm_network_refs`` falls back to ``self._install_dns`` (the
orchestrator's configured public resolver) for run-phase NICs on
``dns=True`` networks.  That gives guests *a* working resolver,
but not cross-VM ``<vm>.<net>`` name resolution the way libvirt's
per-network dnsmasq does.

**Why it bothers us:** tests written against libvirt that use VM
hostnames in run-time assertions (``orch.vms["web"].exec(...)``
implicitly resolves ``web.OuterNet`` from inside the guest)
silently fail on PVE — the names don't exist anywhere.  Workaround
is to use IPs.

**Sketch:** stand up a dnsmasq somewhere PVE-side (host service or
a one-off LXC inside the PVE node) seeded with the orchestrator's
``register_vm`` ledger.  Each VM gets an A record for
``<vm-name>.<network-name>``; the run-phase NICs' DNS field points
at it.  Restores libvirt-style name resolution and makes
cross-backend tests run unchanged.

---

## Cross-cutting

### 5. Inner-orchestrator state isn't swept by `testrange cleanup`

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

### 6. Memory preflight is libvirt-only

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

### 7. Ctrl+C during a remote run leaks resources on the remote

**Where:** `testrange/backends/libvirt/orchestrator.py` —
`__exit__` / `_teardown` over a `qemu+ssh://` connection.

**Why it bothers us:** SIGINT raises `KeyboardInterrupt` on the
main thread.  Paramiko's SSH transport (the tunnel libvirt rides
on for `qemu+ssh://`) tears itself down on SIGINT before
`__exit__` runs, so every teardown call hits ``client socket is
closed`` / ``Cannot recv data: Connection reset by peer``.  The
orchestrator catches each error and logs ``(ignored)``, then
reports ``teardown complete`` even though nothing was actually
cleaned up.  Result: a Ctrl+C mid-run consistently leaves
``tr-build-*`` domains, install networks, and (in the nested case)
inner-orchestrator PVE-side state behind on the remote host.
``testrange cleanup MODULE RUN_ID`` recovers it but the user has to
remember to run it.

A second smaller smell visible in the same trace: the network
teardown loop tries to stop run-phase networks (``OuterNet`` etc.)
that were never bound when Ctrl+C lands during the install phase,
surfacing ``bind_run() must be called before backend_name()`` as
an ignored teardown error.

**Sketch:**

- Block SIGINT inside ``__exit__`` so the connection survives long
  enough for teardown to drive every libvirt destroy/undefine.
  Standard pattern: install a no-op ``SIGINT`` handler at
  ``__exit__`` entry, restore on exit, and treat a *second* Ctrl+C
  as an explicit force-quit that skips remaining teardown.
- Reconnect-on-demand for the case where the connection was
  already dead before ``__exit__`` ran (e.g. paramiko transport
  thread crashed for an unrelated reason).  One ``libvirt.open``
  retry per resource is fine — teardown ops are idempotent.
- Skip-if-not-bound in the network teardown loop so unstarted
  run-phase networks don't pollute the log with teardown errors.

### 8. WinRM communicator missing on Proxmox

**Where:** `testrange/backends/proxmox/` — no parallel to
`testrange.backends.libvirt.guest_agent.GuestAgentCommunicator` /
the WinRM communicator wired up for libvirt VMs.  The proxmox
backend currently routes Windows VMs through QEMU guest-agent
only.

**Why it bothers us:** when a Windows VM doesn't have the QEMU
guest-agent installed (cold install ISO, snapshot from before
guest-agent was rolled out, third-party Windows image without the
``virtio-win`` tools) there's no fallback remote-exec path on
Proxmox.  libvirt tests can use ``communicator='winrm'``; the
same test against ``ProxmoxOrchestrator`` fails because no
communicator class accepts that kwarg.  Blocks every Windows-VM
use case on PVE except the narrow "guest-agent already running"
case.

**Sketch:**

- New ``testrange/backends/proxmox/winrm.py`` mirroring the
  guest-agent module's shape: an ``AbstractCommunicator``
  subclass, but instead of routing exec calls through PVE's
  ``/agent/`` REST endpoints, it talks WinRM directly to the
  guest's ``5985`` / ``5986`` over the routable network the VM
  was provisioned on.
- ProxmoxOrchestrator must ensure WinRM gets enabled at install
  time — same ``Enable-PSRemoting -Force`` autounattend snippet
  the libvirt path injects, plus a Windows-firewall rule for
  the inbound port.  ``WindowsUnattendedBuilder`` already knows
  how to render those steps; the proxmox-side wiring just needs
  to set them up via ``answer.toml`` / first-boot scripts.
- Open question: PVE's SDN simple zones don't bridge inner
  subnets to the outer host without an explicit route (see
  TODO #6 above), so WinRM-from-the-host-to-an-inner-VM only
  works when the outer host can reach the inner IP.  Either
  document the routing requirement or have the communicator
  hop through a PVE-side proxy (proxy via guest-agent if
  available; otherwise raise a helpful error).

### 9. Migrate `_proxmox_prepare` off the `xorriso` binary

**Where:** ``testrange/vms/builders/_proxmox_prepare.py`` —
:func:`prepare_iso_bytes` shells out to the ``xorriso`` CLI via
``subprocess.run`` to inject ``/auto-installer-mode.toml`` into a
vanilla PVE installer ISO while preserving the hybrid boot
layout (``-boot_image any keep``).

**Why it bothers us:** xorriso is the only TestRange dependency
the user can't satisfy via ``pip install``.  Goal across the
project is a single ``pip install testrange[proxmox]`` and zero
binary-shelling-out — every external system call is a portability
hazard (different distros, different versions, missing PATH on
container images, locked-down CI runners) and a test surface
(mocking ``subprocess.run`` to pin argv is not the same kind of
coverage as exercising a real implementation in-process).

The previous pure-Python attempt used :mod:`pycdlib` and silently
broke the prepared ISO's UEFI boot — pycdlib's ``write_fp()``
rebuilt the ISO from its data model, which doesn't track the
hybrid GPT / protective MBR / HFS+ / EFI System Partition layout
PVE's UEFI GRUB depends on.  So the migration target needs to
preserve those four extra layers exactly while still being
in-process Python.

**Migration plan, in tiers from easiest-to-ship to most-correct:**

1. **Try a more-careful pycdlib path that copies the hybrid system
   area verbatim.**  pycdlib has ``rr_strict_iso9660_filenames`` /
   ``add_eltorito`` knobs we didn't use; the hybrid system area
   (the first 32 KiB of the ISO that holds the protective MBR +
   GPT + APM) can be carried across via ``set_system_use_area`` /
   raw-byte preservation if pycdlib exposes a hook.  Cheapest
   option *if* pycdlib's APIs admit it; needs a real boot test
   against PVE 9.x to confirm GRUB no longer drops to ``grub>``.
   Risk: if pycdlib fundamentally rebuilds the GPT during write
   (likely), no amount of careful API use will preserve the
   ESP-as-GPT-partition link, and we end up exactly where we
   started.

2. **Bind ``libisoburn`` directly via ctypes / cffi.**
   ``libisoburn.so.1`` ships in the same OS package as the
   ``xorriso`` binary, so it's available wherever xorriso is —
   but we drop the subprocess hop and gain in-process error
   handling.  pip-installable wrappers exist (search
   ``isoburn``-named projects) but appear unmaintained; we'd
   likely need a thin in-tree ctypes wrapper covering only the
   ``-boot_image any keep`` + ``-map`` operations we use.
   Doesn't pip-install libisoburn itself, so this is a halfway
   house: the subprocess goes away but the system dependency
   remains.

3. **Build a libisoburn-equivalent hybrid-ISO writer in pure
   Python.**  The actual data we need to emit is well-defined:
   ISO9660 + Rock Ridge + El Torito (boot catalog + BIOS image
   pointer + UEFI image pointer) + protective MBR + GPT entries
   for the EFI System Partition + (for Mac) APM + HFS+ wrapper.
   pycdlib handles ISO9660 + Rock Ridge + basic El Torito
   already; we'd need to layer the hybrid bits on top.  Most
   correct, hardest to ship, biggest test surface.  Worth doing
   if (a) we have other ISO-prep needs (cloud-init seed, Windows
   unattend) that benefit from a shared in-process writer, or
   (b) the Proxmox backend grows enough that ``[proxmox]`` extra
   becomes the canonical install path and binary-free becomes a
   selling point.

4. **Vendor a ``proxmox-auto-install-assistant``-equivalent Rust
   crate via PyO3.**  Upstream Proxmox already has a Rust
   implementation of exactly this prep step in
   ``proxmox-auto-install-assistant``.  PyO3 lets us bundle it
   into a Python wheel.  Trades the xorriso runtime dependency
   for a PyO3 build-time dependency, which **is** pip-installable
   (wheels ship pre-compiled).  Probably the most pragmatic path
   to "no system binaries" if we're willing to take a Rust
   build-time toolchain.

**Sketch of the decision criterion:** pick (1) if a quick
investigation shows pycdlib has the hooks we need.  Otherwise (4)
— PyO3-wrapped upstream Rust — is the smallest change with the
strongest correctness guarantee, since we'd be using the same
code Proxmox itself ships.  (2) is OK as a stopgap but doesn't
solve the pip-install goal.  (3) is the long-term ideal and the
right choice if more in-process ISO writing shows up across the
codebase.

**Until then,** the system dependency is documented in
``docs/usage/installation.rst`` and ``prepare_iso_bytes`` raises
:class:`ProxmoxPrepareError` with install hints when ``xorriso``
isn't on ``$PATH``, so the failure mode is a clear "install this
package" rather than a confusing prepared-ISO bug.

### 10. ``shutdown(self)`` ABC takes no orchestrator context

**Where:** `testrange/vms/base.py` — `AbstractVM.shutdown` is
zero-arg.  `ProxmoxVM` works around it with a `set_client(client)`
method that the orchestrator must call before teardown so
`shutdown()` can drive REST without a context handle.

**Why it bothers us:** the workaround couples VM lifetime to the
orchestrator that constructed it.  A VM moved between orchestrators
(theoretically possible — we don't, but the contract allows it)
silently fails on `shutdown()`.  An earlier cut also leaked stale
client references past orchestrator close (now fixed; the handle
is cleared at end of `shutdown`).  The contract is still ugly.

**Sketch:** Change `AbstractVM.shutdown(self)` →
`shutdown(self, context: AbstractOrchestrator)` to match
`build()` / `start_run()`.  Drop `set_client` / `_client`.  Touches
every test that mocks `vm.shutdown()` (call sites pass `self` so
the orchestrator changes are mechanical; test mocks need the
arg added).  Worth doing but invasive — defer until the next
contract-change-friendly slice.

### 11. ``ProxmoxOrchestrator`` is 1900+ lines doing too many things

**Where:** `testrange/backends/proxmox/orchestrator.py` — auth
resolution, node/storage discovery, SDN zone management, install-
vnet picker, switch lifecycle, network lifecycle, VM provisioning,
nested orchestration, bootstrap script, pveproxy wait, cleanup-
by-run-id, list-templates / prune-templates admin, keep_alive_hints.

**Why it bothers us:** large file = expensive to read, expensive
to review, harder to spot logic errors.  Line count itself isn't
a bug, but several recent fixes (zone DHCP, IPAM ordering, IPAM
ledger race) sat in this file because the integration points were
buried in noise.

**Sketch:** Extract three sibling modules:
- `proxmox/sdn.py` — zone ensure, install-subnet picker, dnsmasq
  preflight, IPAM helpers.
- `proxmox/admin.py` — `cleanup`, `list_templates`,
  `prune_templates`, `_open_admin_connection`.
- The remaining `__enter__` / `__exit__` / `_provision_vms` flow
  becomes legible.

High-churn refactor; do it when the next big feature lands so
the diff isn't pure code-movement.

