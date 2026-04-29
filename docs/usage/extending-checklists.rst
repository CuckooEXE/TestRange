Extending: builder + orchestrator checklists
============================================

Deep-dive checklists for the two largest extension surfaces in
TestRange.  Use these alongside :doc:`extending` (which covers the
ABCs at a high level); these pages are the punch-lists you go
through when actually shipping a new builder or backend.

If you're only adding a new package manager, communicator, or VM
class (no install pipeline / no hypervisor lifecycle), the existing
:doc:`extending` page is the right reference — those surfaces are
small and don't have hidden integration points.


.. _builder-checklist:

Adding a new install builder
----------------------------

A builder is the strategy that turns a VM spec into install + run
disks.  The shipped builders cover cloud-init Linux (most distros),
Windows autounattend, ProxMox VE auto-installer, and a no-op
"bring-your-own-image" path.  A new builder might wrap kickstart,
preseed, Alpine apkovl, FreeBSD bsdinstall, or anything else with
its own install ritual.

The contract lives at
:class:`testrange.vms.builders.base.Builder`.  The deliverable is
two dataclasses (``InstallDomain`` from
:meth:`prepare_install_domain` and ``RunDomain`` from
:meth:`prepare_run_domain`) that the orchestrator's backends turn
into native VM definitions — your builder doesn't touch libvirt or
the PVE REST API directly.

Pre-flight: do you actually need a new builder?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Does your install flow boil down to "boot an ISO, drop a seed
  alongside, wait for poweroff"?  Cloud-init builder already does
  this for any cloud-init-aware distro; reach for it first.
* Is your distro non-cloud-init but still runs ``runcmd``-equivalent
  hooks?  Often a ``Builder`` subclass that emits a different seed
  format on top of the same shape is enough — :class:`ProxmoxAnswerBuilder`
  is exactly this (PVE's answer.toml instead of cloud-init's
  user-data).
* Does your install need post-install hooks at run time, not install
  time?  That's an orchestrator concern, not a builder concern —
  see ``ProxmoxOrchestrator._bootstrap_pve_node`` for the SSH-side
  pattern.

The builder ABC
~~~~~~~~~~~~~~~

Required:

1. ``needs_install_phase() -> bool`` — return ``True`` when your
   builder produces an :class:`InstallDomain`.  Set ``False`` only
   if your VM ships a pre-installed image (BYOI / NoOpBuilder).
2. ``default_communicator() -> str`` — one of ``"ssh"`` /
   ``"guest-agent"`` / ``"winrm"``.  Picked when the user doesn't
   pass ``communicator=`` on the VM.  PVE picks ``"ssh"`` because
   the base install ships sshd but no guest-agent; cloud-init picks
   ``"guest-agent"`` because the package list seeds it.
3. ``needs_boot_keypress() -> bool`` — return ``True`` for
   installers whose bootloader prompts at the boot menu (Windows is
   the usual offender).  The orchestrator spams ENTER if so.
4. ``cache_key(vm) -> str`` — see :ref:`builder-cache-key` below.
5. ``prepare_install_domain(vm, run, cache) -> InstallDomain`` —
   the install-phase spec.  See :ref:`builder-install-domain`.
6. ``prepare_run_domain(vm, run, mac_ip_pairs) -> RunDomain`` — the
   run-phase spec.  See :ref:`builder-run-domain`.
7. ``install_manifest(vm, config_hash) -> dict`` — small JSON-able
   summary of what was installed; cached alongside the qcow2 for
   debugging.

Optional:

* ``ready_image(vm, cache, run)`` — only override when
  ``needs_install_phase()`` is ``False`` and you're handing back a
  pre-existing disk.

.. _builder-cache-key:

The cache-key contract (load-bearing)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``cache_key(vm)`` is what makes the install-phase cache safe.  Two
VM specs whose installs would produce **byte-identical disks** must
produce the same key; two specs that would produce **different
disks** must produce different keys.

What goes IN the hash:

* ``vm.iso`` — the install-source URL.  Different ISOs → different
  installs.
* ``vm.users`` — usernames + sudo flag (NOT SSH keys; see exclusions).
* ``vm.pkgs`` — list of ``repr()`` strings.  Different packages →
  different installed system.  ``repr()`` is the canonical form;
  if you accept a new package class, make sure its ``__repr__``
  is stable across runs (no addresses, no random IDs).
* ``vm.post_install_cmds`` — command list as-is.
* ``vm._primary_disk_size()`` — the ``HardDrive(...)`` value.  PVE's
  ``answer.toml`` ``[disk-setup]`` block embeds this; libvirt's
  ``virt-install`` allocates against it.
* Any builder-specific install-time state that affects the disk.
  ``ProxmoxAnswerBuilder`` folds in the ``[network]`` block of
  answer.toml because it bakes into ``/etc/network/interfaces``.
  ``WindowsUnattendedBuilder`` folds in product key, locale,
  computer-name template.

What stays OUT of the hash:

* **SSH keys.**  Operators rotate keys; we don't want every key
  rotation to invalidate the install cache.  SSH keys land in the
  seed but the cached disk doesn't carry them — phase-2 seed
  injection re-stamps keys on every run.
* **Run-phase IPs / MACs.**  Different test runs hit different
  install-phase networks; the cache is keyed by what gets baked
  into the disk, not by which network the disk later attaches to.
  (Exception: if your builder bakes a static IP into the installed
  system — like the PVE answer-toml ``[network]`` block — then
  yes, fold it in.)
* **Time / run-id.**  Anything that changes per run blocks all
  cache hits.

Sanity check: two construction-equivalent specs should hash to the
same key.  Add a ``test_cache_key_*`` block that constructs two
specs with intentionally-equal fields and asserts equal hashes,
then mutates one field at a time to assert the hash splits.

.. _builder-install-domain:

``prepare_install_domain``
~~~~~~~~~~~~~~~~~~~~~~~~~~

Returns an :class:`~testrange.vms.builders.base.InstallDomain`:

* ``work_disk`` — the storage ref the install will write to.
  Allocate via ``run.create_blank_disk(name, size)``.
* ``seed_iso`` — optional seed media path; built and uploaded by
  this method.  Use ``run.path_for(filename)`` for the staging
  location and ``run.storage.transport.write_bytes(ref, bytes)``
  for the upload.
* ``extra_cdroms`` — tuple of additional CD-ROM refs (the prepared
  installer ISO is the usual occupant).  ProxMox has the prepared
  PVE ISO + the answer-toml seed ISO here.
* ``uefi`` — UEFI vs BIOS firmware.  Defaults differ per builder;
  Windows + PVE are UEFI-only in practice, generic Linux can go
  either way.
* ``boot_cdrom`` — set to ``True`` if the installer boots off the
  prepared installer ISO rather than the work disk (PVE does this).
* ``windows`` — ``True`` only for ``WindowsUnattendedBuilder``;
  toggles ``virtio-win`` driver injection and a different domain
  XML shape.

Common gotchas:

* **Power-off vs reboot completion signal.**  Most installers reboot
  at the end; TestRange's install-phase watcher keys on the SHUTOFF
  edge.  If your installer's default is reboot-loop into the
  installed system, configure it to power off (PVE: ``reboot-mode =
  "power-off"``; Debian preseed: ``preseed/late_command`` with
  ``poweroff``).  Otherwise the wait spins until timeout and the
  cached disk lacks the install-completion marker.
* **Network during install.**  Cloud-init ``apt install`` and
  Windows-side downloads need internet.  TestRange attaches every
  install-phase VM to a separate "install vnet" with
  ``internet=True`` regardless of whether the user-declared networks
  have it.  Your builder shouldn't bake the install-vnet's IP into
  the cached disk — use a generic DHCP config or a ``from-answer``-
  style swap that rewrites the IP at run-phase boot.
* **DNS during install.**  PVE installer needs DNS to resolve
  ``download.proxmox.com`` etc.  The install vnet runs dnsmasq on
  its gateway (libvirt) or uses PVE's per-vnet dnsmasq integration
  with the gateway acting as the resolver.  Don't pin a public DNS
  in your seed — let the install vnet's DHCP/DNS service tell the
  guest where to look.

.. _builder-run-domain:

``prepare_run_domain``
~~~~~~~~~~~~~~~~~~~~~~

Returns an :class:`~testrange.vms.builders.base.RunDomain`:

* ``seed_iso`` — optional phase-2 seed.  Cloud-init uses one to
  re-stamp SSH keys on each run; PVE doesn't (the keys baked in at
  install time stay valid).
* ``uefi``, ``windows`` — must match the install-phase values.
  Mismatched OVMF vs SeaBIOS at run-phase boot panics the disk.

The run-phase NIC list is supplied separately as ``mac_ip_pairs``.
Each tuple is ``(mac, cidr_or_ip, gateway, dns)``:

* ``mac`` — deterministic from ``(vm.name, network.name)``;
  consistent across phases so the guest's ``/etc/network/interfaces``
  references stable interface names regardless of which phase
  attached the NIC.
* ``cidr_or_ip`` — either a static IP (cloud-init writes a static
  config) or empty (cloud-init uses DHCP).  TestRange's
  deterministic-pick stamps a registered IP onto the vNIC even when
  the user didn't pass ``ip=``, so this is rarely empty.
* ``gateway`` — empty for ``internet=False`` networks (no default
  route).  Set to the network's ``.1`` host for ``internet=True``.
* ``dns`` — empty for ``dns=False`` networks.  Otherwise points at
  the gateway (where dnsmasq lives, on both backends).

If your builder consumes more network info than this tuple carries
(VLAN tags, additional aliases, IPv6), extend ``mac_ip_pairs`` only
if the data is genuinely backend-neutral; otherwise pull it off
``vm.devices`` directly inside your builder.

Builder registration
~~~~~~~~~~~~~~~~~~~~

Builders auto-select via :func:`testrange.vms.builders.register_builder`.
Each entry is a predicate ``(iso: str) -> bool`` plus a builder
factory.  First match wins; the cloud-init builder is the fallback
because its predicate is "anything that looks like a Linux ISO".

Example::

    from testrange.vms.builders import register_builder

    def is_kickstart_iso(iso: str) -> bool:
        return iso.endswith(".iso") and "rocky" in iso.lower()

    register_builder(is_kickstart_iso, KickstartBuilder)

Predicates should be cheap (substring scan, no I/O).  If yours
needs to download or open the ISO to know, that's a sign the
builder choice belongs at the user's call site, not in the
auto-selector.


.. _orchestrator-checklist:

Adding a new orchestrator backend
---------------------------------

An orchestrator is the backend that lifecycles VMs + networks +
SDN.  Two ship today: libvirt (KVM via ``qemu:///system`` or
``qemu+ssh://``) and Proxmox VE (REST API).  A new orchestrator
might wrap VMware ESXi, AWS EC2, GCP, OpenStack, or local
QEMU/KVM without libvirt.

The contract lives at
:class:`testrange.orchestrator_base.AbstractOrchestrator` and the
shipped backends in :mod:`testrange.backends.{libvirt,proxmox}`
are the working references.

Pre-flight: do you actually need a new orchestrator?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Are you targeting a different libvirt connection URI (remote
  ``qemu+ssh://`` host, custom socket)?  The libvirt orchestrator
  already takes a URI — you don't need a new backend, just pass
  ``host=`` differently.
* Are you running KVM without libvirt?  The libvirt orchestrator's
  domain-XML emitter is the largest moving part; if you can keep
  that, the ``virt-install`` / ``qemu-system`` swap is a small
  delta.
* Does your platform have a real REST API (cloud, ESXi vSphere,
  Proxmox)?  Yes → new orchestrator.  Use ``ProxmoxOrchestrator``
  as the reference shape.

The orchestrator ABC
~~~~~~~~~~~~~~~~~~~~

The shipped orchestrators implement (or inherit) the following.
Every line in this checklist is something you have to think about,
even if your answer is "no-op for this platform":

**Lifecycle**

1. ``__enter__()`` — authenticate, resolve node + storage, ensure
   SDN / equivalent zone exists, run preflight checks (memory,
   dependencies), bring up networks + VMs, enter nested
   orchestrators.
2. ``__exit__(...)`` — tear down in reverse provisioning order.
   Honour ``leak()`` — propagate it to inner orchestrators BEFORE
   closing the nested stack (proxmox got this wrong in an early
   cut; the inner ``__exit__`` ran full teardown despite the
   operator wanting to preserve VMs).  Never raise from teardown
   — log warnings and continue so the original exception
   propagates cleanly.
3. ``leak()`` — set a flag.  Each backend's ``__exit__`` should
   short-circuit destructive teardown when this is set.

**Networks (DHCP / DNS / IPAM)**

This is the most subtle area.  Read it twice.

4. ``_setup_test_networks`` (or equivalent) — bind every network
   to the run ID at the TOP of this method, before registering
   any VMs.  ``bind_run`` clears the network's per-run ledger as
   a side effect, so binding *after* registrations wipes them
   silently — Proxmox shipped this bug for an entire release
   cycle.

5. **DHCP**: pick a model and stick with it cluster-wide.

   * **Bridge-local DHCP** (libvirt's pattern): each network gets
     its own dnsmasq instance bound to the bridge.  MAC reservations
     give registered VMs deterministic IPs; unregistered VMs get
     dynamic-range leases.  Pro: works without an external IPAM
     plugin.  Con: per-bridge state, no cluster view.
   * **Per-vnet DHCP via central IPAM** (Proxmox's pattern): one
     IPAM (PVE has built-in ``pve``-IPAM; ESXi/cloud have their
     own).  Subnets carry a ``dhcp = "..."`` selector at zone
     scope; each VM's ``(mac, ip)`` is registered as an IPAM entry.
     Pro: cluster-wide view.  Con: depends on the IPAM service
     being healthy.

   Either way, reserve a static slice at the low end of each subnet
   for IPAM static reservations and start the dynamic DHCP range
   above it — see
   :data:`testrange.backends.proxmox.network._DHCP_RANGE_RESERVED_HEAD`
   for the layout.

6. **DNS**: ``<vm>.<network>`` should resolve from any guest on
   the network.  libvirt's dnsmasq does this automatically from
   the DHCP reservation table.  Proxmox's per-vnet dnsmasq does it
   when guests DHCP-in with their hostname; static-IP guests need
   a separate DNS plugin or have their hostname registered via
   IPAM (currently not wired on Proxmox — open follow-up).
   Whichever you pick, document it on the orchestrator's class
   docstring so users know what `<vm>.<network>` does (and
   doesn't) resolve.

7. **Subnet pool for install-phase networks**: pick 8-16 subnets
   that don't overlap with anything else on the platform.  See
   ``_INSTALL_SUBNET_POOL`` in either backend.  Keep your pool
   numerically distinct from the other backends' pools so users
   running both side-by-side don't collide (libvirt uses
   ``192.168.240``-``254``; proxmox uses ``192.168.230``-``239``).

8. **IPAM register_vm**: fold MAC + IP into the per-vnet IPAM (if
   the platform has one) so DHCP serves the right address.  POST
   ordering matters — the SDN config has to be applied (e.g.
   ``cluster.sdn.put()``) before the IPAM lookup recognises the
   subnet.  Apply once after subnet create, register IPAM, apply
   again to regenerate the dnsmasq config.

9. **Routing**: SDN subnets typically aren't routed back to the
   test runner.  Either require the user to add routes manually
   (libvirt's pattern: VMs sit on host bridges that are routable
   by definition), use a guest-agent communicator that hops
   through the platform's REST API (Proxmox), or set up a NAT
   gateway as part of orchestration.

**VMs**

10. ``_provision_vms`` — build (install + cache) → start_run →
    register communicator.  The install phase always uses the
    install vnet (with internet); run phase uses the user's
    declared networks.
11. ``vm.shutdown()`` — best-effort, never raise.  Clear cached
    handles at the end (proxmox shipped a bug where ``set_client``
    references survived past orchestrator close, so a stray
    cleanup retry tried to drive a dead transport).

**Cleanup + admin**

12. ``cleanup(module, run_id)`` — sweep every resource the
    orchestrator might have left behind.  Resources you create
    must include the run-id in their name so cleanup can find
    them by prefix.  Don't forget cache-state-shaped resources
    (proxmox's per-VM seed ISOs, install-time templates).
13. ``check_name_collisions(vms, networks)`` — run at construction
    and verify no two VMs / networks share a truncated backend
    name.  Each platform has its own truncation rules: libvirt
    caps domain names at 10 chars, network names at 6.  PVE caps
    SDN vnet names at 8 chars.

**Nested orchestration**

14. ``prepare_outer_vm(hv)`` — class method called when
    ``Hypervisor(orchestrator=YourOrchestrator, ...)`` is
    constructed.  Stamp any apt packages / post-install commands
    the outer VM needs to host an inner instance of you.  KEEP IT
    EMPTY when possible — every package you stamp invalidates the
    outer VM's qcow2 cache hash, so a bootstrap script change
    rebuilds the entire installed image.  See
    ``ProxmoxOrchestrator._bootstrap_pve_node`` for the SSH-side
    alternative (run apt installs after the cached qcow2 boots,
    not as part of the install).
15. ``root_on_vm(hypervisor, outer)`` — class method that
    constructs a fresh inner orchestrator instance pointing at
    *hypervisor*'s reachable IP / API.  Doesn't enter it — the
    outer's ExitStack does that.  This is the right hook for
    SSH-side bootstrap (see #14) — run it inside ``root_on_vm``
    before the inner ``__enter__`` fires.

API surface
~~~~~~~~~~~

For REST-API-driven backends (Proxmox, anything else that takes
``ProxmoxAPI``-style HTTP requests):

* **Validate every endpoint against the upstream schema**, not
  the docs.  Proxmox's ``apidoc.js`` is canonical; we shipped
  several "I bet this endpoint exists" calls that turned out to be
  ``501`` (``GET /cluster/sdn/subnets``) or ``400`` (``hostname``
  on the IPAM POST).  Cross-checking against the schema catches
  these at code-review time instead of at first-contact-with-real-
  PVE time.
* **Differentiate transient from terminal errors in poll loops.**
  ``5xx`` and network errors → retry; ``4xx`` (auth, not-found) →
  bail.  Proxmox's ``_wait_for_task`` looped on permanent errors
  for the full timeout window before this got fixed.
* **Atomic uploads**: when caching base images on a backend,
  upload to a ``.part`` sibling and rename atomically.  A direct-
  to-final-path upload that gets interrupted leaves a partial
  file that ``exists()`` treats as a cache hit on the next run —
  silent corruption.

Footguns
~~~~~~~~

* **Process-global state** (libvirt's ``registerErrorHandler``,
  signal handlers, environment vars).  Concurrent ``run_tests``
  calls share the process; what one orchestrator install/restores
  another can clobber.  Use module-level locks + reference
  counters for shared resources you can't avoid.
* **``Exception`` vs ``BaseException`` in ``__enter__`` rollback.**
  Catch ``BaseException`` so Ctrl+C during a long install also
  triggers rollback before the interrupt propagates.  ``Exception``
  alone leaks every resource the orchestrator had brought up.
* **Static IPs vs DHCP-discovery vNICs.**  When a vNIC reaches
  registration without an explicit ``ip=``, the deterministic-pick
  picks one and stamps it back onto the vNIC.  Downstream readers
  (cloud-init network-config, answer.toml ``[network]`` block)
  must inspect ``vNIC.ip`` after the orchestrator's setup phase,
  not at construction.

Test surface
~~~~~~~~~~~~

For each new orchestrator, ship at minimum:

* ``test_<backend>_orchestrator.py`` — happy path through
  ``__enter__`` + ``__exit__`` with a mocked client.  Verify the
  REST / API call sequence (creates, attaches, deletes) lands in
  the right order.
* ``test_<backend>_install_vnet.py`` — install-vnet picker, pool
  exhaustion, install network teardown.
* ``test_<backend>_root_on_vm.py`` — nested-orchestrator path
  including bootstrap.
* ``test_<backend>_live.py`` — gated on a ``TESTRANGE_<BACKEND>_HOST``
  env var, runs against a real cluster.  Skipped in CI by default.
  Use this to validate API call shapes — mocks lie, but the live
  test will tell you ``cluster/sdn/subnets`` returns 501.

The ``test_backend_contract.py`` file checks every concrete
orchestrator against the abstract contract — your new backend
will get its share of cross-cutting assertions for free once it
inherits from ``AbstractOrchestrator``.
