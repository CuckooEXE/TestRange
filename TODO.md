# TestRange — TODO / Board

Project task board. This Markdown file is the **source of truth** for what
is in flight; it replaces the `ktui` (kanban-tui) board, migrated out of ktui
on 2026-06-06.

- **Status sections:** Doing (in progress) · Ready (backlog + to-do) · Done · Archive (older history).
- **Categories** (swimlanes): PVE · BACKEND · NET · CACHE · ORCH · BUILD · COMM · PROXY · CORE · CI · DOCS · ESXI · PROV · REL.
- **Ticket shape:** `<type> | <ID>: description` — `type` ∈ feat/bugfix/chore/ci/test/docs/EPIC; `ID` uses the swimlane prefix.

> Migrated 294 tickets — Doing 13, Ready 26, Done 242, Archive 13.

## Doing (11)

### CORE

- [ ] **CORE-41** · `chore` — per-driver preflight capability rejection (firmware/installer-origin)

  > Code-review remediation (feature/builders, 2026-06-01). The Builder ABC says the driver MUST reproduce firmware/installer-origin, but nothing rejects a plan requesting firmware=uefi or installer-origin against a backend that cannot honor it (drivers/base.py:317-324). Latent today (only libvirt ships it). Add a per-driver preflight finding for unsupported firmware/origin, mirroring mgmt_unsupported_findings. User: drivers should offer their own pre-flight that rejects findings like this.

### ESXI

- [ ] **ESXI** · `EPIC` — ESXi (pyVmomi) HypervisorDriver

  > Umbrella for making a STANDALONE ESXi host a first-class TestRange backend implementing the full HypervisorDriver ABC (drivers/base.py), same surface MockDriver (reference) and the in-flight Proxmox driver satisfy.
  >
  > Target: standalone ESXi host at 40.160.34.83 (root). NOT vCenter -> standard vSwitch + portgroup only; DVS/dvportgroup are out of scope (future vCenter ticket). Datastore /folder HTTPS endpoint is the pool-I/O transport (no vmkfstools subprocess, ADR-0001); volume format vmdk. VMware Tools guest-ops need per-call guest creds -> forces in the credential= kwarg the ABC deferred (ADR-0008). Firmware: bios certified, uefi accepted-but-unvalidated.
  >
  > Module layout mirrors drivers/proxmox/: _client/_profile/_naming/_net/_storage/_vm/devices/_guest/_serial/driver.
  >
  > Children: CORE-60 (credential kwarg prereq), spikes ESXI-S1/S2/S3, core ESXI-1..9, ADR + cert tail ESXI-10..15. Pairs with COMM-2 (VMware Tools communicator) and PROXY-1 (console proxy = guest_gateway). Created 2026-06-01.

### ORCH

- [ ] **ORCH-21** · `chore` — harden _VMBuildPlan origin invariant + boot_iso disk-kind

  > Code-review remediation (feature/builders, 2026-06-01).
  > 1. _VMBuildPlan (build_phase.py:68-88): add __post_init__ enforcing exactly-one-of (base_path is None) != (boot_media_path is None). installer_origin derives from base_path alone, so 'both set' silently drops the boot medium and 'neither' yields a blank unbootable disk; the only current guard is a remote OrchestratorError in _probe_vm.
  > 2. Boot media is staged under kind='build_seed' + volume_suffix('build_seed') (build_phase.py:325,329), conflating a bootable installer ISO with a cidata seed. Add a distinct 'boot_iso' (or 'build_media') kind to the documented suffix vocabulary (drivers/base.py:199-207) and use it.

### BUILD

- [ ] **BUILD-18** · `chore` — review nits (ABC docstring, firmware comment, digest helper, dead code, ISO ids, test doubles)

  > Code-review remediation (feature/builders, 2026-06-01). NITs:
  > - base.py:101-120 prepare_boot_media ABC docstring overstates a caching guarantee the seam does not make (caller calls unconditionally); trim to describe the seam, leave caching to concretes. Also move PVE-specific prose out of the generic ABC default (base.py:86-92,108-118).
  > - spec.py:22-29 firmware comment 'media only boots under EFI' contradicts ADR-0022 (hybrid ISO boots BIOS via El Torito) and the ESXi BIOS combo; soften to 'firmware the media was validated under'.
  > - proxmox.py:242-244 vs 202-204 two first-boot-script digests w/ mismatched truncation -> shared _first_boot_digest() helper.
  > - proxmox.py:307,465 drop dead 'root.password or ""' (validation guarantees non-empty).
  > - ISO9660 ids /ANSWER.TOM;1, /BOOT.EFI.CFG deviate from .;1 convention.
  > - Dedup _OriginlessBuilder/_Weird test doubles across files.
  > - _vm.py:100 drop dead _cdrom_xml(dev='sda') default (seed/boot-media collision risk); make dev required.

- [ ] **BUILD-16** · `bugfix` — drop gratuitous ESXi-8 version gating (disk floor + prose)

  > Code-review remediation (feature/builders, 2026-06-01). Unnecessary-limitation cleanup.
  > - Drop _MIN_OS_DISK_GB=33 hard ValueError floor (esxi.py:44,149-153): the installer fails loud on undersized disk; the builder floor needlessly rejects ESXi 7 and pre-empts ESXi 9. Drop the floor and its test (test_undersized_disk_rejected).
  > - Drop the 'ESXi 8'-specific assertions/prose in esxi.py module + class docstrings (esxi.py:11-15): directives are ESXi 5-8 generic. Soften to 'ESXi (validated on 8)'.

- [ ] **BUILD-15** · `bugfix` — installer-builder footguns (ESXi pw chars, apt_insecure persistence, NIC-flip hardcode)

  > Code-review remediation (feature/builders, 2026-06-01).
  > 1. ESXiKickstartBuilder rootpw injection footgun (_esxi_prepare.py:82): reject root passwords containing newlines/control chars at construction (lab env, not a security boundary, but a malformed-ks footgun). Same for ssh_key.
  > 2. apt_insecure persists TLS-off into the run image (proxmox.py:463-470): rm /etc/apt/apt.conf.d/99-testrange-insecure in the first-boot footer so it does not survive the build.
  > 3. _NETWORK_FLIP hardcodes NIC=enp1s0 (proxmox.py:437-448) while network_interface is configurable and feeds answer.toml filter.ID_NET_NAME. Parameterize the flip on self.network_interface (template/helper called from _first_boot_script) — also makes the prepared-ISO script digest vary with network_interface so a changed NIC re-preps.

### BACKEND

- [ ] **BACKEND-11** · `feat` — remote-libvirt guest_gateway — SSH-jump through the qemu+ssh host (ADR-0020/0021)

  > A remote (qemu+ssh) LibvirtDriver's guests sit on the remote host's internal networks, unreachable from the orchestrator (the depth-2 nested wall, and any SSHCommunicator inner VM). Mirror ProxmoxDriver.guest_gateway: return an SSHJumpGateway through the qemu+ssh host (host/user/key parsed from the connect URI). Local qemu:///system stays None (direct). Motivated by the depth-2 Gateway experiment requested 2026-05-31.

### DOCS

- [ ] **DOCS-22** · `docs` — integrate project logos into README + Sphinx site

  > User added project logos (icon + horizontal wordmark, PNG + SVG). Moved them into `docs/_static/` (was `docs/images/`; `_static/` is what `html_static_path` already serves, and creating it also clears the pre-existing missing-`_static` build warning). Wire-up: README header (horizontal PNG), Sphinx `html_logo` = icon SVG + `html_favicon` = icon PNG in conf.py, horizontal SVG hero on `docs/index.md`.

- [ ] **DOCS-8** · `docs` — ADR-0022 rescope (two prep modules + ESXi patch) + xorriso install note

  > Code-review remediation (feature/builders, 2026-06-01). ADR-0022 is scoped PVE-only / one module but the branch ships a second sanctioned subprocess module (_esxi_prepare.py), acknowledged by the ban whitelist + pyproject. Rescope ADR-0022 to installer-ISO prep generally; name both sanctioned modules; document the ESXi -boot_image any patch divergence (vs PVE keep) and the -rockridge off / two-pass flags. Add the 'apt install xorriso' system-dep note to docs/user/drivers (Proxmox section) — currently only pip install -e ..

### PROV

- [ ] **PROV** · `EPIC` — Bare-metal provisioning: `provision` installs a hypervisor onto iron via an out-of-band controller

  > Umbrella for a third CLI verb — `provision <plan> --profile <name> --controller <name>` — that installs a hypervisor (or any installer-origin OS) onto PHYSICAL hardware via its BMC (Redfish/iLO/iDRAC), leaving a host the existing build/run target with a normal --profile. Pipeline: provision (iron->hypervisor) -> build -> run, decoupled. Design of record: PLAN.md "Bare-metal provisioning via out-of-band controller" + ADR-0027 (pending, PROV-1). Key decisions: third verb, NOT a run flag (re-imaging iron is once-per-server); controller is its OWN ABC, NOT a HypervisorDriver (the box hosts one thing — itself — so a driver would NotImplementedError its core); ProvisioningPlan is nameless (no teardown, stomp-overwrite); HostRecipe = spec x builder (no communicator, explicit builder injection, no front-door classmethods); HostSpec is a requirements contract (Required* devices, 1:1-matched against discovered inventory, undeclared hardware never touched); installer-origin only v1. Children PROV-1..13; deferrals PROV-14..16. Created 2026-06-06.

### REL

- [x] **REL-22** · `bugfix` — build_cache.py: post-install order marker unreadable by the non-root communicator _(done: 2026-06-08)_

  > Found running `tests/plans/generic/build_cache.py` live (REL-14/15/16). `post_install_commands_ran_in_order` reads `/root/order` via the `admin` SSHCommunicator and gets `b''`. Not an ordering or cache bug: the disk-content tests pass, so the post-install echoes *did* run. The marker is written to `/root/order` by cloud-init `runcmd` (as root) at build time, but `/root` is mode 0700, so the non-root `admin` user can't read it at run time — `cat` returns empty stdout and the assert fails. Fix: write the ordering marker to a world-readable path that survives reboot (the other markers live under `/srv`, which is root-owned 0755), and point the test at it. DONE in working tree; changes `runcmd` so the next run is a cache miss + rebuild.

- [x] **REL-23** · `bugfix` — Communicator.close() contract: NativeCommunicator is terminal, SSHCommunicator reconnects _(done: 2026-06-08)_

  > Found running `tests/plans/generic/lifecycle.py` live (REL-16). Four failures, all the same root cause: `churn_survives_repeated_power_cycles`, `reboot_persists_on_disk_state`, `oversized_os_drive_grew_on_first_boot` (latter two cascade — they reuse the communicator the first test already closed), and `headless_survives_power_cycle`. The portable power-cycle idiom — `driver.start_vm(...)` → `com.close()` → `com.execute(...)` — relies on `close()` dropping the stale session and the *next* call transparently reconnecting. `SSHCommunicator` honors this (`close()` nulls `_client`; `_ensure_connected()` re-dials, retrying up to 180s for sshd to return) — `snapshots.py` uses the exact same idiom and passes. `NativeCommunicator.close()` is *terminal* (sets `_closed`, nulls callables, `_check_usable()` raises `CommunicatorClosedError` forever). The cert corpus is the portability spec and two plans depend on the reconnect semantic, so the contract must be uniform. Fix (product side): make `NativeCommunicator.close()` a session reset that leaves the communicator bound + usable (QGA is sessionless — callables are domain-bound closures), and have its `execute`/`read_file`/`write_file` tolerate the post-power-cycle agent-not-ready window by retrying on `GuestAgentError` up to a timeout (mirror SSH's `_ensure_connected` reconnect loop; the bind-time wait in `run_phase._wait_one_communicator_ready` doesn't cover in-test power cycles). Keep the re-*bind* guard (a closed/ bound communicator still must not be re-bound to another VM). Applies to every backend's NativeCommunicator path (libvirt + Proxmox). Document the `Communicator.close()` contract on the ABC + PLAN.

- [x] **REL-24** · `bugfix` — DHCP NICs get no lease on native-communicator guests _(done: 2026-06-08)_

  > Found running `tests/plans/generic/{concurrency,networking}.py` live (REL-16). Symptom: DHCP NICs acquire no lease while static NICs on the *same* guest come up fine. `concurrency.every_node_has_a_distinct_lease`: node shows only `lo`. `networking.client_dhcp_lease_in_pool`: client shows only its static priv-net addr (10.20.0.101), no 10.30.0.x lease. Downstream cascade in networking (all trace to the missing lease): `client_has_exactly_one_default_route` (got []), `client_reaches_public_web_across_labels_via_dns` + `public_web_reaches_internet_through_nat` (curl exit 6 = couldn't resolve — no DHCP-supplied resolver/route). Strong correlation: every SSH-communicator VM leased (build_cache/snapshots/users_credentials on lab-net got 10.40.0.x); every native-communicator VM that needed DHCP did not — even though lab-net's switch/sidecar config is byte-identical between build_cache (SSH, leases) and concurrency (native, no lease). The orchestrator only polls the sidecar dnsmasq lease file for SSH VMs (`discover_ip`), never for native VMs, so native DHCP failures were previously invisible. RESOLVED 2026-06-08: not a guest or sidecar bug — DHCP works. It's an orchestrator race. Live repro (one native + one SSH VM on one DHCP switch) showed the native guest leases at ~11s post-boot, but the orchestrator binds a NativeCommunicator the instant its QGA answers (~8s) and runs tests immediately; SSH VMs are gated by `discover_ip`'s sidecar-lease poll. concurrency's three tests ran in a ~1.5s window starting ~8s in, before the lease landed. Static NICs apply at boot, hence client's static showed but its DHCP NIC didn't. Fix: added `run_phase.wait_dhcp_leases(ctx)` — a backend-agnostic gate (extracted `_wait_for_dhcp_lease` from `discover_ip`) that polls the per-switch sidecar dnsmasq lease file for every DHCP NIC on every VM, wired into `runtime.__enter__` after `wait_communicators_ready`. Regression test `test_qga_dhcp_nic_waits_for_sidecar_lease`. Verified live: concurrency 3/3, networking 7/7 (incl. the pub-sw two-Network-on-one-L2 cross-label DNS + NAT paths, which were only masked by the race — L2 sharing confirmed good).

- [x] **REL-25** · `test` — libvirt integration: serial-sink test stale vs build_nic-OR-boot_media gating _(done: 2026-06-08)_

  > Found running `pytest -m "not proxmox and not esxi"` on feature/release-check: `tests/integration/test_libvirt.py::test_vm_lifecycle_serial_sink_and_snapshots` fails with `DriverError: no serial listener open for 'tr-vm-...-box'`. Not a product regression — the test is stale. It boots a NIC-less, seed-only guest and assumes a seed makes `create_vm` open the unix-socket serial sink, the pre-CORE-47 contract. The gating was deliberately reconciled (CORE-47, ~2026-06-01) to `build_nic is not None or boot_media_ref is not None`: a seed alone is a sidecar (monitored via QGA, not the serial sink), and binding the host-side socket for one is dead weight locally and outright broken on a remote daemon (nested-virt's remote-security-driver constraint, ADR-0021). Every production serial-sink boot sets `build_nic` (build_phase.py:512). FIX (test-side): attach a `boot_media_ref` so the boot is installer-origin and the sink opens per contract, staying NIC-less (the OS disk is bootable at order 1, so the guest still boots the base image and emits console bytes; the medium at order 2 is never reached). DONE 2026-06-08 in the working tree.

- [ ] **REL** · `EPIC` — 1.0.0 validation & release: adversarial e2e suite on an unmanaged nested host fleet, then cut v1.0.0

  > Umbrella for the road to 1.0.0 — a validation pass, not a feature. Prove all three backends (libvirt, Proxmox, ESXi) hold up under the same adversarial end-to-end suite, on the same portable plans, on independently-built hosts; then reconcile docs/PLAN/TODO against validated reality and freeze the public API. Design of record: PLAN.md "1.0.0 validation & release" + ADR-0028 (REL-1).
  >
  > Three pillars: (1) a certification & regression corpus in `tests/plans/{generic,libvirt,proxmox,esxi}/` — one portable PLAN + a few TESTS per file (docstring states WHAT it stresses + WHY), run via `testrange run --profile <name> tests/plans/<tier>/<plan>.py` and NOT collected/executed by pytest (named without a `test_` prefix, no `__init__.py`); generic tier runs on every backend, per-driver tiers pin the driver Hypervisor + that backend's concrete device types. This IS the new backend certification (it supersedes examples/capabilities.py, which is slated for deletion). (2) Unmanaged, scripted nested host fleet in `tools/hypervisor-hosts/` — each hypervisor stood up as a RAW libvirt VM (virt-install + kickstart/answer), independent of TestRange (no GuestHypervisor — validating TestRange with TestRange is circular); tr-egress NAT gives build VMs egress, SUPERSEDING the env-block on ESXI-11/12/13 + BUILD-13. (3) 1.0.0 = all three backends green on the full e2e suite (hard gate) + public-API freeze (flip major_version_zero=false, /api-diff baseline).
  >
  > Children: REL-1 (ADR/PLAN, done), REL-2 (tests/plans scaffolding + README, done 2026-06-07), REL-3..6 (generic plans, done 2026-06-07), REL-7..9 (per-driver plans, done 2026-06-07), REL-10..13 (host fleet), REL-14..16 (run + report, ESXi->PVE->libvirt order), REL-17..19 (docs/PLAN/TODO reconciliation), REL-20 (cut v1.0.0). NO LONGER gated on nested ESXi: ESXI-16/18 (ESXi-as-a-guest) SHELVED post-1.0.0 (2026-06-07); the ESXi backend is certified via REL-11's raw kickstart host (no GuestHypervisor), which was always the plan for the host fleet. Created 2026-06-06.

## Ready (54)

### CORE

- [ ] **CORE-3** · `feat` — `pytest-testrange` plugin

  > Expose ranges + tests as pytest fixtures/items.

### PVE

- [ ] **PVE-45** · `feat` — QGA chunked guest-file-write (lift single-write cap)
  _(blocks: PVE-49, PVE-32)_

  > Lift the ~45 KB single-write cap in drivers/proxmox/_guest.py (_write_file raises GuestAgentError at _guest.py:111).
  >
  > **DEFERRED 2026-05-31 (driver-only judgment call):** not a capabilities.py blocker — every write_file payload in capabilities.py is a few bytes. And PVE's agent/file-write is a **one-shot truncating** write (QGA exposes no handle/offset/append through the PVE wrapper), so chunking cannot be repeated file-write calls — it needs in-guest assembly: write {path}.partN files (each ≤ cap) then agent/exec `cat part0 part1 … > path && rm parts`. That relies on a guest shell + cat/rm and only pays off once a real >45 KB native-agent write exists. Recommended approach recorded; implement when a payload demands it.

- [ ] **PVE-31** · `feat` — multi-node cluster support (node-scoping is baked in)

  > BACKGROUNDED 2026-05-24 (design pending ADR). Discussion concluded the 'cluster' idea is TWO concepts split by where the connection lives: (1) NATIVE CLUSTER (PVE cluster, vCenter) = ONE endpoint with internal hosts + shared SDN/storage -> belongs INSIDE the driver as a placement seam (resolution returns (node,vmid) via /cluster/resources, survives HA migration; create_vm optionally takes/records a placement target). NOT a new Plan type. (2) FEDERATION (Cluster(*hypervisors), N endpoints, no shared L2) = ORCH-2's AbstractHypervisor, cross-backend; the inter-backend-networking problem (sidecar L2 doesn't span backends) is the hard part and must be named up front. v1 SCOPE DECISION: PVE backend is single-node; clusters deferred. Extractable near-term piece (independent of the design, needed for honest feature-complete): a single-node preflight guard that warns/errs when node= is set on a multi-node host (names the migration->teardown-leak risk). Needs an ADR capturing the scope boundary + the two-concept split.

- [ ] **PVE-33** · `feat` — proxmox-specific StoragePool(s) — block storage (lvm/zfs/ceph)
  _(blocks: PVE-46)_

  > Introduce proxmox-specific StoragePool subclass(es) for block-storage backends (lvm/zfs/ceph) + block-store volume I/O — the current dir+SFTP-into-content-dir model has no content dir on block stores (likely needs qm importdisk / REST alloc instead of sftp_put). Surfaced in capabilities-px.py (PVE-46), NOT the portable example. "Everything that makes sense", no niche over-optimizing. Proxmox-specific device per the driver-only scope.

- [ ] **BUILD-13** · `test` — nested-PVE build smoke vs libvirt reference
  _(blocked by: BUILD-1d, BUILD-2e, BUILD-12)_

  > PARTIAL 2026-06-01. Runnable slice DONE: tests/integration/test_proxmox_prepare.py exercises the sanctioned xorriso prepare_iso end-to-end (real subprocess: injects /auto-installer-mode.toml + /proxmox-first-boot, preserves source content) — 2 tests green (xorriso present). BLOCKED on environment for the FULL nested-PVE build to green: needs a cached PVE 9 ISO + nested KVM + a bound libvirt-local profile (none in this sandbox). Run on a cert host: python -m testrange.cli run --profile libvirt-local examples/capabilities.py (pve-node).

### ESXI

- [ ] **ESXI-16** · `test` — examples/capabilities-nested-esxi.py portable nested-ESXi plan + TESTS + live cert

  > **SHELVED 2026-06-07 (post-1.0.0): ESXi-as-a-guest deferred.** Build phase is
  > certified end-to-end on libvirt L0; the run-phase nested cert is blocked only by
  > ESXI-18 (vmk0 keeps the build-NIC MAC), whose fix is known but parked. The ESXi
  > *backend* is certified via REL-11's raw kickstart host instead — nested adds no
  > coverage the e2e suite needs. Resume post-1.0.0.
  >
  > examples/capabilities-nested-esxi.py + GuestHypervisor.esxi DONE (code+unit gate-green). LIVE 2026-06-02: fixed BACKEND-13 (IDE installer CD) + BUILD-22 (heredoc) + CORE-62 (--build-timeout). ESXi install now validated end-to-end to DCUI on libvirt L0. ESXI-17 RESOLVED 2026-06-06 (build-result via %post vsish→logPort=com1; build phase certified end-to-end on libvirt L0). CORE-63 RESOLVED 2026-06-06 (EcdsaKey for the FIPS-sshd run-phase key). CORE-65 RESOLVED 2026-06-07 (pNIC discovery: the inner cache-only run was spuriously pNIC-validating the build switch's uplink against the inherited libvirt bridge-name map; preflight now passes build_switch=None under require_cache so the never-realized build switch is exempt — unit-proven + hello_world smoke green). Remaining: the live nested run-phase cert (esxcli/SSH to the node + inner VM over pyVmomi) end-to-end on libvirt L0. Move to Done once that run-phase cert is green.

- [ ] **ESXI-18** · `bugfix` — nested ESXi vmk0 keeps the build-NIC MAC → run-phase DHCP-lease discovery misses

  > **BUILDER FIX LANDED 2026-06-08 (merged to feature/release-check); live cert
  > still SHELVED post-1.0.0.** `%firstboot` now seeds `local.sh` with a
  > sentinel-guarded one-shot reboot that sets `Net.FollowHardwareMac=1` + persists
  > (auto-backup.sh), and the rendered ks.cfg digest is folded into `config_hash`.
  > Unit-gated. The live nested run-phase cert (run with `--lease-timeout ~600`;
  > ad-hoc plan at `~/Desktop/TestRange-Adhoc/esxi-followhwmac.py`) remains shelved
  > — and was NEVER actually live-tested (the earlier "Option A" live run executed
  > pre-fix code from this branch). Earlier diagnosis below.
  >
  > **SHELVED 2026-06-07 (post-1.0.0): ESXi-as-a-guest deferred.** The diagnosis
  > below is complete and the deterministic fix is known (Fallback A/B); we are
  > parking nested ESXi until after the 1.0.0 release rather than finishing it now.
  > The ESXi *backend* is certified via REL-11 (raw kickstart host), which needs
  > none of this. Pick this up when nested ESXi becomes a priority.
  >
  > LIVE-FOUND under ESXI-16 (2026-06-07). esxi-a's run boot never satisfied
  > `discover_ip` → "did not acquire a DHCP lease on 'lab-net' within Ns". NOT a
  > timeout: proven by reading the lab sidecar's dnsmasq.leases mid-run —
  > esxi-a DID lease (10.50.0.33) but under `02:df:31:62:2b:5e` (the **build NIC**
  > MAC), while the orchestrator polls the run/lab NIC MAC `02:54:e8:60:81:17`.
  > ROOT CAUSE: ESXi binds vmk0's MAC to the pNIC present at **install** (the
  > dedicated build NIC) and keeps it (`Net.FollowHardwareMac=0` default); the
  > captured image is later booted on a different run NIC, so vmk0 DHCPs under the
  > stale build MAC. FIX (user-chosen): bake `Net.FollowHardwareMac=1` into the
  > kickstart so vmk0 follows its uplink's current hardware MAC. Wrinkle: %post
  > powers off during build (ESXI-17), so %firstboot first runs in the run phase
  > AFTER vmk0's early DHCP under the build MAC. LIVE-PROVEN 2026-06-07: setting the
  > flag at runtime + bouncing vmk0 (down/up) does NOT move a live vmk0's MAC (DCUI
  > still showed the build-MAC IPv6) and the down/up broke rc.local. So local.sh
  > (after hostd) sets the flag, persists config (auto-backup.sh), and does a
  > ONE-SHOT guarded reboot (sentinel /etc/vmware/.trfollowhwmac); the second boot
  > re-inits vmk0 under the hardware (run) MAC so the lease lands under the polled
  > MAC. Needs a longer run-phase lease window (two nested-ESXi boots). Also fold
  > the rendered kickstart digest into ESXiKickstartBuilder.config_hash so the
  > template change busts the stale esxi-a cache (CORE-64-style gap). Live-verify.

- [ ] **ESXI-19** · `bugfix` — ESXi builder enabled sshd from credential-key presence, not from SSH transport _(code landed + merged 2026-06-08)_

  > Code landed and merged to feature/release-check 2026-06-08 (unit-gated).
  > `ESXiKickstartBuilder` no longer infers sshd-enable from `root.ssh_key`
  > presence; `GuestHypervisor.esxi` derives `enable_ssh=isinstance(communicator,
  > SSHCommunicator)` and passes it to the builder (the Builder ABC forbids the
  > builder seeing a Communicator). `enable_ssh` defaults True. The vmk0 MAC-follow
  > block (ESXI-18) is un-gated — transport-independent. Latent until a non-SSH
  > ESXi communicator (COMM-2) lands; pure hardening, no live cert needed. Move to
  > Done at the next board sweep. Touchpoints: `builders/esxi.py`,
  > `builders/_esxi_prepare.py`, `vms/nested.py` + unit tests.

- [ ] **ESXI-11** · `test` — live testrange run smoke (hello_world) on 40.160.34.83
  _(blocks: ESXI-12; blocked by: ESXI-2, ESXI-3, ESXI-4, ESXI-8)_

  > ESXI-11 live hello_world smoke. PIPELINE PROVEN LIVE 2026-06-02 end-to-end (preflight->L2->sidecar-ready-via-guest-ops->build VM boot->serial result). NOT green: build apt needs internet egress; host has no VM-egress path (no internet pNIC; public LAN locks DHCP; ESXi can't host-NAT). User confirmed no egress available 2026-06-02. Driver/pipeline correct; environment-blocked.

- [ ] **ESXI-12** · `test` — examples/capabilities.py ESXi-node VM + TESTS entry
  _(blocks: ESXI-13; blocked by: ESXI-11)_

  > Extend the portable examples/capabilities.py with whatever the ESXi backend adds to the driver-facing contract and a corresponding TESTS entry that verifies it end-to-end (CLAUDE.md rule 4). Stays backend-agnostic (Hypervisor, no host/creds in the file); bind at run time via --profile esxi-local. Mirrors BUILD-12 (PVE). Depends ESXI-11. Created 2026-06-01.

- [ ] **ESXI-13** · `test` — capabilities.py full-green certification on the live ESXi host
  _(blocks: ESXI-14, ESXI-15; blocked by: ESXI-5, ESXI-6, ESXI-9, ESXI-10, ESXI-12)_

  > ESXI-13 capabilities cert. BLOCKED on environment egress (build VMs cannot apt-install — no VM internet path on this ESXi host; user confirmed none available 2026-06-02). Driver + full pipeline proven live (hello_world). Not a driver defect.

- [ ] **ESXI-15** · `feat` — examples/capabilities-esxi.py — additive ESXi-specific example
  _(blocked by: ESXI-13)_

  > Once ESXi is certified working against the portable example, add examples/capabilities-esxi.py exercising ESXi-specific behavior (controller-bus selection, VMXNET3, datastore specifics) that the portable plan can't express — the per-driver additive example, mirroring PVE-46. Depends ESXI-13. Created 2026-06-01.

### ORCH

- [ ] **ORCH-3** · `feat` — `--resume <run_id>`

  > State schema is already future-proofed (intent_at/outcome_at + metadata); wire the runtime to resume a partially-built run.

- [ ] **ORCH-10** · `feat` — concurrency — one user, multiple different plans at once

  > Future support. Distinct run_ids, different VMs/backends, run concurrently. Solve shared-cache fixed-name .partial (B1), HTTP fetch landing (see CACHE-3), cache RMW. Distinct from ORCH-1 (multiple Hypervisors in one plan) and ORCH-4 (parallel build within a run).

- [ ] **ORCH-11** · `feat` — concurrency — one user, same plan twice

  > Future support. Needs run-scoped backend naming verified collision-free across runs, VMID/switch/SDN-zone collision handling.

- [ ] **ORCH-12** · `feat` — concurrency — different plans, same profile

  > Future support. Connection/profile sharing, per-run resource namespacing, SDN zone / pool collision avoidance. Home of the cross-process FileLock idea PLAN §16 declined.

### BUILD

- [ ] **BUILD-9** · `feat` — Windows Unattended builder
  _(blocked by: BUILD-1, COMM-1)_

  > WindowsUnattendedBuilder — autounattend.xml-driven unattended Windows install. Setup partitions a blank disk, installs, then OOBE FirstLogonCommands install virtio drivers, enable WinRM, run winget packages + post-install, and signal+shutdown.
  >
  > DEPENDS ON BUILD-1 (#26): blank work disk + Windows install ISO + virtio-win driver ISO as boot media + autounattend seed ISO + boot_cdrom + UEFI + BOOT-KEYPRESS consumption. DEPENDS ON COMM-1 WinRM (#28) for wait_ready/run-phase ops.
  >
  > == Behavioral reference (lessons-learned only — do NOT port the code) ==
  > .bak/.old/testrange/vms/builders/unattend.py   (full OLD-ABC impl, incl. build_autounattend_iso_bytes)
  > .bak/.old/testrange/communication/winrm.py
  > .bak/.old/tests/test_unattend.py
  > .bak/.old/docs/usage/windows.rst
  > On the dead ABC (prepare_install_domain/InstallDomain/power-off-edge). Port mechanics to the 5-method ABC, not the shape.
  >
  > == Map onto current ABC ==
  > - render_seed(): autounattend.xml seed ISO, label UNATTEND (pycdlib; build_autounattend_iso_bytes). Setup scans every attached FAT/NTFS/CDFS volume for /autounattend.xml. NOTE: the Windows install ISO itself is UNPATCHED — NO xorriso needed (big contrast to ESXi/PVE); only pycdlib for the seed, already a project dep.
  > - os_disk_base() -> None. Setup creates its own GPT (ESP 260 / MSR 128 / Primary extend).
  > - credentials: root Credential -> Administrator password (PlainText). Non-root creds -> LocalAccounts (Administrators if sudo else Users). wait_ready over WinRM (COMM-1).
  > - config_hash(): iso + users + winget package reprs + post_install + disk size + product_key/ui_language/timezone + autounattend XML digest. Purity per ADR-0007.
  > - Needs a virtio-win ISO cache helper (cf. .bak get_virtio_win_iso): NetKVM + qemu-ga MSI.
  >
  > == autounattend.xml structure — gotchas ==
  > Three passes:
  > - windowsPE: International-Core-WinPE (locales) + Microsoft-Windows-Setup (DiskConfiguration wipe DiskID 0; ESP/MSR/Primary; ImageInstall OSImage InstallTo DiskID 0 PartitionID 3). ProductKey MUST live inside UserData in the windowsPE pass — anywhere else and Setup SILENTLY ignores it ("can't read product key from the answer file").
  > - specialize: Shell-Setup ComputerName + TimeZone.
  > - oobeSystem: Shell-Setup AdministratorPassword + LocalAccounts + OOBE skip-screens (HideEULA, SkipMachineOOBE, SkipUserOOBE, etc.) + FirstLogonCommands.
  > ProductKey default = Win10/11 Pro KMS generic install key (EDITION SELECTION, not activation — multi-edition consumer ISOs refuse to proceed silently without it). None omits the element (Enterprise eval / single-edition media).
  >
  > == FirstLogonCommands sequence (the install-time bake) ==
  > 1. pnputil /add-driver every *.inf from the virtio-win volume /install (NetKVM etc.).
  > 2. qemu-ga MSI silent install from the virtio-win volume.
  > 3. Enable-PSRemoting -Force -SkipNetworkProfileCheck.
  > 4. WinRM: AllowUnencrypted + Auth/Basic + firewall allow TCP 5985.
  > 5. winget packages (pkg.install_commands() for package_manager == "winget").
  > 6. caller post_install_cmds.
  > 7. result signal + shutdown.
  >
  > == Result signal (ADR-0012) — central new work vs .bak ==
  > .bak ended FirstLogonCommands with `shutdown /s /t 0` and relied on power-off-edge. Current contract: write `TESTRANGE-RESULT: ok` to COM1 before shutdown — e.g. a final SynchronousCommand / SetupComplete.cmd doing PowerShell [System.IO.Ports.SerialPort] or `cmd /c echo TESTRANGE-RESULT: ok > COM1`, then `shutdown /s /t 0`. Failure path is the hard part: FirstLogonCommands has no global trap — either wrap each command and emit `TESTRANGE-RESULT: fail rc=.. cmd=..` on first non-zero, or drive the whole bake from one PowerShell script with try/catch. Requires a serial port wired in the build domain (driver read_build_result_sink, ADR-0012).
  >
  > == Boot keypress ==
  > Windows UEFI media shows "Press any key to boot from CD or DVD..." for ~5s; no keypress -> OVMF falls through the empty disk to the EFI shell and the install never starts. The builder must signal needs_boot_keypress so the orchestrator/driver spams keystrokes during early boot (BUILD-1 / driver contract — a new capability the cloud-init path never needed).
  >
  > == Done ==
  > - WindowsUnattendedBuilder on the current ABC + unit tests (autounattend XML render / seed ISO / config_hash) on the mock driver.
  > - virtio-win cache helper.
  > - examples/capabilities.py: add a Windows VM to the portable plan + a TESTS entry (rule 4); WinRM-reachable.
  > - PLAN.md + ktui board updated.
  > - Smoke: Windows build to green against real libvirt with virtio + WinRM.

### NET

- [ ] **NET-3** · `feat` — `Switch(gateway=True)` — implicit router VM

  > Cross-subnet routing between the Networks on a single Switch.
  >
  > Re-expressed 2026-05-26 from the old "implicit router VM" framing: post-NET-9 the always-present sidecar IS the router VM, so this is an added sidecar capability — route between the Switch's Networks — layered on the existing Sidecar, not a new implicit VM. Distinct from NAT egress (Sidecar(nat=True), which masquerades OUT the uplink); this is inter-Network L3 WITHIN the Switch. mgmt stays a passive host adapter.

- [ ] **NET-4** · `feat` — multi-subnet mgmt IPs

  > A `Switch(mgmt=True)` derives its single `.2` adapter from the first network; generalize to N host adapters when a plan needs it.

- [ ] **NET-5** · `feat` — IPv6 / VLAN tagging / VXLAN / NAT port-forwards

  > L2/L3 features beyond the current IPv4 + flat-subnet model.

- [ ] **NET-17** · `feat` — inner-uplink bridge on the guest hypervisor; chained-NAT egress
  _(blocked by: CORE-38)_

  > Inner-VM RUNTIME egress (chained NAT through host-a). NOT required for nesting to work (verified with isolated inner net); deferred enhancement. The build-time egress (apt) already works via L0 build switch. When needed: GuestHypervisor builder provisions a bridge on host-a + inner profile maps the inner uplink to it.

### BACKEND

- [ ] **BACKEND-3** · `feat` — Hyper-V driver

  > WMI (`Msvm_*`) + PowerShell Direct for in-guest ops; per-vNIC VLAN; SMB/WinRM pool transfer.
  >
  > ADR-0016 (2026-05-29) update: managed build egress is GONE (no New-NetNat/fence in TestRange). Switch.uplink is a profile-resolved logical name Hyper-V maps to an external/internal VMSwitch and attaches. NAT egress = plain Switch(uplink=<named>, sidecar=Sidecar(dhcp,dns,nat)); the route-out network behind the named VMSwitch is operator-provisioned out-of-band. Build switch always carries a sidecar (DHCP/DNS) when it needs egress.

- [ ] **BACKEND-4** · `chore` — QGA libvirt-stderr silencer

  > Mute "guest agent is not responding" retry noise via a process-global `registerErrorHandler`. Refcounted mutable global state — rides BACKEND-1.

- [ ] **BACKEND-5** · `feat` — remote-libvirt egress over qemu+ssh:// (verify named uplink on remote host)

  > REFRAMED 2026-05-30 (BACKEND-1 drops pyroute2). The original blocker — host-local netlink can't reach a remote URI — is GONE: L2 is realized by the libvirt DAEMON via the network API, so a remote qemu+ssh:// daemon builds the bridge/dnsmasq/NAT remotely. What remains is much smaller: (1) the named uplink bridge (e.g. tr-egress) must already exist ON THE REMOTE HOST; (2) verify pool stream I/O + serial unix-socket + QGA all work across a remote connection (the serial <serial type=unix> socket path is on the remote host); (3) preflight check that the resolved uplink exists remotely. No virInterface*/SSH side-channel/remote-agent needed. Rides BACKEND-1.

- [ ] **BACKEND-6** · `feat` — content-addressed image cache at the driver ABC (ADR-0011 draft)

  > DRAFT design in docs/adr/0011-content-addressed-backend-image-cache.md (not implemented). Reshapes the ABC storage surface from pool byte-I/O (upload_to_pool/download_from_pool/create_blank_volume/resize_volume) to content-addressed images: ensure_image(id, fetch)->ref (idempotent, lazy fetch, skip-on-resident), capture_image(vm,id,sink), list_images/evict_image (GC), create_vm consumes ImageRef. Keeps orchestrator dumb (no has_cached_layer branching; warm hit = fast return inside driver) and prevents disk spray (content-addressed names + images as a distinct lifecycle kind, GC via list/evict). Amends ADR-0008/0010. Open: eviction policy location (lean: cross-backend cache layer, cascade-on-local-eviction) + capture sink shape. Then implement per-backend, PVE first.

### CACHE

- [ ] **CACHE-1** · `feat` — push-only HTTP cache mode for CI

  > ADR-0010 §5 added best-effort upstream push of built disks; add a dedicated push-only mode for build farms.

### COMM

- [ ] **COMM-1** · `feat` — WinRM communicator
  _(blocks: BUILD-9)_

  > For Windows guests reachable over the network.

- [ ] **COMM-2** · `feat` — VMware Tools communicator

  > Guest-ops over VMware Tools (pairs with BACKEND-2).

### PROXY

- [ ] **PROXY-1** · `feat` — `Proxy` ABC ported fresh from `.bak`

  > Two-shape tunnel into a hypervisor's inner-VM network namespace: `connect((host,port)) -> socket` for `sock=`-accepting clients (paramiko, requests, asyncio) and `forward((host,port), bind=...) -> (host,port)` for opaque clients. Concretes per backend (SSH jumphost, ESXi web console proxy, Proxmox proxy node). Required for any Communicator to reach an inner-only VM.

### PROV

- [ ] **PROV-2** · `feat` — Requirement vocabulary + `HostSpec`
  _(blocks: PROV-3, PROV-4)_

  > Required* device value types — RequiredCPU(cores), RequiredMemory(mb), RequiredOSDrive(gb), RequiredDataDrive(gb), RequiredNIC() — positional, value-is-minimum (Required* prefix = ">="), backend-agnostic (NOT the Libvirt*/ESXi* concretes). HostSpec = scalar firmware (top-level, like VMSpec.firmware) + discrete devices list (1:1, no count). Pure data + full unit coverage (construction, validation, repr). No matcher here.

- [ ] **PROV-3** · `feat` — `HostRecipe` + `ProvisioningPlan` value types
  _(blocked by: PROV-2)_

  > HostRecipe = spec (HostSpec) x builder (the existing Builder ABC); no communicator; builder injected explicitly (no .proxmox()/.esxi() classmethods). ProvisioningPlan(host: HostRecipe) — nameless, single positional. Pure data + unit coverage.

- [ ] **PROV-4** · `feat` — inventory matcher (1:1 bipartite, don't-touch)
  _(blocked by: PROV-2)_

  > Pure function: match HostSpec.devices 1:1 against a HostInventory -> resolved assignment (role -> physical id) + don't-touch set (unmatched physical); raise on no perfect matching (sufficiency fail, BEFORE any destructive step). Deterministic tiebreak: OS = smallest-sufficient disk, data = remaining largest-first, stable by hardware id. Renders the human-readable assignment plan ("OS->disk0 ..., untouched: ..."). Heavy table-driven + property unit tests (heterogeneous sizes, surplus disks, ambiguous-identical, insufficient).

- [ ] **PROV-5** · `feat` — `OutOfBandController` ABC + inventory/power value types
  _(blocks: PROV-6, PROV-7, PROV-9, PROV-11)_

  > Abstract surface: connect/disconnect, inventory() -> HostInventory, attach_media(url)/detach_media, set_boot_override(BootTarget, persist=), power(PowerState, graceful=)/power_cycle/power_state. NOT a HypervisorDriver (no switch/network/pool/vm). Value types: HostInventory(firmware, cpu_cores, memory_mb, drives, nics), DiscoveredDrive(size, kind, stable id), DiscoveredNIC, BootTarget/PowerState enums. No concrete here.

- [ ] **PROV-6** · `test` — `MockController` (test-only)
  _(blocked by: PROV-5)_

  > In-memory OutOfBandController in tests/ (mirrors tests/mock_driver.py): settable fake inventory + recorded media/boot/power calls, so the matcher + provisioner orchestrator get full unit coverage without live hardware.

- [ ] **PROV-7** · `feat` — `ControllerProfile` registry + `--controller` flag + RedfishControllerProfile table
  _(blocked by: PROV-5)_

  > ControllerProfile ABC (scheme ClassVar + _from_table + build_controller) mirroring BackendProfile; load_controller(path, name) dispatch by driver=; --controller [file:]name CLI parsing (reuse the _parse_profile_spec shape). RedfishControllerProfile table (bmc_host/user/password, media_url_base, verify_tls) -> build_controller() returns the PROV-11 concrete. Unit-tested via a stub scheme.

- [ ] **PROV-8** · `feat` — ephemeral ISO staging server
  _(blocks: PROV-9)_

  > Serve the prepared installer ISO at a BMC-reachable URL under media_url_base; one-time unguessable path; started before realize, torn down after the BMC has pulled it. Names the credential-on-the-wire wrinkle (ISO embeds baked answer creds) + the HTTPS/CIFS upgrade path. Unit-tested (serves bytes, one-shot, teardown).

- [ ] **PROV-9** · `feat` — provisioner orchestrator (preflight->build->stage->realize->wait->finalize)
  _(blocked by: PROV-3, PROV-4, PROV-5, PROV-6, PROV-8)_

  > Separate, lean orchestrator: preflight (controller.connect -> inventory() -> 1:1 match (PROV-4) -> print assignment -> idempotency gate via profile.build_driver().connect()+version) -> build (reuse Builder seam + cache) -> stage (PROV-8) -> realize (attach_media -> set_boot_override(CD, one-time) -> power_cycle) -> wait (readiness = profile driver connects, bounded by --provision-timeout) -> finalize (set_boot_override(DISK, persist) -> detach_media). NO switch/network/pool/nested phase, NO teardown. Unit-tested end-to-end against MockController + a fake profile driver.

- [ ] **PROV-10** · `feat` — `provision` CLI verb + `provision_range()` entrypoint
  _(blocked by: PROV-9)_

  > argparse subcommand: provision <plan> --profile <name> --controller [file:]name --provision-timeout; load the ProvisioningPlan module; exit codes consistent with build/run. provision_range(PLAN, *, profile, controller, ...) programmatic entrypoint used by the example __main__. Unit-tested (arg parsing, exit codes, plan load).

- [ ] **PROV-11** · `feat` — `RedfishController` concrete
  _(blocked by: PROV-5)_

  > Redfish-backed OutOfBandController (sushy or redfish lib, lazy _import_redfish() with [redfish] extra): inventory via Systems/*/Storage + EthernetInterfaces + Memory/Processors + Bios; VirtualMedia InsertMedia/EjectMedia; ComputerSystem boot override; Reset for power. Covers iLO5+/iDRAC9+/XCC/Supermicro X11+. Unit-tested against recorded Redfish JSON; live-validated in PROV-13.

- [ ] **PROV-12** · `docs` — `examples/provision-proxmox.py` + user guide
  _(blocked by: PROV-10)_

  > Portable provisioning plan (clean, no inline comments): ProvisioningPlan(HostRecipe(HostSpec(firmware, Required* devices), ProxmoxAnswerBuilder(...))). docs/user/provisioning-bare-metal.md: the three-verb pipeline, connect.toml controller section, --controller flag, idempotency, don't-touch. Explicitly NOT a capabilities.py entry (controller capability, not a driver contract) — note the rationale. Passive check: testrange describe.

- [ ] **PROV-13** · `test` — live validation: provision real Proxmox onto the iLO box
  _(blocked by: PROV-10, PROV-11)_

  > Out-of-band live suite (like the libvirt/proxmox cert suites, NOT in the gate): provision examples/provision-proxmox.py --profile proxmox --controller <real BMC> end-to-end onto physical hardware -> Proxmox installs unattended -> profile driver connects -> run examples/hello_world.py --profile proxmox goes green against the freshly-provisioned host. Records the real BMC/box + any hardware gotchas (UEFI, NIC names, disk selection) found live.

- [ ] **PROV-14** · `feat` — DEFERRED: image-origin onto iron (writer stage)

  > DEFERRED (out of scope for the initial epic; named, not silent). Image-origin builders (CloudInitBuilder) have no "upload to pool" equivalent on iron — realizing them needs a writer stage: the BMC boots a small live/iPXE env that fetches the disk image, writes it to the physical disk, then reboots (the MAAS/Tinkerbell pattern). v1 is installer-origin only. Implement when a real image-origin-on-iron need exists.

- [ ] **PROV-15** · `feat` — DEFERRED: disk-identity override at the bind layer

  > DEFERRED until a concrete box defeats the constraints-plus-deterministic matcher (PROV-4). The narrowest override (force OS onto a specific physical disk) is HOST-SPECIFIC, so it lands at the bind layer (a provision-time --disk flag or a connect.toml map), NEVER in the portable plan. No speculative knob now; add with a real failing case to shape it.

- [ ] **PROV-16** · `feat` — DEFERRED: legacy iLO4 / raw-IPMI controller concrete

  > DEFERRED. A second OutOfBandController concrete for pre-Redfish BMCs (iLO4 virtual media, raw IPMI power). Messier/vendor-specific; only build if a target box lacks Redfish (iLO5+/iDRAC9+/XCC/Supermicro X11+ all speak it).

### REL

- [ ] **REL-11** · `chore` — unmanaged ESXi host (`esxi-e2e` profile)

  > Stand up a standalone ESXi host (operator-provided — raw kickstart, virt-install on L0 or bare metal; NOT via the driver/GuestHypervisor), reusing the install lessons from BACKEND-13 (IDE installer CD on BIOS), BUILD-22 (heredoc terminator), ESXI-17 (%firstboot). Egress out-of-band. Emit the `esxi-e2e` profile bound to the host. Supersedes the egress block on ESXI-11/12/13. First host (user's order: ESXi -> PVE -> libvirt). _(No shared in-repo scaffold — REL-10 WontDo 2026-06-08; standing the host up is the operator's job.)_

- [ ] **REL-12** · `chore` — unmanaged Proxmox host (`proxmox-e2e` profile)

  > Stand up a single-node Proxmox VE host (operator-provided — PVE auto-installer answer.toml). Egress out-of-band. Emit the `proxmox-e2e` profile bound to the host. Unblocks the nested-PVE build (BUILD-13). Second host. _(No shared in-repo scaffold — REL-10 WontDo 2026-06-08.)_

- [ ] **REL-13** · `chore` — unmanaged libvirt host (`libvirt-remote-e2e` profile)

  > Stand up a remote libvirt host (operator-provided — Debian + libvirtd), reachable over qemu+ssh. Emit the `libvirt-remote-e2e` profile bound to the host. Exercises the remote-libvirt path (BACKEND-5 egress / BACKEND-11 guest_gateway) — a remote backend rather than the local qemu:///system the reference cert uses. Third host. _(No shared in-repo scaffold — REL-10 WontDo 2026-06-08.)_

- [ ] **REL-14** · `test` — run full e2e suite vs hosted ESXi -> discrepancy report
  _(blocked by: REL-3, REL-4, REL-5, REL-6, REL-9, REL-11)_

  > Loop `testrange run --profile esxi-e2e` over `tests/plans/generic/*.py` + `tests/plans/esxi/*.py` against the hosted ESXi (REL-11) — the corpus runs via `testrange run`, NOT pytest. Record every discrepancy/bug/surprise in docs/dev/e2e-findings-esxi.md; file a bug ticket per finding in its swimlane (ESXI/CORE/ORCH/...). First in the run order. Report findings to the user before moving on.

- [ ] **REL-15** · `test` — run full e2e suite vs hosted Proxmox -> discrepancy report
  _(blocked by: REL-3, REL-4, REL-5, REL-6, REL-8, REL-12)_

  > Loop `testrange run --profile proxmox-e2e` over `tests/plans/generic/*.py` + `tests/plans/proxmox/*.py` against the hosted PVE (REL-12). Record findings in docs/dev/e2e-findings-proxmox.md; file a bug ticket per discrepancy. Second.

- [ ] **REL-16** · `test` — run full e2e suite vs hosted libvirt -> discrepancy report
  _(blocked by: REL-3, REL-4, REL-5, REL-6, REL-7, REL-13)_

  > Loop `testrange run --profile libvirt-remote-e2e` over `tests/plans/generic/*.py` + `tests/plans/libvirt/*.py` against the hosted libvirt (REL-13). Record findings in docs/dev/e2e-findings-libvirt.md; file a bug ticket per discrepancy. Third.

- [ ] **REL-17** · `docs` — documentation reconciliation pass (docs/user + docs/dev)
  _(blocked by: REL-14, REL-15, REL-16)_

  > After the validation pass produces concrete findings: reconcile docs/user (install, connecting-to-a-backend, drivers/*, running-tests, writing-a-plan) and docs/dev against validated reality — cert-status tables flipped, the e2e how-to, the host-fleet runbook linked. Done with facts in hand, not intent.

- [ ] **REL-18** · `docs` — PLAN.md restructure/cleanup
  _(blocked by: REL-14, REL-15, REL-16)_

  > PLAN.md is ~1740 lines and feels messy (user). Separate living design from point-in-time status, prune deferred entries now built, fix code<->PLAN drift surfaced by validation, and tighten the §-numbered decision list. Keep it the living-design source of truth; move stale historical status to a clearly-marked tail or out.

- [ ] **REL-19** · `docs` — TODO.md cleanup + reconciliation
  _(blocked by: REL-14, REL-15, REL-16)_

  > Flow completed tickets Done -> Archive, reconcile Doing/Ready against post-validation reality, close env-blocked tickets superseded by the host fleet (ESXI-11/12/13, BUILD-13), fix the stale header/migration counts. Leave the board honest going into the release.

- [ ] **REL-20** · `chore` — cut v1.0.0 (all-three-green gate + public-API freeze)
  _(blocked by: REL-14, REL-15, REL-16, REL-17, REL-18, REL-19)_

  > GATE: full e2e suite green on hosted libvirt + Proxmox + ESXi (REL-14/15/16 all clean). Then: capture an `/api-diff` baseline + freeze the public surface (testrange.__init__ exports, the driver ABC, the CLI); flip `major_version_zero = false` in pyproject so commitizen enforces SemVer major-on-break; `/release-notes` -> CHANGELOG since the last tag; `cz bump` to 1.0.0 + tag v1.0.0. Push is the user's call (never auto-push).

## Done (271)

### REL

- [x] **REL-10** · `feat` — unmanaged host harness: `tools/hypervisor-hosts/` common scaffold _(wontdo: 2026-06-08)_

  > WONTDO 2026-06-08 (user directive). Scripts/orchestration for standing up
  > validation hosts will NOT live in this repo. If someone wants to validate or
  > certify a backend, standing up the host is their job — the repo ships the
  > certification corpus (`tests/plans/`) and the `testrange run` runner, not a
  > nested-fleet provisioning harness. The planned deliverables (the
  > `tools/hypervisor-hosts/` scaffold: nested-KVM enablement, `tr-egress` NAT
  > network, virt-install wrappers, `connect.toml` profile generator, fleet
  > runbook) are dropped. `tools/hypervisor-hosts/` was never created.
  > Consequences: REL-11/12/13 no longer share an in-repo scaffold and are no
  > longer blocked by this ticket — each `*-e2e` host is stood up out-of-band by
  > the operator and bound to its profile. REL-14/15/16 run the corpus against
  > those operator-provided profiles unchanged. PLAN.md + ADR-0028 pillar (2)
  > still describe the scripted fleet — reconciled under REL-17/18.

- [x] **REL-1** · `docs` — ADR-0028 + PLAN.md "1.0.0 validation & release" section (design of record) _(done: 2026-06-06)_

  > DONE 2026-06-06. ADR-0028 (docs/adr/0028-release-validation-strategy.md, indexed) + PLAN.md "1.0.0 validation & release (ADR-0028)" section both landed. Records the three pillars: (1) adversarial e2e suite in tests/end-to-end/ distinct from capabilities.py; (2) unmanaged scripted nested host fleet in tools/hypervisor-hosts/ (raw libvirt, NOT GuestHypervisor — circularity), tr-egress NAT supersedes the ESXi/PVE egress block; (3) 1.0.0 = all-three-backends-green hard gate + major_version_zero=false public-API freeze. Rejected: stress-in-capabilities.py (conflates survey vs depth), GuestHypervisor hosts (circular), ESXi-beta ship (gate is all three), manual runbook (not CI-able). Opens the REL epic.

- [x] **REL-2** · `test` — `tests/plans/` scaffolding + README/conventions _(done: 2026-06-07)_

  > DONE 2026-06-07. REFRAMED from `tests/end-to-end/` + pytest harnesses → a standalone `testrange run` corpus under `tests/plans/{generic,libvirt,proxmox,esxi}/` (ADR-0028 amended in place). Created the tree + `tests/plans/README.md` (per-plan WHAT/WHY index, the how-a-backend-gets-certified workflow, docstring template + authoring conventions: one PLAN + a few TESTS per file, inline-single-use, generic-stays-portable, driver-tiers-pin-the-driver). DROPPED from the original design: the `test_e2e_<backend>.py` harnesses, the conftest profile helper, and the `esxi` pytest marker — the corpus is NOT collected/executed by pytest (files named without `test_` prefix, no `__init__.py`; confirmed `pytest -m "not proxmox and not libvirt"` collects 0 from tests/plans). It IS linted/typed (ruff + mypy --strict, gate-clean). This corpus is the new backend certification (supersedes examples/capabilities.py, slated for deletion).

- [x] **REL-3** · `test` — generic: VM lifecycle, power churn & identity _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/generic/lifecycle.py` (power-cycle churn, graceful shutdown → shutoff, reboot persistence, oversized OS-drive first-boot growth, NIC-less native-agent under churn) + `tests/plans/generic/users_credentials.py` (SSH key vs password auth, non-admin sudo denied, declared group membership, explicit per-NIC resolver). Portable; runs on all three backends. Live cert rides REL-14/15/16.

- [x] **REL-4** · `test` — generic: networking edge matrix _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/generic/networking.py` — multi-`Network`-per-`Switch`, the air-gap reachability matrix (private internal-only both ways, public via NAT), DHCP pool-boundary lease, exactly-one-default-route, cross-label DNS, and the negative isolation assertions (air-gapped segment cannot egress). Portable. Live cert rides REL-14/15/16.

- [x] **REL-5** · `test` — generic: build/cache reuse + disk integrity _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/generic/build_cache.py` — multi-data-disk content integrity (disk-unique seed survives build→cache→run, no swap), apt + pip, post-install command ordering; docstring documents the run-twice warm-cache byte-stability procedure. Portable. Live cert rides REL-14/15/16.

- [x] **REL-6** · `test` — generic: snapshots + concurrency _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/generic/snapshots.py` (disk snapshot create/list/restore/delete + memory snapshot restores running tmpfs state) + `tests/plans/generic/concurrency.py` (independent multi-VM fan-out, run with `--jobs N`, distinct hostname/lease per node + teardown completeness documented). Portable. Live cert rides REL-14/15/16.

- [x] **REL-7** · `test` — libvirt-specific plans _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/libvirt/devices.py` (`LibvirtOSDrive`/`LibvirtDataDrive` bus → /dev/vd* virtio vs /dev/sd* sata/scsi; `LibvirtNetworkIface` e1000e model via sysfs driver check — CORE-61) + `tests/plans/libvirt/firmware_uefi.py` (`VMSpec(firmware="uefi")` OVMF boot, /sys/firmware/efi present). Pinned `LibvirtHypervisor`. The remote qemu+ssh guest_gateway path (BACKEND-11) is deferred to that ticket. Live cert rides REL-16.

- [x] **REL-8** · `test` — proxmox-specific plan _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/proxmox/devices.py` — `ProxmoxHardDrive` bus → /dev/sd* (scsi) vs /dev/vd* (virtio), pinned `ProxmoxHypervisor`, native QGA communicator. Block StoragePool (lvm/zfs/ceph) deferred to PVE-33. Live cert rides REL-15.

- [x] **REL-9** · `test` — esxi-specific plan _(done: 2026-06-07)_

  > DONE 2026-06-07. `tests/plans/esxi/devices.py` — `ESXiHardDrive` bus → /dev/sd* (scsi/sata) vs /dev/nvme* (nvme), pinned `ESXiHypervisor`, VMware Tools native guest-ops (open-vm-tools-plugins-all), bios firmware; docstring notes the qcow2→vmdk inflate a booting guest certifies. Subsumes the additive-example intent of ESXI-15. Live cert rides REL-14.

- [x] **REL-21** · `chore` — retire `examples/capabilities*` (superseded by tests/plans) _(done: 2026-06-07)_

  > DONE 2026-06-07 (user directive). Deleted all four `examples/capabilities*.py` (portable cert, -px showcase, -nested + -nested-esxi demos) now that `tests/plans/` is the certification corpus. Removed the dependent pytest wiring: `tests/integration/test_nested.py` (existed solely to run capabilities-nested.py) deleted; `test_capabilities_example_certifies` + helpers removed from `tests/integration/test_proxmox.py` (driver-primitive PVE tests retained). CLAUDE.md rule 4 repointed examples/capabilities.py → tests/plans/. NOTE: nested-virt lost its example-as-test coverage (accepted); residual prose references to capabilities* across README/docs/user/PLAN/ADRs left for the REL-17/18 doc reconciliation.

### PROV

- [x] **PROV-1** · `docs` — PLAN.md section + ADR-0027 (provisioning design of record) _(done: 2026-06-06)_

  > DONE 2026-06-06. PLAN.md section "Bare-metal provisioning via out-of-band controller (ADR-0027)" + ADR-0027 (docs/adr/0027-bare-metal-provisioning.md, indexed) both landed. Records: third verb, NOT a run flag; controller is its OWN ABC, not a HypervisorDriver; nameless ProvisioningPlan; HostRecipe = spec x builder (no communicator, explicit builder injection, no front-door classmethods); HostSpec requirements-contract (Required* devices, discrete 1:1, no count/selector); inventory matcher + don't-touch; installer-origin only v1. Rejected alternatives captured in the ADR: BMC-as-HypervisorDriver, [proxmox.install] TOML block, --controller-on-run, count=/selector on requirements.

### CORE

- [x] **CORE-61** · `feat` — libvirt-concrete device types (bus/model) + driver wiring
  _(done: 2026-06-02)_

  > Libvirt device concretes (user directive): testrange/devices/disk/libvirt.py (_LibvirtDisk(_Disk) base + LibvirtOSDrive/LibvirtDataDrive, bus virtio|sata|ide|scsi) + testrange/devices/network/libvirt.py (LibvirtNetworkIface, model virtio|e1000|e1000e|rtl8139). drivers/libvirt/_vm.py honors bus/model: per-bus-prefix dev allocator (vd*/sd*/hd*) so a sata OS disk never collides with sata seed/installer CDROMs; build NIC inherits guest's declared model (_build_nic_model). Generic OSDrive/NetworkIface keep virtio (back-compat). Unit tests test_libvirt_devices.py + test_libvirt_vm.py. DONE 2026-06-02 (code+unit; live boot rides ESXI-16).

- [x] **CORE-2** · `feat` — cross-format disk conversion (qcow2 ↔ vmdk ↔ raw)
  _(blocks: ESXI-3; done: 2026-06-01)_

  > Sanctioned qemu-img subprocess module for qcow2<->vmdk (+raw) conversion. DONE 2026-06-01: testrange/drivers/_diskconvert.py (require_qemu_img shutil.which preflight; convert/qcow2_to_vmdk streamOptimized/vmdk_to_qcow2); ADR-0024; pyproject ruff carve-out + test_subprocess_ban whitelist; tests/unit/test_diskconvert.py real round-trip. BLOCKS ESXI-3.

- [x] **CORE-60** · `feat` — per-call guest credential kwarg on native_guest_* (ADR-0008 seam)
  _(blocks: ESXI-5; done: 2026-06-01)_

  > Add optional per-call guest 'credential' kwarg to native_guest_* (ADR-0008 seam). DONE 2026-06-01: base.py kwarg + libvirt/proxmox/mock no-op overrides; orchestrator native_guest_credential() resolver threads admin/sole builder cred into the NativeCommunicator bind; tests/unit/test_native_credential.py. BLOCKS ESXI-5. | VERIFIED 2026-06-01: full examples/capabilities.py 30/30 green on live libvirt (native-agent tests incl.) — CORE-60 native-bind threading does not regress the certified backend.

- [x] **CORE-65** · `bugfix` — preflight must skip the build switch in a cache-only (`require_cache`) run _(done: 2026-06-07)_

  > Found under ESXI-16 (nested-ESXi cert). The inner ESXi run (`nested_phase`,
  > `require_cache=True`) never builds, yet `_preflight_and_initialize` still
  > resolved the inner plan's build switch and fed it to `driver.preflight`. The
  > manufactured inner ESXi profile inherits the OUTER libvirt uplink map
  > (`egress`→`tr-egress`, a *bridge* name), so ESXi's live `_uplink_pnic_findings`
  > validated that bridge name as a vmnic against the nested host's pNICs (only
  > `vmnic0`) → spurious `esxi-uplink-pnic-missing` → inner preflight aborted. The
  > build switch is realized on L0/libvirt during `build_nested_inner_vms` (where
  > the bridge name is correct); it has no meaning in the cache-only L1 run.
  > FIX: preflight ABC `build_switch` is now `Switch | None`; `_preflight_and_
  > initialize` passes `None` when `require_cache`; all four drivers (mock/libvirt/
  > proxmox/esxi) assemble the sweep via a new shared `preflight.preflight_switches()`
  > helper that drops a `None` build switch. Tests: `test_preflight.TestPreflight
  > Switches`, `test_esxi_preflight.py` (None drops the pNIC check; concrete still
  > flagged), `test_orchestrator` (None under `require_cache`, concrete otherwise).
  > Gates green (ruff/mypy --strict/1061 unit) + hello_world smoke on live libvirt
  > (normal-run path unchanged: still validates a concrete build switch). The
  > live nested cert itself stays with ESXI-16.

- [x] **CORE-64** · `bugfix` — config_hash must fold the baked SSH key (cache served wrong-key disk) _(done: 2026-06-06)_

  > Found live during ESXI-16: the ESXi + Proxmox builders folded only SSH-key
  > *presence* into config_hash, not the key value ("rotation must not bust the
  > cache"). But run VMs boot the cached disk with NO re-seed (run_phase.py:122
  > `seed_iso_ref=None`), so the key baked at build IS the only authorized_keys —
  > a plan with a different key cache-hits a disk it can't log into. Hit it when a
  > throwaway ESXi smoke (Ed25519) and the cert's esxi-a (ECDSA) collided on the
  > same hash; esxi-a booted the smoke's key and was unreachable. FIX: fold the
  > key by value — ESXi folds `root.ssh_key.auth_line`; Proxmox folds all creds'
  > `auth_line`s (the answer file's root-ssh-keys). cloud-init was already correct
  > (the key lands in user-data, which is folded). Flipped the two
  > `insensitive_to_ssh_key_rotation` tests to assert sensitivity. User-directed.

- [x] **CORE-63** · `feat` — SSHKey algorithm variants (ed25519 / ecdsa) _(done: 2026-06-06)_

  > `testrange.utils.SSHKey` was Ed25519-only; ESXi 8 sshd runs in FIPS mode and
  > SILENTLY rejects Ed25519 pubkeys (proven live: key in authorized_keys +
  > `esxcli system ssh key list`, yet denied; auth.log only "FIPS mode initialized").
  > DONE: refactored to a base `SSHKey` (Ed25519, unchanged → cache-stable) + an
  > `EcdsaKey(SSHKey)` subclass (deterministic NIST P-256, FIPS-approved), sharing
  > one encoder; `.algorithm` ClassVar for introspection. `ESXiKickstartBuilder`
  > now REJECTS an Ed25519 root key at construction with a fix-it message.
  > capabilities-nested-esxi + nested-phase tests use `EcdsaKey` for the ESXi root.
  > tests/unit/test_sshkey.py. (RSA deferred — deterministic RSA needs a seeded
  > prime search cryptography doesn't expose, and ECDSA covers the FIPS need.)
  > User-requested 2026-06-06; unblocks ESXI-16 run-phase host SSH.

- [x] **CORE-59** · `bugfix` — cleanup --all aborts whole sweep when one run's backend is gone
  _(done: 2026-06-01)_

  > Reported 2026-06-01: a leftover state.json referenced a PVE host that no longer exists; `testrange cleanup --all` failed instead of cleaning the other runs.
  >
  > Root cause: state/cleanup.py::cleanup_run calls driver.connect() OUTSIDE the per-resource try/except (cleanup.py:93). A dead/unreachable backend makes connect() raise DriverError. cleanup_all only catches StateLockedError/StateError per run, so DriverError propagates out of the generator; the CLI does list(cleanup_all(...)), so the exception aborts the entire sweep AND discards results already collected. The per-RESOURCE loop is resilient; the per-RUN path is not.
  >
  > Fix: cleanup_all must attempt every state file independently — broaden the per-run catch (mirroring the existing broad 'except Exception' in cleanup_run's resource loop) so a connect/instantiate failure for one run is recorded as an error CleanupResult and the sweep continues. The unreachable run's ledger is left on disk for a later retry. TDD: failing test in test_state_cleanup.py (TestCleanupAll) first.
  >
  > **DONE 2026-06-01:** cleanup_all now catches any per-run failure (not just StateLockedError/StateError) — a backend that's gone (connect() raises DriverError) is recorded as a '(driver)' error CleanupResult and the sweep continues to the next state file; the unreachable run's ledger is left on disk for a later retry. TDD: test_state_cleanup.py::TestCleanupAll::test_dead_backend_does_not_abort_the_sweep (red→green). Gates green (ruff/format/mypy/960 unit).

- [x] **CORE-50** · `bugfix` — isolate build serial firehose from --log-level
  _(done: 2026-06-01)_

  > DONE 2026-06-01 (KEPT — the sole survivor of the abandoned rich epic CORE-49). Stdlib fix: configure() calls _quiesce_firehose() pinning CONSOLE_LOGGER + TESTOUT_LOGGER to WARNING, decoupled from --log-level; only the --verbose live tail (live_output) lowers them to DEBUG. Regression test test_log.py::test_firehose_isolated_from_root_log_level. build_phase.py _console comment + PLAN.md ORCH-6/CORE-6 updated (firehose via --verbose, not --log-level debug). No rich. Gates green: ruff, format, mypy --strict (188), 955 unit.

- [x] **CORE-48** · `chore` — convention nits -- section markers, dead code, accept_serial lock
  _(done: 2026-06-01)_

  > DONE 2026-06-01. Removed 20 section-divider comments (_tui.py + proxmox driver/_vm/_storage/_client). Deleted dead proxmox/_naming helpers (is_iso_ref/volid_filename/volid_storage). Locked accept_serial's _serial_listeners read (libvirt/_conn.py). PID helpers kept (deliberate breadcrumb surface, is_pid_alive tested).

- [x] **CORE-47** · `chore` — strip cross-cutting architecture narration from docstrings
  _(done: 2026-06-01)_

  > DONE 2026-06-01. Trimmed cross-cutting architecture narration from docstrings in networks/base.py, devices/network/base.py, devices/cpu/base.py, vms/nested.py, communicators/native.py, preflight.py, cli.py, orchestrator/build_phase.py. Kept legitimate driver-mechanics docs.

- [x] **CORE-46** · `bugfix` — State.replace_resource silent no-op
  _(done: 2026-06-01)_

  > DONE 2026-06-01. State.replace_resource raises StateError on no-match (was silent no-op). Test: test_state_schema.py::TestState.

- [x] **CORE-45** · `bugfix` — _format_size truncates fractional units
  _(done: 2026-06-01)_

  > DONE 2026-06-01. cli.py _format_size carried a float instead of floor-dividing; sizes no longer collapse to X.0. Test: test_cache_cli.py::TestFormatSize.

- [x] **CORE-44** · `chore` — merge feature/{builders,concurrent,nested-virt} into main
  _(done: 2026-06-01)_

  > Integrated the three feature worktrees back into main, preserving functionality. Order: builders -> nested-virt -> concurrent (--no-ff merge commits b51198e, e4baae0, c716122). DONE 2026-06-01.
  >
  > Conflict resolutions of note:
  > - libvirt serial-sink gating reconciled: build_nic OR boot_media_ref (builders' installer-origin) while sidecars stay on QGA (nested-virt's remote-daemon fix).
  > - run_phase bind_communicators: nested-virt's guest_gateway() binding folded into concurrent's parallelized _bind_one_communicator.
  > - ADR number collision: concurrent's in-process-io-parallelism renumbered 0020->0023 (0020 is guest-gateway-abstraction); refs disambiguated.
  > - PLAN.md: dropped now-built deferred entries (parallel build, installer-origin).
  >
  > Gates green on merged HEAD: ruff, ruff-format, mypy --strict, 935 pytest passed. Safety tag pre-merge-CORE-44 at cc50255. Live libvirt/proxmox integration suites NOT run (no backend here) — run out-of-band before push.

- [x] **CORE-43** · `bugfix` — reject --jobs < 0 at CLI boundary (0 stays serial)
  _(done: 2026-06-01)_

  > ADR-0020 review: --jobs silently coerced negatives to serial. FIX: argparse type=_jobs_arg rejects n<0; 0 and 1 stay serial (user pref). Help text shows default 8. Tests: test_concurrency_guards.TestJobsArg. Done 2026-06-01.

- [x] **CORE-42** · `bugfix` — GuestHypervisor boundary checks + dead-weight cleanup
  _(done: 2026-06-01)_

  > Code-review findings (ADR-0021). (1) inner typed Hypervisor but runtime is libvirt-only; add __post_init__ TypeError at construction, drop the now-dead orchestrator isinstance guard. (2) Drop dnsmasq from _LIBVIRT_APT_STACK + fix the factually-wrong docstring (sidecar ships its own dnsmasq; libvirt emits no <dhcp>). (3) base sentinel -> required kwarg (matches CloudInitBuilder). (4) Split _admin_ssh_key 'no credential' vs 'not key-bearing' messages. (5) NestedHandle.vms/driver typed -> object; annotate real types. (6) Remove mechanic comment; generic-prose tidy. Completed 2026-06-01.

- [x] **CORE-40** · `test` — examples/capabilities-nested.py portable nested plan + TESTS
  _(blocked by: ORCH-20; done: 2026-05-31)_

  > DONE 2026-05-31. examples/capabilities-nested.py + 4 TESTS + tests/integration/test_nested.py. VERIFIED LIVE green: host_runs_libvirtd, inner_webapp_reachable, inner_webapp_serves, inner_webapp_on_inner_subnet all PASS.

- [x] **CORE-39** · `feat` — CPU(nested=) portable HW-virt knob + nested-KVM preflight
  _(blocked by: DOCS-7; done: 2026-05-31)_

  > DONE 2026-05-31. CPU(count,*,nested=False) knob; LibvirtDriver._nested_kvm_findings preflight (local-only sysfs probe, fail-loud); tests in test_libvirt_driver.py.

- [x] **CORE-38** · `feat` — GuestHypervisor recipe type (VMRecipe subclass + inner Hypervisor)
  _(blocks: BACKEND-10, BUILD-14, ORCH-20, NET-17; blocked by: DOCS-7; done: 2026-05-31)_

  > DONE 2026-05-31. GuestHypervisor(VMRecipe)+inner in testrange/vms/nested.py, exported; .libvirt() sugar; tests in test_guest_hypervisor.py. 800 unit tests green, gates clean.

- [x] **CORE-37** · `chore` — drop stale 'via pyroute2' from create_switch ABC docstring
  _(done: 2026-05-31)_

  > testrange/drivers/base.py create_switch() docstring listed the libvirt L2 realization as 'a host bridge (via pyroute2)'. Stale since BACKEND-1: libvirt builds the bridge through the daemon's network API (networkDefineXML/Create), no pyroute2, no CAP_NET_ADMIN. Sibling bullets name only the primitive, not the SDK — dropped the '(via pyroute2)' parenthetical to match. Surfaced while closing BACKEND-1/131.
  >
  > DONE 2026-05-31: parenthetical dropped (base.py:136 now 'a host bridge that networks attach to'). Gates green: ruff + ruff format + mypy --strict + pytest (754 passed).

- [x] **CORE-6** · `feat` — --verbose flag — BuildKit-style collapsing live tail for streaming output
  _(done: 2026-05-31)_

  > Re-introduce a --verbose CLI flag (global) that renders streaming output as a Docker-BuildKit-style live tail: a fixed-height region (~15 lines, configurable) redrawn in place showing only the most-recent lines, so a firehose doesn't scroll the whole screen. On step completion, collapse the region to a one-line summary (e.g. '=> build web  DONE 47s').
  >
  > SOURCES to feed the tail:
  > - build-phase serial console stream (orchestrator/build_phase.py _ConsoleStreamer).
  > - user test-function output: prints/stdout/stderr from TESTS — runner wraps each test (contextlib.redirect_stdout/stderr or a tee) into the same renderer.
  >
  > REQUIREMENTS / design notes:
  > - TTY-only. Non-TTY (CI, piped, redirected) falls back to plain per-line logging — mirror the ProgressReporter TTY/non-TTY split. Restore cursor / tear down the scroll region cleanly on exception (never leave the terminal wedged).
  > - PREREQUISITE (can land first, independently valuable): scrub ANSI/CSI escape sequences + C0 control bytes (incl. embedded \r and cursor-position-report responses) from the streamed console AND the decoded BuildFailedError log. Today the raw guest escapes hijack the operator's terminal (clears/overwrites observed in live PVE runs) and garble the captured fail-log. The scrub fixes that even without the tail UI.
  > - Compose with --log-level: --verbose is the friendly tail UI; --log-level debug stays the full firehose to the logger. Pick one renderer for the terminal so they don't fight (e.g. verbose owns the TTY region; debug dumps full to stderr/non-TTY).
  > - Reusable renderer (new _tui.py or extend _progress.py): ring buffer of N lines, width/height + SIGWINCH handling.
  > 2026-05-24. '(again!)' — a --verbose existed before and was descoped.
  >
  > --- PROGRESS 2026-05-31 ---
  > Stage 1 (prereq) DONE: testrange/_ansi.py::scrub_terminal_control (CSI/OSC/Fe escapes + C0/DEL; keeps \n/\t). Wired into _ConsoleStreamer (live mirror) + BuildFailedError log decode. 12 scrub tests + 2 wiring tests; gates green (ruff/mypy --strict/731 unit). PLAN.md §21 updated.
  > Remaining: _tui.py LiveTail renderer (ring buffer, redraw-in-place, collapse-on-done, SIGWINCH, cursor-restore), --verbose global flag, wire build serial stream + per-test stdout/stderr into it.
  >
  > --- STAGE 2 DONE 2026-05-31 ---
  > testrange/_tui.py::LiveTail (logging.Handler): ring-buffer region redrawn in place, per-step collapse summary (=> build web DONE 47s), SIGWINCH-aware, cursor restored on teardown. live_output(verbose=) context = TTY/non-TTY split (TTY: sole testrange handler; non-TTY: bump console/testout loggers to DEBUG for plain logging). capture_test_output tees per-test stdout/stderr into TESTOUT_LOGGER (re-entrancy-guarded vs logging.handleError recursion). --verbose global CLI flag wired around run/build; composes with --log-level. Tests: test_tui (11), runner verbose tee + passthrough (2), cli verbose e2e (3). PLAN §21 updated. Gates green (ruff/mypy --strict/747 unit). COMPLETE.

- [x] **CORE-36** · `chore` — misc lows
  _(done: 2026-05-31)_

  > TO_LOOK_AT lows. memory/base.py:14 MB/MiB docstring backwards; _naming.py truncation magic-number (-7 vs [:6]) can desync; redundant Pip.__post_init__ override (packages/pip.py:23); _-prefixed orchestrator helpers crossing stovepipe; test_orchestrator.py:309-311 asserts private o._leak (behavioral assert already covers it).
  >
  > Completed 2026-05-31 (the _-prefixed-helper-across-stovepipe nit left as-is: the orchestrator is the sanctioned broker per feedback_stovepipes; renaming risks churn for a debatable gain).

- [x] **CORE-35** · `chore` — state/cache mediums — schema fail-loud + resolve nits
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums/lows. state/schema.py:123 from_json accepts unknown schema_version & silently degrades -> fail loud (ADR-0003). cache/_names.py:16 regex allows pure-dot names — exclude. cache/local.py:176 resolve sha-prefix O(n) full-scan returns first match on short-prefix collision silently — fail loud / document.
  >
  > Completed 2026-05-31.

- [x] **CORE-34** · `chore` — CLI nits — ASCII-safe glyphs + --log-level alias
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums/lows. Non-ASCII glyphs (warn/arrows) UnicodeEncodeError on LANG=C/redirect (describe output goes into reports) — ASCII fallback. cli.py:539 accepts both WARN and WARNING — pick WARNING.
  >
  > Completed 2026-05-31.

- [x] **CORE-33** · `bugfix` — describe routes backend errors to stderr + non-zero
  _(done: 2026-05-31)_

  > TO_LOOK_AT H13. cli.py:321 — DriverError from resolve_backend prints 'backend: ERROR' to stdout with Exit.OK, so 'describe && run' proceeds on a broken binding. Route to stderr + non-zero.
  >
  > Completed 2026-05-31.

- [x] **CORE-32** · `bugfix` — CLI cache-error & cleanup-error nets
  _(done: 2026-05-31)_

  > TO_LOOK_AT H12. cli.py:166-189/271-281 — _build/_run wrap CacheError/CacheMissError family during base resolution (@_cache_errors); _cleanup gets TestRangeError/KeyboardInterrupt net (it's the recovery path).
  >
  > Completed 2026-05-31.

- [x] **CORE-31** · `bugfix` — wrap plan module load -> stderr + Exit.USAGE
  _(done: 2026-05-31)_

  > TO_LOOK_AT B4. cli.py:122 exec_module runs arbitrary user code; Hypervisor.__post_init__ raises bare ValueError via networks/validate.py. Catch ValueError/PlanError/Exception -> stderr, exit Exit.USAGE. Restores documented 'invalid plan -> exit 2'.
  >
  > Completed 2026-05-31.

- [x] **CORE-30** · `bugfix` — replace PID-liveness ownership guard with fcntl.flock
  _(done: 2026-05-31)_

  > TO_LOOK_AT H10. state/store.py:159-166 + state/cleanup.py:74. Advisory flock on state.lock held by owner for run lifetime; cleanup flock(LOCK_EX|LOCK_NB) acquires (owner gone) or fails (alive) — no PID-reuse window. state.pid kept as breadcrumb only. Regression test: held lock -> refuse, released -> proceed.
  >
  > Completed 2026-05-31.

- [x] **CORE-29** · `docs` — ADR-0018 single-instance-only + PLAN.md §16 rewrite
  _(done: 2026-05-31)_

  > TO_LOOK_AT B5/H-doc. New ADR-0018 'Single-instance-only; multi-instance deferred'. Rewrite PLAN.md §16 (l.372-401): one testrange process per user/profile at a time, not guarded beyond state.pid/flock liveness, multi-instance out of scope -> ORCH-10/11/12. Cross-ref ADR-0002.
  >
  > Completed 2026-05-31.

- [x] **CORE-28** · `test` — single-instance-contract tests
  _(done: 2026-05-31)_

  > TO_LOOK_AT B5. Replace the missing-concurrency-test gap with the correct tests for a single-instance tool: initialize() refuses pre-existing state.json; require_dead rejects live owner / passes dead owner; cache atomic-rename leaves no .partial after a simulated mid-write failure (single process). Extend tests/unit/test_state_store.py + test_cache_local.py.
  >
  > Completed 2026-05-31.

- [x] **CORE-27** · `chore` — reframe state/cache docstrings as single-instance crash recovery
  _(done: 2026-05-31)_

  > TO_LOOK_AT B5/H11. Rewrite over-claiming prose in state/store.py (module doc, PID-first comment l.110-114, RMW docstrings), cache/local.py (l.61-62 thread-safe claim), cache/manager.py, cache/http.py. Keep crash-safe write-ordering docs (sidecar-LAST). Drop 'thread-safe'/'two writers'/'concurrent cleanup --all' claims. No behavior change; machinery stays.
  >
  > Completed 2026-05-31.

- [x] **CORE-26** · `test` — re-enable no-net + add single-static capability; verify netplan survives seed-less run boot
  _(blocked by: ORCH-9, BACKEND-7, BUILD-6; done: 2026-05-30)_

  > RETIRED 2026-05-30 — folded away after the ADR-0017 spike.
  >
  > (1) Verification gate (does the rendered netplan survive the seed-less run boot?) — SATISFIED by the 2026-05-30 libvirt spike; recorded in ADR-0017 Validation. BUILD-6 keeps the disable guard unconditionally as a result.
  > (2) Capability re-enable (no-net) + single-static case — moved into ORCH-9 per working-agreement rule #4 (capabilities land in the same change as the feature).
  >
  > Nothing left here. Kept (not deleted) per the never-delete norm; move/delete at will.

- [x] **CORE-24** · `test` — capabilities data_disk_bytes_survived_capture uses non-root blkid
  _(done: 2026-05-30)_

  > Found during BACKEND-1.D libvirt certification (2026-05-30). data_disk_bytes_survived_capture runs 'blkid -s UUID -o value /dev/vdb' as the non-root SSH user (admin) -> returns empty (reading a raw block device needs root), so it compares ''(live) vs the root-written seeded UUID and fails. The data DID survive: data_disks_mounted + data_disks_carry_their_own_content PASS (fs mounted via fstab LABEL, /srv/b/which==disk-b). So this is a TEST bug, not data loss / not a driver bug. FIX: run blkid via sudo (or read the UUID from a root-readable path). WORKAROUND (2026-05-30): data_disk_bytes_survived_capture commented out in examples/capabilities.py.

- [x] **CORE-25** · `test` — capabilities memory_snapshot writes /run (root-only) but keybox SSH runs as non-root admin
  _(done: 2026-05-30)_

  > Re-scoped from COMM-4 after a controlled repro (2026-05-30). ORIGINAL symptom: capabilities memory_snapshot_restores_running_state FAILS on keybox (SSHCommunicator) — /run/mem-marker absent after restore_snapshot(mem=True), though get_vm_power_state==running.
  >
  > ROOT CAUSE (confirmed): the test writes the marker to /run/mem-marker, which is root-owned 0755. keybox is reached as SSHCommunicator('admin') — a NON-root user — so 'echo live > /run/mem-marker' fails with permission denied (the test never checks that write's exit code); the snapshot captures no marker and the post-restore cat finds nothing. NOT a driver/snapshot/transport bug.
  >
  > EVIDENCE: a controlled repro of keybox's EXACT two-snapshot sequence (create pre-write -> reboot -> restore pre-write -> create mem-snap mem=True -> rm -> restore mem-snap) driven over QGA (which execs in-guest as root) PASSES: marker restored to 'live', state running. The only difference from the failing capabilities case is the communicator's in-guest UID: QGA=root vs SSHCommunicator=admin(non-root). disk_snapshot_lifecycle passes over the same SSH because its sentinel is /home/admin/snapshot-test (user-writable).
  >
  > This is the SAME class as CORE-24 (blkid needs root). The libvirt driver's memory-snapshot capability is correct (QGA-proven). KEY SEMANTIC: QGA/NativeCommunicator runs as root; SSHCommunicator runs as the plan's user. FIX (test/example): write the marker to a user-writable path (e.g. /home/admin or /tmp) so it works as the SSH user, OR exercise the mem-snapshot capability on a root/QGA-reached VM. WORKAROUND (2026-05-30): memory_snapshot_restores_running_state commented out in examples/capabilities.py.
  >
  > DONE 2026-05-30: fixed by moving the marker to /dev/shm/mem-marker (tmpfs => proves RAM restore; mode 1777 => writable by the non-root SSH admin user). Re-enabled memory_snapshot_restores_running_state in examples/capabilities.py. Confirmed: [PASS] memory_snapshot_restores_running_state over SSHCommunicator('admin'); capabilities now 23/23 green. Confirms root cause (root-only /run vs non-root SSH user) and that the driver's mem-snapshot works over SSH.

- [x] **CORE-23** · `feat` — named uplinks —  map, driver-resolved logical uplink, preflight unmapped finding
  _(done: 2026-05-29)_

  > Switch.uplink becomes a logical name. [<profile>.uplinks] maps name->host iface; profile passes the map into build_driver (rides on the driver like backing_storage). Driver resolves switch.uplink in create_switch; unmapped name -> DriverError, surfaced up-front as a preflight finding (unknown-uplink). Mock + Proxmox (+ libvirt) carry the map. 2026-05-29.

- [x] **CORE-22** · `feat` — connect.toml multi-profile + --profile name (rename from --connect)
  _(done: 2026-05-29)_

  > One TOML, many  tables, each with its own driver= key + backend keys. load_profile(path, name). CLI flag --connect -> --profile, grammar '<file>:<name>' with default file connect.toml ('foo' => connect.toml:foo). Update describe/UNBOUND messaging. Drop the  table parser + build_switch field on BackendProfile. 2026-05-29.

- [x] **CORE-17** · `docs` — capabilities example exercising all driver aspects
  _(done: 2026-05-28)_

  > examples/capabilities.py: single portable-topology plan on the generic Hypervisor (CORE-7/CORE-10, feature/backend-binding) exercising every driver-facing capability, with a live TESTS list (28 tests across 8 VMs) verifying each.
  >
  > STATUS 2026-05-28: DONE. File authored; passes ruff + ruff format; plan constructs/validates and 'testrange describe' renders it. End-to-end run remains forward-looking, blocked on (a) CORE-10 '--connect' backend resolution and (b) relaxing build_phase._probe_vm's zero-NIC rejection so the 'no-net' VM can build — both tracked separately. The example's existence and structure satisfy CORE-17; the rule that any new capability lands here in the same change is codified in CLAUDE.md §4.
  >
  > Coverage: no-runtime-NIC (zero NICs, Native); unmanaged NIC (addr=None, Native); multi-NIC mixed types (static/dhcp/unmanaged); SSH key auth + nic_idx over DHCP-discovered host; SSH password auth + non-admin sudo denial + groups; data disks w/ disk-unique content + build->run integrity (blkid UUID match); OSDrive growpart; Apt + Pip; explicit-vs-derived resolver; sidecar dhcp/dns/nat; multi-Network-per-Switch (cross-label DNS reach); air-gap reachability matrix; disk + memory snapshot lifecycle + power-state.
  >
  > PLAN.md examples list updated.

- [x] **CORE-12** · `docs` — ADR + user guide + portable example for the backend-binding split
  _(blocked by: CORE-10, CORE-11; done: 2026-05-27)_

  > HIGH PRIORITY (lands with the feature, not after). Capture the decomposition and migrate the canonical example so the new portable workflow is the documented default.
  >
  > SCOPE:
  > - ADR (docs/adr/00NN-backend-binding.md via /adr): record the decision -- generic Hypervisor (topology) vs concrete *Hypervisor (pins driver); connection supplied via a local TOML profile on --connect; concrete entry pins the driver and the profile overrides CONNECTION only (mismatch = error); env-knobs (build_uplink/backing_storage/node) live on the binding, not the topology. Note the three-layer compatibility preflight (pin match / portability lint / live capability findings). Link ADR-0008 (driver ABC) and ADR-0010 (build/run phases).
  > - User guide (docs/user/...): 'Writing a portable plan' (use `from testrange import Hypervisor`, generic devices only) + 'Connecting to your backend' (the connect.toml schema, secret-from-env/file, build_uplink for host-NAT, --connect / TESTRANGE_CONNECT) + 'Pinning a plan to a backend' (use ProxmoxHypervisor when the test genuinely needs PVE). Respect the generic-prose rule: say 'Hypervisor'/'driver' in generic sections.
  > - Examples: convert examples/hello_world.py to the generic `Hypervisor` (topology only, no creds -- and per the no-comments-in-examples rule, keep it clean copy-paste shape). Add examples/connect.toml.example (a documented sample profile; .example so it's safe to commit). Keep examples/px_hello.py as the PINNED-Proxmox example and add a one-line pointer in its docstring that hello_world is the portable form.
  > - .gitignore: add connect.toml (and *.connect.toml) so a real profile with creds is never committed; keep the .example tracked.
  > - PLAN.md: new section documenting Plan-entry vs binding split + the resolve_backend matrix; update the storage/CLI sections that reference plan.hypervisor connection fields.
  >
  > ACCEPTANCE:
  > - ADR merged and numbered; user-guide pages render; hello_world.py parses under `testrange describe` as UNBOUND (no --connect) and runs with --connect examples/connect.toml.example pointed at a real host; PLAN.md reconciled; .gitignore updated. No code logic here -- docs + example + config only.
  >
  > DONE 2026-05-27: ADR-0015-backend-binding (+adr index); examples/hello_world.py -> generic Hypervisor (clean); examples/connect.toml.example; px_hello.py pointer; .gitignore connect.toml/*.connect.toml; docs/user/connecting-to-a-backend.md (+toctree, writing-a-plan pointer); PLAN.md §22. Sphinx builds clean, both pages render. Gates green (645).

- [x] **CORE-11** · `feat` — CLI `--connect <profile>` wiring + `describe` shows the resolved binding
  _(blocks: CORE-12; blocked by: CORE-9, CORE-10; done: 2026-05-27)_

  > HIGH PRIORITY. Surface the connection profile on the CLI and thread it through run/build/repl/describe.
  >
  > SCOPE (testrange/cli.py):
  > - Add `--connect PATH` to run, build, repl, describe (a per-subcommand arg on the plan-taking verbs; NOT cache/cleanup). Env fallback TESTRANGE_CONNECT when the flag is absent.
  > - On the plan verbs: if --connect given, load_profile(path) (CORE-9) and pass profile=... into build_range / run_tests / Orchestrator (CORE-10). If absent, pass None (concrete plans keep working unchanged).
  > - Error mapping -> exit codes consistent with the existing scheme: profile file not found / parse error -> 2 (usage-ish); binding/pin mismatch (the resolve_backend hard errors from CORE-10) -> 2 with the message on stderr; keep PreflightError->2, BuildFailed->1, etc.
  > - _print_describe: when a profile is supplied (or the entry is concrete), print the RESOLVED binding block: driver, host, port, node, build_uplink, build_uplink_addr. Password handling: the user has OK'd printing creds for this internal-lab tool, but default describe to MASK it (show 'password: ***set*** / (from env PVE_PW) / (unset)') and leave the value unprinted -- describe output is the thing most likely to get pasted into a report/PR. (Confirm masking default with the user when implementing.)
  > - When the plan is generic AND no --connect AND no env: describe should still render the topology, but flag 'backend: UNBOUND (pass --connect to run)'. run/build on that combo error via CORE-10.
  >
  > ACCEPTANCE / TESTS (tests/unit/test_cli.py):
  > - run/build parse --connect, load the profile, and pass it through (assert via a patched runner capturing the profile arg).
  > - TESTRANGE_CONNECT env fallback honored; flag beats env.
  > - Profile-not-found -> exit 2, message on stderr.
  > - describe on a generic plan with --connect prints the resolved binding with the password MASKED; describe on a generic plan WITHOUT --connect prints 'backend: UNBOUND'.
  > - describe on a concrete plan (no --connect) unchanged from today.
  > - Full suite green; ruff/format/mypy --strict clean.
  >
  > --- DESCOPE 2026-05-27 (user) ---
  > NO `TESTRANGE_CONNECT` env fallback. `--connect PATH` is the only way to supply a profile; absent => None (concrete plans unchanged). Drop all env-fallback wiring and its test.
  >
  > DONE 2026-05-27: cli.py --connect on run/build/repl/describe (no env fallback per descope); _load_profile_arg (ProfileError->exit2); resolve_backend DriverError->exit2; _print_binding block (password MASKED, UNBOUND for generic). tests/unit/test_cli.py (CORE-11 classes). Gates green (645).

- [x] **CORE-10** · `feat` — `resolve_backend` binding resolver + orchestrator consumes `ResolvedBackend` (pin/override/require matrix + compatibility gate)
  _(blocks: CORE-11, CORE-12; blocked by: CORE-7, CORE-8, CORE-9; done: 2026-05-27)_

  > HIGH PRIORITY. The heart of the feature: fold (Plan entry, optional CLI profile) into a single ResolvedBackend the orchestrator consumes, and enforce the compatibility/pin rules. Implements the user-confirmed decisions: (a) profile-file connection; (b) a concrete *Hypervisor PINS the driver, the profile may override CONNECTION only (driver mismatch = hard error); (c) env-knobs (build egress + backing_storage/node) live on the BINDING.
  >
  > NEW: `ResolvedBackend` dataclass: { driver: HypervisorDriver, build_switch: Switch | ManagedBuildSwitch | None, backing_storage/node as needed, driver_uri: str }. This is the single thing the orchestrator reads instead of reaching into plan.hypervisor for driver/env/uri. (build_switch REPLACES the old build_uplink + build_uplink_addr pair — see RESOLVED note.)
  >
  > NEW: resolve_backend(plan, profile: BackendProfile | None) -> ResolvedBackend, implementing the matrix (pin = is_pinned(plan.hypervisor) from CORE-8):
  >   concrete + none  -> TODAY'S PATH: driver_for(hyp); env-knobs+driver_uri from the entry (full back-compat).
  >   concrete + given -> profile.driver scheme MUST == scheme_for_hypervisor(hyp), else HARD ERROR; driver built from profile connection; env-knobs from profile; topology from plan.hypervisor.
  >   generic + none   -> HARD ERROR: 'this plan is backend-agnostic; pass --connect <profile>'.
  >   generic + given  -> driver from driver_for_profile(profile); env-knobs from profile; driver_uri from the resolved driver/conn.
  >
  > COMPATIBILITY PREFLIGHT = three layers: (1) static pin/driver-match in resolve_backend; (2) portability lint compatibility_findings(plan, driver) -> tuple[PreflightFinding,...] (near-empty today, the honest hook for backend-specific device subclasses); (3) live capability findings (native_capability_findings, mgmt_unsupported_findings) run against the RESOLVED driver. NOTE: supports_managed_build_egress (CORE-8 #69) is checked here when build_switch is a ManagedBuildSwitch.
  >
  > ORCHESTRATOR REFACTOR (testrange/orchestrator/runtime.py): __init__ / build_range / run_tests / _run gain profile: BackendProfile | None = None. Replace _build_driver()=driver_for(plan.hypervisor) and the getattr(plan.hypervisor,'build_uplink'/'driver_uri') reads with a single resolve_backend(...) call; hold ResolvedBackend on the context. _build_switch consumes ResolvedBackend.build_switch directly (no synthesis). state.initialize uses ResolvedBackend.driver_uri + driver.DRIVER_NAME.
  >
  > ACCEPTANCE / TESTS (tests/unit/test_resolve_backend.py + orchestrator tests): all four matrix cells incl. concrete+matching-profile and concrete+mismatched-profile (hard error w/ both scheme names); generic+none names --connect; env-knobs source per cell; MockHypervisor end-to-end build_phase green with profile=None; compatibility_findings returns () for a valid generic plan against MockDriver + a stub-driver test proving a finding blocks.
  >
  > build_switch PLACEMENT (RESOLVED 2026-05-26, with NET-11 #99): ResolvedBackend carries a SINGLE build_switch: Switch | ManagedBuildSwitch field, REPLACING build_uplink + build_uplink_addr (set from the BackendProfile). This SUPERSEDES any build_uplink/build_uplink_addr references above — substitute one build_switch field throughout. Phasing: NET-11 (#99) FIRST lands build_switch on the concrete *Hypervisor (like-for-like with today's build_uplink) so it ships independently; THIS ticket relocates it to the binding exactly as it was going to relocate the two env-knobs. Build egress lives on the binding, NOT the generic topology Hypervisor (CORE-7 #68).
  >
  > DONE 2026-05-27: testrange/orchestrator/backend.py ResolvedBackend + resolve_backend (4-cell pin matrix) + compatibility_findings (layer-2 hook). RunContext.resolved (driver via property); build_phase + runtime read the binding not plan.hypervisor. runner build_range/run_tests + Orchestrator take profile=. tests/unit/test_resolve_backend.py. Gates green (637).

- [x] **CORE-9** · `feat` — `BackendProfile` — connection-profile file (TOML) schema, loader, secret resolution
  _(blocks: CORE-10, CORE-11; blocked by: CORE-8; done: 2026-05-27)_

  > HIGH PRIORITY. The local, gitignored file a dev points --connect at to supply their backend: driver + connection + environment knobs. Keeps secrets out of the committed test and out of shell history; the only connection form that cleanly carries build_uplink/backing_storage (a URI can't).
  >
  > FORMAT: TOML, parsed with stdlib tomllib (Python 3.11+, already required by the codebase -- datetime.UTC / hashlib.file_digest are used; ZERO new dependency). Example:
  >
  >     driver = "proxmox"
  >     host = "40.160.34.83"
  >     user = "root@pam"          # optional; driver default otherwise
  >     password = "Target123!"     # or password_env / password_file (mutually exclusive)
  >     # password_env = "PVE_PW"
  >     port = 8006                  # optional
  >     verify_ssl = false           # optional
  >     # --- environment knobs (lifted out of topology; see CORE-10 decision) ---
  >     build_uplink = "vmbr9"
  >     backing_storage = "local"
  >     node = ""
  >     ssh_user = "root"
  >     ssh_password = "..."         # or ssh_password_env / _file
  >     ssh_port = 22
  >               # structured StaticAddr
  >     addr = "10.10.10.2/24"
  >     gw = "10.10.10.1"
  >     dns = ["1.1.1.1"]
  >
  > SCOPE (new module testrange/connect.py or testrange/backend_profile.py):
  > - `BackendProfile` frozen dataclass mirroring the schema; driver (required) + the rest optional.
  > - load_profile(path: Path) -> BackendProfile: read+parse TOML, validate, resolve secrets:
  >     * password / password_env / password_file are mutually exclusive; _env reads os.environ (clear error if unset), _file reads & strips a file (mode warning if world-readable? keep simple: just read).
  >     * same pattern for ssh_password.
  >     * build_uplink_addr table -> StaticAddr(addr, gw=, dns=tuple) with the same prefix/validation rules StaticAddr already enforces.
  >     * driver must be a non-empty string (scheme); unknown-scheme is NOT validated here (CORE-8's driver_for_profile owns that) -- keep this module backend-agnostic, it only parses+validates shape.
  >     * unknown top-level keys -> hard error (typo protection), with the offending key named.
  > - to_mapping() -> dict for the registry from_profile (CORE-8); connection fields only (driver + host/user/password/port/verify_ssl/node/backing_storage/ssh_*). build_uplink/_addr are read off the BackendProfile directly by the resolver, NOT passed to from_profile.
  >
  > ACCEPTANCE / TESTS (tests/unit/test_backend_profile.py, table-driven):
  > - Full profile round-trips to the right fields incl. structured build_uplink_addr.
  > - password_env resolves from env; missing env -> clear error. password + password_env together -> error. password_file reads & strips.
  > - Missing driver -> error; unknown key -> error naming the key; bad build_uplink_addr (no prefix) -> error.
  > - Minimal profile (driver+host only) parses.
  > - Full suite green. No CLI/orchestrator wiring here (that's CORE-11/CORE-10).
  >
  > --- DESCOPE 2026-05-27 (user) ---
  > Secret resolution is KEPT SIMPLE: profiles carry plain `password` and `ssh_password` strings inline. Dev backends are firewalled labs and creds-in-the-TOML is acceptable. DROP the `password_env`/`password_file`/`ssh_password_env`/`_file` indirection and the mutual-exclusion logic entirely — they are not implemented. `.gitignore` (CORE-12) keeps a real profile out of git; that is the only secret-handling measure.
  >
  > DONE 2026-05-27: testrange/connect.py BackendProfile + load_profile (tomllib) + to_mapping; ProfileError. Inline-secrets per descope. build egress via  table -> ManagedBuildSwitch (BYO plain-Switch egress = pin the plan). tests/unit/test_backend_profile.py. Gates green (628).

- [x] **CORE-8** · `feat` — Driver registry — scheme map, pin introspection, per-driver `from_profile`
  _(blocks: CORE-10, CORE-9; done: 2026-05-27)_

  > HIGH PRIORITY. Registry plumbing so a connection profile can name a driver by short scheme and so the binding resolver can tell pinned (concrete) from generic Plan entries.
  >
  > CURRENT STATE: testrange/drivers/_registry.py has register(hypervisor_cls, driver_name, from_hypervisor, from_uri) with two maps: _FROM_HYP (type->factory) and _FROM_NAME (driver_name->from_uri). driver_for(hyp) dispatches on type; driver_for_name(name, uri) is the cleanup path.
  >
  > SCOPE:
  > - Extend register() with: scheme: str (short token, e.g. 'proxmox', 'mock') and from_profile: Callable[[Mapping], HypervisorDriver].
  > - Add maps: _BY_SCHEME -> from_profile factory; _SCHEME_FOR_HYP -> scheme.
  > - Add lookups:
  >     * driver_for_profile(profile_mapping) -> HypervisorDriver  (reads profile['driver'] scheme, dispatches; clear error listing known schemes on miss).
  >     * scheme_for_hypervisor(hyp) -> str | None  (None == generic/unregistered -> not pinned).
  >     * is_pinned(hyp) -> bool  (type(hyp) in _FROM_HYP).
  > - Update the two register() call sites (MockDriver in drivers/mock.py, ProxmoxDriver in drivers/proxmox/driver.py) to pass scheme + from_profile.
  > - Implement from_profile on each driver:
  >     * Proxmox: profile dict -> ProxmoxConn (host/user/password/port/verify_ssl/node/backing_storage/ssh_*) -> ProxmoxDriver(conn). Reuse the realm-normalisation already in ProxmoxHypervisor.conn() (bare user -> user@pam) -- factor that into a shared helper so the profile path and the Plan-entry path agree.
  >     * Mock: profile dict -> MockDriver(pool_root=..., backing_capacity_gb=...). Mostly for tests / a 'mock' profile.
  >
  > OUT OF SCOPE: build_uplink / build_uplink_addr are NOT driver-construction inputs (the orchestrator reads them, not the driver) -- they ride on the BackendProfile (CORE-9) and are surfaced via ResolvedBackend (CORE-10), not via from_profile.
  >
  > ACCEPTANCE / TESTS (tests/unit/test_driver_registry.py):
  > - register with scheme; driver_for_profile({'driver':'mock',...}) builds a MockDriver; unknown scheme -> DriverError listing known schemes.
  > - scheme_for_hypervisor(MockHypervisor(...)) == 'mock'; scheme_for_hypervisor(Hypervisor(...)) is None; is_pinned() agrees.
  > - Proxmox from_profile builds a ProxmoxConn with realm-normalised user (assert via a faked client, no live PVE).
  > - Full suite green.
  >
  > ManagedBuildSwitch (NET-10/NET-11) synergy: add a per-driver supports_managed_build_egress capability alongside the scheme/from_profile plumbing (Proxmox=True via SDN SNAT, libvirt=True, ESXi=False). Fits the existing capability-introspection pattern; preflight reads it to accept/reject ManagedBuildSwitch. No conflict (unlike CORE-7/CORE-10).
  >
  > DONE 2026-05-27: registry scheme map (_BY_SCHEME/_SCHEME_FOR_HYP) + driver_for_profile/scheme_for_hypervisor/is_pinned; from_profile on mock/proxmox/libvirt; register() takes scheme+from_profile; normalize_realm shared helper. tests/unit/test_driver_registry.py. Gates green (612).

- [x] **CORE-7** · `feat` — Generic `Hypervisor` topology type (backend-agnostic Plan entry)
  _(blocks: CORE-10; done: 2026-05-27)_

  > HIGH PRIORITY. Foundation for decoupling the test (topology) from the backend (connection+driver). See ADR in CORE-12.
  >
  > PROBLEM: today every Plan entry is a concrete `*Hypervisor` (ProxmoxHypervisor, MockHypervisor) that conflates four jobs: (1) topology networks/pools/vms, (2) backend selection via the class type -> driver_for(type), (3) connection host/creds/port/node/ssh, (4) env-knobs build_uplink/backing_storage. Jobs 2-4 force a portable test (e.g. examples/hello_world.py) to hardcode a specific backend. The topology layer is ALREADY 100% backend-agnostic (verified: no backend-specific device/builder/communicator/network/vm subclasses; only docstrings mention backends).
  >
  > DELIVERABLE: a new generic `Hypervisor` frozen dataclass that holds ONLY portable topology and selects NO driver / carries NO connection.
  >
  > SCOPE:
  > - Add `Hypervisor` (likely testrange/plan.py or a new testrange/hypervisor.py) with fields: networks: Sequence[Switch]=(), pools: Sequence[StoragePool]=(), vms: Sequence[VMRecipe]=().
  > - __post_init__: tuple-freeze the sequences and call validate_hypervisor_plan(networks, pools, vms) -- identical to MockHypervisor/ProxmoxHypervisor __post_init__ (DRY: consider a shared mixin/helper, but do NOT over-abstract per the no-speculative-abstraction rule -- a plain shared free function is fine).
  > - Expose `all_switches` property (parity with the concrete entries; the orchestrator reads it).
  > - Do NOT register it in the driver registry _FROM_HYP (that's what marks it 'generic / unpinned').
  > - Export from testrange/__init__.py so test authors write `from testrange import Hypervisor`.
  >
  > ACCEPTANCE / TESTS (tests/unit/test_plan.py or new test_hypervisor.py):
  > - Construction with topology works; validate_hypervisor_plan is invoked (bad addressing raises).
  > - It is frozen; all_switches returns the switches tuple.
  > - driver_for(Hypervisor(...)) raises a CLEAR DriverError naming it as backend-agnostic and pointing at --connect (this error text is finalized in CORE-10; here just assert it raises for an unregistered type).
  > - Existing suite stays green; no behavior change to concrete entries yet.
  >
  > NOTE: this ticket adds the type only. Wiring the orchestrator to accept it without a concrete driver is CORE-10.
  >
  > build_switch PLACEMENT (RESOLVED 2026-05-26, with NET-11 #99): build_switch does NOT live on this generic topology Hypervisor. It binds on BackendProfile / ResolvedBackend (CORE-10 #71) because build egress is a backend-specific BINDING concern (job 4 above), e.g. ManagedBuildSwitch(uplink='vmbr9'). This type carries ONLY portable topology — networks/pools/vms (job 1). No build_switch field here.
  >
  > DONE 2026-05-27: testrange/hypervisor.py (generic Hypervisor) + exported from testrange/__init__; tests/unit/test_hypervisor.py (8 cases). Gates green (603 pass).

- [x] **CORE-21** · `chore` — move non-network plan validation out of networks/; rename wait_builder_ready
  _(done: 2026-05-27)_

  > Two review items. DONE 2026-05-27 — gates green (ruff, ruff-format, mypy --strict testrange tests [132 files], 595 pytest passed).
  >
  > 1. Non-network validation moved out of networks/:
  > - NEW testrange/vms/validate.py — validate_vm_plan(vms, pools): VM-name uniqueness/safety, __-reserved for VM names, -data<N> marker (with the PVE-30 comment), OSDrive->pool ref. Owns _DATA_DISK_MARKER.
  > - networks/validate.py keeps validate_addressing, validate_name, network-name uniqueness/safety, __-reserved for net/switch names, NIC->network ref, and the composer validate_hypervisor_plan which now delegates VM/pool checks to validate_vm_plan.
  > - Cycle (vms.validate imports validate_name from networks.validate) broken via a function-local import of validate_vm_plan inside the composer — networks.validate has no top-level vms.validate import.
  > - Behavior-preserving: test_plan.py exercises every structural check through MockHypervisor construction; all green. Driver import path unchanged (testrange.networks.validate.validate_hypervisor_plan) so mock/proxmox/libvirt drivers + dev docs need no edit.
  >
  > 2. wait_builder_ready -> await_guest_readiness:
  > - run_phase.py (def + expanded docstring noting run-phase-not-build, internal comment, __all__ re-sorted), runtime.py (import + call site), docs/adr/0010 line 185 (updated, keeps 'formerly wait_builder_ready' for traceability).

- [x] **CORE-20** · `chore` — review-driven cleanup (builders/cache/comm/devices/state/networks)
  _(done: 2026-05-27)_

  > Code-review cleanup. DONE 2026-05-27 — gates green (ruff, ruff-format, mypy --strict testrange tests, 595 pytest passed).
  >
  > DONE as requested:
  > - cloudinit: removed bug-history hint from module docstring; collapsed insecure_apt/insecure_dnf → single insecure_pkg_manager (APT-ONLY emit per your call; dnf config + constant removed); collapsed sudo→admin (PosixCred.sudo removed, c.admin in builder); _validate_init_params now raises on non-Apt/Pip packages; inlined _joliet_name_for into the add_fp 4-tuple; moved DEBIAN_FRONTEND=noninteractive into the apt-only branch.
  > - cache/http.py: removed all '# ---- … ----' separators (and codebase-wide: cache/manager.py x3, drivers/base.py x2 de-banded, build_phase.py x1, cloudinit netplan box); resolve() now rejects a name-resolved value that isn't a 64-hex sha.
  > - communicators/ssh.py: dropped the in-memory-key docstring note.
  > - devices/network/base.py: dropped 'Renders dhcp4: false' from the abstract NIC.
  > - state/store.py: initialize() writes state.pid BEFORE state.json; replaced update(mutator) callback with inlined read-modify-write in record_intent/confirm/forget/set_phase (dropped Callable import).
  > - networks/base.py + sidecar.py: docstrings now reference _addressing_consts names instead of literal .1/.2/.10-.99; removed the 'this is why uplink is a Switch concern' parenthetical.
  > - tests + examples migrated: sudo→admin (5 examples, test_credentials, test_cloudinit); insecure tests rewritten for insecure_pkg_manager (apt-only) + new package-type-validation test.
  >
  > DIVERGED (with reason):
  > - builders/base.py default wait_ready 'del': KEPT. The del is not ARG-related (ruff has no ARG rule) — it satisfies bugbear B027 (empty non-abstract ABC method). pass/.../docstring-only all re-trip B027. Added a comment explaining why.
  > - networks/sidecar.py _uplink_network_name: NOT inlined. It's used in 3 places (sidecar, orchestrator/provision, orchestrator/build_phase), centralizing the __uplink__<switch> naming; inlining would scatter the literal across 3 files. Kept + documented.
  >
  > Follow-up 2026-05-27: also trimmed the same 'private half held in memory, never written to disk' note from PosixCred.ssh_key docstring (matches the ssh.py removal).

- [x] **CORE-19** · `docs` — ADR addendums for CORE-15/16 accuracy (0008/0009, verify 0014)
  _(done: 2026-05-27)_

  > Appended dated addendums recording CORE-16: ADR-0008 (§3 native-capability declaration+preflight rescinded; transport unchanged; re-add with Hyper-V/WinRM) and ADR-0009 (interim mgmt gate still in effect; native_capability_findings cross-ref obsolete; findings no longer carry severity). Each affected section also got an inline '> Amended — see Addendum' pointer so the body isn't misleading in isolation. ADR-0014 verified accurate (references the supports_managed_build_egress capability + 'preflight-rejected', never the changed helper) — no addendum needed. No ADR showed the old Plan shape. Decision bodies left intact (point-in-time records). Done 2026-05-27.

- [x] **CORE-18** · `chore` — evaluate _progress.py vs a pip dependency
  _(done: 2026-05-27)_

  > Code review 2026-05-27: 'there's gotta be a pip install for this'. DECISION (2026-05-27): keep the stdlib ProgressReporter. tqdm/rich don't do the non-TTY periodic-INFO-log behavior (CI/build-farm visibility) out of the box — wrapping either would keep most of this code AND add a runtime dep for ~120 lines. Left as-is; no new dependency. Done 2026-05-27.

- [x] **CORE-17** · `refactor` — cli.py + _log.py review follow-ups
  _(done: 2026-05-27)_

  > Code review 2026-05-27. cli.py: (a) ExitCode IntEnum replacing magic 0/1/2/3/130; (b) _load_plan_module validates TESTS is a list of 1-arg callables; (c) de-version the _build_manager comment (drop 'matches v0.0.1'); (d) per-subcommand cache dispatch via set_defaults(func=...) instead of the cache_subcommand string switch. _log.py: drop the _CONFIGURED global, inspect root.handlers for idempotency.

- [x] **CORE-16** · `refactor` — preflight slimming — drop warning severity + speculative native_capability_findings + egress kwarg
  _(done: 2026-05-27)_

  > Code review 2026-05-27. (1) Drop the unused 'warning' severity: PreflightFinding -> (code,message,fix_hint), bool(report)=no findings, .errors/.warnings consumers -> .findings. (2) Remove native_capability_findings + MockDriver._native_caps affordance (speculative; no current backend triggers it; re-add with Hyper-V). (3) managed_build_egress_findings: fold into a concrete driver-ABC method reading self.supports_managed_build_egress; drop the supported= kwarg.

- [x] **CORE-15** · `feat` — Plan(name, *hypervisors) — required leading positional name
  _(done: 2026-05-27)_

  > Move 'name' from a required kwarg to the first positional arg: Plan(name, *hypervisors). Update ~15 call sites (examples/*.py, tests). Code review 2026-05-27.

- [x] **CORE-4** · `chore` — disk capture temp file defaults to tmpfs /tmp; large captures ENOSPC
  _(done: 2026-05-24)_

  > Disk capture downloaded the built OS disk to a tempfile.NamedTemporaryFile with no dir=, landing in the system tempdir (often a small tmpfs /tmp) and ENOSPCing on a multi-GiB capture. Fix: LocalCache.staging (sibling of isos/ under the cache root) + CacheManager.staging; _capture_disk passes dir=ctx.cache.staging so the download stays on the cache filesystem (and the subsequent ingest is a cheap intra-fs copy). Done 2026-05-24.

- [x] **CORE-5** · `feat` — read_build_result_sink driver capability (live stream) + mock
  _(done: 2026-05-24)_

  > A new optional accessor on HypervisorDriver — read_build_result_sink(backend_name) returning a live BuildResultSink (context-manager + byte-iterator; b"" heartbeat contract). Default raises DriverError. MockDriver is the reference sink (canned ok; build_result_stream / build_result_wedge knobs). Done 2026-05-24 (ADR-0012).

- [x] **CORE-1** · `chore` — drop dead `Plan` dataclass field defaults

  > **Done 2026-05-22.** Removed the inert `hypervisors`/`name` field defaults in `testrange/plan.py` (the hand-written `__init__` owns construction); kept the field declarations (they drive the frozen dataclass's `__eq__`/`__repr__`) with a comment saying why. `Plan()` no longer reads as constructible. ruff + mypy clean, suite green.

- [x] **CORE-13** · `chore` — untrack accidental .bak gitlink + local-exclude it

  > '.bak/' was tracked as a 160000 gitlink (embedded git repo), nagging as a 'modified submodule' in every status. Fix: git rm --cached .bak (keeps the dir on disk) + appended '.bak/' to .git/info/exclude (local-only, not committed/shared). Done 2026-05-27.

- [x] **CORE-14** · `chore` — history rewrite — eradicate .bak from all refs + squash all wip(claude) commits

  > Two full-history rewrites (all refs). (1) git filter-repo --path .bak --invert-paths: removed the accidental .bak 160000 gitlink from all 127 commits incl. published origin/main ancestor + v0.0.1/v0.1.0 trees. (2) Squashed every wip(claude) checkpoint into named commits on the linear stack main(0 wip,39)/proxmox(0,53)/net9(0,57): 5 genesis wips folded into Phase 0; 23-wip block B -> 5 themed commits (sidecar/drivers/orchestrator/core/docs); net9 trailing wips -> feat(driver/libvirt) BACKEND-1.1 + chore(.tours). All branch tip trees byte-identical to pre-squash. Release tags re-pointed. NOT pushed — user force-pushes (origin/main is published). Backups: bundle /tmp/testrange-bak-rewrite/, tag claude-pre-bak-rewrite-*, claude-presquash-* still hold old history. Done 2026-05-27.

- [x] **CORE-62** · `feat` — testrange run --build-timeout flag

  > ESXi-on-KVM installer-origin builds (install+reboot+%firstboot) exceed the 600s default build-VM serial-result timeout. Added --build-timeout SECONDS to run, threaded run_tests(build_timeout_s=) -> Orchestrator. DONE 2026-06-02.

### PVE

- [x] **PVE-CERT** · `EPIC` — Proxmox capabilities certification (driver-only)
  _(blocked by: PVE-32, PVE-34, ORCH-16; done: 2026-06-01)_

  > **Goal:** \`examples/capabilities.py\` runs green end-to-end on live Proxmox via the existing connect.toml profile — the Proxmox analog of the libvirt BACKEND-1.D certification. Plus \`examples/capabilities-px.py\`, an **additive** PVE-specific example (does NOT replace capabilities.py; capabilities.py stays THE portable check).
  >
  > **Hard scope (per user):** change **only the Proxmox driver**; may introduce **proxmox-specific devices / StoragePools** if necessary. **Defer** anything that would require MAJOR deviation in the orchestrator/base/ABC — file each such blocker as its own task under this epic rather than reaching into base.
  >
  > **Decisions captured:**
  > - ADR-0009 mgmt semantics ratified as **(B)**: \`Switch(mgmt=True)\` = the hypervisor host has an L2 presence at \`.2\`; guests reach the hypervisor and vice-versa. \`.2\` is a **hypervisor-local** reachability guarantee, NOT promised reachable from a remote test runner.
  > - QGA chunked write, block-storage StoragePools, capabilities-px.py all IN scope (driver/proxmox-specific).
  >
  > **Out of scope:** PVE-31 multi-node clusters (single-node node-pinned only). Adding a mgmt(.2)-reachability assertion to the *portable* capabilities.py TESTS is a deferred nicety (touches shared example + must keep libvirt green).
  >
  > **Children:** PVE-43 (ADR-0009 B) → PVE-44 (mgmt realize+drop gate) → PVE-47 (net live); PVE-45 (QGA chunked) → PVE-49 (guest/snap live); PVE-33 (block StoragePool) → PVE-46 (capabilities-px.py); PVE-48 (storage live); terminal PVE-32 (full green + integration wiring); PVE-34 (docs).
  >
  > **DONE 2026-06-01:** Proxmox certified — capabilities.py full-green on live single-node PVE, wired into pytest -m proxmox (PVE-32), docs page added (PVE-34). Remaining children are independent follow-ups, out of cert scope: PVE-33 (block StoragePools), PVE-45 (QGA chunked write, deferred), PVE-31 (multi-node), BUILD-13 (nested installer build, env-blocked).

- [x] **PVE-34** · `docs` — Proxmox driver setup page + cert status + mgmt(B)
  _(blocks: PVE-CERT; blocked by: PVE-32; done: 2026-06-01)_

  > Add \`docs/user/drivers/proxmox.md\`: connect.toml profile shape, the import-content/dir-storage prerequisites, named-uplink mapping, mgmt(B) semantics (host .2, hypervisor-local), and capabilities certification status. Refresh PLAN.md §Proxmox. Depends on PVE-32.
  >
  > **DONE 2026-06-01:** docs/user/drivers/proxmox.md created (install extra, connect profile shape, storage prereqs incl. import-content + dir/nfs-only, named uplinks, mgmt(B), cert-status table + reproduce-cert snippet). Linked from drivers/index.md toctree + status bullet flipped to certified. PLAN.md Proxmox section refreshed.

- [x] **PVE-32** · `test` — capabilities.py certification — full green on live PVE + integration wiring
  _(blocks: PVE-34, PVE-CERT; blocked by: PVE-44, PVE-45, PVE-46, PVE-47, PVE-48, PVE-49, ORCH-16; done: 2026-06-01)_

  > Terminal certification task for PVE-CERT. Drive capabilities.py to **full green** on the live host (connect.toml) and wire it into \`tests/integration/test_proxmox.py\` behind the \`proxmox\` marker, mirroring the libvirt BACKEND-1.D capabilities certification. Update PLAN.md Proxmox status to "certified". Depends on the rest of the epic.
  >
  > **DONE 2026-06-01:** user ran capabilities.py full-green on live single-node PVE. Wired as tests/integration/test_proxmox.py::test_capabilities_example_certifies (marked `proxmox`, gated on TESTRANGE_PVE_PROFILE/_NAME; loads the example PLAN+TESTS and asserts a clean run_tests sweep). PLAN.md and docs/user/drivers flipped to certified. Gates green (ruff/format/mypy/959 unit).

- [x] **PVE-56** · `bugfix` — concurrent qmcreate contends on pve-storage-local flock (--jobs>1 times out)
  _(done: 2026-06-01)_

  > Found 2026-06-01 running examples/capabilities.py against px-cloud with the default job pool (--jobs 8). Run-phase VM creation fires N concurrent qmcreate calls, each import-from against the single 'local' storage; PVE serializes storage ops behind /var/lock/pve-manager/pve-storage-local with a bounded timeout, so one qmcreate failed: 'unable to create VM 107 - cannot import from local:import/... - cant lock file /var/lock/pve-manager/pve-storage-local - got timeout'.
  >
  > CONFIRMED purely concurrency: a --jobs 1 rerun passed 30/30 with clean teardown (29 ok, 0 failed). So --jobs 1 is a working interim mitigation on PVE; libvirt is unaffected (30/30 at default jobs).
  >
  > NOT a regression from the CORE-47 review batch (build was a full cache hit; run-phase create path unchanged). Fix: make the proxmox storage-import safe under concurrency — serialize the import critical section (per-storage lock) and/or retry qmcreate on a lock-timeout, keeping non-storage parallelism. Secondary: cleanup/destroy_vm of a never-created VM raises instead of being idempotent (surfaced as a stale state record after the failed run; host itself verified clean, 0 orphaned volumes). Belongs under PVE-CERT (concurrency blocker for capabilities cert on PVE).
  >
  > DONE 2026-06-01: Per-storage import lock on ProxmoxDriver (_storage_import_lock) serializes create_vm's storage-alloc critical section so concurrent --jobs>1 qmcreate import-froms no longer race PVE's per-storage flock (we trade the bounded-timeout failure for an orderly wait; no real parallelism lost since PVE serializes those imports anyway). destroy_vm now tolerates a missing stamped name (no-op) like destroy_network/destroy_pool/delete_volume, so teardown over a create-that-failed-mid-flight no longer raises. resolve_vmid stays strict for lifecycle ops. Tests: storage-serialization regression guard (test_concurrency_guards) + destroy-missing-is-noop (test_proxmox_lifecycle). ADR-0023 §1 amended. Gates green (ruff/format/mypy --strict/953 unit); libvirt hello_world smoke 3/3 PASS (unaffected). Did NOT add the optional qmcreate retry-on-lock-timeout: the lock removes the TestRange-internal contention that was the observed bug; retry would be speculative hardening for unobserved external contention.

- [x] **PVE-55** · `bugfix` — download_from_pool CDROM-slot collision guard
  _(done: 2026-06-01)_

  > DONE 2026-06-01. download_from_pool bus scan skips media=cdrom entries so a seed/boot CDROM at a colliding slot isn't downloaded. Test: test_proxmox_storage.py::TestDownload.

- [x] **PVE-54** · `bugfix` — size REST connection pool to the I/O-phase worker ceiling
  _(done: 2026-06-01)_

  > ADR-0020 review: proxmoxer session urllib3 pool_maxsize=10 throttles --jobs>10. FIX: connect() mounts HTTPAdapter(pool_maxsize=32) on _api._store['session'] (verified path, proxmoxer 2.3.0), defensive try/except. Done 2026-06-01.

- [x] **PVE-53** · `bugfix` — serialize SDN create/destroy_switch (cluster-wide apply race)
  _(done: 2026-06-01)_

  > ADR-0020 review: SDN create/destroy_switch fanned via parallel_map; _ensure_zone check-then-create on shared zone + cluster-wide PUT /cluster/sdn raced. FIX: per-driver _state_lock serializes the SDN critical section (zone-ensure→vnet post→apply). Test: test_concurrency_guards.TestProxmoxSdnSerialization (apply never overlaps, zone posted once). Done 2026-06-01.

- [x] **PVE-52** · `bugfix` — blank data disks allocated raw, break qcow2 re-import on cold build
  _(done: 2026-05-31)_

  > PVE allocated bare <storage>:N blank data disks as RAW, breaking qcow2 re-import at run (capture -> .qcow2-named raw -> import-from fails). Fix: format=qcow2 on blank data-disk allocation in _vm.create_vm. **Done + live-verified 2026-05-31** — capabilities-px cold build now green (2/2). Pre-existing latent bug surfaced by the first PVE cold-build of blank data disks.

- [x] **PVE-46** · `feat` — examples/capabilities-px.py — additive PVE-specific example
  _(blocks: PVE-32; blocked by: PVE-33; done: 2026-05-31)_

  > examples/capabilities-px.py — standalone, ProxmoxHypervisor-pinned showcase (NOT a superset of capabilities.py). Showcases ProxmoxHardDrive (selectable data-disk controller bus): a multibus VM with a scsi + virtio data disk; asserts the guest sees /dev/sd* and /dev/vd*. **Done + live-verified 2026-05-31** — cold-built green on px-cloud (2/2). Exercises bus placement, bus-aware capture, re-import on the chosen bus, guest visibility.

- [x] **PVE-49** · `test` — live capabilities.py — guest/cred/snapshot matrix
  _(blocks: PVE-32; blocked by: PVE-45, ORCH-16; done: 2026-05-31)_

  > Run capabilities.py live and fix driver-fixable guest/cred/snapshot fallout: multi-user SSH (password + key), non-admin sudo denial, explicit resolver, disk + **memory** snapshot lifecycle (vmstate=1, running-VM restore). Defer base-deviation blockers. Depends on PVE-45.

- [x] **PVE-48** · `test` — live capabilities.py — storage/disk matrix
  _(blocks: PVE-32; done: 2026-05-31)_

  > Run capabilities.py live and fix driver-fixable storage fallout: multi data-disk Option-2 re-resolution + content survival build→cache→run (watch PVE-30 name-overlap), OS-drive grow-on-boot (oversized OSDrive). Defer base-deviation blockers as separate tasks.

- [x] **PVE-47** · `test` — live capabilities.py — networking matrix
  _(blocks: PVE-32; blocked by: PVE-44, ORCH-16; done: 2026-05-31)_

  > Run capabilities.py live (connect.toml) and fix **driver-fixable** networking fallout: multi-NIC VMs (multihome static/DHCP/unmanaged), multi-Network-per-one-SDN-vnet (pub-a/pub-b), sidecar DHCP/DNS/NAT over the SDN vnet + uplink bridge, cross-label DNS, air-gap reachability matrix (private-web isolated, public-web NAT egress). File any base/orchestrator-deviation blockers as separate tasks. Depends on PVE-44.

- [x] **PVE-51** · `bugfix` — public_web internet fails — egress uplink (vmbr9) has no DHCP/DNS
  _(blocked by: NET-8; done: 2026-05-31)_

  > public_web DNS/egress. **Done 2026-05-31** — root cause was vmbr9 NAT-only (no DHCP/DNS); fixed by NET-8 (static sidecar uplink addr + explicit upstream resolver). Verified green in run #4.

- [x] **PVE-50** · `bugfix` — data-disk capture test is device-agnostic (mountpoint, not /dev/vd*)
  _(done: 2026-05-31)_

  > data_disk capture test device-agnostic (findmnt by mountpoint); virtio reverted. **Done + live-verified 2026-05-31** (run #4): data_disk_bytes_survived_capture passes on virtio-scsi (/dev/sd*).

- [x] **PVE-44** · `feat` — realize Switch(mgmt=True) on PVE; drop mgmt gate
  _(blocks: PVE-47, PVE-32; blocked by: PVE-43; done: 2026-05-31)_

  > Realize Switch(mgmt=True) on PVE as an SDN subnet (gateway=.2) on the per-Switch vnet in _sdn.py; drop mgmt_unsupported_findings from ProxmoxDriver.preflight. **Done 2026-05-31** — _sdn._ensure_mgmt_subnet + subnet teardown-before-vnet; gate dropped; 4 new unit tests + flipped test_mgmt_is_accepted; gates green (ruff/format/mypy/758 pytest). Live-validated through real _sdn against px-cloud: .2 subnet realized + clean teardown. Guest→.2 reachability assertion rides PVE-47.

- [x] **PVE-43** · `docs` — ratify ADR-0009 as (B) — mgmt = host L2 presence at .2
  _(blocks: PVE-44; done: 2026-05-31)_

  > Move ADR-0009 from Draft → Accepted with option (B): Switch(mgmt=True) = host L2 presence at .2; guest↔hypervisor reachable; .2 is hypervisor-local (not remote-runner-promised); single-node node-pinned. **Done 2026-05-31** — ADR-0009 ratified with a Ratification addendum; libvirt already realizes it, Proxmox realization is PVE-44, gate stays for unrealized backends (mock/ESXi/Hyper-V).

- [x] **DOCS-5** · `docs` — refresh libvirt backend status in user docs (no longer 'deleted/roadmap')
  _(done: 2026-05-31)_

  > install.md and drivers/index.md still describe libvirt as 'deleted and slated for a rebuild' / lump it with ESXi+Hyper-V as 'on the roadmap'. Reality (2026-05-31): BACKEND-1 libvirt rebuild against the multi-backend ABC is in Doing with 1.0/1.A/1.B/1.C/1.D/1.S done — driver implemented (VM lifecycle, L2 via libvirt network API, serial build-result sink, QGA, per-run dir pools + stream I/O), registered in drivers/__init__.py, exercised by the capabilities + integration suite against local qemu:///system. Fix: install.md + drivers/index.md to reflect libvirt = rebuilt against the ABC / wrapping up (BACKEND-1), ESXi+Hyper-V = future. Flagged by user; created+done 2026-05-31.

- [x] **PVE-42** · `bugfix` — proxmox driver mediums
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums. destroy_switch stale-vnet-list serial assumption (_sdn.py:84-98 comment/re-fetch); create_switch silently drops unmapped uplink to None (driver.py:229-233 enforce invariant); _sftp_makedirs swallows all OSError (_client.py:99-103); _MAX_WRITE_CONTENT naming nit (_vm.py).
  >
  > Completed 2026-05-31.

- [x] **PVE-41** · `bugfix` — VMID allocation TOCTOU + serial websocket ticket TTL
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums. _vm.py:171 cluster/nextid->qemu.post unreserved: retry-on-collision + document single-instance assumption (CORE-29). _client.py:409,427: refresh PVE ticket or document max build time (serial is the build verdict).
  >
  > Completed 2026-05-31.

- [x] **PVE-40** · `bugfix` — _resize_os_disk retry catches synchronous failures
  _(done: 2026-05-31)_

  > TO_LOOK_AT H3. _vm.py:231-257 — widen except DriverError to catch+translate Exception so a synchronous proxmoxer raise (not a UPID) engages the transient retry.
  >
  > Completed 2026-05-31.

- [x] **PVE-39** · `bugfix` — translate raw proxmoxer exceptions to DriverError
  _(done: 2026-05-31)_

  > TO_LOOK_AT H1. Mirror _guest.py except Exception->GuestAgentError at unwrapped callsites: _vm.py (list_vms l.55, content_volume_exists l.130, _wait_unlocked l.223, get_vm_power_state l.303), _sdn.py, _storage.py, _client.py:252, driver.py. Orchestrator teardown keys on DriverError.
  >
  > Completed 2026-05-31.

- [x] **PVE-38** · `test` — harden proxmox fakes to model real proxmoxer exception types
  _(done: 2026-05-31)_

  > TO_LOOK_AT H1 corollary. Fakes raise plain Exception/DriverError -> suite green while real backend leaks raw proxmoxer.core.ResourceException/AuthenticationError/requests errors. Make fakes raise real types. Prereq for PVE-39/40.
  >
  > Completed 2026-05-31.

- [x] **PVE-37** · `chore` — Spike — confirm PVE SDN VNet-firewall REST surface for managed build-egress fence
  _(done: 2026-05-26)_

  > FINDINGS 2026-05-26 (live host 40.160.34.83, PVE 9.2.2):
  > 1. GET /version -> 9.2.2 (release 9.2). Matches PLAN's recorded host.
  > 2. SDN VNet-firewall surface EXISTS: /cluster/sdn/vnets/{vnet}/firewall/{options,rules}.
  > 3. firewall/options PUT accepts enable=1 + policy_forward="DROP".
  > 4. firewall/rules POST accepts type=forward (+ action/dest/enable/comment); type=out REJECTED (400). So forward is the correct rule type.
  > 5. snat=1 subnet (type=subnet, gateway) reconfirmed (PVE-36).
  > 6. CRITICAL: PVE PREPENDS each posted rule at pos 0, and IGNORES an explicit pos on this surface (probed both). => rules must be POSTed in REVERSE of intended eval order.
  >
  > BUG FOUND + FIXED: _fence_egress_vnet POSTed in forward order, so after prepend the allow-internet ACCEPT landed at pos 0 and shadowed the RFC1918 drops -> fence was INERT. Fixed to POST reversed; unit test (fake now models prepend) pins eval order . Gates green (578 pass). RESIDUAL CLOSED.
  >
  > Not done here (-> PVE-32): a full live end-to-end of the driver managed-egress path (create_switch -> teardown) as an integration test; the individual REST calls + sequencing are confirmed.
  >
  > ---
  >
  > NET-11 (#99) shipped the managed build-egress fence as PVE SDN VNet firewall (/cluster/sdn/vnets/{vnet}/firewall/{options,rules}) per ADR-0014, but PVE-36 only live-confirmed snat=1. Confirm against live host (40.160.34.83, PVE): (1) GET /version; (2) does the VNet-firewall REST surface exist on this PVE version; (3) what params firewall/options accepts (enable, policy_forward?); (4) does firewall/rules accept type=forward + action/dest; (5) reconfirm snat=1 subnet. Throwaway zone/vnet, tear down. Findings feed _fence_egress_vnet correctness.

- [x] **PVE-36** · `chore` — Spike — PVE SDN simple-zone SNAT for managed build egress
  _(done: 2026-05-26)_

  > VIABLE (2026-05-26) — SDN simple-zone SNAT is the REST-native replacement for the vmbr9 + iptables hack; satisfies the proxmoxer-only transport policy (no iptables/ssh to the host).
  >
  > PVE docs (Subnets): subnet property 'snat' = 'Enable Source NAT which allows VMs from inside a VNet to connect to the outside network by forwarding the packets to the nodes outgoing interface'; works on layer-3 zones (Simple/EVPN). The node joins the subnet at the gateway IP and masquerades guest traffic out its own NIC — exactly the vmbr9 behavior, configured over REST.
  >
  > API/config shape:
  >  - zone: type=simple (+ automatic-dhcp, IPAM=pve) on /cluster/sdn/zones
  >  - vnet on the zone: /cluster/sdn/vnets
  >  - subnet on /cluster/sdn/vnets/{vnet}/subnets with subnet=<cidr>, gateway=<ip>, snat=1, optional dhcp-range + dhcp-dns-server
  >  - apply via reload (PUT /cluster/sdn)
  >  - dnsmasq installed per node only if using PVE-native DHCP (apt install dnsmasq; systemctl disable --now dnsmasq)
  >
  > CAVEATS / for the ADR (NET-10):
  >  1. SNAT is stable; PVE DHCP/IPAM integration is PVE 8.1+ and flagged TECH PREVIEW in the docs.
  >  2. DESIGN FORK: PVE SDN provides SNAT *and* DHCP/DNS natively, overlapping the testrange sidecar. ManagedBuildSwitch(PVE) can either (a) use SDN SNAT for egress only and KEEP the sidecar for DHCP/DNS (cross-backend-consistent; double-NAT but both halves managed), or (b) lean on PVE for SNAT+DHCP+DNS and DROP the sidecar (cleanest, but PVE-only, tech-preview DHCP, and diverges from sidecar-brokers-everything). Lean (a).
  >  3. RESIDUAL: confirm the live PVE version on the target host (read-only GET /version + /cluster/sdn) and that a snat subnet applies cleanly — fold into NET-11 impl.
  >
  > Refs: PVE wiki Setup_Simple_Zone_With_SNAT_and_DHCP; pve-docs chapter-pvesdn (Subnets section).

- [x] **PVE-28** · `bugfix` — _resize_os_disk can double-issue a resize on a wait_task poll-timeout
  _(done: 2026-05-24)_

  > CLOSED 2026-05-24: not a bug (see RE-VERIFIED analysis below); the residual LOW nit (classify on task exitstatus rather than free-form error text) is left unaddressed by decision — no correctness issue. --- RE-VERIFIED 2026-05-24: NOT a bug as filed. The premise (wait_task poll-timeout message contains 'timeout') is false — _client.wait_task raises "PVE task '...' did not finish within 600s" (no 'timeout'/'lock' substring), so _resize_os_disk's transient check is already False for a poll-timeout and it propagates without retry. The only retried case is a genuine task-FAILURE whose exitstatus mentions timeout/lock (the qemu-img file-lock, which means the resize did NOT commit — safe to retry).

- [x] **PVE-35** · `feat` — convert examples to ProxmoxHypervisor (px_hello connection shape)
  _(done: 2026-05-24)_

  > DONE 2026-05-24: all five example plans converted in place MockHypervisor -> ProxmoxHypervisor (px_hello connection block, per-switch uplink=vmbr9). test_cli describe assertion updated. data_disk.py's Mock build/run/data-disk lifecycle coverage moved to an inline plan in test_cli_build_run.py (TestDataDiskLifecycle / _DATA_DISK_PLAN_SRC). FOLLOW-UP DONE: runtime-NAT switches now set Switch.uplink_addr (NET-7, already-existing knob — NET-8 was redundant) so network_modes (uplink-sw .3, both-sw .4) + private_public (pub-sw .3) get a static sidecar eth1 on vmbr9 and egress on the single-public-IP host. Gates green (546 passed).

- [x] **PVE-27** · `bugfix` — create_vm should consume known build-vs-run intent, not probe the backend
  _(done: 2026-05-24)_

  > DONE 2026-05-24: create_vm now decides build-vs-run data disks from the orchestrator's intent (seed_iso_ref presence) instead of probing the backend (content_volume_exists). Build/sidecar creates carry a seed and attach blanks (ADR-0010 §4); run creates have seed_iso_ref=None and import the cached staging disk. Same seed signal the OS-disk grow already keys on, so they never disagree — and a stale staging volume left by a crashed build can no longer be mis-imported. Caveat documented: installer-based OS-disk origins (BUILD-1) may carry no seed and must revisit this. Tests: test_data_disk_intent_follows_seed_not_staging (new) + the two existing data-disk tests re-keyed on seed presence.

- [x] **PVE-30** · `bugfix` — resolve_disk name-overlap — VM named '<x>-data0' collides with <x>'s data0 disk
  _(done: 2026-05-24)_

  > DONE 2026-05-24: plan validation now rejects VM names ending in a '-data<N>' marker (validate.py _DATA_DISK_MARKER, case-insensitive, covers -/_/. separators a backend folds to '-'). Closes the collision class where a VM named like another VM's data disk ('<vm>-data<i>') produces an identical volume ref (silent clobber on upload / mis-resolution on capture). Backend-agnostic (the marker is the orchestrator's, in artifacts.py), so it protects all drivers. Tests: test_vm_name_data_disk_marker_rejected, test_vm_name_non_marker_data_allowed.

- [x] **PVE-29** · `bugfix` — serial build-result sink — keepalive-failure==poweroff ambiguity + empty-frame busy-spin
  _(done: 2026-05-24)_

  > DONE 2026-05-24: serial sink hardened. (a) a keepalive-send failure now raises DriverError ('serial transport ... failed mid-build') instead of returning — the orchestrator no longer misreads a transport blip as 'console closed without ok' (BuildFailedError) and fails a healthy build. (b) a non-timeout empty data frame now yields a b'' heartbeat instead of a bare continue, so the watchdog deadline keeps ticking (no busy-spin). Tests: test_keepalive_failure_raises_not_silent_eof (new), test_empty_frame_yields_heartbeat_not_eof (updated).

- [x] **PVE-26** · `bugfix` — volume delete/probe swallow all exceptions -> silent leak / data loss
  _(done: 2026-05-24)_

  > DONE 2026-05-24: replaced the swallow-all _content_exists with content_volume_exists (lists storage content + tests membership, propagating real API/permission errors instead of reading them as 'absent'). _storage._delete_content no longer swallows — delete_volume establishes absence via the listing check (gone -> no-op) and lets a present-volume delete failure propagate, so teardown won't forget+leak a resource on a real error. Tests: test_delete_volume_tolerates_absence (rewritten), test_delete_volume_propagates_real_error (new).

- [x] **PVE-25** · `bugfix` — upload_to_pool re-uploads unconditionally (breaks ABC idempotency)
  _(done: 2026-05-24)_

  > DONE 2026-05-24: upload_to_pool now checks content_volume_exists(target) first and short-circuits (returns the ref) if already staged — honors the ABC idempotency contract, no multi-GB re-transfer on retry/resume. Test: test_upload_skips_when_already_staged.

- [x] **PVE-9** · `test` — live `testrange run` smoke test on Proxmox
  _(done: 2026-05-24)_

  > End-to-end: a ProxmoxHypervisor plan through build (sidecar NAT) + run (QGA NativeCommunicator, no SSH reachability needed) to green, against the live host. Validates the QGA wire (PVE-4) for real. DONE 2026-05-24: green end-to-end via examples/px_hello.py (debian-13 + nginx, reached over QGA; build egress via host-NAT internal bridge vmbr9 + static build_uplink_addr per NET-7). Exercised: SDN L2, SFTP upload, import-from OS disk, serial build-result sink (PVE-17), QGA exec. NOT yet exercised live: multi-VM / multi-NIC / multi-data-disk / multi-switch / mem-snapshot (see PVE-32).

- [x] **PVE-1** · `feat` — Proxmox `HypervisorDriver` concrete + `ProxmoxHypervisor` Plan entry
  _(done: 2026-05-24)_

  > Keystone: 'testrange/drivers/proxmox/__init__.py', 'ProxmoxHypervisor' Plan-entry dataclass (host/node + connection config + networks/pools/vms/build_uplink), and the 'ProxmoxDriver(HypervisorDriver)' concrete wiring connect/disconnect/preflight + compose_resource_name/mac/volume_ref/volume_suffix (delegating to _client/_naming) and L2 (delegating to _sdn). Register ProxmoxHypervisor->ProxmoxDriver and ProxmoxConn URI round-trip in _registry. Storage/VM/guest/snapshot methods raise DriverError('PVE-x: not implemented') until their tickets land (ABC is all-abstract; driver must be instantiable). VERIFIED HOST (2026-05-22): PVE 9.2.2, single node 'ns1001849', one storage 'local' (type dir, content images,iso,vztmpl,snippets...), SDN present+empty. preflight: backend reachable (connect probe), pool min-capacity floor via GET /nodes/{node}/storage/{storage}/status, native-cap gap, mgmt_unsupported, reject uplink+nat (SDN simple zone has no NAT in v1). Unit-tested with a duck-typed fake ProxmoxClient (api/node/wait_task/sftp_get). || COMPLETED 2026-05-22: driver.py (ProxmoxHypervisor + ProxmoxDriver) + __init__ + registry wiring landed; connect/disconnect/preflight(plan-side)/naming/L2 implemented, storage/VM/guest/snapshot raise DriverError(PVE-x). 28 unit tests via fake client; ruff+mypy --strict+434-test suite green.

- [x] **PVE-24** · `bugfix` — open_serial_websocket uses unresolved _conn.node (empty under auto-detect) -> 501 malformed termproxy path
  _(done: 2026-05-24)_

  > Live (PVE-9): 'POST /nodes/qemu/101/termproxy 501 not implemented' — node segment missing. open_serial_websocket read self._conn.node (the config value, now '' since PVE-20 node auto-detect) instead of self.node (the resolved property the rest of the driver uses). Fix: use self.node. Pre-PVE-20 node was always explicit so it worked. Done 2026-05-24.

- [x] **PVE-23** · `bugfix` — upload volume bytes via SFTP (PVE REST upload endpoint 501s on large import images)
  _(done: 2026-05-24)_

  > Live (PVE-9): upload of a 94 MiB import disk -> '501 Not Implemented: for data too large'. Root cause: proxmoxer DOES stream (>10MiB threshold), so not client buffering — PVE's REST upload endpoint rejects large import content server-side. Fix: volume bytes now go over SFTP both directions (sftp_put for upload_to_pool/write_to_pool, mirroring sftp_get download), writing into the storage content dir where dir/nfs storage discovers by scan -> same volid. Removed REST upload_content + the now-dead _ProgressFile/progress_file streaming wrapper (ProgressReporter kept for sftp progress). Transport policy amended: proxmoxer=control plane, SFTP=volume bytes both ways, websocket=serial. Done 2026-05-24.

- [x] **PVE-22** · `feat` — ProxmoxHypervisor host/user/password kwargs instead of a connection URI
  _(done: 2026-05-24)_

  > Revises PVE-20: the connection URI's @realm + special-char-password escaping was friction (hit live as a 401, PVE-21). Author surface is now plain kwargs: ProxmoxHypervisor(host=..., password=..., ...). user defaults root@pam (bare 'root' normalised), node='' auto-detect, build_uplink='vmbr0', backing_storage='local', ssh reuses API creds. The proxmox:// URI survives only as the internal teardown-state serialization (driver_uri/ProxmoxConn.to_uri/from_uri). Updated tests, examples/px_hello.py, PLAN. Done 2026-05-24.

- [x] **PVE-21** · `bugfix` — default the PVE realm (@pam) when the connection URI omits it
  _(done: 2026-05-24)_

  > PVE-20 regression: 'proxmox://root:pw@host' yields user='root' (no realm) -> proxmoxer 401 'Couldn't authenticate user: root'. PVE needs a realm (root@pam). conn() now appends @pam when the resolved user has no @. Explicit realms (root@pam, user@pve, user@ldap) preserved. Found on first live run (PVE-9). Done 2026-05-24.

- [x] **PVE-20** · `feat` — lighten ProxmoxHypervisor to a connection URI + sane defaults
  _(done: 2026-05-24)_

  > Lightened ProxmoxHypervisor: positional connection URI 'proxmox://user:pass@host[:port]' + sane defaults (node='' auto-detect single node at connect; build_uplink='vmbr0'; backing_storage='local'; verify_ssl=False). user=/password= override URI userinfo (URI-hostile chars); SSH reuses API creds. DROPPED sdn_zone from the surface: driver mints a per-run 'tr<hex>' zone (8-char PVE limit), self-discovered at teardown. Renamed the teardown-URI property connection->driver_uri (orchestrator persists that, not the bare author URI). Scheme pve://->proxmox://. Reconciled _sdn docstring + PLAN. Updated examples/px_hello.py, unit tests (+URI/node-autodetect/per-run-zone coverage). 530 unit tests green. Done 2026-05-24.

- [x] **PVE-15** · `feat` — upload progress visibility + actionable error on slow/degraded host
  _(done: 2026-05-24)_

  > Done: _progress.py (ProgressReporter: TTY bar / throttled INFO) wired into _client.upload_content + sftp_get; _ProgressFile(io.IOBase) so proxmoxer keeps streaming (no OOM). Actionable DriverError naming avg MiB/s on failure. test_progress.py (9 tests). Done 2026-05-24.

- [x] **PVE-13** · `bugfix` — missing requests-toolbelt caps proxmoxer uploads at 2 GiB
  _(done: 2026-05-24)_

  > Fixed: requests-toolbelt>=1 added to the  extra (proxmoxer needs it to stream multipart uploads; without it it buffers and caps at 2 GiB). Importable in env. Done 2026-05-24.

- [x] **PVE-12** · `bugfix` — create_vm leaves the VM config-locked; orchestrator's start_vm races it
  _(done: 2026-05-24)_

  > Fixed: _vm._wait_unlocked polls VM config until 'lock' clears before create_vm returns; _resize_os_disk retries the transient post-import file-lock race. Regression: test_create_waits_for_config_lock_to_clear (+ resize_fails knob). Code-complete + unit-tested; live confirm rides PVE-9. Done 2026-05-24.

- [x] **PVE-11** · `bugfix` — VM NIC wired to composed network name, not the SDN vnet id
  _(done: 2026-05-24)_

  > Fixed: driver keeps composed-net-name->vnet-id map (populated in create_network), translates network_refs in create_vm; uplink bridge passes through. Regression: test_create_vm_translates_nic_bridge_to_vnet_id. Code-complete + unit-tested; live confirm rides PVE-9. Done 2026-05-24.

- [x] **PVE-10** · `bugfix` — proxmoxer 5s default timeout aborts large image uploads
  _(done: 2026-05-24)_

  > Fixed: _client.connect builds ProxmoxAPI with session timeout=600s (was proxmoxer's 5s default, aborting large uploads). Regression: test_connect_uses_generous_http_timeout. Code-complete + unit-tested; live confirm rides PVE-9. Done 2026-05-24.

- [x] **PVE-19** · `chore` — reconcile PLAN.md Proxmox status with the board
  _(done: 2026-05-24)_

  > Reconciled PLAN.md Proxmox section with the board: 'feature-complete and live-validated' -> 'feature-complete in code, validated piecewise, not yet green end-to-end'; added the PVE-9..17 Open-work list; fixed the stale 'PVE-1..8' sequencing line. Done 2026-05-24.

- [x] **PVE-17** · `feat` — Proxmox serial0 reader over termproxy->vncwebsocket (live build-result sink)
  _(done: 2026-05-24)_

  > Proxmox build-result sink: read serial0 live over termproxy->vncwebsocket (websocket-client). _client.open_serial_websocket (termproxy POST -> authenticated ws; password-ticket auth, get_tokens) + _serial.read_build_result_sink (Generator orchestrator sink: raw PTY frames, b'' heartbeat + '2' keepalive on idle, closes on exit), wired into driver.py. 2nd sanctioned transport (ADR-0008 §6 amended, ADR-0012). Unit-tested w/ faked ws; live exercise rides PVE-9. Done 2026-05-24.

- [x] **PVE-18** · `chore` — Spike — unwrap termproxy/vncwebsocket framing to recover clean serial bytes
  _(done: 2026-05-24)_

  > SPIKE COMPLETE (2026-05-24). Framing confirmed via read-only PoC against live node-shell termproxy (websocket-client 1.9.0). termproxy streams RAW PTY BYTES in binary ws frames — NOT a VNC/RFB protocol (RFB is the vncproxy/noVNC graphical path, a different endpoint). Recipe: (1) auth -> set PVEAuthCookie cookie manually (ticket is in the body, not Set-Cookie) + CSRF; (2) POST termproxy -> {port, ticket(=vncticket PVEVNC:..), user}; (3) ws vncwebsocket?port=&vncticket= with Cookie PVEAuthCookie + Origin header, TLS verify off; (4) send '{user}:{vncticket}\n' -> server replies binary b'OK'; (5) concatenate subsequent binary frame payloads = serial stream, scan for TESTRANGE-RESULT. Notes: idle ws times out (send '2' ping during quiet, hold from start_vm through build); ANSI/control bytes interleave (base64-framed log is robust); password-ticket auth required. Consumer protocol proven on node shell; VM serial0 is the same consumer. Full recipe in RESEARCH.md 'PVE-18 addendum'. Created 2026-05-24, done 2026-05-24.

- [x] **PVE-16** · `chore` — Spike — read serial0 console bytes over the proxmoxer/REST transport
  _(done: 2026-05-24)_

  > SPIKE COMPLETE (2026-05-24). FINDING: serial-over-REST is NOT viable on PVE within the transport policy. PVE exposes no REST GET for serial output; the only console path is termproxy(POST)->vncwebsocket(websocket upgrade), which needs a websocket consumer proxmoxer can't provide + a second transport (against the policy's single-SFTP-exception rule) + a fragile VNC-framed, stream-only feed. Verified empirically against live host ns1001849 (console endpoints all POST-only/ws-upgrade; no buffer GET).
  >
  > RESOLUTION: PVE reads the build-result record from a small ephemeral RESULT DISK pulled back over the existing download_from_pool SFTP path (the one sanctioned exception). Snapshot-only; never cached. Format (raw-offset blob vs FAT-by-label) deferred to PVE-17.
  >
  > Architectural correction fed back to PLAN.md §21 + RESEARCH.md 'PVE-16 spike': the universal vector is the TESTRANGE-RESULT *record* (every OS writes it guest-side); the HOST READ differs per backend (libvirt=serial, PVE=disk). Capability renamed read_build_result_sink (snapshot read); BUILD-3 emits to serial AND the disk sink when provisioned. Created 2026-05-24, done 2026-05-24.

- [x] **PVE-7** · `test` — Proxmox integration suite
  _(done: 2026-05-22)_

  > Tests behind the `proxmox` pytest mark, gated on `TESTRANGE_PVE_HOST`. || COMPLETED 2026-05-22: tests/integration/test_proxmox.py (marked proxmox; gated on TESTRANGE_PVE_HOST + base qcow2; self-cleaning). RAN GREEN against live host: connect/preflight, SDN roundtrip, storage upload/delete, full VM lifecycle+snapshot (4 passed, QGA skipped pending an agent guest).

- [x] **PVE-5** · `feat` — Proxmox snapshots incl. memory
  _(done: 2026-05-22)_

  > `create_snapshot` with `vmstate=1`; map to the snapshot ABC + LIFO teardown. || COMPLETED 2026-05-22: _vm snapshot ops via PVE REST (snapshot.post snapname/description/vmstate; vmstate=1 for mem; list excludes synthetic 'current', oldest-first; delete no-op if absent; rollback; create dup + restore-missing raise DriverError). Wired into driver. LIVE-VALIDATED (create/list/rollback/dup/delete). 8 snapshot tests; gates green (474).

- [x] **PVE-4** · `feat` — Proxmox native guest agent transport
  _(done: 2026-05-22)_

  > QGA over the PVE API (async pid + poll, no stdin, size limits → chunk writes). Back `NativeCommunicator`; declare `native_guest_capabilities()`. || COMPLETED 2026-05-22: _guest.py — QGA over PVE REST (agent/exec pid+poll exec-status; file-read->bytes; file-write binary-safe via base64+encode=0, single-write cap raises). native_guest_capabilities full set; driver overrides wired. Unblocks NativeCommunicator + DHCP lease readback. 9 guest tests; gates green. QGA wire confirmed by PVE-7 live suite (needs a booted guest w/ agent).

- [x] **PVE-8** · `feat` — Proxmox VM lifecycle (create_vm / start / shutdown / destroy / power-state)
  _(done: 2026-05-22)_

  > THE GAP: no existing ticket covered create_vm or the power lifecycle. _vm.py: create_vm via POST /nodes/{node}/qemu (allocate vmid via /cluster/nextid; agent=1 for QGA; serial0/vga for cloud-init; scsi0 from the OS disk via 'import-from' the uploaded volume; HardDrives as extra scsiN; ide2 = cloud-init seed CDROM from seed_iso_ref; netN=virtio,bridge=<vnet>,macaddr=<compose_mac> wired from network_refs) + start_vm/shutdown_vm(graceful then stop on timeout)/destroy_vm(stop+purge disks)/get_vm_power_state (qmpstatus -> running|shutoff). Stamp composed name (PVE-6) for teardown recovery. Depends on PVE-1 (driver), PVE-3 (disk refs to import), PVE-6 (name->vmid). Blocks PVE-4 (needs a running VM) and the integration suite.  || COMPLETED 2026-05-22: _vm.create_vm (nextid; import-from OS->scsi0 + resize-to-spec when seed present; blank vs import data disks via _content_exists; seed ide2 CDROM; net<i> stable-MAC on bridge/vnet; agent=1; stamps name) + start/shutdown(graceful+forceStop)/destroy(stop+purge)/get_vm_power_state(stopped->shutoff). Wired into driver. LIVE-VALIDATED end-to-end on the host through proxmoxer. 25 lifecycle/vm tests; gates green (463). All proxmoxer, no qemu-img.

- [x] **PVE-3** · `feat` — Proxmox pool I/O (`upload_to_pool` / `download_from_pool`)
  _(done: 2026-05-22)_

  > _storage.py: create_pool/destroy_pool (a per-run subdir namespace inside the static 'dir' storage, NOT provisioning) + write_to_pool/create_blank_volume/resize_volume/upload_to_pool/download_from_pool/delete_volume. Host 'local' is type=dir. UPLOAD: REST POST /nodes/{node}/storage/{storage}/upload needs 'import' content type, which 'local' does NOT currently have enabled (verified content=images,iso,vztmpl,snippets,backup) -> add a preflight check that fails loud with a fix_hint ('enable import content on the storage'), or document it as host setup. DOWNLOAD: no REST byte-download -> paramiko SFTP off the node (ProxmoxClient.sftp_get). create_blank_volume/resize via REST or qm over the volume's dir path. Constrained to dir/nfs so compose_volume_ref stays filename-deterministic (ADR-0008 §6). || COMPLETED 2026-05-22: _storage.py — pools as filename-prefix namespace; SFTP upload/write/download; delete via REST content (tolerant). Disk model = Option-2: download_from_pool re-resolves the stable ref to the live vm-scoped scsiN disk (_vm.resolve_disk + VM config), heavily documented. create_blank/resize deferred to create_vm. Preflight requires import content (proxmox-import-content-missing). 16 storage tests; gates green (453). || COMPLETED 2026-05-22 (Option A): upload_to_pool/write_to_pool via proxmoxer REST upload endpoint (content=import/iso, stored-name via temp symlink); download_from_pool sole SFTP op (no REST byte-egress). No qemu-img/subprocess. Live-validated upload+import-from round-trip on host. 47 proxmox tests; gates green (453).

- [x] **PVE-6** · `feat` — Proxmox name → (node, vmid) resolution
  _(done: 2026-05-22)_

  > Resolution helper feeding VM lifecycle (PVE-8) + snapshots (PVE-5): vmid is allocated by PVE at create time, so stamp the composed backend name into the VM 'name' (and/or notes/tags) at create, and resolve composed-name -> (node, vmid) by scanning /cluster/resources or /nodes/{node}/qemu on every lifecycle/teardown call. No external map -> a from_uri-rebuilt teardown driver recovers the handle (ADR-0008 §6). compose_resource_name already sanitises to a PVE DNS label in _naming.py. || COMPLETED 2026-05-22: _vm.resolve_vmid/list_vms (stamped-name->vmid, ADR-0008 §6, no external map) + _vm.resolve_disk (Option-2 disk re-resolution, longest-prefix owner match) + pure _naming.parse_disk_ref/disk_scsi_index. 11 unit tests; gates green (448).

- [x] **PVE-2** · `feat` — Proxmox L2 via SDN (`create_switch` / `create_network`)
  _(done: 2026-05-22)_

  > STATUS: code already written in 'drivers/proxmox/_sdn.py' (create_switch/destroy_switch/create_network) but not wired into a driver or unit-tested. This ticket = wire _sdn into ProxmoxDriver + unit tests against a fake API. Per-Switch SDN vnet (8-char alnum id) in the configured 'simple' zone (ProxmoxConn.sdn_zone, default 'trzone'); networks share the switch's vnet; stage then 'PUT /cluster/sdn' to apply. destroy_switch is self-discovering (reads vnet zone, drops zone when its last vnet goes) so from_uri teardown needs no run_id. v1 ISOLATED ONLY: uplink+nat rejected in preflight (simple zone = no NAT/uplink segment), create_switch always returns None. Host SDN verified present+empty. OPEN: reconcile per-run-zone wording in _sdn docstring vs fixed configured zone in _client (settling on fixed zone for single-run v0). || COMPLETED 2026-05-22: _sdn wired into ProxmoxDriver; isolated SDN vnet + uplink path landed — uplink+nat returns the existing host bridge (switch.uplink, e.g. vmbr0) for the sidecar eth1, so testrange run auto-builds. Preflight verifies the bridge exists (proxmox-uplink-bridge-missing). 31 unit tests; gates green. Live-SDN verify rides PVE-7.

### ESXI

- [x] **ESXI-17** · `bugfix` — ESXi build-result signaling over serial _(done: 2026-06-06)_

  > RESOLVED. Root cause was NOT "%firstboot doesn't run" (it does). Two real bugs:
  > (1) ESXi has no userspace serial write — the UART char device is held by the
  > vmkernel and isn't a tty, so `echo > /dev/ttyS0` is swallowed; and the OLD
  > `esxcli system shutdown poweroff` in %firstboot HANGS (needs hostd, not up that
  > early) — that was the BuildTimeout. (2) the run-phase SSH probe used an Ed25519
  > key, which ESXi 8 FIPS sshd silently rejects (split out to CORE-63).
  > FIX (folded into _esxi_prepare.render_kickstart): emit the build-result from
  > `%post` (installer env) via `vsish -e set /system/log "${_t}-${_r}: ok"` — the
  > installer boots with `logPort=com1` (added to _patch_bootcfg) so its vmkernel
  > log streams out COM1 → the build VM's serial sink — then `poweroff -f`. Marker
  > assembled from shell vars so the literal never appears in the ks.cfg source
  > (weasel echoes section bodies to the same serial at parse time, which would
  > false-trigger the parser). %firstboot now carries only run-phase provisioning.
  > VALIDATED: standalone one-off (`/home/user/esxi-sanity/prep4.py`) AND end-to-end
  > via `testrange build --profile libvirt-local` — orchestrator read `ok`, captured
  > the disk (663 MiB, key f324abca2b6875d9), re-build = clean cache hit, ~5.5 min.
  > Also wired `--build-timeout` onto the `build` verb (was run-only; build_range
  > hardcoded 600s) + hardened prepare_iso against an existing-output overwrite.
  > ADR-0012 + PLAN §build-signal amended with the ESXi write-side dialect.

- [x] **ESXI-S3** · `chore` — Spike — datastore-file serial port read + framing for the build-result sink
  _(blocks: ESXI-8; blocked by: ESXI-S1; done: 2026-06-02)_

  > S3 spike — datastore-file serial read. DECIDED + IMPLEMENTED 2026-06-01: VirtualSerialPort FileBackingInfo  <vm>/serial0.log; tail via /folder Range GET (folder_read_from), b'' heartbeats, EOF on poweroff. Folded into drivers/esxi/_serial.py + _vm serial device. LIVE PROOF PENDING host root creds.

- [x] **ESXI-10** · `test` — ESXi integration suite (pytest -m esxi) + pyVmomi fakes
  _(blocks: ESXI-13; blocked by: ESXI-1; done: 2026-06-02)_

  > CODE-COMPLETE + GATE-GREEN 2026-06-01 (ruff/mypy --strict/pytest unit). 275:ESXI-10:fakes+unit. LIVE VALIDATION PENDING host root creds (password changed after relicense; awaiting new password).

- [x] **ESXI-9** · `feat` — preflight — firmware/datastore/uplink/CIDR checks
  _(blocks: ESXI-13; blocked by: ESXI-1; done: 2026-06-02)_

  > CODE-COMPLETE + GATE-GREEN 2026-06-01 (ruff/mypy --strict/pytest unit). 273:ESXI-9:preflight. LIVE VALIDATION PENDING host root creds (password changed after relicense; awaiting new password).

- [x] **ESXI-8** · `feat` — serial build-result sink (read_build_result_sink)
  _(blocks: ESXI-11; blocked by: ESXI-S3, ESXI-1; done: 2026-06-02)_

  > ESXI-8 serial build-result sink. LIVE-PROVEN 2026-06-02: datastore-file serial port read via /folder Range; the end-to-end hello_world run read the build VM console live and detected the build outcome. Gate-green.

- [x] **ESXI-5** · `feat` — native guest agent over VMware Tools guest-ops
  _(blocks: ESXI-13; blocked by: CORE-60, ESXI-1; done: 2026-06-02)_

  > ESXI-5 VMware Tools guest-ops. LIVE-CERTIFIED 2026-06-02: exec + file read/write work once the guest has open-vm-tools-plugins-all (vix plugin; base pkg omits it -> GuestComponentsOutOfDate). Gate-green.

- [x] **ESXI-6** · `feat` — snapshots — create/list/delete/restore incl. memory
  _(blocks: ESXI-13; blocked by: ESXI-1; done: 2026-06-01)_

  > ESXI-6 snapshots. LIVE-CERTIFIED 2026-06-01: create/list/delete/restore (disk-only leaves shutoff). Gate-green.

- [x] **ESXI-4** · `feat` — VM lifecycle — create_vm + start/shutdown/destroy + power state
  _(blocks: ESXI-11; blocked by: ESXI-1; done: 2026-06-01)_

  > ESXI-4 VM lifecycle + devices. LIVE-CERTIFIED 2026-06-01: create_vm/start/shutdown/power. Live bug fixed: VirtualDeviceConfigSpec->VirtualDeviceSpec. Gate-green.

- [x] **ESXI-3** · `feat` — datastore pool I/O — /folder upload/download + blank/resize/write/delete volume
  _(blocks: ESXI-11; blocked by: CORE-2, ESXI-S2, ESXI-1; done: 2026-06-01)_

  > ESXI-3 datastore storage + S2 ingest. LIVE-CERTIFIED 2026-06-01: qcow2->vmdk->VMFS inflate->resize->export->qcow2 content-verified round-trip. Gate-green.

- [x] **ESXI-2** · `feat` — L2 — create/destroy_switch (vSwitch) + create/destroy_network (portgroup)
  _(blocks: ESXI-11; blocked by: ESXI-1; done: 2026-06-01)_

  > ESXI-2 L2 (vSwitch/portgroup/mgmt vmk/shared uplink). LIVE-CERTIFIED 2026-06-01 on 40.160.34.83 (create/verify/destroy, clean teardown). Gate-green.

- [x] **ESXI-S2** · `chore` — Spike — land a bootable vmdk on a datastore purely via API
  _(blocks: ESXI-3; blocked by: ESXI-S1; done: 2026-06-01)_

  > S2 spike — vmdk ingest path. DECIDED + IMPLEMENTED 2026-06-01 (notes/esxi/S1-recon.md): qcow2->monolithicSparse vmdk (qemu-img, CORE-2) -> /folder PUT staging -> VirtualDiskManager.CopyVirtualDisk_Task inflate to VMFS thin (bootable+growable; ExtendVirtualDisk for resize). Egress: GET descriptor+ -flat -> qemu-img vmdk->qcow2. Folded into drivers/esxi/_storage.py. LIVE PROOF PENDING host root creds (password changed after relicense).

- [x] **ESXI-7** · `feat` — name -> MoRef resolution + compose_resource_name / compose_mac
  _(blocked by: ESXI-1; done: 2026-06-01)_

  > drivers/esxi/_naming.py: compose_resource_name/compose_mac(VMware manual MAC range 00:50:56:00:00:00-3f:ff:ff)/compose_volume_ref( pool/name.vmdk)/volume_suffix + vswitch/portgroup name helpers; name->MoRef via _client.find_vm/require_vm. DONE 2026-06-01: unit-tested (tests/unit/test_esxi_naming.py).

- [x] **ESXI-1** · `feat` — ESXiDriver concrete + ESXiHypervisor entry + ESXiProfile + registry wiring
  _(blocks: ESXI-2, ESXI-3, ESXI-4, ESXI-5, ESXI-6, ESXI-7, ESXI-8, ESXI-9, ESXI-10; blocked by: ESXI-S1; done: 2026-06-01)_

  > Skeleton drivers/esxi/ mirroring proxmox. DONE 2026-06-01 (live-validated connect on 40.160.34.83): _client.py (EsxiConn/EsxiClient: SmartConnect verify off, standalone-host inventory resolve host/compute/rp/datacenter/datastore, wait_for_task, /folder HTTPS byte I/O, lazy _import_pyvmomi/_import_requests with  hint); driver.py (ESXiDriver + ESXiHypervisor marker, _translates vmodl/requests faults, naming wired, ESXI-2..9 stubbed); _naming.py (ESXI-7 pure: compose_resource_name/compose_mac VMware-manual-range/compose_volume_ref bracket form/volume_suffix); _profile.py (ESXiProfile scheme=esxi); registry + drivers/__init__ wiring; pyproject  extra + mypy override + esxi-local connect.toml profile. BLOCKS ESXI-2..9.

- [x] **ESXI-S1** · `chore` — Spike — pyVmomi recon of the live ESXi host
  _(blocks: ESXI-S2, ESXI-S3, ESXI-1, ESXI-ADR; done: 2026-06-01)_

  > Connect pyVmomi (SmartConnect, verify_ssl off) to the standalone ESXi host at 40.160.34.83 and record the SDK surface that the driver builds on: ESXi version/build, confirm STANDALONE (no vCenter / no DVS), datastores (name/capacity/free/type), physical vmnics + existing standard vSwitches/portgroups, default resource pool / host MoRef, and the GuestOperationsManager + serial-port device options. Capture findings to notes/ for the driver tickets.
  >
  > First ticket of the epic — de-risks and informs ESXI-1, ESXI-2, ESXI-9 and the ADR. Created 2026-06-01.

### ORCH

- [x] **ORCH-2** · `feat` — nested orchestration _(done: 2026-06-08)_

  > `AbstractHypervisor` shape designed fresh (not copied from `.bak`).
  >
  > Goal (clarified 2026-05-31): a VM in the Plan can itself be a Hypervisor that hosts VMs — nested orchestration.
  >
  > DONE: shipped via ADR-0021 (recursive orchestration over qemu+ssh; GuestHypervisor + inner Hypervisor) and merged to main (e4baae0). The feature/nested-virt branch + worktree were retired in the 2026-06-08 cleanup. Residuals carried as their own tickets: BACKEND-11 (remote-libvirt guest_gateway) and nested-ESXi (ESXI-16/18, shelved post-1.0.0).

- [x] **ORCH-32** · `feat` — nested inner-backend dispatch + ESXi inner (GuestHypervisor.esxi)
  _(done: 2026-06-02)_

  > Lifted libvirt-only inner guard (vms/nested.py __post_init__ allows LibvirtHypervisor|ESXiHypervisor); added GuestHypervisor.esxi() front door (ESXiKickstartBuilder+license, SSHCommunicator root, ESXiHypervisor inner). nested_phase per-inner-backend dispatch via _synthesize_inner_binding (libvirt: qemu+ssh+keyfile; esxi: pyVmomi password, no keyfile) + _esxi_root_password; NestedRun.keyfile now Path|None. New drivers/esxi/_nested.py (inner_esxi_profile + wait_esxi_ready). build_nested_inner_vms (BUILD-14) confirmed backend-agnostic (inner Debian VMs build on L0 w/ egress; nested ESXi just boots cached qcow2->vmdk). Unit tests test_nested_phase.py TestEsxiInner. DONE 2026-06-02 (code+unit; live recursion rides ESXI-16).

- [x] **ORCH-1** · `feat` — multiple top-level Hypervisors in a Plan
  _(done: 2026-06-01)_

  > `Plan(*hypervisors)` is already variadic; lift the v0 "exactly one" runtime check and broker across backends. -- DELIVERED 2026-06-01 across ORCH-26..31 (phases 0-5): Hypervisor.name+scoped namespacing, per-hyp binding + repeatable --profile, N-driver lifecycle + state schema v2, cap lifted + end-to-end mock cert, describe per-hyp, capabilities-multihyp example + ADR-0025.

- [x] **ORCH-31** · `test` — multi-hyp Phase 5 — portable two-hypervisor demo in capabilities.py + ADRs/PLAN
  _(blocked by: ORCH-30; done: 2026-06-01)_

  > Phase 5 of ORCH-1 (#21). DONE 2026-06-01: examples/capabilities-multihyp.py (two portable islands site-a/site-b reusing same names+CIDR, bound via default --profile or keyed; 6 isolation/namespacing TESTS) + unit smoke (describe renders both, Tests:6). Docs: ADR-0025 (new) + index; PLAN.md :56 + :424 mark ORCH-1 delivered; ADR-0003 v2 note. ruff+mypy clean, 988 unit pass.

- [x] **ORCH-30** · `feat` — multi-hyp Phase 4 — describe over N hypervisors + handle key namespacing
  _(blocks: ORCH-31; blocked by: ORCH-29; done: 2026-06-01)_

  > Phase 4 of ORCH-1 (#21). DONE 2026-06-01: cli describe loops plan.hypervisors — per-hyp section header (multi) / 'Plan (...)' (single), per-hyp binding via resolve_backend_for, aggregate binding_ok (H13 preserved); _print_hypervisor_topology extracted. (OrchestratorHandle namespacing landed in Phase 3.) NEW TestMultiHypervisorDescribe (3 sections/independent binding/broken-binding-exit-2). ruff+mypy clean, 987 unit pass.

- [x] **ORCH-29** · `feat` — multi-hyp Phase 3 — lift the cap; cross-hypervisor concurrency
  _(blocks: ORCH-30; blocked by: ORCH-28; done: 2026-06-01)_

  > Phase 3 of ORCH-1 (#21). DONE 2026-06-01: plan.py removed >1 guard + validates unique hyp names + .hypervisor raises on multi; OrchestratorHandle.drivers map + driver_for + single-entry driver property; _build_handle iterates plan.hypervisors w/ scoped vms keys. test_plan multi-supported/dup-rejected; NEW end-to-end TestMultiHypervisor (2 hyps/2 MockDrivers, scoped namespacing, per-hyp state bindings+tags certified). ruff+mypy clean, 984 unit pass.

- [x] **ORCH-28** · `feat` — multi-hyp Phase 2 — N-hyp iteration, N-driver lifecycle, state schema v2, addressing islands
  _(blocks: ORCH-29; blocked by: ORCH-27; done: 2026-06-01)_

  > Phase 2 of ORCH-1 (#21). DONE 2026-06-01: RunContext per-hyp maps (resolved_by_hyp/addressing_by_hyp + driver_for/resolved_for/addressing_for/scoped, single-entry back-compat props); state schema v2 (DriverBinding table + Resource.hypervisor, SCHEMA_VERSION=2, v1 rejected); cleanup+teardown dispatch per r.hypervisor; N-driver connect/disconnect lifecycle + per-hyp preflight via synthesized Plan; ALL phases (run/build/provision/nested) iterate plan.hypervisors w/ driver_for + ctx.scoped() namespacing (no-op at count==1) + record_intent hypervisor= tagging; nested inner Hypervisor named. ruff+mypy clean, 980 unit pass (+ schema-v2 tests).

- [x] **ORCH-27** · `feat` — multi-hyp Phase 1 — per-hyp binding model + repeatable --profile
  _(blocks: ORCH-28; blocked by: ORCH-26; done: 2026-06-01)_

  > Phase 1 of ORCH-1 (#21). resolve_backend_for(hyp,profile) in backend.py (resolve_backend now a shim); cli.py --profile action=append + _parse_profile_arg/_load_profiles/_profile_usage_error (bare NAME default + hyp=NAME keyed, validates keys vs plan hyp names); Orchestrator/runner accept profiles= (default via profile=). Count stays 1. COMPLETED 2026-06-01: 14 new tests (parse table, load matrix, routing, resolve_backend_for parity); ruff+mypy clean, 978 unit pass.

- [x] **ORCH-26** · `chore` — multi-hyp Phase 0 — Hypervisor.name + per-hyp namespacing scaffolding
  _(blocks: ORCH-27; done: 2026-06-01)_

  > Phase 0 of ORCH-1 (umbrella #21). Add Hypervisor.name (validate_name from networks/validate.py) as a defaulted first field (name='default') so existing single-hyp constructions stay untouched; validated in __post_init__. Identity foundation consumed from Phase 1 (binding) + Phase 4 (describe). Behavior-preserving at count==1. NOTE: qualify()/namespace-segment threading MOVED to Phase 2 (ORCH-28). COMPLETED 2026-06-01: field+validation+4 tests; ruff+mypy clean, 964 unit pass.

- [x] **ORCH-25** · `bugfix` — parse_build_result loose token match
  _(done: 2026-06-01)_

  > DONE 2026-06-01. parse_build_result matches the exact first token (ok/fail), not startswith. Test: test_build_result.py::TestParseTokenStrictness.

- [x] **ORCH-24** · `test` — concurrency test fidelity (multi-VM teardown, jobs>1, overlap counters)
  _(done: 2026-06-01)_

  > ADR-0020 review: no multi-VM partial-failure teardown test; wall-clock overlap asserts flaky. FIX: added multi-VM fail->teardown no-leak test, max-in-flight overlap counters (run/build/readiness), parallel_map drain test, +PVE-53/BACKEND-13/CORE-43/CACHE-6 tests. 794 unit pass. Done 2026-06-01.

- [x] **ORCH-23** · `docs` — correct parallel_map fail-fast claims + ADR-0020 LIFO wording
  _(done: 2026-06-01)_

  > ADR-0020 review: parallel_map oversold 'cancel pending' + 'deterministic submission order'; ADR overstated strict-LIFO under parallel build. FIX: corrected docstring (drain-before-raise, earliest-completed) + ADR-0020 §Decision/§stays-serial wording + §1 maps-not-thread-safe-by-instance. Done 2026-06-01.

- [x] **ORCH-22** · `bugfix` — nested recursion depth guard + inner-build run_id isolation
  _(done: 2026-06-01)_

  > Code-review findings (ADR-0021). (1) build_phase/run_nested_phase have no depth-2 termination guard: a GuestHypervisor whose inner plan itself contains a GuestHypervisor builds all disks then fails late on L2 reachability. Reject single-level-only loudly (ValueError) at build_phase top + run_nested_phase top via vms.nested.reject_unsupported_nesting. (2) _inner_build_ctx shares the outer run_id, so compose_resource_name(run_id,'build_pool','build') collides across levels; derive a distinct deterministic inner run_id. Add unit coverage for both. Completed 2026-06-01.

- [x] **ORCH-19** · `feat` — overlap run-phase readiness waits (communicator/sidecar/DHCP/builder)
  _(blocked by: ORCH-17; done: 2026-05-31)_

  > Stage 3 of the in-process concurrency epic — folded in last. Convert the serial readiness loops in run_phase.py to parallel_map: communicator readiness per-VM (:195), sidecar readiness per-switch (:159), DHCP lease / IP discovery per-VM (:293 in discover_ip), builder wait_ready per-VM (:230). SSH polls lock-free; native polls take the ORCH-17 per-driver call lock (released during the 2s sleeps, still overlap). Preserve timeout/continue semantics; surface first failure with VM name. Depends on ORCH-17. Tests: mock readiness with injected per-VM delays, wall-clock ~= max not sum, single-VM timeout still raises with right VM.
  > --- COMPLETED 2026-06-01 ---
  > communicator/sidecar/DHCP/builder readiness loops over parallel_map; native polls serialize on the per-driver call_lock (sleeps overlap). Tests in test_readiness_parallel.py.

- [x] **ORCH-4** · `feat` — parallel build pass
  _(done: 2026-05-31)_

  > Parallelize the per-VM build loop (build_phase.py currently builds misses serially).
  >
  > ADR-0017 INTERACTION: each in-flight build VM gets a dedicated build NIC with a static address from the build switch's .3-.9 infra range. Serial build uses one fixed slot; concurrent builds need a DISTINCT build IP per in-flight VM — allocate from .3-.9 (caps concurrency at ~7) or widen the reserved range. Must be handled here, not in ORCH-9.
  >
  > --- 2026-05-30 (handed back, stays in Doing) ---
  > BLOCKED ON A DESIGN DECISION: ORCH-4 contradicts ADR-0002 ("Install brings up one VM at a time"; "No asyncio, no ThreadPoolExecutor"). Before implementing, decide: amend ADR-0002 (or write a superseding ADR) to permit bounded thread-pool concurrency in the BUILD phase only, and make StateStore writes thread-safe (it is not today — atomic os.replace per write, but concurrent record_intent/confirm/forget would race).
  > BUILD-IP allocation (decided 2026-05-30): allocate SEQUENTIALLY, one per in-flight build VM, starting at BUILD_NIC_OFFSET (.3) and KEEP GOING UP — not capped to .3-.9. NB: the build switch sidecar serves DHCP in .10-.99 by the shared addressing convention, so sequential build IPs climbing past .9 must either widen/relocate that DHCP pool on the build switch or the allocator must skip it (no overlap with the sidecar DHCP range).
  > ORCH-9 groundwork already in place: _build_nic_for() (build_phase.py) synthesizes the per-VM build NIC at the fixed .3 slot for serial build; generalize its offset to a sequential per-in-flight allocation. The build IP feeds config_hash via the rendered netplan, so the allocation must stay deterministic per VM (a stable function of the VM), not scheduling-dependent, or the cache key churns.
  >
  > --- 2026-05-31 (epic reframed: in-process concurrency, transfers-first) ---
  > This is STAGE 2 of the in-process concurrency epic. Design blocker RESOLVED: ADR-0002 is amended under ORCH-17 (#190) to permit a bounded thread pool in the orchestration I/O phases only (per-worker driver connections; shared bookkeeping under one mutex). Build the substrate (ORCH-17) first, then this. Parallelize the transfer-heavy points: per-role capture DOWNLOAD (build_phase.py:425, loop :352), base UPLOAD + seed write (:282,:316), data-disk create (:288), cold-cache base FETCH (:163,:188); background the HTTP mirror push (manager.py:113). Build-IP allocation per 2026-05-30 note (sequential from .3, deterministic per VM, skip DHCP .10-.99) still holds. Depends on ORCH-17.
  > --- COMPLETED 2026-06-01 ---
  > build-miss loop over parallel_map; per-in-flight deterministic build IP via _build_ip_offset (.3-.9 then .100+, skipping DHCP .10-.99), folded into config_hash. Capture overlaps across VMs. Tests in test_build_phase_parallel.py. Note: shared-connection model (see ORCH-17), not per-worker.

- [x] **ORCH-18** · `feat` — parallel run-phase bring-up (per-VM upload/create/start)
  _(blocked by: ORCH-17; done: 2026-05-31)_

  > Stage 1 of the in-process concurrency epic — the upload throughput win. Fan the per-VM bring-up loop (run_phase.py:59-105) over parallel_map, each VM on its own per-worker driver connection: OS-disk upload -> data-disk uploads (run_phase.py:75,90) -> create_vm -> start_vm. Parallelize independent pool/switch/sidecar provisioning (run_phase.py:43-57, provision.py, materialize_sidecar_for incl. sidecar base upload provision.py:166 + config write :184); networks within one switch stay ordered. record_intent/confirm and ctx dict writes go through the ORCH-17 mutex; uploads run lock-free on the worker connection. Depends on ORCH-17. Tests: multi-VM mock, ledger integrity under threads, upload overlap (timing), clean abort on one VM's failure.
  > --- COMPLETED 2026-06-01 ---
  > run_phase fans pools/switch+sidecar/VM bring-up over parallel_map on the shared driver; tests in test_run_phase_parallel.py (overlap timing + ledger integrity + clean abort).

- [x] **ORCH-17** · `chore` — thread-safety substrate for in-process concurrency + ADR amending ADR-0002
  _(blocks: ORCH-18, ORCH-19; done: 2026-05-31)_

  > Stage 0 of the in-process concurrency epic (plan: leads with transfer parallelism). Lays the thread-safety foundation, lands serial + green before any phase goes parallel.
  >
  > Scope:
  > - state/store.py: private threading.Lock wrapping each read-modify-write pair (record_intent/confirm/forget/set_phase) — today atomic per-write, not per-pair (store.py:226-273).
  > - orchestrator/context.py: orchestration Lock guarding the RunContext mutable dicts (network_backends/switch_backends/sidecar_backends/built_disk_paths); per-worker connected-driver factory (store BackendProfile / build_driver callable; thread-local worker_driver() that build_driver()+connect() once per worker thread).
  > - per-driver RLock for the native-communicator path (drivers/*/\_guest.py) routed through the shared connection; SSH communicators hold a per-VM client (communicators/ssh.py:207) and need no lock.
  > - cache/local.py: replace fixed-name .download.partial (local.py:149) with tempfile.mkstemp — absorbs CACHE-4 (#157). Content-addressed <sha>.bin/.json .partial paths unchanged.
  > - new orchestrator/_parallel.py: bounded ThreadPoolExecutor parallel_map helper (--jobs cap, first-exception propagation, cancel/join rest).
  > - ADR superseding/amending ADR-0002: permit a bounded thread pool in orchestration I/O phases ONLY (provision/bring-up/readiness/build) — NOT test execution. Records the two rules (per-worker connections; shared bookkeeping under one mutex). ADR-0018 single-instance unchanged.
  > - PLAN.md §16 update.
  >
  > Worker model + non-goals decided with user 2026-05-31. Absorbs CACHE-4 (#157).
  >
  > --- COMPLETED 2026-06-01 ---
  > Landed: parallel_map helper (--jobs, default 8), StateStore RMW lock, ctx.ledger_lock, LocalCache mkstemp for ALL staging partials + write-lock alias-merge (CACHE-4 absorbed+extended), per-driver call_lock (libvirt _agent_command, proxmox agent calls). ADR-0020 + PLAN §16.
  > DESIGN PIVOT: per-worker driver connections (the original plan) were dropped — they break on drivers' per-instance resource maps (libvirt _libvirt_net_by_network, proxmox _vnet_by_network + random _sdn_zone); a network created on one connection is unresolvable on another. Real-libvirt SMOKE caught it. Final model: ONE shared thread-safe connection driven concurrently. WorkerDrivers/driver_factory removed.

- [x] **ORCH-20** · `feat` — nested_phase — recursive inner Orchestrator + OrchestratorHandle.nested + LIFO teardown
  _(blocks: CORE-40, CI-8; blocked by: CORE-38, BACKEND-10, BUILD-14; done: 2026-05-31)_

  > DONE 2026-05-31. orchestrator/nested_phase.py (run_nested_phase/teardown_nested/NestedHandle), wired into runtime.__enter__/__exit__, OrchestratorHandle.nested. VERIFIED LIVE: recursive inner Orchestrator over qemu+ssh, LIFO teardown 8 ok + 8 ok. Fixes: ResolvedBackend.uplinks inheritance; serial sink gated on build_nic (sidecar pty).

- [x] **ORCH-16** · `feat` — GuestGateway ABC + SSH ProxyJump for remote backends
  _(blocks: PVE-47, PVE-49, PVE-32, PVE-CERT; done: 2026-05-31)_

  > GuestGateway ABC + SSHJumpGateway. **Done + live-verified 2026-05-31** (run #4): all SSH-communicator tests (keybox/users/fileserver) pass over the ProxyJump through the PVE host. Gates green.

- [x] **ORCH-15** · `bugfix` — build-timeout wall-clock guard + serial accept heartbeat
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums. build_phase.py:577-580 deadline checked after each chunk — a driver violating the b'' heartbeat contract hangs wait_for_build_result forever; add wall-clock guard. libvirt/_serial.py:52 accept blocks 60s with no heartbeat past a shorter build deadline.
  >
  > Completed 2026-05-31.

- [x] **ORCH-14** · `bugfix` — guard teardown double-fault window
  _(done: 2026-05-31)_

  > TO_LOOK_AT medium. orchestrator/teardown.py:59-66 — guard final set_phase/release/remove like the rest so a bookkeeping failure in __exit__ can't mask the original bring-up exception.
  >
  > Completed 2026-05-31.

- [x] **ORCH-13** · `bugfix` — best-effort post-capture deletes
  _(done: 2026-05-31)_

  > TO_LOOK_AT H4. orchestrator/build_phase.py:353-363 — one delete_volume raising must not abort the whole multi-VM build and skip teardown_build_phase (build pool/switch/sidecar leak). Wrap per-VM post-capture body best-effort; state-driven teardown is the backstop.
  >
  > Completed 2026-05-31.

- [x] **ORCH-9** · `feat` — build phase injects a dedicated build NIC (drop wiring declared spec.nics at build)
  _(blocks: BUILD-6, CORE-26; blocked by: BACKEND-7; done: 2026-05-30)_

  > Discovered during BACKEND-1.D libvirt certification (2026-05-30); design settled in ADR-0017; core mechanism validated by a hand-rolled libvirt spike (2026-05-30).
  >
  > ROOT CAUSE: build_phase.py wires the build switch only to a VM's declared spec.nics (network_refs = {nic.network: build_net_backend for nic in spec.nics}); the libvirt driver then emits one <interface> per spec.nics. A zero-NIC VM (e.g. examples/capabilities.py 'no-net', reached only over QGA at run time) therefore builds with NO network and any network-needing builder fails (apt-get exits 100). Backend-agnostic; hidden only because the mock never runs real apt.
  >
  > FIX (ADR-0017 §1/§2): build_one_vm provisions every build VM with exactly ONE dedicated, transient build NIC on the build switch, independent of spec.nics, and does NOT attach the declared NICs during build. Build NIC is statically addressed from the build switch's NetworkAddressing — reserved .3-.9 infra slot; sidecar at .1 is gw/dns when nat. RUN phase unchanged.
  >
  > CAPABILITIES (rule #4 — land with the feature): re-enable the no-net VM + its two tests (no_net_agent_executes, no_net_has_no_ethernet) in examples/capabilities.py (commented out as the 2026-05-30 workaround), AND add a single-static-NIC VM + test (a VM whose only NIC is a StaticAddr — the case that also failed apt under the old build-NIC==declared-NIC model). Both build via the dedicated build NIC.
  >
  > SCOPE: build_phase.py + build.py (build-NIC synthesis/addressing) + examples/capabilities.py. The build NIC's MAC slot is BACKEND-7; the netplan collapse it enables is BUILD-6.
  >
  > ORCH-4 interaction: serial build uses one fixed build-NIC slot; parallel build needs a build IP per in-flight VM (see ORCH-4).
  >
  > COMPLETED 2026-05-30: implemented on feature/backend-binding (BACKEND-7 sentinel+BuildNic+.3 slot; ORCH-9 build-phase build-NIC synthesis; BUILD-6 single match-by-MAC netplan). Gates green.

- [x] **CORE-19** · `feat` — concrete *Hypervisor become topology-only scheme markers (drop inline connection)
  _(blocked by: CORE-18; done: 2026-05-28)_

  > Review (2026-05-27): concrete *Hypervisor must no longer carry connection details — it exists ONLY to assert 'this topology MUST run on a <scheme> backend' (PVE CPU type, libvirt NIC model, etc.).
  >
  > DONE 2026-05-28 (feature/backend-binding, follow-on to CORE-18):
  > - ProxmoxHypervisor / LibvirtHypervisor / MockHypervisor are now empty @dataclass(frozen=True) subclasses of generic Hypervisor — pure scheme markers. ALL connection/env fields stripped (host/password/user/port/verify_ssl/node/backing_storage/ssh_*, uri/backing_pool, pool_root/backing_capacity_gb, build_switch).
  > - ProxmoxHypervisor.conn() / .driver_uri properties deleted; ProxmoxConn.from_profile already gone in CORE-18; only normalize_realm remains as a helper shared with ProxmoxProfile. Host validation moved into ProxmoxProfile.__post_init__.
  > - Driver registry: dropped _FROM_HYP / driver_for / register(from_hypervisor=...). Kept from_uri / driver_for_name (cleanup) and scheme_for_hypervisor / is_pinned (now keyed on _SCHEME_FOR_HYP).
  > - resolve_backend matrix collapsed: concrete+none ERROR (names pinned scheme); concrete+given scheme-match check + profile.build_driver(); generic+none ERROR; generic+given profile.build_driver(). Both 'no-profile' cells error.
  > - managed_build_egress_findings moved off the driver onto a free function in testrange.preflight; orchestrator merges with compatibility_findings and driver.preflight. Driver still declares supports_managed_build_egress.
  > - CLI _print_binding: pinned-no-profile UNBOUND now names the required scheme ('pinned to X; pass --connect <X-profile>').
  > - Examples updated: px_hello / native_agent / data_disk / private_public / network_modes all dropped host/password/build_switch kwargs; hello_world / capabilities already used generic Hypervisor.
  > - ADR-0015 addendum added documenting CORE-18 + CORE-19; docs/user/connecting-to-a-backend.md rewritten ('Constraining a plan to a backend scheme' replaces 'Pinning a plan to a backend'); PLAN.md §22 binding section updated with new matrix.
  > - Tests reshuffled: test_proxmox_driver dropped .conn() / .driver_uri / requires_host / driver_for dispatch tests (replaced by the per-backend profile tests + a profile-side build_switch resolves test); test_libvirt_driver dropped uri/backing_pool/driver_for/managed-egress preflight tests; test_mock_driver dropped its managed-egress preflight test; test_preflight reframed around the new free function; test_resolve_backend updated for collapsed matrix; test_hypervisor uses is_pinned/scheme_for_hypervisor.
  > - Gates: ruff/ruff-format clean; mypy --strict clean; pytest -m 'not libvirt' 653 passed, 5 skipped. Describe smoke-tested all four CLI paths.

- [x] **CORE-18** · `feat` — BackendProfile ABC + per-backend concrete profiles (drop flat shape)
  _(blocks: CORE-19; done: 2026-05-28)_

  > connect.py review (2026-05-27): the flat BackendProfile with _CONNECTION_KEYS held every backend's keys at once (node/backing_storage/ssh* PVE-only; ESXi would differ). Replaced with an ABC where each backend declares the keys IT expects.
  >
  > DONE 2026-05-28 (feature/backend-binding):
  > - connect.py: BackendProfile is now an ABC (backend-agnostic) — ClassVar scheme, build_switch: ManagedBuildSwitch|None, abstract build_driver()->HypervisorDriver, abstract classmethod _from_table(Mapping, Path)->Self, abstract describe_fields() (password masked) for the CLI. load_profile(path) reads TOML, dispatches on 'driver' scheme to the registered concrete profile class.
  > - Per-backend concrete profiles self-register via register_profile(): testrange/drivers/proxmox/_profile.py ProxmoxProfile(host/user/password/port/verify_ssl/node/backing_storage/ssh_*), testrange/drivers/libvirt/_profile.py LibvirtProfile(uri/backing_pool), MockProfile (inline in mock.py) (pool_root/backing_capacity_gb). Each builds its own driver; PVE realm-normalisation + SSH defaulting moved into ProxmoxProfile.build_driver().
  > - Registry: _BY_SCHEME and driver_for_profile dropped; register() lost from_profile. ProxmoxConn.from_profile / LibvirtDriver.from_profile / MockDriver.from_profile removed.
  > - orchestrator/backend.py resolve_backend uses profile.build_driver() + profile.scheme (no more profile.to_mapping()).
  > - cli.py _print_binding uses profile.describe_fields() for the profile path; pinned-no-profile path keeps the old hardcoded attr fallback.
  > - Tests: test_backend_profile.py rewritten for ABC + dispatch + common errors; test_driver_registry.py trimmed to pin introspection only; test_resolve_backend.py + test_cli.py updated to use concrete profile classes; new test_mock_profile.py / test_libvirt_profile.py / test_proxmox_profile.py.
  > - PLAN.md §binding updated.
  > - Gates: ruff/ruff-format clean; mypy --strict clean; pytest -m "not libvirt" 661 passed, 5 skipped.

- [x] **ORCH-5** · `chore` — build-phase review fixes — drop builder-specific assumptions
  _(done: 2026-05-27)_

  > Review feedback on the backend-binding epic (2026-05-27). Make the generic build phase lean on the Builder ABC, not CloudInitBuilder specifics:
  > 1. _probe_vm: drop the 'declares no NICs' rejection — a NoOp/QGA build disk can be valid with no NICs; not the orchestrator's call.
  > 2. _probe_vm: drop isinstance(builder, CloudInitBuilder) gate. NOTE: builder.base (OS-disk origin) is CloudInitBuilder-specific (not on the ABC) — overlaps deferred BUILD-1 materialize_os_disk seam (#26). Pending user decision on depth.
  > 3. resolve_backend (backend.py): direct hyp.build_switch instead of getattr — the pinned branch always has a concrete entry.
  > 4. build_one_vm: render_seed may yield no seed (NoOp builder) — make Builder.render_seed -> bytes | None and skip seed-ISO provisioning + pass seed_iso_ref=None when none.
  >
  > DONE 2026-05-27: (1) dropped no-NICs rejection in _probe_vm; (2) dropped isinstance(CloudInitBuilder) gate — added abstract Builder.os_disk_base() -> CacheEntry | None seam (BUILD-1-lite, user-approved), _probe_vm reads OS-disk origin via ABC, None origin => clear 'installer not supported (BUILD-1)' error; also lifted sidecar_sha into the ABC config_hash contract; (3) resolve_backend reads hyp.build_switch directly (driver_uri stays getattr — MockHypervisor omits it); (4) Builder.render_seed -> bytes | None, build_one_vm skips seed-ISO when None. Tests: test_build_phase no-NIC + installer-origin; test_cloudinit os_disk_base. Gates green (647).

- [x] **ORCH-8** · `chore` — Confirm build phase has a bounded timeout (network-starved build can't hang)
  _(done: 2026-05-26)_

  > RESOLVED (2026-05-26) — no prerequisite fix needed before NET-11.
  >
  > wait_for_build_result (testrange/orchestrator/build_phase.py:499) sets deadline = monotonic() + ctx.build_timeout_s (default 600s, runtime.py:78) and checks it on EVERY loop iteration (build_phase.py:536). The silent-guest hole is closed at the ABC contract (testrange/drivers/base.py:347): read_build_result_sink MUST yield empty heartbeat chunks periodically so the watchdog stays reachable even when the guest emits nothing ('without being held hostage by a silent guest'). The mock honors it (mock.py:399 yields empty chunks forever on a wedge); the PVE sink emits empty frames too (cf. PVE-29 #78, ORCH-7 #61 empty-frame work).
  >
  > Conclusion: a network-starved build (e.g. CloudInitBuilder paired with a no-DHCP/no-sidecar switch) fails LOUD via BuildTimeoutError after build_timeout_s, never an indefinite hang. The NET-11 decision to drop the DHCP/DNS coherence preflight is therefore sound — the serial-result watchdog is the contractual safety net.
  >
  > Original purpose: confirm the build phase can't hang on a guest that never reports.

- [x] **ORCH-5** · `feat` — cross-process locking on `state.json`
  _(done: 2026-05-24)_

  > WON'T DO (2026-05-24): superseded by the single-instance-per-plan constraint — only one `testrange` invocation may operate on a given plan/run at a time (now documented in PLAN.md + docs/user/build-vs-run.md). With that invariant there is no legitimate concurrent mutation of state.json, so cross-process FileLock is unnecessary. Original: FileLock if multiple processes ever legitimately mutate the same run.

- [x] **ORCH-7** · `bugfix` — build-result live-run hardening (capture-after-poweroff gate, watchdog ordering, serial empty-frame)
  _(done: 2026-05-24)_

  > Pre-live-run sweep (code-reviewer) found regressions in ORCH-6/PVE-17, invisible to the mock: (1) CRITICAL capture races VM poweroff — wait_for_build_result returns on 'ok' but guest then poweroffs; download_from_pool SFTPs a live qemu file => torn/corrupt disk. Fix: gate capture on get_vm_power_state==shutoff. (2) HIGH watchdog deadline checked before processing the just-received chunk => a chunk carrying 'ok' at the deadline boundary is discarded as a timeout. Fix: process chunk first. (3) MED _serial empty data frame treated as EOF (could be keepalive echo) => spurious mid-build failure. Fix: continue, rely on WebSocketConnectionClosedException for true close. (4) MED keepalive-send failure returns silently as 'console closed'; add WARNING. 2026-05-24.

- [x] **ORCH-6** · `feat` — wait_for_build_result + BuildFailedError (replace power-off-as-success)
  _(done: 2026-05-24)_

  > wait_for_build_result + parse_build_result/BuildResult replace wait_for_shutoff; capture gated on ok token. BuildFailedError(vm,rc,cmd,log) [BuilderError] surfaced by CLI; BuildTimeoutError kept as wedge watchdog. No more get_vm_power_state polling during build. Done 2026-05-24 (ADR-0012).

- [x] **ORCH-DONE** · `feat` — build/run split (Phases B0–B6)

  > **Done 2026-05-22 (ADR-0010).** `build_phase` warms the cache and nothing else; `run_phase` creates pools, gates sidecar readiness, pushes every built disk (OS + data) per VM, runs tests. `testrange build` / `testrange run` (auto-build on miss; `--require-cache`) are distinct CLI verbs. `config_hash` keys the disk set; `create_blank_volume` + `resize_volume` replaced `create_disk_from_base`.

### BUILD

- [x] **BUILD-10** · `chore` — feature/builders worktree (venv + .claude hooks) _(done: 2026-06-08)_

  > Git worktree at .claude/worktrees/builders on branch feature/builders for upcoming builders feature work. Copied the gitignored local env into it: .claude/hooks (auto-commit, log-interesting-commands, session-start) + settings.local.json, and the .venv with all absolute paths relocated to the worktree — bin/* console-script shebangs, activate VIRTUAL_ENV, and the editable-install MapPathFinder (__editable___testrange_0_2_0_finder.py MAPPING) — so 'import testrange' resolves to the worktree source, not main. Verified: ruff/mypy/pytest run from the worktree venv, 794 tests collect. Created 2026-05-31.
  >
  > DONE 2026-06-08: worktree served its purpose — feature/builders merged to main (b51198e); the .claude/worktrees/builders worktree + branch were removed in the 2026-06-08 worktree cleanup.

- [x] **BUILD-17** · `test` — ISO-prep error-path + network-precedence coverage _(done: 2026-06-01)_

  > Code-review remediation (feature/builders). DONE 2026-06-01. Added unit tests: _patch_bootcfg idempotency/append/CRLF, missing-xorriso, ESXi+PVE non-zero-exit (via unwritable outdev), network gw/dns precedence + dns line + no-NIC + no-static, network_interface->answer+flip, ESXi pw control-char rejection. De-brittled: wait_ready argv (len==1), installer-origin asserts on recorded _CreatedVM.os_disk/boot_media not substrings, ks.cfg lowercase pinned. BONUS: exercising the assertions surfaced TWO real production bugs (fixed under this ticket): (1) xorriso -rockridge off uppercased ks.cfg -> KS.CFG, weasel's case-sensitive ks=cdrom:/ks.cfg would ENOENT -> added -compliance lowercase; (2) _patch_bootcfg used read_text() whose universal-newline xlate stripped \r, so CRLF was NEVER preserved despite the docstring -> switched to read_bytes/write_bytes. All 861 unit+prep tests green; ruff+mypy --strict clean.

- [x] **BUILD-21** · `feat` — ESXiKickstartBuilder license= kwarg (serialnum at install)
  _(done: 2026-06-02)_

  > Add license: str|None kwarg to ESXiKickstartBuilder -> render_kickstart emits top-level 'serialnum --esx=<key>' (after accepteula); folds into config_hash; control-char/empty rejection. Unit tests in test_esxi_builder.py (TestLicense + config_hash sensitivity). DONE 2026-06-02 (code+unit). Live serialnum validation rides ESXI-16 Stage D (fallback %firstboot vim-cmd vimsvc/license if weasel rejects).

- [x] **BUILD-20** · `bugfix` — CloudInitBuilder apt-insecure emits GPG-bypass flags, not TLS verify-off
  _(done: 2026-06-01)_

  > CloudInitBuilder._INSECURE_APT_CONFIG dropped Acquire::AllowInsecureRepositories / AllowDowngradeToInsecureRepositories / APT::Get::AllowUnauthenticated, which bypass repo *signature* verification. The real failure on Debian 12/13 is the TLS handshake against an internal HTTPS mirror with an untrusted CA, which those flags do nothing for. Now emits Acquire::https::Verify-Peer "false" / Verify-Host "false", matching ProxmoxAnswerBuilder._APT_INSECURE. Updated comment/docstring (signature->TLS) and tests. Completed 2026-06-01.

- [x] **BUILD-19** · `bugfix` — ESXi _extract swallows failed xorriso exit on partial output
  _(done: 2026-06-01)_

  > DONE 2026-06-01. _esxi_prepare._extract now treats any non-zero xorriso exit (incl. partial output) as failure unless stderr matches the absence strings; drops the partial. Tests in test_esxi_builder.py::TestPrepErrorPaths.

- [x] **BUILD-8** · `feat` — ESXi Kickstart builder
  _(blocked by: BUILD-1; done: 2026-05-31)_

  > DONE 2026-06-01 (BUILD-8 ESXi Kickstart builder). testrange/builders/esxi.py + _esxi_prepare.py (sanctioned xorriso, ADR-0022). On the 5-method ABC: os_disk_base None, boot_media=installer ISO, prepare_boot_media=two-pass xorriso (patch BOOT.CFG kernelopt->runweasel ks=cdrom:/ks.cfg drop cdromBoot, inject ks.cfg, -rockridge off, -boot_image any patch, -return_with FAILURE 32, chmod extracted cfg writable). render_seed->None (single-CDROM). config_hash folds root-pw+ssh-presence+disk+firmware+base_sha, excludes ssh-key value. wait_ready SSH-up. firmware from VMSpec (bios default; uefi unvalidated). build-result in %firstboot (set -e subshell -> ok/fail to /dev/ttyS0 -> poweroff). CONTRACT REFINEMENT: libvirt+proxmox _vm.py now key serial sink + is_build on (seed OR boot_media) so a no-seed installer build still gets the sink + blank disks. 16 unit + 2 xorriso-integration tests. NOT added to portable capabilities.py (heavy/nested, not certified — mirrors the PVE-node decision; capabilities-esxi.py once certified). SMOKE (full nested ESXi build) pending: needs real ESXi 8 ISO + nested KVM (BIOS/i440fx/IDE) + bound profile. Subprocess-ban whitelist + ruff per-file allow extended to _esxi_prepare.py.

- [x] **BUILD-14** · `feat` — build nested inner VMs on the L0 backend into the shared cache
  _(blocks: ORCH-20; blocked by: CORE-38; done: 2026-05-31)_

  > DONE 2026-05-31. build_phase.build_nested_inner_vms + _inner_build_ctx: inner VMs build on L0 into shared cache, inner run is cache-hit upload-and-boot. VERIFIED LIVE: webapp built on L0, ran on host-a.

- [x] **BUILD-2** · `feat` — Proxmox answer-file builder
  _(blocked by: BUILD-1; done: 2026-05-31)_

  > DONE 2026-06-01 (BUILD-2 epic). ProxmoxAnswerBuilder built across B1-B5 (builders/proxmox.py + _proxmox_prepare.py): answer.toml + PROXMOX-AIS seed, xorriso prep (ADR-0022), first-boot script (network-flip+repo-swap+threaded provisioning+serial result), config_hash (ssh-key-excluded), wait_ready SSH-up. 26 unit + 2 integration tests. capabilities.py pve-node added. Smoke (full nested build) pending cert host — see BUILD-13.

- [x] **BUILD-1** · `feat` — installer-based OS-disk origin
  _(blocks: BUILD-2, BUILD-8, BUILD-9; done: 2026-05-31)_

  > DONE 2026-06-01 (BUILD-1 epic). Installer-origin seam built across A1-A5: Builder.boot_media()/prepare_boot_media(); VMSpec.firmware (bios/uefi); create_vm boot_media_ref + OVMF/bootable-CDROM in libvirt (firmware='efi'/q35, certified path) + proxmox (bios=ovmf+efidisk0, pending live-PVE cert); orchestrator materialize branch; preflight builder_origin_findings. ADR-0010 §6.

- [x] **BUILD-12** · `test` — examples/capabilities.py PVE-node VM + TESTS entry
  _(blocks: BUILD-13; blocked by: BUILD-2e; done: 2026-05-31)_

  > DONE 2026-06-01, REVISED. The portable pve-node VM was added then REMOVED from examples/capabilities.py at the user's direction: ProxmoxAnswerBuilder needs a cached PVE 9 ISO + nested KVM, so it breaks the survey's portability and PVE isn't certified yet. Per CLAUDE.md rule 4's documented carve-out, the PVE capability will land in examples/capabilities-proxmox.py once the backend is certified working; until then it's exercised by binding the portable plan. KEPT: the describe(boot_media) fix in cli.py (ABC-seam collection, surfaces installer-origin media refs) — general correctness, independent of the pve-node.

- [x] **BUILD-2e** · `feat` — ProxmoxAnswerBuilder assembly + wait_ready + unit tests
  _(blocks: BUILD-12, BUILD-13; blocked by: BUILD-1c, BUILD-1d, BUILD-2a, BUILD-2b, BUILD-2c, BUILD-2d; done: 2026-05-31)_

  > DONE 2026-06-01. ProxmoxAnswerBuilder assembled on 5-method ABC + boot_media/prepare_boot_media seams; wait_ready SSH-up; registered in builders/__init__ + pyproject  extra (pycdlib). 26 unit tests green. Added prepare_boot_media seam to Builder ABC + orchestrator staging (in-scope BUILD-1 refinement).

- [x] **BUILD-2d** · `feat` — ProxmoxAnswerBuilder.config_hash
  _(blocks: BUILD-2e; blocked by: BUILD-2a, BUILD-2c; done: 2026-05-31)_

  > DONE 2026-06-01. config_hash folds network block + disk layout + first-boot digest (covers packages/post_install) + base_sha (vanilla ISO sha); EXCLUDES ssh keys (rotation-stable). Tests: test_proxmox_builder.TestConfigHash (determinism, sensitivity, ssh-rotation insensitivity).

- [x] **BUILD-2c** · `feat` — first-boot script (network-flip + repo-swap + threaded provisioning + serial result)
  _(blocks: BUILD-2d, BUILD-2e; blocked by: BUILD-2a; done: 2026-05-31)_

  > DONE 2026-06-01. _first_boot_script: network-flip (flush vmbr0+dhclient) + repo-swap (no-subscription) + threaded apt/pip/post_install under set -eE+ERR trap + TESTRANGE-RESULT serial framing + systemctl poweroff. apt_insecure prologue. Tests: test_proxmox_builder.TestFirstBootScript.

- [x] **BUILD-2b** · `feat` — prepared-ISO derivation (xorriso) + cache keying
  _(blocks: BUILD-2e; blocked by: BUILD-2a, BUILD-11; done: 2026-05-31)_

  > DONE 2026-06-01. testrange/builders/_proxmox_prepare.py (sanctioned subprocess, ADR-0022): xorriso -boot_image any keep + auto-installer-mode.toml + /proxmox-first-boot, -return_with FAILURE 32. prepare_boot_media seam caches prepared ISO keyed by first-boot digest. ruff per-file allow + test_subprocess_ban whitelist + guard test.

- [x] **BUILD-2a** · `feat` — answer.toml renderer + PROXMOX-AIS seed ISO
  _(blocks: BUILD-2b, BUILD-2c, BUILD-2d, BUILD-2e; blocked by: BUILD-1a; done: 2026-05-31)_

  > DONE 2026-06-01. answer.toml renderer (build_answer_toml) + PROXMOX-AIS seed ISO (pycdlib); root PosixCred+password required (fail loud); static from-answer / from-dhcp fallback; PVE 9.x kebab keys. builders/proxmox.py. Tests: test_proxmox_builder.TestAnswerToml/TestSeedIso.

- [x] **BUILD-1e** · `feat` — preflight validation for installer-origin
  _(blocked by: BUILD-1c; done: 2026-05-31)_

  > DONE 2026-06-01. preflight.builder_origin_findings (no-os-disk-origin) wired into mock+libvirt+proxmox preflight; fails loud before backend stand-up when a builder declares neither origin. Tests: test_preflight.TestBuilderOriginFindings.

- [x] **BUILD-1d** · `feat` — Proxmox driver OVMF + bootable CDROM
  _(blocks: BUILD-2e, BUILD-13; blocked by: BUILD-1b; done: 2026-05-31)_

  > DONE 2026-06-01. libvirt _vm.py: OVMF via firmware='efi' on q35 + bootable installer CDROM (sdb, boot order) + blank-disk fall-through; per-device boot order. proxmox _vm.py: bios=ovmf+efidisk0+q35, ide0 bootable installer, blank scsi0 for installer-origin, resize guard. PVE efidisk0 documented-form; flagged for live-PVE cert (libvirt is the certified path).

- [x] **BUILD-1c** · `feat` — orchestrator build_one_vm materialize branch
  _(blocks: BUILD-1e, BUILD-2e; blocked by: BUILD-1b; done: 2026-05-31)_

  > DONE 2026-06-01. orchestrator build_phase: _VMBuildPlan.installer_origin + boot_media_path; _probe_vm folds boot_media sha into base_sha; build_one_vm materialize branch (create_blank_volume + upload boot media + boot_media_ref to create_vm + cleanup). Test: test_build_phase.test_installer_origin_materializes_blank_disk_and_boots_media.

- [x] **BUILD-1b** · `feat` — VMSpec firmware (bios/uefi) + create_vm firmware/boot-media contract
  _(blocks: BUILD-1c, BUILD-1d; blocked by: BUILD-1a; done: 2026-05-31)_

  > DONE 2026-06-01. VMSpec.firmware (bios/uefi, FIRMWARES frozenset) — validated str matching ProxmoxHardDrive.bus convention. create_vm gains boot_media_ref across ABC+mock+libvirt+proxmox; firmware read from spec. Tests: test_vmspec firmware cases.

- [x] **BUILD-1a** · `feat` — Builder.boot_media() seam + None-origin semantics
  _(blocks: BUILD-1b, BUILD-2a; done: 2026-05-31)_

  > DONE 2026-06-01. Builder.boot_media() seam added (builders/base.py, default None). os_disk_base docstring updated. Tests: test_cloudinit.test_image_origin_has_no_boot_media.

- [x] **BUILD-7** · `bugfix` — constrain Package.name (shell/arg injection)
  _(done: 2026-05-31)_

  > TO_LOOK_AT H9. packages/base.py:15-17 validates only non-emptiness; name flows unquoted into apt-get install / pip3 install at builders/cloudinit.py:494/515/519. Constrain to package-name charset or shlex.quote at the boundary.
  >
  > Completed 2026-05-31.

- [x] **BUILD-6** · `feat` — CloudInitBuilder — single match-by-MAC netplan, retire run-phase staging
  _(blocks: CORE-26; blocked by: ORCH-9, BACKEND-7; done: 2026-05-30)_

  > Per ADR-0017 §3/§4. With ORCH-9 giving every build VM a dedicated build NIC and detaching declared NICs at build, the install-vs-run netplan split collapses. Core mechanism validated by the 2026-05-30 libvirt spike.
  >
  > DELETE: render_network_config (install DHCP-by-name path), _render_run_netplan_write_files + _render_run_netplan_yaml staging. The whole 'Static IPs' staging section of the module docstring goes.
  >
  > KEEP: the 99-testrange-disable-network.cfg write_files entry — the spike confirmed it is what pins the build-boot-rendered netplan across the seed-less run boot (cloud-init does not re-render). It is NOT conditional.
  >
  > REPLACE WITH: one match-by-MAC renderer used directly as cloud-init network-config, containing the build NIC (static, from build switch addressing) + every declared NIC with its real addr. Applied live on the build boot. Same file persists into the cached image: at run the build NIC's MAC is absent (stanza inert) and declared NICs come up; during build the declared NICs are physically absent (stanzas inert) so apt egresses via the build NIC with no route conflict / carrier wait. (All four conditions observed in the spike.)
  >
  > Depends on BACKEND-7 (build-NIC MAC) and ORCH-9 (declared NICs detached at build + capabilities). Update networking-modes.md + the module docstring in the same change.
  >
  > COMPLETED 2026-05-30: implemented on feature/backend-binding (BACKEND-7 sentinel+BuildNic+.3 slot; ORCH-9 build-phase build-NIC synthesis; BUILD-6 single match-by-MAC netplan). Gates green.

- [x] **BUILD-5** · `bugfix` — StaticAddr dns not applied to guest resolv.conf (systemd-resolved stub)
  _(done: 2026-05-30)_

  > Found during BACKEND-1.D libvirt certification (2026-05-30). capabilities 'users' VM declares NetworkIface(StaticAddr('10.30.0.120', gw=..., dns=('9.9.9.9',))). The CloudInitBuilder netplan staging sets the address+gw but the guest's /etc/resolv.conf is the systemd-resolved stub (nameserver 127.0.0.53), so users_uses_explicit_resolver fails (9.9.9.9 not present). Backend-agnostic builder/netplan-DNS gap (mock never inspects resolv.conf). FIX: render the StaticAddr dns into netplan nameservers AND ensure it reaches /etc/resolv.conf (e.g. netplan nameservers + systemd-resolved, or write resolv.conf directly). WORKAROUND (2026-05-30): users_uses_explicit_resolver commented out in examples/capabilities.py.

- [x] **BUILD-3** · `feat` — builder emit-result contract (TESTRANGE-RESULT on serial) + CloudInitBuilder
  _(done: 2026-05-24)_

  > Builder ABC documents the emit-result obligation (fail-fast + TESTRANGE-RESULT record on serial + poweroff). CloudInitBuilder renders one fail-fast bash -c runcmd (set -eE + ERR trap -> framed fail record + base64 log on /dev/ttyS0; success -> sync+ok+poweroff); apt moved out of packages: directive into the trapped script. Done 2026-05-24 (ADR-0012).

- [x] **BUILD-22** · `bugfix` — ESXi kickstart %firstboot heredoc terminator indented -> install hangs, no result

  > Live-found 2026-06-02: render_kickstart indented the %firstboot subshell body incl. the cat<<'KEYEOF' heredoc terminator ('  KEYEOF'). busybox plain heredoc only closes on column-0 'KEYEOF', so it swallowed the rest of the script (chmod/poweroff/TESTRANGE-RESULT echo) -> install completes to DCUI but never reports/poweroffs. Never caught (full install never run before). Fix: flat (un-indented) subshell body in _esxi_prepare.render_kickstart; also fixes leading-space-in-authorized_keys. Regression test added. DONE.

### NET

- [x] **NET-8** · `feat` — per-switch uplink static address (generalize build_uplink_addr)
  _(blocks: PVE-51; done: 2026-05-31)_

  > NET-8 profile-driven static sidecar uplink addressing. **Done + live-verified 2026-05-31** (run #4): public_web_can_reach_internet passes — sidecar eth1 static 10.10.10.2/24 on vmbr9, dnsmasq forwards to 1.1.1.1, NAT egress works. See ADR-0016 addendum.

- [x] **NET-16** · `bugfix` — duplicate-static keyed by shared wire, not Network name
  _(done: 2026-05-31)_

  > TO_LOOK_AT H8. networks/validate.py:135,194 — seen_per_net keys on nic.network, but Networks on one Switch share one CIDR/L2 wire. Two VMs w/ same static IP on different Networks of one Switch pass validation then collide. Key by Switch/CIDR. Also confirm/strip dead mgmt-slot validation (l.177).
  >
  > Completed 2026-05-31.

- [x] **NET-15** · `bugfix` — addressing layout hard-assumes /24
  _(done: 2026-05-31)_

  > TO_LOOK_AT H7. networks/_addressing_consts.py:35-39 + sidecar.py:196 + validate.py:183 — DHCP_RANGE_HI=99 overruns broadcast on /26,/28. Reject prefixes longer than /24 in Switch.__init__ OR compute pool bounds from subnet size, consistently across all three sites.
  >
  > Completed 2026-05-31.

- [x] **NET-14** · `chore` — migrate examples + capabilities.py + connect.toml.example + docs to out-of-band egress / named uplinks
  _(done: 2026-05-29)_

  > examples/*.py uplink= -> named; capabilities.py gains named-uplink + NAT-egress coverage entry (project rule 4); connect.toml.example -> multi-profile + . Docs: connecting-to-a-backend.md, writing-a-plan.md, drivers/networking-modes.md, dev/architecture.md, dev/extending/drivers.md. 2026-05-29.

- [x] **NET-13** · `feat` — delete ManagedBuildSwitch/ManagedEgress + supports_managed_build_egress
  _(done: 2026-05-29)_

  > Remove ManagedBuildSwitch, ManagedEgress (networks/base.py), supports_managed_build_egress capability (drivers/base.py + concretes), the managed_egress kwarg on create_switch, the PVE SDN snat/fence realization (_sdn.py/_naming.py/driver.py), preflight.managed_build_egress_findings, BUILD_EGRESS_CIDR/MANAGED_EGRESS_DNS/_managed_build_switch (build.py). build_switch: Switch|None everywhere (hypervisor profile, ResolvedBackend, resolve_build_switch). Update tests. 2026-05-29.

- [x] **NET-12** · `docs` — ADR superseding 0014 — drop ManagedBuildSwitch/managed-egress; out-of-band egress + named uplinks
  _(done: 2026-05-29)_

  > Supersede ADR-0014. TestRange no longer manufactures/fences a build-internet egress segment. 'Magic egress' is just a host bridge the user provisions out-of-band (NAT/DHCP behind it); a NAT Sidecar's eth1 rides it and DHCPs from it. Drop MagicEgressSwitch idea (pure sugar) — it's a plain Switch(uplink=<named>, sidecar=Sidecar(dhcp,dns,nat)). Amend ADR-0008 L2 section for named-uplink resolution (driver-held map, sibling of backing_storage). Update PLAN §10. Closes-supersedes NET-10/NET-11. 2026-05-29.

- [x] **NET-11** · `feat` — user-declared Hypervisor.build_switch + ManagedBuildSwitch (retire build_uplink)
  _(done: 2026-05-26)_

  > SUPERSEDED by NET-13/ADR-0016 (2026-05-29): ManagedBuildSwitch/ManagedEgress/supports_managed_build_egress deleted; uplinks are profile-named, egress out-of-band. Original: user-declared Hypervisor.build_switch + ManagedBuildSwitch (retire build_uplink).

- [x] **NET-10** · `docs` — ADR — user-declared Hypervisor.build_switch + ManagedBuildSwitch
  _(done: 2026-05-26)_

  > SUPERSEDED by NET-12/ADR-0016 (2026-05-29): ManagedBuildSwitch removed; egress is out-of-band, build_switch is a plain portable Switch on the Hypervisor. Original: ADR — user-declared Hypervisor.build_switch + ManagedBuildSwitch.

- [x] **NET-2** · `feat` — `Switch(router=True)` — sidecar as router
  _(done: 2026-05-26)_

  > Sidecar gets `ip_forward=1` + nftables MASQUERADE on its uplink, and dnsmasq advertises a real default gateway via DHCP option 3 (currently suppressed — `testrange/networks/sidecar.py`). mgmt stays a host adapter; router is the active-forwarding capability.
  >
  > SUPERSEDED 2026-05-26 by NET-9 (#86) -> Sidecar(nat=True). render_nftables_ruleset emits the eth1 MASQUERADE, render_sysctl_conf emits ip_forward=1, and render_dnsmasq_conf advertises dhcp-option router=.1 (option 3 no longer suppressed) whenever sidecar.nat is set. Shipped under a different name; closing as superseded.

- [x] **NET-9** · `feat` — consolidate sidecar flags into Switch(sidecar=Sidecar(...))
  _(done: 2026-05-24)_

  > DONE 2026-05-24. NET-9 umbrella COMPLETE: all of NET-9.1..9.9 (tasks 87-95) landed on branch feature/net-9-sidecar (off feature/proxmox, since 9.5 touches the PVE driver). Consolidated sidecar service flags (dhcp/dns/nat/uplink_addr->addr) into frozen Sidecar value object; Switch now carries sidecar: Sidecar|None and keeps L2 topology (cidr/uplink/mgmt). nat-requires-uplink enforced in Switch.__init__; addr-requires-nat + explicit-prefix in Sidecar.__post_init__. needs_sidecar == 'sidecar is not None'. Gates green: ruff, ruff format, mypy --strict, pytest 553 passed/5 deselected. All 6 examples describe cleanly. NOT yet committed/squashed/pushed.

- [x] **NET-9.9** · `docs` — ADR + PLAN §10 + networking-modes.md + base docstrings
  _(done: 2026-05-24)_

  > DONE 2026-05-24. ADR-0013 (Switch/Sidecar split, api-design discipline) + index. PLAN.md §10 retitled 'Switch owns L2 topology; Sidecar owns the services' + shape/prose/build-phase/limits updated. networking-modes.md rewritten to Switch+Sidecar (shape, addressing table, knobs vs services, topology diagrams, per-driver table, examples, build_uplink). Also fixed writing-a-plan.md + dev/architecture.md + networks/base.py & drivers/base.py docstrings for reality. ADR-0008 left immutable (superseded by 0013). Part of NET-9 (task 86).

- [x] **NET-9.8** · `chore` — Migrate all examples to Sidecar shape
  _(done: 2026-05-24)_

  > Rewrite Switch(...) in all 6 examples (hello_world, px_hello, data_disk, private_public, native_agent, network_modes[4 switches]) to uplink-on-Switch + sidecar=Sidecar(...). Keep examples comment-free (no-comments-in-examples rule). Verify with 'testrange describe examples/*.py'. NOTE: hello_world gets re-touched by CORE-12 genericization later — expected per chunk ordering. Depends NET-9.2.

- [x] **NET-9.7** · `test` — Migrate unit tests to Sidecar shape (green gate)
  _(done: 2026-05-24)_

  > Rewrite all Switch(...) constructions in tests/unit/ (test_networks, test_sidecar, test_plan, test_mock_driver, test_proxmox_driver, test_build_phase, test_cli_build_run, test_test_runner, test_orchestrator, test_cloudinit) to sidecar=Sidecar(...). Add coverage for the new Switch.__init__ nat-requires-uplink path. This is the step that brings 'pytest -m not integration' back to green after the cutover. Depends NET-9.3, NET-9.4, NET-9.5, NET-9.6.

- [x] **NET-9.6** · `feat` — Build synthesis constructs Sidecar internally
  _(done: 2026-05-24)_

  > orchestrator/build.py: _build_switch builds Switch(uplink=..., sidecar=Sidecar(dhcp,dns,nat,addr=uplink_addr)) for the uplink case and Switch(sidecar=Sidecar(dhcp,dns)) for no-uplink. _sidecar_spec reads switch.sidecar. Hypervisor.build_uplink/build_uplink_addr stay as-is; call sites in runtime.py/build_phase.py unchanged. Depends NET-9.2.

- [x] **NET-9.5** · `feat` — Update drivers (mock + proxmox) nat reads to switch.sidecar
  _(done: 2026-05-24)_

  > drivers/mock.py (lines ~222,225) and drivers/proxmox/_sdn.py (create_switch uplink+nat gate) and drivers/proxmox/driver.py (~250-253 wanted-uplinks set incl build_switch) change 'switch.nat' -> 'switch.sidecar is not None and switch.sidecar.nat'. switch.uplink reads unchanged (stays on Switch). Depends NET-9.2.

- [x] **NET-9.4** · `feat` — Update validate.py + provision.py + cli describe to switch.sidecar
  _(done: 2026-05-24)_

  > validate_addressing (needs_sidecar/dhcp reads) and provision.py (needs_sidecar) and cli.py _print_describe (dhcp/dns/nat now under a sidecar block; uplink/mgmt stay on Switch). mgmt reads in preflight.py unchanged. Depends NET-9.2.

- [x] **NET-9.3** · `feat` — Update sidecar render fns to switch.sidecar
  _(done: 2026-05-24)_

  > testrange/networks/sidecar.py: render_dnsmasq_conf / render_sidecar_interfaces / render_nftables_ruleset / render_sysctl_conf / sidecar_nic_specs read services via switch.sidecar (dhcp/dns/nat/addr) instead of switch.* . Callers only invoke these when needs_sidecar, so sidecar is non-None here. Depends NET-9.2.

- [x] **NET-9.2** · `feat` — Reshape Switch to sidecar=Sidecar(...)
  _(done: 2026-05-24)_

  > Drop dhcp/dns/nat/uplink_addr from Switch; add sidecar: Sidecar|None=None. KEEP uplink + mgmt + cidr on Switch (uplink is L2 topology — the documented pure-bridge uplink-without-sidecar mode proves it). Move 'nat requires uplink' into Switch.__init__ (only object seeing both). Redefine needs_sidecar -> 'sidecar is not None'. Update NetworkAddressing.from_switch to read switch.sidecar (dhcp/nat/dns). BREAK POINT: every downstream construction site stops compiling until NET-9.3-.8 land — this and the rest are one atomic cutover. Depends NET-9.1.

- [x] **NET-9.1** · `feat` — Sidecar value object + validation (TDD)
  _(done: 2026-05-24)_

  > DONE 2026-05-24. Frozen Sidecar dataclass (dhcp/dns/nat: bool, addr: StaticAddr|None) in testrange/networks/base.py with __post_init__ validation (at-least-one-service; addr-requires-nat; addr-needs-explicit-prefix). Exported from testrange.networks. Tests: TestSidecar in tests/unit/test_networks.py. Additive — landed green alone (mypy --strict clean, 554 passed). Part of NET-9 (task 86).

- [x] **NET-6** · `chore` — host-disconnect preflight warning (`--check-uplinks`)
  _(done: 2026-05-24)_

  > WON'T DO (2026-05-24): descoped during board grooming; the opt-in uplink-disconnect preflight warning is not being pursued. Original: Enslaving the host's only routable NIC drops the host off the network; warn at preflight in an opt-in pass.

- [x] **NET-7** · `feat` — static IP for the sidecar uplink (eth1)
  _(done: 2026-05-24)_

  > Static IP for the sidecar's MASQUERADE uplink NIC (eth1) instead of DHCP-from-upstream-LAN, for hosts that won't lease the sidecar's MAC (single-public-IP / host-NAT'd bridges). Switch.uplink_addr: StaticAddr (requires nat, explicit prefix); ProxmoxHypervisor.build_uplink_addr threads into the synthesized build switch. render_sidecar_interfaces -> static eth1 (addr/netmask/gateway); render_dnsmasq_conf -> no-resolv + server=<uplink dns> (static eth1 won't populate resolv.conf). Live-confirmed cause: OVH-style box DHCPs only the host MAC. Done 2026-05-24.

- [x] **NET-1** · `bugfix` — `validate.py` hardcodes the user-static pool bounds
  _(done: 2026-05-24)_

  > Fixed: validate.py now uses USER_STATIC_LO/HI from _addressing_consts for the DHCP-pool hint (was hardcoded +100/+254 - the exact drift those consts prevent). Regression: test_dhcp_pool_hint_tracks_user_static_consts. Done 2026-05-24.

### BACKEND

- [x] **BACKEND-2** · `feat` — ESXi driver
  _(done: 2026-06-01)_

  > SUPERSEDED 2026-06-01: decomposed into the ESXI epic (task 260) — driver core absorbed into ESXI-1 (task 265). Original scope (pyVmomi; vSwitch+portgroup; VMware Tools guest-ops; /folder datastore I/O) lives across ESXI-1..15 + CORE-60 + spikes ESXI-S1/S2/S3. Standalone-host scope (no vCenter/DVS). See the ESXI swimlane.

- [x] **BACKEND-13** · `bugfix` — guard driver resource-maps + lazy-init under concurrency
  _(done: 2026-06-01)_

  > ADR-0020 review: libvirt _libvirt_net_by_network / proxmox _vnet_by_network + lazy inits (serial dir/listener, _ensure_ssh) unguarded under parallel build. FIX: per-driver _state_lock guards the maps; double-checked call_lock guards the lazy inits. Tests: test_concurrency_guards vnet-map; existing driver suite. Done 2026-06-01.

- [x] **BACKEND-12** · `bugfix` — nested-KVM preflight probe hardening + comm.gateway depth-1 guard
  _(done: 2026-06-01)_

  > Code-review findings (ADR-0021). (1) _host_nested_kvm probe: make tri-state (enabled/disabled/indeterminate) so an unreadable/empty sysfs param does not false-reject a valid plan; only emit the finding on an explicit N/0. (2) Remote L0 silently skips the probe — elevate to a visible warning (was debug) and narrow ADR claim to local-L0 (remote deferred BACKEND-5). (3) Depth-1 remote footgun: nested_phase ignores comm.gateway; add public SSHCommunicator.gateway property + fail loud when the L0 guest is bound via a gateway. (4) Drop inline magic virsh timeout=15.0 (trust 60s default). Completed 2026-06-01.

- [x] **BACKEND-10** · `feat` — programmatic LibvirtProfile/driver for qemu+ssh inner binding + libvirtd readiness
  _(blocks: ORCH-20; blocked by: CORE-38; done: 2026-05-31)_

  > DONE 2026-05-31. testrange/drivers/libvirt/_nested.py: inner_ssh_uri, inner_libvirt_profile (qemu+ssh + keyfile), wait_libvirtd_ready (virsh probe). Tests in test_libvirt_nested.py.

- [x] **BACKEND-1** · `feat` — libvirt driver rebuild against the multi-backend ABC
  _(done: 2026-05-31)_

  > PARENT/EPIC — libvirt driver full rewrite from zero against the current multi-backend ABC. libvirt becomes the REFERENCE implementation; the mock retires to tests/ (unit-only). Tracked as sub-tickets BACKEND-1.0..1.E.
  >
  > SUPERSEDES the earlier BACKEND-1.1 slice + managed-egress framing — killed by ADR-0016 + the decisions below.
  >
  > KEY DECISIONS (proven non-root 2026-05-30): libvirt-python only, pyroute2 DROPPED; L2 via libvirt network API (daemon builds bridge, no CAP_NET_ADMIN); no root/pre-install, 'libvirt' group only; per-run dir pools (create_pool/destroy_pool), backing_pool knob REMOVED, stream API both directions; LibvirtProfile = uri (+uplinks); egress uplink = pre-existing out-of-band host bridge 'tr-egress'.
  >
  > DONE = pytest -m libvirt green AND testrange run --profile libvirt-local examples/capabilities.py green, both as plain user.
  >
  > DONE 2026-05-31: ALL subtasks 1.0-1.E complete. Certification met as plain libvirt-group user (no root): capabilities.py 30/30 green; pytest -m libvirt (tests/integration/test_libvirt.py) 3/3 green. 1.E ratified libvirt as the reference backend (ADR-0019) + full doc sweep. libvirt is the certified reference implementation. Epic closed.

- [x] **BACKEND-1.E** · `chore` — mock -> tests/ + ADR + docs (libvirt is the reference impl)
  _(blocked by: BACKEND-1.D; done: 2026-05-31)_

  > Move testrange/drivers/mock.py -> tests/ (unit-only). Drop its side-effect import from drivers/__init__.py; register the mock scheme in tests/conftest.py so unit tests + --connect mock still resolve. Update hypervisor.py/_registry.py docstrings. New ADR: libvirt is the reference backend, mock is test-only. Update docs/dev/extending/drivers.md, docs/user/install.md, connecting-to-a-backend.md, README. Correct the stale 'as root' integration-run note.
  >
  > UPDATE 2026-05-30: mock MOVED to tests/mock_driver.py; side-effect import dropped; registered via tests/conftest.py; 693 unit tests green. REMAINING: the reference-impl ADR + docs.
  >
  > DONE 2026-05-31: ADR-0019 (libvirt is the reference backend; mock test-only) written + added to adr/index toctree. Doc sweep: drivers.md (reference impl mock->libvirt + path fix tests/mock_driver.py + Tests section), bugfixing.md ('real backend lands' -> libvirt integration suite), connecting-to-a-backend.md (LibvirtProfile(uri,uplinks), no backing_pool). README + user driver-status already done under DOCS-5; tr-egress recipe already in out-of-band-egress.md (DOCS-2). PLAN.md synced to present-tense (reference backend, package tree, BACKEND-1 status, sub-ticket list). hypervisor.py/_registry.py docstrings left as-is (neutral examples, not mock-as-reference). Stale 'as root' note lived only in memory, corrected. Gates: ruff + mypy --strict green (no py changed).

- [x] **BACKEND-9** · `chore` — libvirt driver lows
  _(done: 2026-05-31)_

  > TO_LOOK_AT lows. dead _todo() libvirt/_vm.py:59; data-disk naming breaks past vdz silently (libvirt/_vm.py:147 yields vd{); _net.py:52 interpolates backend_name without escape() while peers use quoteattr; ISO byte-nondeterminism docstrings overclaim (builders/cloudinit.py + sidecar_iso.py).
  >
  > Completed 2026-05-31.

- [x] **BACKEND-8** · `bugfix` — reconcile build_switch ABC type to Switch | None
  _(done: 2026-05-31)_

  > TO_LOOK_AT H2. drivers/base.py:94 declares build_switch: Switch (non-optional) but proxmox/driver.py:122 + libvirt/driver.py:122 guard 'if build_switch is not None'. Set Switch | None so mypy --strict catches bad call sites; or drop the guards.
  >
  > Completed 2026-05-31 (dropped the dead None-guards; ABC type Switch was correct — resolve_build_switch never yields None).

- [x] **BACKEND-7** · `feat` — reserved build-NIC MAC slot in compose_mac + drop name-match fallback
  _(blocks: ORCH-9, BUILD-6, CORE-26; done: 2026-05-30)_

  > Per ADR-0017. The dedicated build NIC needs a deterministic MAC that never collides with a VM's declared NIC MACs and never enters the declared-NIC MAC tuple feeding the run netplan / config_hash. Give HypervisorDriver.compose_mac a reserved sentinel nic_idx (disjoint from declared indices 0..n-1) for the build NIC, implemented across the ABC + every concrete driver (libvirt today; mock). Amends ADR-0006: match-by-MAC becomes the sole netplan interface-matching strategy — drop the 'match: name: en*' belt-and-suspenders in both the install and run renderers (the build NIC and all declared NICs match by MAC, so the build NIC stanza is inert at run and declared stanzas are inert at build). Blocks BUILD-6.
  >
  > COMPLETED 2026-05-30: implemented on feature/backend-binding (BACKEND-7 sentinel+BuildNic+.3 slot; ORCH-9 build-phase build-NIC synthesis; BUILD-6 single match-by-MAC netplan). Gates green.

- [x] **BACKEND-1.D** · `feat` — widen to capabilities certification + integration suite
  _(blocks: BACKEND-1.E; blocked by: BACKEND-1.C; done: 2026-05-30)_

  > Snapshots create/list/delete/restore (disk + mem). Data disks. Static/unmanaged NIC addressing. Password users. Add tests/integration/test_libvirt.py (marked libvirt, self-cleaning). CERTIFICATION (DONE gate): testrange run --profile libvirt-local examples/capabilities.py green AND pytest -m libvirt green, both as plain user.
  >
  > PROGRESS 2026-05-30: snapshots (full internal qcow2; disk-revert + mem-restore both LIVE-verified), data disks, static/dhcp/unmanaged NICs, password users all implemented + working. **testrange run --profile libvirt-local examples/capabilities.py is GREEN** (22/22 enabled tests; teardown 25/25 clean). 5 non-libvirt gaps ticketed + commented out in capabilities.py: ORCH-9 (zero-NIC build NIC), BUILD-4 (pip3), BUILD-5 (static DNS->resolv.conf), CORE-24 (blkid non-root test), COMM-4 (mem-snapshot over SSH; driver QGA-proven).
  > REMAINING for this ticket: write tests/integration/test_libvirt.py (pytest -m libvirt) — the second half of the DONE gate.
  >
  > UPDATE 2026-05-30: tests/integration/test_libvirt.py DONE + passing live (3/3, 64s): storage stream round-trip, isolated-network lifecycle, VM lifecycle + serial build-result sink + snapshots (NIC-less guest, QGA-independent, self-cleaning). pytest -m libvirt green. The 'testrange run capabilities.py green' half is gated on the mgmt reachability decision (ADR-0009) for the SSH-communicator VMs.
  >
  > DONE 2026-05-30: BOTH DONE-gate conditions met as plain user (libvirt group, no root): (1) testrange run --profile libvirt-local examples/capabilities.py GREEN (26/26 enabled; no-net disabled = ORCH-9); (2) pytest -m libvirt GREEN (tests/integration/test_libvirt.py 3/3). mgmt=True (libvirt implements the .2 host adapter + drops the mgmt_unsupported gate) gives the on-host orchestrator SSH reachability. Capabilities test-author bugs fixed inline (python3-pip, resolvectl, sudo blkid, /dev/shm).

- [x] **BACKEND-1.C** · `feat` — L2 via libvirt network API + sidecar + DHCP discovery
  _(blocks: BACKEND-1.D; blocked by: BACKEND-1.S, BACKEND-1.B; done: 2026-05-30)_

  > create_switch/destroy_switch/create_network/destroy_network via networkDefineXML/networkCreate (NO pyroute2). Isolated guest segment = <network> with no <forward>/<dhcp>. NAT uplink = attach sidecar eth1 to pre-existing host bridge switch.uplink resolves to (tr-egress). Boot sidecar, gate readiness (QGA), discover DHCP lease via native guest transport.
  >
  > DONE 2026-05-30: _net implemented; per-Switch isolated libvirt network (shared by its Networks) + uplink passthrough for nat. TWO live root-causes found+fixed: (1) headless domain needs a <video> device or Debian GRUB gfxterm boot-loops forever (added <video> to domain XML); (2) local orchestrator reaches SSH run-VMs by direct host->guest IP, so each Switch network carries a host <ip> at .2 + <dns enable=no> (no dnsmasq, no sidecar clash) per PLAN §20. hello_world.py --profile libvirt-local: build(cache)+sidecar+QGA-ready+SSH+nginx/hostname tests all PASS; only snapshot test fails (1.D). QGA live-validated via sidecar readiness.

- [x] **BACKEND-1.S** · `chore` — tr-egress host bridge + libvirt-local connect.toml profile
  _(blocks: BACKEND-1.C; done: 2026-05-30)_

  > Out-of-band host setup so the libvirt slices have egress + a bound profile.
  > DONE 2026-05-30: tr-egress libvirt NAT network live (Active/Persistent/Autostart, 192.168.199.x via built-in dnsmasq + forward=nat MASQUERADE). Authored gitignored connect.toml with  driver=libvirt, uri=qemu:///system,  egress='tr-egress'. Updated examples/connect.toml.example: libvirt-local maps egress='tr-egress' (was virbr0), dropped backing_pool.

- [x] **BACKEND-1.B** · `feat` — VM lifecycle + serial build-result sink + QGA (no network)
  _(blocks: BACKEND-1.C; blocked by: BACKEND-1.A; done: 2026-05-30)_

  > Domain XML: qcow2 disks, stable-MAC NICs, <serial type=unix> host-connect socket, org.qemu.guest_agent.0 virtio channel (unconditional), seed CD-ROM. create_vm/start_vm/shutdown_vm (graceful+timeout)/destroy_vm/get_vm_power_state. read_build_result_sink: live-tail the unix serial socket -> Generator with b'' heartbeat, close on poweroff. native_guest_{execute,read_file,write_file} over libvirt_qemu.qemuAgentCommand.
  >
  > DONE 2026-05-30: _vm/_serial/_guest implemented + 42 unit tests (XML synthesis, lifecycle, sink heartbeat/EOF, QGA exec-poll/file IO). KEY DESIGN: serial uses mode=connect (driver listens, qemu connects) because bind-mode qemu-owned socket (0775 libvirt-qemu) is NOT connectable non-root; listener pre-bound in create_vm under /tmp/tr-lv-serial-* (0755, daemon-traversable; NOT $TMPDIR which is 0700). LIVE-validated: debian boots under our domain XML, serial sink captured 2859 bytes of real boot console as uid 1000, start/state/destroy + teardown clean. QGA unit-tested; live-validated in first real build (raw image has no agent).

- [x] **BACKEND-1.A** · `feat` — storage — per-run dir pools + stream volume I/O
  _(blocks: BACKEND-1.B; blocked by: BACKEND-1.0; done: 2026-05-30)_

  > TDD against a faked LibvirtClient (no daemon in units). create_pool (defineXML dir pool at /var/lib/libvirt/images/tr-pool-<run8>-<pool> -> build -> create) / destroy_pool (sweep leftover vols -> destroy -> delete -> undefine). compose_volume_ref (<pool>/<name>). create_blank_volume, write_to_pool, upload_to_pool, download_from_pool (libvirt stream API virStorageVol.upload/download, both directions), resize_volume (vol resize, no qemu-img), delete_volume. qcow2 throughout, full-content (no backing chains).
  >
  > DONE 2026-05-30: _storage.py implemented; LibvirtClient gained not-found-tolerant lookup_pool/lookup_volume; 19 unit tests vs faithful in-memory fake; gates green; LIVE-validated through the real driver path on qemu:///system (pool define/build/create non-root, blank qcow2, ISO write+download roundtrip, byte-exact 1MiB upload+download, idempotent re-upload, resize grow, tolerant delete, sweep teardown removes the run dir).

- [x] **BACKEND-1.0** · `chore` — rip out libvirt skeleton + drop pyroute2 + concern-module skeleton
  _(blocks: BACKEND-1.A; done: 2026-05-30)_

  > Delete testrange/drivers/libvirt/* (driver.py _conn _naming _profile) and tests/unit/test_libvirt_*.py. Drop pyroute2 from the extra in pyproject.toml AND from _conn lazy imports (libvirt-python becomes the sole libvirt dep). Lay the fresh concern-module skeleton mirroring proxmox: _conn _naming _profile _net _storage _vm _guest _serial driver.py (each importable, SDK lazy). Keep registration + LibvirtHypervisor scheme marker + LibvirtProfile(uri + uplinks, NO backing_pool). Gates green; testrange describe works against a libvirt Plan.
  >
  > DONE 2026-05-30: pyroute2 dropped (pyproject extra + mypy overrides + _conn); backing_pool dropped (LibvirtConn/LibvirtProfile -> uri+uplinks only); concern modules _net/_storage/_vm/_guest/_serial laid as phase-tagged DriverError stubs; driver delegates; tests updated; connect.toml authored; all gates green (ruff/format/mypy/642 pytest); describe --profile libvirt-local renders the resolved binding.

- [x] **BACKEND-13** · `bugfix` — libvirt installer CDROM must be IDE on BIOS (ESXi weasel ks=cdrom scan)

  > Live-found 2026-06-02 (nested ESXi bring-up): ESXi weasel's ks=cdrom:/ks.cfg scan on i440fx/BIOS only enumerates an IDE optical unit; a sata(AHCI) installer CD fails 'cannot find kickstart file on cd-rom /ks.cfg' before touching disk (600s build timeout). Proven via direct qemu: same ISO sata=fail, IDE=installs. Fix: drivers/libvirt/_vm.py _cdrom_bus(firmware) -> ide for pc/BIOS, sata for q35/UEFI (q35 has no IDE). Case (lowercase ks.cfg) was a red herring. DONE.

### DOCS

- [x] **DOCS-14** · `EPIC` — docs/ audit remediation: fix drift, broken examples, missing pages _(done: 2026-06-08)_

  > Full-tree docs/ quality pass (2026-06-08), 48 tracked source files cross-checked against testrange/ HEAD. Prose clean (zero misspellings); findings were almost all DRIFT. All children DOCS-15..21 landed; `sphinx -b html` builds clean (only the pre-existing `_static` warning). Did NOT subsume DOCS-8 (still open).

- [x] **DOCS-15** · `bugfix` — P0: fix broken minimal-plan example in writing-a-plan.md _(done: 2026-06-08)_

  > writing-a-plan.md headline: `Plan(Hypervisor(...), name="hello")` → `Plan("hello", Hypervisor(...))` (real sig `Plan(name, *hypervisors)`); `PosixCred(pubkey=, privkey=, sudo=)` → `PosixCred("alice", ssh_key=_KEY, admin=True)` (real fields). Verified all minimal-plan imports resolve.

- [x] **DOCS-16** · `docs` — P1: sweep dead examples/capabilities.py references → tests/plans/ _(done: 2026-06-08)_

  > Repointed all live "run it" instructions in user/dev docs to `tests/plans/` (proxmox.md, esxi.md, drivers/index.md, networking-modes.md → generic/networking.py, writing-a-plan.md → generic/build_cache.py `fileserver`, connecting-to-a-backend.md → proxmox/devices.py, bugfixing.md, drivers.md). ADRs back-annotated not rewritten: 0019 addendum (cert gate → tests/plans, ADR-0028); 0017/0021/0025/0027 dead filenames dropped in-line. 0028's own refs left (it's the retiring ADR).

- [x] **DOCS-17** · `docs` — P1: fix dev/extending/* ABC signature drift _(done: 2026-06-08)_

  > builders.md: added abstract `os_disk_base`, `boot_media`/`prepare_boot_media` seam, `build_nic: BuildNic` kw + `bytes | None` on render methods, `sidecar_sha` now ABC (fixed the "not part of the ABC" claim), example overrides updated. devices.md: "none exist yet" → real Libvirt* concretes + private-mixin+MI precedent (verified `_LibvirtDisk`/`LibvirtOSDrive`). drivers.md: `credential=` present-tense + new `guest_gateway()` bullet + cert-suite vs tests/plans wording. communicators.md: `bind` gateway param + VMware-Tools wording.

- [x] **DOCS-18** · `docs` — P1: add missing user/drivers/libvirt.md reference-backend page _(done: 2026-06-08)_

  > Authored docs/user/drivers/libvirt.md parallel to proxmox.md/esxi.md (Connecting / Prereqs / Named uplinks / mgmt option-B / Certification). Wired into the drivers toctree + intro; index.md libvirt bullet now names it the certified reference backend (dropped stale "rebuild still wrapping up"). Sphinx confirms libvirt.html builds + is linked.

- [x] **DOCS-19** · `docs` — P2: internal inconsistencies + over-promising _(done: 2026-06-08)_

  > ADR-0028 Consequences reconciled with its REL-2/REL-10 amendments. proxmox.md de-overpromised (driver-primitives live-proven; full cert tracked under REL) + cert block fixed to real test functions/env vars (`TESTRANGE_PVE_HOST`-family). mgmt unified to "L2 presence" (matches ADR-0009; fixed networking-modes.md). ADR-0015 px_hello/`--connect` addendum. `mock` scheme removed from connect.toml.example + connecting-to-a-backend.md (test-only, unregistered in shipped CLI).

- [x] **DOCS-20** · `docs` — P2: addenda on stale "Accepted" ADRs describing deleted libvirt behavior _(done: 2026-06-08)_

  > Dated addenda added to ADR-0001 (createXMLFrom/backingStore → full-content qcow2 stream), ADR-0002 (ThreadPoolExecutor/--jobs per ADR-0023; pyroute2 dropped), ADR-0006 (`match: name: en*` removed, MAC-only). Decisions left intact; only the drifted mechanics annotated.

- [x] **DOCS-21** · `docs` — P3: config + cosmetic fixes _(done: 2026-06-08)_

  > conf.py: `copyright` now carries a year + `version`/`release` from `importlib.metadata` (ruff-clean). running-tests.md backend name → `tr-vm-<run_id[:8]>-web`. connecting-to-a-backend.md describe sample → ASCII `->` (matches cli.py:378). ADR-0027 "populate"→"populated"; ADR-0009 dead `managed_build_egress_findings` pointer annotated. NOTE: the alleged "ADR-0016 should be 0015" multi-profile attribution was VERIFIED CORRECT (cli.py:151) — left as-is; the illustrative `nginx_is_running` test left as a valid generic example.

- [x] **DOCS-13** · `chore` — delete `RESEARCH.md` (OBE) _(done: 2026-06-08)_

  > User directive 2026-06-08: the repo-root `RESEARCH.md` (open design-notes /
  > candidate-direction scratchpad, distinct from PLAN.md) is overcome-by-events —
  > everything in it has either landed in PLAN.md/ADRs or been abandoned. Deleted.
  > PLAN.md remains the living-design source of truth; TODO.md the work queue.

- [x] **DOCS-12** · `docs` — ADR-0021 amendment (ESXi inner) + ADR-002x libvirt device concretes
  _(done: 2026-06-02)_

  > ADR-0021 amended (ESXi inner backend: per-inner dispatch, GuestHypervisor.esxi, pyVmomi profile synthesis, build-on-L0 egress unblock, nested VMX). New ADR-0026 (libvirt-concrete device types: LibvirtOSDrive/DataDrive bus + LibvirtNetworkIface model, per-bus-prefix dev allocator). index.md updated. docs/user/drivers/esxi.md cert table + Nested-ESXi section. PLAN.md nested/device/builder sections. DONE 2026-06-02.

- [x] **ESXI-14** · `docs` — ESXi driver setup page + cert status
  _(blocked by: ESXI-13; done: 2026-06-02)_

  > ESXI-14 driver setup/cert-status docs (docs/user/drivers/esxi.md + index + PLAN). DONE 2026-06-02: profile shape, prereqs (license/qemu-img/uplink/VMware-Tools-plugins-all/SSH-forwarding), cert-status table (driver+pipeline live-certified; capabilities blocked on env egress).

- [x] **ESXI-ADR** · `docs` — ADR-002x — ESXi standalone-host driver scope & transports
  _(blocked by: ESXI-S1; done: 2026-06-01)_

  > ADR-0025 (docs/adr/0025-esxi-standalone-driver.md): standalone-host scope (std vSwitch+portgroup, no DVS/vCenter); transports (pyVmomi SOAP + datastore /folder HTTPS + qemu-img boundary); disk format A (qcow2 canonical, monolithicSparse ingest via CopyVirtualDisk, derived vmdk never content-addressed); VMware Tools guest-ops + CORE-60 cred seam; serial datastore-file sink; VMware MAC range; bios certified/uefi gated; licensing note. DONE 2026-06-01.

- [x] **DOCS-10** · `docs` — fix broken doc examples + architecture.md staleness
  _(done: 2026-06-01)_

  > DONE 2026-06-01. Fixed writing-a-plan.md (portable Hypervisor + --profile), extending/drivers.md register() sig, contributing.md + bugfixing.md gate command (not proxmox and not libvirt), architecture.md (driver_for/MockDriver/subprocess/flock-cleanup), PLAN.md stale mock import.

- [x] **DOCS-9** · `docs` — ADR-0021 reconciliation + nested example prereq + runtime comment
  _(done: 2026-06-01)_

  > Code-review findings. (1) ADR-0021 Consequences: narrow nested-KVM preflight claim to local-L0; note depth-2 now rejected loudly (not failed-late). (2) runtime OrchestratorHandle.nested comment: 'keyed by their plan name' -> host (guest) name. (3) examples/capabilities-nested.py prereqs: add 'sudo tools/build-sidecar-image/build.sh' step for parity with capabilities.py. Completed 2026-06-01.

- [x] **DOCS-7** · `docs` — ADR-0021 nested-virt recursion model + PLAN.md section
  _(blocks: CORE-38, CORE-39; done: 2026-05-31)_

  > DONE 2026-05-31. ADR-0021 written (docs/adr/0021-nested-virtualization.md), added to index.md; PLAN.md §3 note updated + new §23 added. Recursion model + 4 decisions captured.

- [x] **BUILD-11** · `docs` — ADR-0022 — sanction xorriso for PVE installer-ISO prep
  _(blocks: BUILD-2b; done: 2026-05-31)_

  > DONE 2026-05-31. ADR-0022 written at docs/adr/0022-xorriso-installer-iso-prep.md (Status: Accepted), added to docs/adr/index.md. Renumbered from 0021 to 0022 to avoid collision with feature/nested-virt's reserved ADR-0021. Sanctions xorriso in the single module testrange/builders/_proxmox_prepare.py per ADR-0001's escape hatch; ruff banned-api per-file allow + test_subprocess_ban.py whitelist applied with the module in BUILD-2b.

- [x] **DOCS-6** · `chore` — prune redundant examples (data_disk/network_modes/private_public/px_hello)
  _(done: 2026-05-31)_

  > Removed data_disk.py, network_modes.py, private_public.py, px_hello.py — covered by hello_world.py + capabilities.py + capabilities-px.py. **Done 2026-05-31.** Fixed references: test_cli.py (px_hello->capabilities-px, both ProxmoxHypervisor-pinned), docs (build-vs-run, networking-modes, connecting-to-a-backend, drivers/index, bugfixing, writing-a-plan), PLAN.md examples tree, test_cli_build_run comment. Left ADRs 0009/0014/0015 + CHANGELOG + PLAN past-rework narrative as historical records. Gates green (789).

- [x] **DOCS-5** · `docs` — CHANGELOG rename + examples cleanup
  _(done: 2026-05-31)_

  > TO_LOOK_AT mediums/lows. CHANGELOG [Unreleased] QGACommunicator/examples/qga.py -> NativeCommunicator/native_agent.py. examples/capabilities.py: strip section-divider + inline comments (feedback_examples_no_comments + no_section_marker_comments); reconcile TESTS count (30) with PLAN's 26/26. examples/network_modes.py + private_public.py: resolve commented-out mgmt=True/gated asserts vs current ADR-0009 status (libvirt implements mgmt).
  >
  > Completed 2026-05-31. capabilities.py TESTS=30 (PLAN updated from stale 26/26); mgmt gating left in place for the proxmox-pinned examples (mgmt is libvirt-only) with accurate comments.

- [x] **DOCS-4** · `docs` — README truth pass
  _(done: 2026-05-31)_

  > TO_LOOK_AT H-doc. README contradicts reality: fix to libvirt=certified reference impl, mock=in-memory unit-suite backend, proxmox=in-progress (not 'green end-to-end'). Replace MockHypervisor Quickstart/Plan-shape (moved to tests/; generic Hypervisor is the entry); add mandatory --profile (CORE-19 hard error). Drop TODO.md pointer.
  >
  > Completed 2026-05-31.

- [x] **DOCS-3** · `docs` — PLAN.md drift purge (ADR-0016 killed designs)
  _(done: 2026-05-31)_

  > TO_LOOK_AT H-doc. Delete §22 ResolvedBackend{build_switch} /  table -> ManagedBuildSwitch / managed_build_egress_findings (l.1396,1403,1417); §Build-egress block l.1253-1262; stale ORCH-9 l.1374-1376 (ADR-0017 fixed it, no-net enabled). §16 handled by CORE-29.
  >
  > Completed 2026-05-31.

- [x] **DOCS-2** · `docs` — out-of-band egress how-to (why + why-not-built + per-driver libvirt/proxmox)
  _(done: 2026-05-29)_

  > New page docs/user/drivers/out-of-band-egress.md: how to provision the host NAT bridge a named uplink ('egress') points at — TestRange attaches but never manufactures it (ADR-0016). Explain why you want it (build apt/pip + run-phase internet) and why it wasn't built (not uniform across backends, large backend surface, you-must-be-this-tall-to-ride, host already provides it). Per-driver: libvirt (default virbr0 / custom NAT net) + proxmox (reuse vmbr0 / NAT vmbr + masquerade / static sidecar eth1). Wire into drivers toctree. 2026-05-29.

- [x] **DOCS-1** · `docs` — ADR + docs/ consistency pass — purge obsolete claims
  _(done: 2026-05-27)_

  > Consistency/completeness/obsolescence pass over docs/ (ADR focus). DONE 2026-05-27 — all items landed; offline gate green (ruff, mypy --strict testrange tests, 595 pytest passed).
  >
  > **A. Obsolete vs code (fixed)**
  > 1. ADR-0012 serial build-result now reflected: builders.md step 2, architecture.md build phase + driver component list, writing-a-plan.md Tips.
  > 2. drivers.md cleanup dispatch -> build_* kinds (+ data_disk).
  > 3. writing-a-plan.md prose: dhcp/dns/nat moved to Sidecar (ADR-0013).
  > 4. ADR-0007 corrected: sidecar_sha is a CloudInit concrete extension, not on the Builder ABC.
  >
  > **B. Consistency / gate (fixed)**
  > 5. contributing.md + bugfixing.md gates -> 'mypy --strict testrange tests' + 'pytest -m "not proxmox"' (matches .pre-commit-config).
  > 6. bugfixing.md live-smoke reworded: required once a real backend is configured; not part of offline gate (mock serves no guest).
  > 7. running-tests.md exit codes -> full 0/1/2/3, links build-vs-run#exit-codes.
  >
  > **C. Minor (fixed)**
  > 8. ADR-0009 Consequences -> past tense (examples already comment out mgmt).
  > 9. builders/base.py config_hash docstring: 'post-install disk' -> 'built disk set'.
  > 10. ADR-0011 status unbolded ('Draft').
  > 11. builders.md cache-key guidance notes sidecar_sha.
  >
  > **Deliberately NOT changed:** ADR-0003/0008/0010 'install_*' references are point-in-time history (ADR-0010 is the rename record). docs/_build is stale but gitignored — rebuild with 'make -C docs html' before any publish (#12, informational).

### CI

- [x] **CI-9** · `chore` — drop `uv.lock` + its pre-commit gate (pyproject is authoritative) _(done: 2026-06-08)_

  > User directive 2026-06-08: TestRange is a library, not an app — `pyproject.toml`
  > dependency ranges are the single authoritative declaration of what installs;
  > a pinned lockfile adds drift-maintenance for no reproducible-deploy benefit.
  > Deleted `uv.lock` (was 268 KB, 65 resolved pkgs), removed the `uv-lock-check`
  > pre-commit hook (`uv lock --check`), and added `uv.lock` to `.gitignore` so a
  > stray `uv lock` can't re-introduce it. Gate set unchanged otherwise
  > (ruff/ruff-format/mypy --strict/pytest). The historical CI hook-add ticket
  > note at the `uv lock --check` line stays as the audit trail.

- [x] **CI-8b** · `ci` — pre-commit pytest excludes libvirt-marked tests (unit/MockDriver only)
  _(done: 2026-05-31)_

  > DONE 2026-05-31. .pre-commit-config.yaml pytest hook -> '-m "not proxmox and not libvirt"' (unit/MockDriver only, ~3s); repo CLAUDE.md gate wording aligned. Committed as befb8bb. Surfaced when a commit hook triggered a multi-minute live nested run during the squash.

- [x] **CI-8** · `test` — doubly-nested experiment — run, capture failure mode, log (no fix)
  _(blocked by: ORCH-20; done: 2026-05-31)_

  > DONE 2026-05-31. Ran depth-2 (outer->host-a->host-b->leaf) on real libvirt. FINDING: build recursion works to depth 2 (all disks build+cache on L0); bring-up breaks on L2 guest reachability — host-b leases on host-a's internal net (192.168.50.28), inner driver guest_gateway()=None so orchestrator can't SSH to it (SSH timeout). NOT the predicted build-upload seam. Fix = GuestGateway jump through host-a (ADR-0020), deferred. Findings in ADR-0021. Not patched per instruction. Fixture at /tmp/depth2.py (not committed).

- [x] **CI-5** · `chore` — commitizen commit-msg hook + config
  _(done: 2026-05-30)_

  > Add commitizen pre-commit hook (commit-msg stage),  in pyproject, default_install_hook_types: . Validates real commits only (auto-commit hook uses --no-verify, so wip(claude): checkpoints are exempt). Enables future cz bump for version+CHANGELOG.

- [x] **CI-6** · `chore` — project-rule pre-commit hooks
  _(done: 2026-05-30)_

  > Add high-fit hooks: pygrep-hooks python-check-blanket-type-ignore + python-check-blanket-noqa (enforces CLAUDE.md gate #2 mechanically), shellcheck on .claude/hooks/*.sh, local uv lock --check (lockfile drift), validate-pyproject.

- [x] **CI-7** · `chore` — widen ruff lint rule set
  _(done: 2026-05-30)_

  > Add S (flake8-bandit, replaces standalone bandit), PTH (pathlib, reinforces subprocess ban), SIM, C4, RET, T20 (no print) to  select. Fix findings properly (no blanket per-file-ignores to dodge); scope first.

- [x] **CI-4** · `chore` — add hygiene fixer pre-commit hooks
  _(done: 2026-05-30)_

  > Add file-hygiene fixers to .pre-commit-config.yaml from pre-commit-hooks v5.0.0: end-of-file-fixer, trailing-whitespace, mixed-line-ending (LF), plus check-case-conflict, check-executables-have-shebangs, debug-statements. Type-checker stays mypy --strict (ty deferred, still pre-1.0). Completed 2026-05-30.

- [x] **CI-1** · `chore` — SHA-stamp + version-track the sidecar image

  > **Done 2026-05-22.** Folded the `testrange-sidecar` image's content sha into `config_hash` (`cloudinit.py`; resolved once per probe via `fetch=False` in `build_phase._probe_all`) — every build boots on the sidecar's network, so a drifted sidecar now invalidates the built disk set instead of silently reusing stale artifacts. `build.sh` gained a `SIDECAR_VERSION`, a pinned Alpine `--branch`, an in-image `/etc/testrange-sidecar-version` marker, and a post-build `*.manifest.json` (version + content sha + provenance; gitignored). ADR-0007 + PLAN updated. Tests: `config_hash` moves on sidecar drift; a rebuilt sidecar forces a build-cache miss. Suite green (406), ruff + mypy clean.

- [x] **CI-3** · `ci` — pre-commit `language: system` hooks don't resolve the venv

  > **Done 2026-05-22.** Pinned the local `mypy` / `pytest` hooks to the venv interpreters (`entry: .venv/bin/mypy`, `entry: .venv/bin/pytest -m "not proxmox"`) so a `git commit` from a shell without `.venv` activated still resolves the real deps + stubs. Verified: both hooks pass with `.venv` off `PATH`; suite green.

- [x] **CI-2** · `ci` — pre-commit pytest hook filtered on a stale `libvirt` marker

  > **Done 2026-05-22.** `.pre-commit-config.yaml`'s pytest hook ran `pytest -m "not libvirt"`; the only registered marker is now `proxmox`. Changed to `-m "not proxmox"` (+ hook name). Verified: hook runs green, 404 tests pass.

- [x] **CHORE-CLEANUP** · `chore` — repo-wide TODO / PLAN / docs cleanup

  > **Done 2026-05-22.** Retired the libvirt-era audit (OBE under ADR-0008/0010); rewrote PLAN.md to current truth (MockHypervisor, build/run split, regenerated file tree); swept docs/README/docstrings (deleted `docs/user/drivers/libvirt.md`, `QGACommunicator`→`NativeCommunicator`, `install`→`build`, fixed the broken `libvirt` extra → `proxmox`). Suite green (404 tests), ruff + mypy clean.

### CACHE

- [x] **CACHE-7** · `bugfix` — durability docstring vs missing fsync; perm + manager lock-bypass
  _(done: 2026-06-01)_

  > DONE 2026-06-01. Added utils/fsutil.durable_replace (fsync data+dir before/after rename); local.py/http.py/state/store.py now power-loss durable. http fetch fchmod 0644. manager routes sidecar write through new LocalCache.write_materialized_sidecar (locked, merges aliases). Tests across cache+state suites.

- [x] **CACHE-6** · `bugfix` — fix reader-vs-delete race + stale concurrency docstrings
  _(done: 2026-06-01)_

  > ADR-0020 review: local.py docstrings denied concurrency the _write_lock provides; delete/purge unlinked .bin before .json (reader-vs-delete). FIX: docstrings corrected; delete/purge unlink .json first under _write_lock; mkstemp temps fchmod 0644 (were 0600). Tests: test_cache_local TestConcurrentDeleteAndPerms. Done 2026-06-01.

- [x] **CACHE-4** · `chore` — mkstemp-harden fixed-name .partial paths (optional)
  _(done: 2026-05-31)_

  > TO_LOOK_AT B1/B2. Per-write tempfile.mkstemp in cache/local.py instead of the fixed-name .download.partial.
  >
  > ABSORBED INTO ORCH-17 (#190) 2026-05-31: the in-process concurrency epic now makes concurrent cache adds a real intra-process race (parallel build-disk captures), so the mkstemp hardening for cache/local.py:149 lands as part of the ORCH-17 substrate. Will move to Done when ORCH-17 lands. (The earlier 'deferred into ORCH-10' note was under the single-instance contract; the .download.partial fix is now needed sooner for in-process parallelism.)
  >
  > --- COMPLETED 2026-06-01 (under ORCH-17 #190) ---
  > mkstemp now used for .download.partial, the .bin copy, and the .json sidecar; plus a write lock merges concurrent same-sha alias adds. Regression test in test_cache_local.py.

- [x] **CACHE-5** · `feat` — `cache purge` — delete all local cache entries
  _(done: 2026-05-31)_

  > Add a 'testrange cache purge' subcommand that deletes every entry from the local cache in one shot. LocalCache.purge() + CacheManager.purge() (local-only — HTTP tier has no listing protocol, same as list/del). CLI gated behind --yes (full-wipe footgun guard; no interactive prompt, stays scriptable) + --dry-run to preview; without --yes it's a no-op that prints the count. Requested 2026-05-31.

- [x] **CACHE-3** · `bugfix` — HTTP fetch integrity + atomic landing
  _(done: 2026-05-31)_

  > TO_LOOK_AT B3 + TLS sec-note. cache/http.py:138-149 — fetch to temp, verify sha256(tmp)==sha, then os.replace. Closes permanent-corrupt-cache-hit + most of verify=False note (content self-certifies). Decide: keep documented verify=False (VPN/mTLS) or opt-in verify.
  >
  > Completed 2026-05-31. verify=False kept (documented); content now self-certifies against the sha.

- [x] **CACHE-2** · `feat` — cache eviction (LRU + size cap)
  _(done: 2026-05-24)_

  > WON'T DO (2026-05-24): the local-cache eviction story is being absorbed into BACKEND-6/ADR-0011's content-addressed image cache (list_images/evict_image GC + cross-backend cascade-on-local-eviction); a standalone LRU+size-cap is not pursued separately. Original: Bound the local cache; evict least-recently-used entries past a size cap.

### COMM

- [x] **COMM-5** · `bugfix` — SSH write_file unverified + stderr drain wedge
  _(done: 2026-06-01)_

  > DONE 2026-06-01. ssh.write_file uses putfo(confirm=True) (fails loud on truncation); execute drains stderr on a helper thread (no shared-channel-window wedge). Tests: test_ssh_communicator.py TestSFTP + TestConcurrentDrain.

- [x] **COMM-4** · `bugfix` — SSH read-timeout wrap + deadlock + thread-safety doc
  _(done: 2026-05-31)_

  > TO_LOOK_AT medium. communicators/ssh.py:210-214 — socket.timeout from stdout.read() not wrapped in CommunicatorError; full stdout-before-stderr read can deadlock on chatty stderr; document cached-client thread-safety.
  >
  > Completed 2026-05-31.

- [x] **COMM-3** · `feat` — serial console communicator
  _(done: 2026-05-24)_

  > WON'T DO (2026-05-24): descoped during board grooming. Serial console is used for build-result signaling (ADR-0012), but a full interactive serial *communicator* for guest-ops on networkless/agentless guests is not being pursued. Original: For guests with no network and no native agent.

## Archive (13)

### CORE

- [x] **CORE-58** · `bugfix` — --verbose must keep rich log rendering (filter-split, not handler-swap)
  _(blocked by: CORE-54; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-57** · `docs` — rich output in PLAN + user docs
  _(blocked by: CORE-52, CORE-53, CORE-54, CORE-55, CORE-56; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-56** · `chore` — cli.py print() → Console
  _(blocks: CORE-57; blocked by: CORE-51; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-55** · `feat` — progress → rich.progress.Progress
  _(blocks: CORE-57; blocked by: CORE-51; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-54** · `feat` — live-tail → rich.live.Live
  _(blocks: CORE-57, CORE-58; blocked by: CORE-51; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-53** · `feat` — describe → rich Tree
  _(blocks: CORE-57; blocked by: CORE-51; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-52** · `feat` — logging stack → RichHandler
  _(blocks: CORE-57; blocked by: CORE-51; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-51** · `chore` — add rich core dep + ADR-0024 + shared Console
  _(blocks: CORE-52, CORE-53, CORE-54, CORE-55, CORE-56; done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

- [x] **CORE-49** · `feat` — EPIC — migrate all terminal output to rich
  _(done: 2026-06-01)_

  > REVERTED 2026-06-01 — rich migration abandoned at user request; reverted to stdlib output. Only CORE-50 (firehose isolation bugfix, stdlib) was kept. See CORE-49 for context.

### ORCH

- [x] **ORCH-DONE** · `feat` — Switch owns DHCP/DNS/mgmt; per-Switch dnsmasq sidecar

  > **Done v0.0.1–2026-05-16 (ADR-0009).** Sidecar replaces backend-native dnsmasq; lease discovery over the native guest agent.

- [x] **ORCH-DONE** · `feat` — builder readiness hook, stable MACs, snapshots, deterministic `config_hash`, cleanup on all exceptions

  > **Done v0.0.1–2026-05-16 (ADR-0006, ADR-0007).** See PLAN §16/§19 and the ADRs.

### NET

- [x] **NET-DONE** · `feat` — `NetworkIface.addr` sum type + `nic_idx`

  > **Done 2026-05-21 (ADR-0008).** `addr: DHCPAddr | StaticAddr | None`; `None` → unconfigured (`dhcp4: false`), `DHCPAddr()` → lease, `StaticAddr(...)` → static (explicit-wins resolution). `SSHCommunicator(nic_idx=)` selects the NIC by position. Fixed the `dhcp4:true`-for-no-DHCP-NIC bug.

### BACKEND

- [x] **BACKEND-DONE** · `feat` — multi-backend driver ABC

  > **Done 2026-05-21 (ADR-0008).** Driver owns the Switch (`create_switch`); `MockDriver` is the reference backend; `QGACommunicator` → `NativeCommunicator`; native-capability + pool-capacity preflight. The original libvirt driver was deleted (rebuild = BACKEND-1).
