Changelog
=========

Significant changes to TestRange, newest first.  Versions follow
`Semantic Versioning <https://semver.org/>`_ once the API stabilises;
during the ``0.1.x`` series anything may change.

Unreleased
----------

Proxmox API audit against PVE 9.x schema (`apidoc.js`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cross-checked every proxmoxer call against the PVE API schema
(``https://pve.proxmox.com/pve-docs/api-viewer/apidoc.js``) and
live-validated the SDN paths against a running PVE 9.1.1 node.

**Fixed: ``GET /cluster/sdn/subnets`` doesn't exist in PVE 9.x.**
``ProxmoxOrchestrator._pick_install_subnet`` was calling that
endpoint to enumerate cluster-wide claimed subnets; the live API
returns 501.  The schema has only per-vnet subnet listings
(``GET /cluster/sdn/vnets/{vnet}/subnets``), so the picker now
walks all vnets via ``GET /cluster/sdn/vnets`` and unions their
subnet CIDRs.  Per-subnet entries carry an explicit ``cidr``
field; older PVE versions get a fallback that derives the CIDR
from the auto-generated ``subnet`` ID
(``<zone>-<addr>-<prefix>`` → ``<addr>/<prefix>``).

The previous code soft-failed on 501 with an empty result set,
which silently turned the picker's collision detection into a
no-op — concurrent runs against the same cluster would race the
same install subnet.  The walk fixes that.

**Validated** (no code change needed): every other proxmoxer call
in ``testrange/backends/proxmox/`` matches the schema —
``/nodes/{node}/qemu/...`` for VM lifecycle (create / start /
stop / delete / clone / template / resize / config),
``/nodes/{node}/storage/...`` for content + upload,
``/nodes/{node}/tasks/{upid}/{status,log}`` for task polling,
``/nodes/{node}/apt/changelog`` for the dnsmasq preflight,
``/cluster/sdn/{zones,vnets,subnets,ips}`` for SDN management,
``/cluster/nextid``, ``/version``, and the agent endpoints (which
already use the call-syntax ``agent("file-read")`` to handle
hyphens since proxmoxer's attribute access doesn't translate
``_`` → ``-``).

**Test surface:** ``_orch`` helper in
``test_proxmox_networking_parity.py`` updated to stub the
walk-vnets shape (one synthetic vnet per claimed CIDR) instead
of the no-longer-existent single-call shape; the
``TestInstallSubnetPool`` tests pass unchanged.
``test_proxmox_install_vnet.py``'s shared helper updated the
same way.  Suite: 1047 passed / 14 skipped / 0 failed.

PVE SDN: ``dhcp = "dnsmasq"`` moves to zone scope (PVE 9.x schema)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: subnet POST rejected with "property is not defined in
schema" for the** ``dhcp`` **field.**  Earlier slices put
``dhcp = "dnsmasq"`` on the SDN subnet POST, but PVE 9.x's SDN
schema only accepts that field at the *zone* level — the subnet
endpoint rejects it with::

  400 Bad Request: Parameter verification failed. -
  {'dhcp': 'property is not defined in schema and the schema does
   not allow additional properties'}

Schema split per PVE 9.x:

* **Zone**: ``dhcp = "dnsmasq"`` selects the DHCP backend for
  every subnet under it.
* **Subnet**: ``dhcp-range = ["start-address=…,end-address=…"]``
  defines the lease range; ``dhcp-dns-server`` overrides the
  advertised resolver.  No ``dhcp`` field.

Moved the field to both zone-creation paths:

* :meth:`ProxmoxOrchestrator._ensure_sdn_zone` — POSTs the
  default zone with ``dhcp="dnsmasq"`` set.  Pre-existing zones
  from earlier TestRange versions (no ``dhcp`` field) are
  upgraded in place via PUT so the next subnet create starts
  spawning dnsmasq.
* :meth:`ProxmoxSwitch.start` — same field added to user-defined
  zone POSTs so DHCP/DNS works in non-default zones (VLAN /
  VXLAN / EVPN with explicit ``Switch(...)``).

Subnet POST keeps ``dhcp-range`` and drops ``dhcp``.

**Test surface:** new ``TestZoneCreationCarriesDhcpDnsmasq``
class in ``test_proxmox_networking_parity.py`` with three tests
(create-includes-dhcp, present-with-dhcp-noop,
present-without-dhcp-upgraded).  ``TestSubnetDnsmasq``'s
``test_subnet_post_carries_dhcp_dnsmasq`` renamed to
``test_subnet_post_carries_dhcp_range_only`` and now asserts
``dhcp not in kwargs``.  ``TestProxmoxSwitchLifecycle.test_creates_zone_when_absent``
gains a ``dhcp == "dnsmasq"`` assertion.  Suite is now 1047
passed / 14 skipped / 0 failed.

PVE dnsmasq: preflight via /apt/changelog + disable systemd service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two related fixes after the first-boot script finally started
running its apt steps successfully.

**Fixed: ``_preflight_dnsmasq_installed`` always reported missing
even when ``dnsmasq`` was demonstrably installed.**  The probe was
``GET /nodes/{node}/apt/versions``, but PVE hardcodes that endpoint
to a curated list of "important Proxmox packages" (kernel,
pveproxy, qemu-server, …); ``dnsmasq`` is never in the list
regardless of install state.  Switched to
``GET /nodes/{node}/apt/changelog?name=dnsmasq``, which actually
runs ``apt-get changelog dnsmasq`` server-side and returns the
changelog text on success or errors with a 500 if the package
isn't installed.  Works for any installed package, doesn't depend
on a hardcoded allowlist.

**Added: first-boot script disables ``dnsmasq.service`` after
install.**  Per the PVE SDN docs, after ``apt install dnsmasq`` the
default systemd ``dnsmasq.service`` (shipped enabled by the apt
postinst) must be disabled and stopped, because PVE's SDN spawns
its own dnsmasq instances per-vnet via ``ifupdown`` hooks — not
via the systemd unit.  The systemd unit binds ``0.0.0.0:53/67``
immediately on install and conflicts with every per-vnet instance
PVE tries to start on a bridge.  ``_first_boot_script`` now
appends ``systemctl disable --now dnsmasq`` whenever ``dnsmasq``
is in ``vm.pkgs`` — only that case, since the disable is
dnsmasq-specific.

The preflight error message also gained the ``systemctl disable``
hint so manual installers (pre-existing PVE clusters where the
operator runs the install themselves) don't hit the same
port-conflict footgun.

**Test surface:** ``TestDnsmasqPreflight`` rewritten for the new
endpoint shape (``apt.changelog.get(name="dnsmasq")``); asserts
both the success and "package missing" paths and that the error
message includes the disable hint.  New
``test_dnsmasq_install_is_followed_by_systemctl_disable`` pins
the install→disable ordering, and
``test_disable_only_when_dnsmasq_is_in_pkg_list`` pins that the
systemctl call doesn't fire for unrelated apt packages (a VM
asking for ``tmux`` doesn't get a stray dnsmasq disable).  Suite
is now 1044 passed / 14 skipped / 0 failed.

PVE first-boot: swap enterprise repos for pve-no-subscription
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: first-boot apt-install failed on fresh PVE installs without
a paid subscription.**  The PVE installer pre-configures
``enterprise.proxmox.com/debian/{pve,ceph-squid}`` repos in
``/etc/apt/sources.list.d/``; without an enterprise subscription,
``apt-get update`` returns ``401 Unauthorized`` against those
mirrors and the ``set -euo pipefail`` first-boot script aborts
before reaching the install line.  Net effect: ``dnsmasq`` (and any
other ``vm.pkgs``) silently never installed, the inner
``ProxmoxOrchestrator``'s ``_preflight_dnsmasq_installed`` then
reported "package missing" with no obvious cause.

The first-boot script now removes the enterprise repo files
(both ``.list`` and ``.sources`` variants — PVE 9 ships the
deb822 ``.sources`` format) and adds
``deb http://download.proxmox.com/debian/pve $codename
pve-no-subscription`` to ``/etc/apt/sources.list.d/`` before the
``apt-get update`` call.  The codename is read from
``/etc/os-release`` so PVE 8 (bookworm), PVE 9 (trixie), and any
future release all resolve to the right mirror.

Subscription users who actually want the enterprise repos can
subclass ``ProxmoxAnswerBuilder`` and override
``_first_boot_script``; the default behaviour matches what
TestRange's primary use case (test-range provisioning of
throwaway PVE nodes) needs.

The repo swap is only emitted when ``vm.pkgs`` carries at least
one ``Apt`` package — VMs whose first-boot script only carries
``post_install_cmds`` skip the fetch path entirely and don't
need their repo config touched.

**Test surface:** new
``test_apt_install_swaps_to_pve_no_subscription`` (asserts both
``.list`` and ``.sources`` are removed, the no-subscription URL
is added, codename is dynamic, and the swap precedes
``apt-get update``) plus
``test_no_repo_swap_when_only_post_install_cmds``.  Suite is now
1042 passed / 14 skipped / 0 failed.

**Operator note:** the script body changed, which invalidates both
the qcow2 and the prepared-ISO caches via the cache-key folds
landed in earlier commits.  The next nested-PVE build will
re-prep + re-install once; subsequent runs cache-hit normally.

Operator pause-on-error for SSH-based debugging
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Added: ``TESTRANGE_PAUSE_ON_ERROR=1`` env var.**  When set, any
exception during the orchestrator's ``__enter__`` (libvirt or
proxmox) or during the test function blocks on ``input()`` before
teardown.  Provisioned VMs and SDN vnets stay alive so the
operator can SSH in and inspect log files (``cat
/var/log/proxmox-first-boot.log``), poll PVE state (``pvesh get
/cluster/sdn/vnets``), examine guest network config, etc., before
the resources get torn down.  Intercepts the three places an
exception can fire: libvirt ``__enter__``, proxmox ``__enter__``,
and the test body inside ``Test.run``.

EOF / Ctrl+C at the prompt lets teardown proceed without
re-raising — operators can ^C twice to interrupt the whole run if
they actually want to abort.  Default off; pause-only-on-opt-in
keeps CI runs from hanging on a debug prompt.

Implementation lives in a tiny new module
``testrange/_debug.py``; the prompt also surfaces the
orchestrator's ``keep_alive_hints()`` so the operator sees exactly
which ``virsh`` / ``pvesh`` invocations correspond to live
resources.

**Test surface:** new ``tests/test_debug.py`` (6 tests):
no-op-when-unset, prompts-when-set, EOF + KeyboardInterrupt let
teardown proceed, orchestrator hints printed, and
hints-failure-is-swallowed-not-raised.  Suite is now 1040 passed
/ 14 skipped / 0 failed.

The example ``examples/nested_proxmox_public_private.py`` gets a
"Debugging an install/test failure" docstring section pointing at
the env var and listing useful starting points (the first-boot
log, dnsmasq presence check, SDN vnet listing).

PVE first-boot: prep-version cache key (closes the cache-vs-fix loop)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Final piece of the first-boot saga: the previous chmod-0755 fix
in ``prepare_iso_bytes`` was correct but didn't take effect on
re-runs because the *prepared-ISO cache* hit the broken file from
the previous prep version.  The cache key was hashing only the
input bytes (vanilla ISO + first-boot script), not the prep
*behaviour*.  Same input → same hash → cache hit on the file that
was built before the chmod fix existed.

**Fixed: prepared-ISO cache key now incorporates a behaviour
version.**  New ``PREP_VERSION = "v2-chmod0755-firstboot"``
constant in ``testrange/vms/builders/_proxmox_prepare.py``;
``CacheManager.get_proxmox_prepared_iso`` folds an 8-char sha256
prefix of it into the cache filename
(``proxmox-prepared-<vanilla>-<version>[-fb<script>].iso``).  Bump
the constant whenever ``prepare_iso_bytes`` semantics change
(POSIX mode bits on embedded files, boot-image preservation
strategy, mode-toml schema, etc.) — every cached prepared ISO
re-preps on next access, picking up the new behaviour.

The ``v2`` value bakes in the chmod-0755 fix from the previous
slice so existing v1 caches (where the embedded
``/proxmox-first-boot`` was mode 0600 and PVE silently couldn't
exec it) get bypassed and re-prepped.

**Test surface:** new
``tests/test_cache.py::TestGetProxmoxPreparedIso::test_prep_version_bump_invalidates_cache``
monkeypatches ``PREP_VERSION`` to a different value and asserts
the cache misses + re-preps.  Suite is now 1034 passed / 14
skipped / 0 failed.

**Operator impact:** cached prepared ISOs from before this commit
(filename pattern ``proxmox-prepared-<vanilla>[-fb<script>].iso``,
no version segment) become orphans on disk — the new cache key
doesn't match them.  Safe to ``rm
/var/tmp/testrange/<user>/images/proxmox-prepared-*-fb*.iso`` to
reclaim space; they'll regenerate on first PVE Hypervisor build
under the new key.

PVE first-boot: chmod 0755 + script body in qcow2 cache key
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two related fixes for the cache-vs-install gap that left the
first-boot mechanism silently broken even after the previous
"prepared ISO not seed ISO" fix.

**Fixed: first-boot script is now chmod 0755 on the prepared
ISO.**  ``tempfile.NamedTemporaryFile`` creates files mode 0600
and xorriso's ``-map`` preserves source POSIX bits via Rock
Ridge, so the embedded ``/proxmox-first-boot`` was landing
unexecutable.  Verified empirically by extracting that file from
a reference ISO produced by ``proxmox-auto-install-assistant
prepare-iso --on-first-boot SCRIPT`` (the upstream tool produces
0755) and from our own cached prepared ISO (we were producing
0600).  ``prepare_iso_bytes`` now ``chmod 0755``'s the script
temp file before xorriso reads it, matching upstream.

**Fixed: ``ProxmoxAnswerBuilder.cache_key`` now folds in the
rendered first-boot script body.**  This is what made the chmod
fix not appear to work on its own: the prepared-ISO cache key
already incorporated the script body's hash (so a script change
correctly produced a fresh prepared ISO), but the **qcow2 cache
key didn't** (it hashed only ``vm.pkgs`` / ``vm.post_install_cmds``
/ ``[network]``, not the rendered script).  Result: a script-side
fix produced a new prepared ISO, but the qcow2 cache hit the old
broken install and the new prepared ISO was never used — the VM
booted from a disk where first-boot had already failed silently.
Folding ``_first_boot_script(vm)`` into ``cache_key`` guarantees
that any rendering change (chmod, embed mechanism, the script's
own commands) invalidates the qcow2 cache too.  No-script case
hashes an empty string so VMs with no ``vm.pkgs`` /
``vm.post_install_cmds`` keep the same cache key as before.

**Test surface:** new
``tests/test_proxmox_answer.py::TestProxmoxAnswerBuilderCacheKey::test_changes_with_first_boot_script_body``
pins the contract: monkeypatching ``_first_boot_script`` to a
different return value MUST change ``cache_key``.  Suite is now
1033 passed / 14 skipped / 0 failed.

**Operator note:** existing cached qcow2 images for PVE Hypervisor
VMs will be invalidated by this change (their cache key now
includes a hash of the first-boot script body, which they
previously ignored).  The next run rebuilds with the chmod fix
applied; subsequent runs cache-hit normally.

PVE first-boot script lands on the prepared ISO, not the seed ISO
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: first-boot script embedded at the wrong ISO path.**  The
previous "Proxmox SDN" slice (just below) added the
``[first-boot] source = "from-iso"`` mechanism so PVE Hypervisor
builds could install ``dnsmasq`` (and any user-supplied
``vm.pkgs``).  The script body was embedded on the *answer seed
ISO* at ``/first-boot``; PVE's ``proxmox-fetch-answer`` reads it
from the *prepared installer ISO* at ``/proxmox-first-boot``
instead, so the install aborted with "Failed loading first-boot
executable from iso (was iso prepared with --on-first-boot)".
Path verified by grepping the local
``/usr/bin/proxmox-auto-install-assistant`` binary.

The script now lives where PVE expects it: an extended
``prepare_iso_bytes`` (in
``testrange/vms/builders/_proxmox_prepare.py``) takes an optional
``first_boot_script=`` kwarg and adds a second ``-map`` to the
xorriso invocation, embedding the script at ``/proxmox-first-boot``
alongside the existing ``/auto-installer-mode.toml``.
``CacheManager.get_proxmox_prepared_iso`` grew the same kwarg and
now hashes the script body into the prepared-ISO cache key
(``proxmox-prepared-<vanilla-sha>-fb<script-sha>.iso``) — same
vanilla + same script gets a cache hit, different script triggers
a fresh prep.  ``ProxmoxAnswerBuilder.prepare_install_domain``
threads the rendered script to the prepared-ISO call instead of
the seed-ISO call; ``build_proxmox_seed_iso_bytes`` loses its
``first_boot_script=`` parameter (it never belonged there).

**Test surface:** new
``tests/test_cache.py::TestGetProxmoxPreparedIso::test_first_boot_script_changes_cache_key``
pins the cache-key contract (no script vs. two different scripts
→ three distinct cached files).  New
``tests/test_proxmox_answer.py::TestPrepareInstallDomain::test_first_boot_script_threads_to_prepared_iso``
asserts the answer builder calls
``cache.get_proxmox_prepared_iso`` with the rendered script.
``TestBuildProxmoxSeedIsoBytes`` keeps a single test asserting
the seed ISO carries *only* ``answer.toml`` (no stray
``/first-boot`` from a future refactor).  Suite is now 1032
passed / 14 skipped / 0 failed.

Proxmox SDN: per-vnet dnsmasq + IPAM (libvirt parity)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Closes the last big gap between the libvirt and Proxmox backends:
guests on a Proxmox SDN vnet now get the same per-network
DHCP + DNS surface libvirt's bridge-local dnsmasq has always
provided.

**Changed: every TestRange-created subnet ships with**
``dhcp = "dnsmasq"`` **enabled.**
:meth:`ProxmoxVirtualNetwork.start` POSTs the SDN subnet with
PVE's ``dhcp = "dnsmasq"`` field set, plus a ``dhcp-range`` covering
the subnet's high half (``.11`` upward — the low ten host addresses
stay reserved for IPAM static entries).  PVE then spawns a per-vnet
``dnsmasq`` instance bound to the gateway address that serves DHCP
+ DNS for the vnet.

**Added: IPAM registration for every** :meth:`register_vm` **call.**
Each ``(mac, ip, hostname=<vm>.<vnet>)`` tuple lands in PVE's
``pve``-IPAM via ``POST /cluster/sdn/vnets/{vnet}/ips``.  PVE turns
those into ``dhcp-host=mac,ip,hostname`` directives in the
auto-generated dnsmasq config, which gives:

* deterministic DHCP leases — a registered MAC always gets its
  reserved IP, so test assertions naming expected IPs stay stable
  across runs;
* libvirt-style FQDN DNS — querying the gateway for ``<vm>.<vnet>``
  from another guest on the same vnet returns the right IP, with
  no extra DNS-plugin (PowerDNS) configuration.

**Added: ``dnsmasq`` apt-package preflight in
:meth:`ProxmoxOrchestrator.__enter__`.**  Substring-searches
``GET /nodes/{node}/apt/versions`` for ``dnsmasq``; raises
:class:`OrchestratorError` with apt/dnf install hints if missing.
Catches the dependency at orchestrator entry rather than letting it
manifest as a cryptic guest-boot timeout.

**Added: ``dnsmasq`` injection into the PVE Hypervisor's default
package list.**  New
:meth:`ProxmoxOrchestrator.prepare_outer_vm` override prepends
``Apt("dnsmasq")`` so any
``Hypervisor(orchestrator=ProxmoxOrchestrator, …)`` build
satisfies the preflight by construction — no manual install on
the freshly-built PVE node.  This in turn required teaching
:class:`~testrange.vms.builders.ProxmoxAnswerBuilder` to consume
``vm.pkgs`` / ``vm.post_install_cmds`` (silently ignored before):
when either is set, the answer-toml emitter adds a ``[first-boot]``
section pointing at a ``/first-boot`` script on the seed ISO that
runs ``apt-get update`` + ``apt-get install -y <pkgs>`` followed by
the post-install commands.  Non-Apt packages on PVE are skipped
with a warning (the platform is Debian-based; Pip/Dnf/Brew don't
make sense as install-time deps for the host).

**Removed: ``ProxmoxOrchestrator(install_dns=…)`` kwarg.**  The
kwarg was a stop-gap for "PVE SDN doesn't ship a per-bridge
resolver"; with the dnsmasq integration now standard, dnsmasq is
the resolver and its upstream forwarder is whatever the PVE
node's ``/etc/resolv.conf`` lists.  Air-gapped / sovereign-DNS
deploys configure the node-level resolv.conf instead — there's no
clean per-vnet upstream override through PVE's SDN API today, and
the kwarg was always going to give the wrong impression about
what could be controlled.  Run-phase NICs on ``dns=True`` networks
now point at the gateway IP (which is dnsmasq), matching the
libvirt backend's bridge-local-dnsmasq pattern exactly.  Removed
helpers: ``_install_dns_for`` and ``_PUBLIC_DNS_FALLBACK`` in
``testrange/backends/proxmox/vm.py``.

**Test surface:** the previous ``TestRunPhaseDns`` /
``TestInstallDnsKwarg`` classes in
``tests/test_proxmox_networking_parity.py`` are replaced with
``TestDnsmasqPreflight`` (3 tests: package-present pass, package-
missing fail, error-message contents) and ``TestSubnetDnsmasq``
(3 tests: subnet POST carries ``dhcp = "dnsmasq"``, IPAM POST is
called per VM with the FQDN, ``/30`` subnets raise clearly).
``TestRunPhaseDns`` keeps the same shape but asserts gateway-as-DNS
instead of install_dns-as-DNS.  ``test_proxmox_answer.py`` gains a
new ``TestFirstBootScript`` class plus two ``[first-boot]``-aware
seed-ISO tests.  Suite is now 1030 passed / 14 skipped (live PVE
tests, expected) / 0 failed across five back-to-back runs.

Docs + tests cleanup
~~~~~~~~~~~~~~~~~~~~

**Changed: user guide covers the Proxmox networking knobs.**
:doc:`/usage/networks` gains a new ``DHCP-discovery vNICs``
sub-section under "Static IPs" that documents the no-``ip=`` form,
the deterministic-pick rule, and the subnet-exhausted error path.
A second new section, ``Proxmox: install-vnet pool and
install_dns``, walks through the per-run vnet picker (10-entry pool,
``OrchestratorError`` when full, where to widen) and the
``install_dns=`` resolver pin (default ``"1.1.1.1"``, override for
air-gapped / sovereign-DNS / split-horizon, also fixes run-phase
``dns=True`` resolution on PVE SDN).

**Changed: API reference no longer claims the Proxmox backend is
"scaffolding only".**  :doc:`/api/backends`'s "The Proxmox backend"
block previously said the orchestrator's ``__enter__`` raised
``NotImplementedError``.  Rewrote it to reflect what's actually in
the box: the SDN simple-zone, per-run vnet naming, qcow2 install-
cache equivalent, guest-agent communicator, install-vnet pool +
``install_dns``, and the explicit-Switch two-layer model.
:doc:`/usage/installation`'s top sentence loses the
"libvirt is currently the only fully implemented backend" claim
and points readers at the Proxmox-specific install steps below.

**Fixed: two flaky/broken tests.**
``tests/test_orchestrator.py::TestCleanupStaleInstallNetworks::test_runs_before_install_network_start``
intermittently raised ``TypeError: unsupported format string passed
to MagicMock.__format__`` because the test fed the install-cleanup
ordering check a bare ``MagicMock`` VM whose ``_memory_kib()``
return value the memory preflight then tried to format with
``f"{...:.2f}"``.  The test isn't checking memory behaviour, so it
now monkeypatches ``_preflight_memory`` to a no-op.
``tests/test_proxmox_template_cache.py::TestBuildCacheMiss::test_install_flow_runs_and_promotes_then_clones``
broke under ``proxmox: networking parity`` because the new
``install_dns`` look-up path (``getattr(context, "_install_dns",
…)``) returns an auto-generated child mock when ``context`` is a
bare ``MagicMock`` — that mock then leaked into cloud-init seed
serialisation as ``sentinel.DEFAULT`` and crashed YAML.  Pinned a
real ``_install_dns="1.1.1.1"`` on the test's context.  Test suite
now reports 1020 passed, 14 skipped, 0 failed across five
back-to-back runs.

**Fixed: ``examples/nested_proxmox_public_private.py`` no longer
hardcodes the user's ``~/.ssh/id_ed25519`` keypair.**  The example
had a session-debug ``TEMP:`` block bypassing
``_generate_run_keypair()`` because the user's specific
``testrange.exe.xyz`` host has an accept-any-key registration shell
that was contaminating the libvirt RPC stream.  That's a
deployment-specific quirk, not example material; reverted to the
original ephemeral-keypair flow.

**Removed: TODO #11.**  The flaky preflight test it described is now
fixed; the remaining numbered items keep their numbers.

Proxmox networking parity
~~~~~~~~~~~~~~~~~~~~~~~~~

Three Proxmox networking gaps closed together — the same code paths
in :class:`~testrange.backends.proxmox.ProxmoxOrchestrator` and the
same test surface, so it made sense to ship them as one slice.

**Added: DHCP-discovery vNICs on Proxmox.**
:class:`~testrange.devices.vNIC` without an explicit ``ip=`` is now
legal on the Proxmox backend (matching the libvirt backend's existing
behaviour).  The orchestrator picks the next free host address on the
network's subnet — skipping the gateway and any IP another vNIC
already registered — and threads it through cloud-init / answer.toml
exactly as if the user had written ``ip=`` themselves.  Determinism
in declaration order keeps test assertions stable.  Static-IP
behaviour is unchanged; the new path only kicks in when ``ip`` is
``None``.

**Added: install-vnet subnet pool.**  Replaces the single hardcoded
``192.168.230.0/24`` install subnet with a 10-entry pool spanning
``192.168.230.0/24`` – ``192.168.239.0/24`` (still safely below
libvirt's pool at ``192.168.240.0/24``+).  At ``__enter__`` time
``ProxmoxOrchestrator._pick_install_subnet`` queries
``cluster/sdn/subnets`` once and chooses the first pool entry not
already claimed by another in-flight run on the cluster.  Pool-
exhausted backpressure surfaces as a clear
:class:`~testrange.exceptions.OrchestratorError` rather than a
silent collision.

**Added: ``install_dns=`` kwarg on
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator`.**  Defaults
to ``"1.1.1.1"`` (preserves prior behaviour); override for air-
gapped, sovereign-DNS, or split-horizon setups.  Replaces the
``_PUBLIC_DNS`` constant in
``testrange/backends/proxmox/vm.py`` so cloud-init / answer.toml
advertise the same resolver across both install and run phases.

**Fixed: run-phase NICs on ``dns=True`` networks no longer point at a
dead address.**  Previously
:meth:`ProxmoxOrchestrator._vm_network_refs` set ``nameserver =
net.gateway_ip if net.dns else ""``, but PVE doesn't run a resolver
on the SDN gateway, so a ``dns=True`` network produced
``/etc/resolv.conf`` pointing at an unresolvable address.  Post-fix:
``dns=True`` resolves to the orchestrator's ``install_dns``;
``dns=False`` continues to leave the nameserver empty.  Tests that
relied on names at run time silently failed before; they work now.

ProxMox installer ISO prep moves to xorriso
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: prepared PVE ISO no longer drops to ``grub>`` shell on
UEFI.**  ``testrange/vms/builders/_proxmox_prepare.py`` previously
used :mod:`pycdlib` to add ``/auto-installer-mode.toml`` to a vanilla
PVE installer ISO and write a new image.  pycdlib's ``write_fp()``
only preserves the basic El Torito boot record — it strips the
hybrid GPT/MBR layout, the ``--grub2-mbr`` hybrid-MBR setup, the
HFS+ wrapper, and (critically) the ``-efi-boot-part`` reference that
wires the EFI System Partition into the GPT.  PVE's UEFI GRUB binary
walks the GPT to locate the ESP at boot; without it, GRUB started
fine but couldn't find ``/boot/grub/grub.cfg`` and dropped to the
interactive ``grub>`` shell every time.

Replaced with a ``xorriso -indev VANILLA -outdev OUT -boot_image any
keep -map TOML /auto-installer-mode.toml -commit`` invocation.  The
``-boot_image any keep`` flag preserves every boot-related artefact
byte-for-byte while xorriso appends the new file; ``-return_with
FAILURE 32`` lifts past xorriso's benign post-write SORRY about the
protective MBR's partition-size field still encoding the original
image size.

**Added: ``xorriso`` as a Proxmox-backend system dependency.**
Documented in :doc:`/usage/installation` under "ProxMox VE installs
(optional)".  Missing-binary path raises a clear
:class:`~testrange.vms.builders._proxmox_prepare.ProxmoxPrepareError`
with apt / dnf / brew install hints; never produces a broken cached
ISO.  Migration plan to drop the binary lives in ``TODO.md`` #10.

Backend-neutral Hypervisor + per-orchestrator outer-VM payload
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Changed: :class:`testrange.Hypervisor` is now a single backend-
neutral class.**  The previous design exposed a libvirt-specific
``Hypervisor(LibvirtVM, AbstractHypervisor)`` at the top level that
auto-injected ``libvirt-daemon-system`` / ``qemu-system-x86`` /
``libvirt-clients`` apt packages and a ``systemctl enable libvirtd``
post-install hook into every Hypervisor spec — useful for libvirt-
on-libvirt nesting, dead weight (and cache-hash pollution) on every
other inner-orchestrator combination.

The new top-level :class:`Hypervisor` is a
:class:`~testrange.GenericVM` plus the three
:class:`~testrange.AbstractHypervisor` data fields.  The outer
orchestrator's existing ``_promote_to_<backend>`` pipeline now
recognises hypervisor inputs and translates them into the backend-
flavoured concrete Hypervisor (``Libvirt|ProxmoxVM +
AbstractHypervisor``) so the lifecycle methods provisioning expects
(``_memory_kib``, ``build``, ``start_run``, …) exist when called.

**Added:** :meth:`AbstractOrchestrator.prepare_outer_vm` classmethod
(default no-op).  Each orchestrator class declares its own outer-VM
payload — ``LibvirtOrchestrator.prepare_outer_vm`` injects the
libvirtd setup; ``ProxmoxOrchestrator`` keeps the no-op default
because the PVE installer is the whole install phase.  All four
cross-product cases (libvirt × {libvirt, proxmox} × proxmox × {libvirt,
proxmox}) work uniformly.

**Lifted:** ``_vcpu_count`` / ``_memory_kib`` / ``_memory_mib`` /
``_primary_disk_size`` / ``_network_refs`` from ``LibvirtVM`` and
``ProxmoxVM`` up to :class:`~testrange.AbstractVM`.  They were
duplicated verbatim across both backends and the libvirt memory
preflight needs them on a top-level Hypervisor before promotion runs.

Switch / VirtualNetwork two-layer model
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The networking surface gained an explicit switch layer mirroring
the standard L2-virtualisation model (ESXi vSwitch + Port Group;
Proxmox SDN Zone + VNet).  For backends that model switches as a
separate layer, this lets one switch host many networks and binds
physical NICs at the switch level instead of per-network.

**Added: :class:`testrange.AbstractSwitch` ABC** in
``testrange/networks/base.py``.  Carries ``name``, optional
``switch_type`` (backend-specific flavour), and optional
``uplinks`` (physical-NIC bindings).  Defines ``start`` /
``stop`` / ``backend_name`` lifecycle, parallel to
:class:`AbstractVirtualNetwork`.

**Added: :class:`testrange.Switch`** — backend-agnostic spec
(parallel to :class:`~testrange.GenericVM`).  Promoted to the
orchestrator's native ``<Backend>Switch`` at ``__init__``.

**Added: :class:`testrange.AbstractVirtualNetwork`'s ``switch=``
parameter.**  Optional reference to a switch instance (or its
name as a string).  ``None`` (default) means "backend's default
switch" — every existing
``VirtualNetwork(name, subnet, ...)`` call works unchanged.

**Added:
:class:`testrange.backends.proxmox.ProxmoxSwitch`** — maps an
:class:`AbstractSwitch` to a PVE SDN zone.  Accepts
``switch_type`` in ``{"simple", "vlan", "qinq", "vxlan",
"evpn"}``, ``uplinks`` (forwarded as PVE's ``bridge=`` for
VLAN/QinQ zones), ``mtu``, and free-form ``zone_extra={...}``
for VXLAN/EVPN knobs not modelled first-class.  Lifecycle is
idempotent: a zone that's already present at ``start`` is
reused as-is and left alone on ``stop``.

**Changed:
:class:`testrange.backends.proxmox.ProxmoxOrchestrator` gained a
``switches=`` kwarg.**  Each declared switch is promoted, brought
up before the user's vnets in ``__enter__``, and torn down after
them in ``__exit__``.  Backwards compatible: omitting the kwarg
keeps the pre-Switch behaviour where every vnet lives in the
orchestrator's default ``"tr"`` simple zone.

**Added: ``examples/proxmox_explicit_zones.py``** — runnable
end-to-end example showing two switches (a simple zone for
isolated test traffic, a VLAN zone bound to a physical uplink)
each hosting multiple vnets.

ProxMox VE: nested orchestration + guest-agent + multi-NIC + install vnet
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ProxMox backend gained the surface area needed to drive the
end-to-end :mod:`examples.nested_proxmox_public_private` example
(an outer libvirt orchestrator boots a PVE Hypervisor; a nested
``ProxmoxOrchestrator`` provisions inner VMs + SDN networks
inside it).  The same fixes apply to a stand-alone remote
``ProxmoxOrchestrator`` — none of them are nesting-specific.

**Added: ``ProxmoxOrchestrator.root_on_vm()`` + nested-stack
unwind.**  Mirrors the libvirt backend's pattern: given a booted
PVE Hypervisor VM, build a configured-but-not-entered inner
``ProxmoxOrchestrator`` pointing at the PVE REST endpoint on the
hypervisor's static IP.  Auth via the hypervisor's root
credential, ``verify_ssl=False`` (PVE ships a self-signed cert).
Waits for ``pveproxy.service`` to reach ``active`` before
returning so the outer orchestrator's ``ExitStack`` doesn't race
the API daemon's startup.  ``__exit__`` unwinds inner
orchestrators first (LIFO) so each inner ``__exit__`` runs while
its hosting PVE VM is still alive.

**Added: real
:class:`~testrange.backends.proxmox.guest_agent.ProxmoxGuestAgentCommunicator`
over PVE REST.** Replaces the stub.  Drives ``qemu-guest-agent``
inside the guest via PVE's ``/api2/json/nodes/{node}/qemu/{vmid}/agent/*``
endpoints.  No inner-VM IP routability needed — agent traffic
hops through PVE's host-mediated virtio-serial channel, so
nested topologies whose inner SDN subnets aren't routed back to
the test runner host work without ``ip route add`` choreography.
Wired through ``ProxmoxVM._make_guest_agent_communicator``;
``communicator='guest-agent'`` on a ``ProxmoxVM`` is now end-to-end.

**Added: dedicated install-phase SDN vnet.**
``ProxmoxOrchestrator`` brings up a separate
:class:`~testrange.backends.proxmox.network.ProxmoxVirtualNetwork`
on a subnet from ``192.168.230.0/24`` – ``192.168.239.0/24``
(``internet=True``) for every install pass.  Without it, a VM
whose only declared NIC was on a ``internet=False`` user network
would hang indefinitely on ``apt install`` during cloud-init.
Symmetric with the libvirt backend's ``tr-instal-*`` network.

**Added: install-phase cloud-init seed describes the install NIC.**
``ProxmoxVM._build_install_mac_ip_pairs`` now returns a single
entry whose MAC + IP + subnet match the install vnet's actual
NIC, not the user's declared NICs.  cloud-init ``Not all expected
physical devices present`` errors gone.

**Added: multi-NIC support in ``ProxmoxVM.start_run``.**  Every
declared :class:`~testrange.devices.vNIC` gets attached at run
phase as ``net0`` … ``netN``, not just ``net0``.  Dual-homed
inner VMs no longer trip cloud-init's "expected MAC missing"
guard.

**Added: PVE storage-upload UPID waiter.**  ``ProxmoxVM`` now
captures the UPID returned from ``upload.create()`` and polls
``/tasks/{upid}/status`` until ``stopped`` before returning.
Closes a class of races where the next REST call referenced a
file the async write hadn't flushed yet (manifested as
intermittent ``500 Internal Server Error: volume … does not exist``
on ``config.put`` immediately after a phase-2 seed upload).

**Added: ``GenericVM`` / ``LibvirtVM`` → ``ProxmoxVM`` and
non-Proxmox network → ``ProxmoxVirtualNetwork`` promotion at
``__init__``.**  Top-level ``testrange.VirtualNetwork`` resolves
to the libvirt-flavoured class for ergonomics, so a user
constructing
``Hypervisor(orchestrator=ProxmoxOrchestrator, networks=[VirtualNetwork(...)])``
no longer hands the inner orchestrator a libvirt-shaped object
that explodes on ``.start()``.  ``RunDir`` now constructed
unconditionally in ``__enter__`` (was a latent ``None``-deref in
``ProxmoxVM.build``'s clone-name code).

**Open follow-ups** — see ``TODO.md`` at the repo root.  Headline
items: hardcoded install-vnet subnet (no concurrent-runs-against-
the-same-PVE-zone support), hardcoded public DNS in the install
seed (PVE SDN doesn't ship a per-bridge resolver), and the
``RunDir``-as-id-carrier pattern.

ProxMox VE template cache
~~~~~~~~~~~~~~~~~~~~~~~~~

**Added: PVE-template-as-cache for ``ProxmoxVM``.** ``build()``
now looks up an existing PVE template named
``tr-template-<config_hash[:12]>`` before doing anything; on a hit
the install path is skipped entirely and the template is cloned
into a fresh run VMID.  On a miss, the install runs, then
``POST /qemu/{vmid}/template`` promotes the install VMID to a
template that subsequent runs hit.  Cache key is the same hash the
libvirt qcow2 cache uses — same spec, same hit, two physical
caches.

Phase-2 cloud-init seed: the cloned VMID inherits the install seed
+ install NIC from the template, both of which need replacing
before the run-phase boot.  ``start_run()`` writes a phase-2 seed
ISO with a rotated instance-id and the run-phase
``mac_ip_pairs``, uploads it, and ``PUT``\ s the cloned VMID's
config to swap ``ide2`` (install seed → phase-2 seed) and ``net0``
(install NIC → run-phase NIC with fresh MAC + run bridge).
Without the rotation cloud-init treats the clone as the same
instance and skips applying the new network-config — VM keeps the
install-network DHCP and SSH attach times out.

Concurrency: a per-config-hash file lock around the find-template
+ install + promote sequence so two test processes building the
same spec at the same time don't race to create duplicate
templates.  Same lock primitive
(:func:`~testrange._concurrency.vm_build_lock`) the libvirt
backend uses.

Cleanup symmetry: ``ProxmoxOrchestrator.cleanup(run_id)``
reconstructs per-run clone names
(``tr-<vm[:10]>-<run_id[:8]>``) + per-run phase-2 seed filenames
+ per-run SDN vnet names and deletes them.  Templates
(``tr-template-*``) are explicitly preserved even if a name
pattern match points at one — they're persistent cache state, the
same way the libvirt qcow2 snapshot cache is.

**Added: template-cache CLI.** ``testrange proxmox-list-templates``
shows every TestRange-managed PVE template on a node;
``testrange proxmox-prune-templates`` deletes them, optionally
filtered by ``--name``.  Both commands open their own connection
and don't require ``__enter__``, so they're safe to invoke from a
shell with the same ``--orchestrator`` URL the run uses.

**Added: crash-recovery for half-promoted templates.** If an
install dies between ``qm create`` and ``qm template`` it leaves a
VMID with the target template's display name but no ``template``
flag.  The next install attempt for the same spec now sweeps such
orphans automatically (logged at WARNING) before re-running
``qm create`` so the install doesn't abort with a duplicate-name
error.

**Added: linked-then-full clone fallback.** Run-phase clones now
attempt ``full=0`` first (snapshot-backed, seconds) and fall back
to ``full=1`` automatically if the storage pool refuses linked
clones (raw LVM, NFS, Ceph without snapshot support).  Same code
path, no user-visible config knob — the user just gets a working
clone either way.

**Added: ``GenericVM`` → ``ProxmoxVM`` promotion.** Backend-agnostic
``GenericVM`` specs are promoted to ``ProxmoxVM`` at orchestrator
construction, mirroring the libvirt backend.  The same test fixture
now runs across both backends without per-backend wrapping.

ProxMox VE install path
~~~~~~~~~~~~~~~~~~~~~~~

**Added: ``ProxmoxAnswerBuilder``** for unattended ProxMox VE
installs.  Auto-selected for ``iso=`` strings matching
``proxmox-ve[-_]*.iso``; emits an ``answer.toml`` to a
``PROXMOX-AIS``-labeled seed ISO and prepares the main installer ISO
in pure Python (no ``proxmox-auto-install-assistant`` host
dependency, no ``xorriso``).  Working PVE 9.x out of the box;
declare ``VirtualNetworkRef(..., ip="...")`` for the run-phase
network and the builder synthesises a ``from-answer`` static config
that survives the install-to-run network swap.  Example:
``examples/nested_proxmox_public_private.py``.

The path lives on top of six PVE-specific behaviours, all
regression-tested.  Five are just correct handling of how PVE 9.x
ships rather than workarounds: activation via
``/cdrom/auto-installer-mode.toml`` at the ISO root (PVE 9.x;
earlier releases looked inside the initrd); kebab-case
``answer.toml`` field names that don't match the underscored
mode-file fields; ``reboot-mode = "power-off"`` to turn the
installer's reboot into the SHUTOFF the cache pipeline expects;
the ``from-dhcp``-vs-``from-answer`` distinction (the former
freezes the install-phase lease as static, the latter takes the
answer's static config verbatim); and interface-name-based NIC
filtering (the install-phase MAC differs from the run-phase MAC,
but interface name is stable across the swap).  The one true
workaround is OVMF-only firmware to sidestep a SeaBIOS + q35 +
SATA-CD GRUB triple-fault during PVE's first boot.

PVE installs also exercise the per-VM UEFI NVRAM sidecar described
under *Cache layout* below, but that is a libvirt-backend mechanism
needed by any UEFI install (Windows was the first guest to surface
it); it lives in :mod:`testrange.backends.libvirt.vm` and the cache
layer, not in the ProxMox builder.

Cache layout
~~~~~~~~~~~~

**Added: per-VM UEFI NVRAM sidecar at ``<vms_dir>/<hash>.nvram.fd``.**
Install-phase NVRAM (where the installer writes EFI ``BootOrder``
entries) is now snapshotted into the cache alongside the qcow2,
because libvirt's ``VIR_DOMAIN_UNDEFINE_NVRAM`` deletes the per-run
NVRAM at teardown.  Run-phase domains seed their NVRAM from the
cached sidecar rather than the empty global ``OVMF_VARS`` template,
so any UEFI install whose distro doesn't write the
``/EFI/BOOT/BOOTX64.EFI`` removable-path fallback (PVE included)
still boots cleanly.  New helpers:
:meth:`~testrange.cache.CacheManager.vm_nvram_ref`,
:meth:`~testrange.cache.CacheManager.store_vm_nvram`,
:meth:`~testrange.cache.CacheManager.get_vm_nvram`.  Backwards-
compatible: existing entries without sidecars stay valid for BIOS
installs (cloud-init), and a missing sidecar on a UEFI install
falls through to the template just as before.

**Added: prepared-ISO cache for ProxMox installer media** at
``<images_dir>/proxmox-prepared-<sha>.iso``, populated by
:meth:`~testrange.cache.CacheManager.get_proxmox_prepared_iso` on
first use.  Keyed by the SHA-256 of the vanilla ISO so the
expensive (~1 s) prep step happens once per upstream version,
amortised across every VM that builds against it.

DAC ownership of UEFI NVRAM
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: NVRAM file ``Permission denied`` after install completes.**
When libvirt creates the per-domain NVRAM by copying the
``<nvram template="...">`` source on first ``domain.create()``,
the DAC security driver records no original-owner xattr — the
file stays ``libvirt-qemu:0600`` after the domain stops, and the
NVRAM-snapshot read fails with EACCES on any non-libvirt-qemu user.
:func:`~testrange.backends.libvirt.vm._preseed_nvram` pre-creates
the NVRAM as the invoking user with mode ``0644`` *before*
``defineXML``; DAC's ``remember_owner`` xattr then has an original
owner to restore on shutdown, and the snapshot reader can open
the file.  Behaviour is identical at install time (the seeded
bytes are the OVMF_VARS template, exactly what libvirt would have
copied).

Windows install path
~~~~~~~~~~~~~~~~~~~~

Four interlocking fixes to make the out-of-the-box
``WindowsUnattendedBuilder`` flow actually reach a cached Windows
image on a standard multi-edition consumer ISO.

**Fixed: UEFI boot-order bug caused installs to hang indefinitely.**
The install-phase domain listed CD-ROMs as ``[seed, *extras]``, which
put the autounattend seed ISO first.  libvirt expands
``<boot dev='cdrom'/>`` by assigning ``bootindex=1`` to the *first*
CD-ROM in the device list, so UEFI tried to boot a non-bootable ISO,
fell through to an empty disk, and idled forever — the
``winbox-install.qcow2`` stayed at ~193 KB across multi-hour runs.
When ``boot_cdrom=True`` the bootable ``extra_cdroms[0]`` (the Windows
ISO) is now placed first; the seed ISO is merely attached so Setup
scans its volume for ``autounattend.xml``.  Regression:
``test_bootable_cdrom_is_first``.

**Fixed: ``<ProductKey>`` in the wrong schema location.**
The autounattend generator placed ``<ProductKey>`` as a direct child
of ``Microsoft-Windows-Setup``; Microsoft's unattend schema requires
it inside ``<UserData>``.  Setup silently ignored it and reported
*"can't read product key from the answer file"*.  Moved the element
into the correct parent.  Regression:
``test_product_key_nested_inside_userdata``.

**Changed: default ``product_key`` now ships the Windows 10/11 Pro generic install key.**
Multi-edition consumer ISOs (``Win10_*_English_x64.iso`` shape)
refuse to install unattended without *either* a ``ProductKey`` or
explicit edition metadata.  The new default
(``VK7JG-NPHTM-C97JM-9MPGT-3V66T``, publicly documented by Microsoft)
tells Setup to pick Pro and continue.  Does not activate — fine for
test-range use.  Pass ``WindowsUnattendedBuilder(product_key=None)``
to restore the old behaviour for Enterprise-eval / single-edition
ISOs.  Regression: ``test_default_product_key_emitted``.

**Added: orchestrator spams spacebars past the "Press any key" prompt.**
Windows install ISOs under UEFI show a five-second *Press any key to
boot from CD or DVD...* prompt that a headless VM has no way to
satisfy — OVMF exhausts boot options and drops to the EFI shell.
:meth:`~testrange.vms.builders.base.Builder.needs_boot_keypress` is a
new method on the builder ABC (default ``False``);
:class:`~testrange.vms.builders.WindowsUnattendedBuilder` returns
``True``.  When set,
:meth:`~testrange.backends.libvirt.VM._run_install_phase` spawns a
daemon thread that calls ``domain.sendKey(KEY_SPACE)`` once per
second for 30 seconds, then exits.  Thread is joined in the
``finally`` block.  Regression: ``TestBootKeypressSpam`` in
``tests/test_vm_libvirt.py``.

Install-phase resilience
~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: interrupted installs leaked libvirt domains.**
Three compounding bugs meant a ``KeyboardInterrupt`` (or any
exception during a 30-minute Windows install wait) left an orphaned
``tr-build-<vm>-<id>`` domain live under ``qemu:///system`` with no
Python process to tidy it.  All three are fixed:

1. :meth:`~testrange.backends.libvirt.VM._run_install_phase` used a
   local ``domain`` variable and reached the destroy/undefine code
   only on the normal-completion path.  Wrapping the wait loop in
   ``try/finally`` (with a new ``_destroy_and_undefine`` helper)
   guarantees cleanup on every exit — shutoff, timeout, cache-write
   error, ``KeyboardInterrupt``, anything.
2. Even if teardown *had* run, it couldn't see the install domain:
   ``vm.shutdown()`` operates on ``self._domain``, which only
   ``start_run()`` populated.  The install-phase domain is now
   stashed on ``self._install_domain`` as a safety net, and
   ``shutdown()`` cleans both.
3. :meth:`~testrange.backends.libvirt.Orchestrator.__enter__` caught
   only ``Exception``; ``KeyboardInterrupt`` and ``SystemExit``
   derive from ``BaseException`` and bypassed teardown.  Widened the
   handler.

Regressions live in ``tests/test_vm_libvirt.py::TestShutdown``,
``TestInstallPhaseCleanup``, and
``tests/test_teardown_resilience.py::test_keyboardinterrupt_during_enter_triggers_teardown``.

Debugging
~~~~~~~~~

**Added: ``TESTRANGE_VNC=1`` environment-variable toggle.**
When set, :func:`~testrange.backends.libvirt.VM._base_domain_xml`
attaches a VNC graphics device listening on ``127.0.0.1`` with an
auto-assigned port, plus a QXL video device.  Off by default so CI
and headless runs stay silent.  Find the port with
``virsh -c qemu:///system domdisplay <domain>``, tunnel over SSH,
connect with any VNC client.  See :doc:`usage/debugging`.

**Added: "Watching an install-phase VM" section in the debugging guide.**
Covers both the new VNC toggle and the terminal-only
``virsh screenshot`` + ``img2txt`` / ``img2sixel`` workflow for
SSH-only setups.

v0.1.0
------

Initial release.
