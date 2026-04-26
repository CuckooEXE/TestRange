"""Proxmox VE VM lifecycle.

Implementation for **Debian-12-style cloud-init VMs**, backed by
PVE templates as the install-once-clone-many cache (symmetric with
the libvirt backend's qcow2-snapshot cache).  Two communicators
are supported:

* ``communicator='ssh'`` (default for the moment): the orchestrator
  waits for sshd on the VM's static IP, then attaches an
  :class:`SSHCommunicator`.  Requires the inner-VM IP to be
  routable from the test runner host.
* ``communicator='guest-agent'``: drives qemu-guest-agent over
  PVE's REST ``/agent/`` endpoints (see
  :class:`~testrange.backends.proxmox.guest_agent.ProxmoxGuestAgentCommunicator`).
  The inner-VM IP does not need to be reachable — useful for
  nested topologies whose SDN subnets aren't routed back to the
  outer host.

Scope explicitly excludes:

- the Windows installer flow.

Gotchas
-------

* **Reachability.** PVE SDN subnets live on a bridge inside the PVE
  node, so the test runner host needs an IP route through the PVE to
  reach VM IPs.  Without it, SSH attach will time out at 300s.  Add
  a route once per subnet on the test runner::

      sudo ip route add <subnet> via <pve-host>

  The orchestrator probes the gateway at ``__enter__`` and logs a
  clear WARNING with the exact command if the route is missing.

* **Root SSH on Debian cloud images.** Debian's stock sshd ships
  with ``PermitRootLogin prohibit-password`` and cloud-init defaults
  to ``disable_root: true``, so a ``Credential('root', ...)`` *only*
  in ``users=`` will fail SSH password auth.  Put a non-root user
  *first* in ``users=[...]`` so
  :meth:`AbstractVM._make_communicator` selects it
  (it picks ``users[0]`` when no credential carries an
  ``ssh_key=``)::

      vm = ProxmoxVM(
          ...,
          users=[
              Credential("debian", "...", sudo=True),  # picked for SSH
              Credential("root", "..."),
          ],
          communicator="ssh",
      )

  This is libvirt-vs-Proxmox-asymmetric: libvirt VMs default to the
  guest agent (no SSH involved), so the issue doesn't surface there.

The flow
--------

The cache is per-PVE: a "VM cache hit" means a previous run for the
same spec already ran and left a PVE template behind.  Hits skip
the install entirely (~minutes saved); misses install once and then
every subsequent run for the same spec is a clone.

:meth:`build` per orchestrator entry:

1. Compute the spec's ``cache_key`` (same hash the libvirt cache
   uses).  Look for a PVE template named
   ``tr-template-<config_hash[:12]>`` on the target node.

2. **Cache miss** — run the install path, then promote to template:

   a. Resolve the VM's ``iso=`` URL to a local qcow2 (existing
      :func:`testrange.vms.images.resolve_image`).
   b. Upload to PVE's ``local`` directory storage as ``import``.
   c. Render the install-phase cloud-init seed (NoCloud
      user-data + meta-data + run-phase network-config).  PVE
      SDN subnets don't run DHCP, so the install seed has to
      carry the static IP.
   d. Allocate the install VMID via ``GET /cluster/nextid``.
   e. ``POST /nodes/{node}/qemu`` with the install VMID's
      display name == the template name (so the post-install
      lookup picks it up directly), and
      ``scsi0=<storage>:0,import-from=local:import/<file>`` so
      PVE 7+ auto-imports the qcow2 in one shot.
   f. Start, poll ``status/current`` until poweroff (cloud-init's
      ``power_state: poweroff`` handshake — same signal libvirt
      uses).
   g. ``POST /nodes/{node}/qemu/{install_vmid}/template`` —
      promotes the install VMID to a template.  Irreversible:
      the VMID can no longer be started directly, only cloned.

3. **Always** — clone the template:

   a. Allocate a run VMID via ``GET /cluster/nextid``.
   b. ``POST /nodes/{node}/qemu/{template_vmid}/clone`` with
      ``newid=<run_vmid>`` and ``full=0`` (linked clone — fast
      on LVM-thin / ZFS / qcow2 file storage).
   c. Return the run VMID; orchestrator passes it to
      :meth:`start_run`.

Concurrency: a per-config-hash file lock around steps 2 + 3 (via
:func:`~testrange._concurrency.vm_build_lock`) prevents two test
processes from racing to install the same template.

:meth:`start_run` per orchestrator entry:

1. Build a phase-2 cloud-init seed with a **rotated instance-id**
   (cloud-init re-runs first-boot logic on the clone) and the
   run-phase ``mac_ip_pairs`` (static IP, gateway, DNS).
2. Upload the phase-2 seed to ``local:iso/`` with a run-id-
   suffixed filename so concurrent runs don't collide.
3. ``PUT /nodes/{node}/qemu/{run_vmid}/config`` to:
   - swap ``ide2`` from the install seed (inherited from the
     template) to the phase-2 seed;
   - replace ``net0`` with the run-phase NIC (new MAC, run
     bridge from the orchestrator's SDN setup).
4. Start the run VMID, wait for SSH on the static IP, attach the
   :class:`~testrange.communication.ssh.SSHCommunicator`.

:meth:`shutdown` stops + deletes the cloned run VMID and its
phase-2 seed.  The template + install seed survive — they're
shared cache state.

:meth:`testrange.backends.proxmox.ProxmoxOrchestrator.cleanup`
mirrors this asymmetry: run clones are reconstructed by name
(``tr-<vm[:10]>-<run_id[:8]>``) and deleted; templates
(``tr-template-*``) are explicitly preserved even if a name
pattern match somehow points at one.
"""

from __future__ import annotations

import socket
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._concurrency import vm_build_lock
from testrange._logging import get_logger, log_duration
from testrange.exceptions import VMBuildError, VMTimeoutError
from testrange.vms.base import AbstractVM

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.builders.base import Builder

_log = get_logger(__name__)

_PUBLIC_DNS = "1.1.1.1"
"""Cloudflare's public resolver, used by the install-phase seed's
network-config when an SDN subnet has ``internet=True`` but no
DNS service of its own — see
:meth:`ProxmoxVM._build_install_mac_ip_pairs` for the rationale."""

_INSTALL_TIMEOUT_S = 1800
"""Maximum wait for the install-phase domain to power itself off
after cloud-init finishes."""

_RUN_BOOT_TIMEOUT_S = 300
"""Maximum wait for SSH reachability on a freshly-started run-phase
domain."""

_STATUS_POLL_INTERVAL_S = 5

_SSH_POLL_INTERVAL_S = 3


def _proxmox_client(context: AbstractOrchestrator) -> Any:
    """Pull the proxmoxer client off a Proxmox orchestrator."""
    return context._client  # type: ignore[attr-defined]


def _proxmox_node(context: AbstractOrchestrator) -> str:
    return context._node  # type: ignore[attr-defined]


def _proxmox_storage(context: AbstractOrchestrator) -> str:
    return context._storage  # type: ignore[attr-defined]


class ProxmoxVM(AbstractVM):
    """Proxmox-VE implementation of :class:`AbstractVM`.

    See the module docstring for the v1 scope (Debian cloud-init +
    SSH only, no caching, static IPs).
    """

    _vmid: int | None
    """Run-phase VMID — the clone of the template.  Allocated in
    :meth:`build`; deleted in :meth:`shutdown`."""

    _template_vmid: int | None
    """VMID of the cached PVE template this run cloned from.  Survives
    :meth:`shutdown` because the template *is* the cache."""

    _node: str | None
    """PVE node the VM landed on."""

    _phase2_seed_filename: str | None
    """Filename in PVE's ``local:iso/`` of the run-phase
    cloud-init seed (rotated instance-id + run-phase network config).
    Per-run; deleted in :meth:`shutdown` along with the run VMID."""

    def __init__(
        self,
        name: str,
        iso: str,
        users: list[Credential],
        pkgs: list[AbstractPackage] | None = None,
        post_install_cmds: list[str] | None = None,
        devices: list[AbstractDevice] | None = None,
        builder: Builder | None = None,
        communicator: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            iso=iso,
            users=users,
            pkgs=pkgs,
            post_install_cmds=post_install_cmds,
            devices=devices,
            builder=builder,
            communicator=communicator,
        )
        self._vmid = None
        self._template_vmid = None
        self._node = None
        self._phase2_seed_filename = None

    # ------------------------------------------------------------------
    # build / start_run / shutdown
    # ------------------------------------------------------------------

    def build(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
    ) -> str:
        """Find or build a PVE template for this spec, clone it for
        the run phase, and return the cloned VMID.

        Cache key is :meth:`Builder.cache_key` (same hash the libvirt
        backend uses for its qcow2 cache).  On a hit (template already
        exists in PVE) the install path is skipped entirely; on a miss
        we run the install, ``qm template`` the install VMID, and
        clone from there.

        :returns: Stringified run-phase VMID — the "backend-local ref"
            the orchestrator hands back to :meth:`start_run`.
        :raises VMBuildError: If any REST call fails or the resolved
            base image can't be uploaded.
        :raises VMTimeoutError: If the install-phase domain doesn't
            power off within :data:`_INSTALL_TIMEOUT_S`.
        """
        from testrange.vms.builders.cloud_init import CloudInitBuilder

        if not isinstance(self.builder, CloudInitBuilder):
            raise VMBuildError(
                f"VM {self.name!r}: ProxmoxVM v1 only supports "
                f"CloudInitBuilder; got "
                f"{type(self.builder).__name__}.  Pass a different "
                "builder= or use the libvirt backend in the meantime."
            )

        client = _proxmox_client(context)
        node = _proxmox_node(context)
        self._node = node

        config_hash = self.builder.cache_key(self)
        template_name = _template_name(config_hash)

        # Concurrency: two test processes building the same spec at
        # the same time would both miss, both install, both try to
        # promote to a template with the same name.  The lock is
        # keyed by config_hash so concurrent runs of *different*
        # specs don't serialise on each other.
        with vm_build_lock(config_hash):
            template_vmid = _find_template(client, node, template_name)
            if template_vmid is None:
                _log.info(
                    "vm %r: PVE template cache MISS for %s — installing",
                    self.name, config_hash[:12],
                )
                # Sweep any orphans (half-promoted templates from
                # earlier crashed installs) so the new install
                # doesn't trip over a duplicate display name.
                _delete_orphan_templates(client, node, template_name)
                template_vmid = self._install_and_template(
                    context, cache, run,
                    install_network_name=install_network_name,
                    install_network_mac=install_network_mac,
                    template_name=template_name,
                    config_hash=config_hash,
                )
            else:
                _log.info(
                    "vm %r: PVE template cache HIT (%s, VMID %d) — "
                    "skipping install",
                    self.name, config_hash[:12], template_vmid,
                )

        self._template_vmid = template_vmid

        # Clone for the run phase.  Each test run gets its own VMID;
        # the template stays untouched.  Try a linked clone first
        # (seconds, requires LVM-thin / ZFS / qcow2 file storage);
        # fall back to full on storage that can't snapshot
        # (raw LVM, Ceph without snapshots, NFS).  Linked vs full
        # is a perf knob for the user, not a correctness one — both
        # produce a runnable clone.
        run_vmid = int(client.cluster.nextid.get())
        self._vmid = run_vmid
        clone_name = f"tr-{self.name[:10]}-{run.run_id[:8]}"
        try:
            try:
                clone_upid = client.nodes(node).qemu(template_vmid).clone.post(
                    newid=run_vmid,
                    name=clone_name,
                    full=0,
                )
                clone_mode = "linked"
            except Exception as linked_exc:
                _log.info(
                    "vm %r: linked clone of template %d failed (%s); "
                    "retrying as full clone",
                    self.name, template_vmid, linked_exc,
                )
                clone_upid = client.nodes(node).qemu(template_vmid).clone.post(
                    newid=run_vmid,
                    name=clone_name,
                    full=1,
                )
                clone_mode = "full"
            with log_duration(
                _log,
                f"{clone_mode} clone template {template_vmid} → "
                f"VMID {run_vmid}",
            ):
                self._wait_for_task(
                    client, node, clone_upid, timeout=600,
                )
        except Exception as exc:
            # Best-effort cleanup of a partially-cloned VMID so the
            # next run doesn't trip over an orphan.
            try:
                client.nodes(node).qemu(run_vmid).delete()
            except Exception:
                pass
            self._vmid = None
            raise VMBuildError(
                f"VM {self.name!r}: clone of template {template_vmid} "
                f"to VMID {run_vmid} failed: {exc}"
            ) from exc

        return str(run_vmid)

    def _install_and_template(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        *,
        install_network_name: str,
        install_network_mac: str,
        template_name: str,
        config_hash: str,
    ) -> int:
        """Run the full install flow and convert the result to a PVE
        template.  Returns the template's VMID.

        The install VM is created with ``name=template_name`` from
        the start so the find-template lookup picks it up after
        promotion (no rename round-trip).
        """
        from testrange.vms.builders.cloud_init import build_seed_iso_bytes
        from testrange.vms.images import resolve_image

        client = _proxmox_client(context)
        node = _proxmox_node(context)
        storage = _proxmox_storage(context)

        # 1. Resolve + upload the base qcow2.
        base_path = resolve_image(self.iso, cache)
        _log.info(
            "vm %r: base image at %s (%.0f MiB)",
            self.name, base_path, base_path.stat().st_size / 1024 / 1024,
        )
        import_filename = f"tr-{self.name}-{self._short_hash(base_path)}.qcow2"
        self._upload_disk_image(client, node, "local", base_path, import_filename)

        # 2. Build + upload the install-phase cloud-init seed.
        # Static IPs go in here too — the SDN install network doesn't
        # run DHCP, so the guest needs a network-config ISO to bring
        # eth0 up at all.  See _build_install_mac_ip_pairs for the
        # rationale.
        mac_ip_pairs = self._build_install_mac_ip_pairs(
            context, install_network_name, install_network_mac,
        )
        seed_bytes = build_seed_iso_bytes(
            meta_data=self.builder.install_meta_data(self, config_hash),
            user_data=self.builder.install_user_data(self),
            network_config=self.builder.run_network_config(mac_ip_pairs),
        )
        install_seed_filename = f"tr-template-{config_hash[:12]}-seed.iso"
        self._upload_iso_bytes(
            client, node, "local", seed_bytes, install_seed_filename,
        )

        # 3. Allocate the install VMID.
        install_vmid = int(client.cluster.nextid.get())
        _log.info(
            "vm %r: allocated install VMID %d (template name %r)",
            self.name, install_vmid, template_name,
        )

        # 4. Create the install VM.  Use ``template_name`` as the PVE
        # display name so the post-install ``qm template`` makes the
        # name lookup work directly.
        params = self._install_qemu_params(
            vmid=install_vmid,
            storage=storage,
            import_filename=import_filename,
            seed_filename=install_seed_filename,
            install_network_name=install_network_name,
            install_network_mac=install_network_mac,
            display_name=template_name,
        )
        try:
            create_upid = client.nodes(node).qemu.post(**params)
            with log_duration(
                _log, f"create + import-disk for install VMID {install_vmid}",
            ):
                self._wait_for_task(
                    client, node, create_upid, timeout=600,
                )
        except Exception as exc:
            self._best_effort_delete(client, node, install_vmid)
            raise VMBuildError(
                f"VM {self.name!r}: failed to create install VMID "
                f"{install_vmid}: {exc}"
            ) from exc

        # 5. Boot, wait for cloud-init's power_state: poweroff.
        try:
            start_upid = client.nodes(node).qemu(install_vmid).status.start.post()
            self._wait_for_task(client, node, start_upid, timeout=60)
            with log_duration(
                _log, f"install VMID {install_vmid} cloud-init poweroff"
            ):
                self._wait_for_status(
                    client, node, install_vmid, "stopped", _INSTALL_TIMEOUT_S,
                )
        except Exception as exc:
            self._best_effort_delete(client, node, install_vmid)
            raise VMBuildError(
                f"VM {self.name!r}: install phase failed: {exc}"
            ) from exc

        # 6. Promote to template (irreversible — the VMID can no longer
        # be started directly, only cloned).
        try:
            client.nodes(node).qemu(install_vmid).template.post()
            _log.info(
                "vm %r: promoted install VMID %d to template %r",
                self.name, install_vmid, template_name,
            )
        except Exception as exc:
            # Best-effort delete the half-promoted VMID — easier to
            # rebuild from scratch than diagnose a partial template.
            self._best_effort_delete(client, node, install_vmid)
            raise VMBuildError(
                f"VM {self.name!r}: promote VMID {install_vmid} to "
                f"template failed: {exc}"
            ) from exc

        return install_vmid

    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> None:
        """Configure the cloned VMID for this run, boot it, wait for
        SSH, attach the communicator.

        The clone inherited its NIC + cloud-init seed from the
        template.  Both need replacing for the run phase: the NIC so
        the VM lands on the run-phase SDN bridge with a fresh MAC,
        and the seed so cloud-init re-runs (with a new instance-id)
        and applies the run-phase static IP.  Without these, the
        clone keeps the install-network NIC + install-time IP and
        the SSH attach times out.

        :param installed_disk: VMID returned by :meth:`build`,
            stringified.  This is the cloned run-phase VMID, not
            the template's.
        :param network_entries: ``(backend_network_name, mac)`` per
            NIC in the order they should attach.
        :param mac_ip_pairs: ``(mac, ip_with_cidr, gateway, dns)``
            per NIC.  At least one must carry a static IP — DHCP
            discovery is a future slice.
        """
        from testrange.vms.builders.cloud_init import build_seed_iso_bytes

        client = _proxmox_client(context)
        node = _proxmox_node(context)
        vmid = int(installed_disk)
        self._vmid = vmid
        self._node = node

        # 1. Build the phase-2 cloud-init seed.  Rotated instance-id
        #    forces cloud-init to treat this as a new instance and
        #    re-apply network-config; the run-phase ``mac_ip_pairs``
        #    carry the static IPs the orchestrator allocated for
        #    this run.
        seed_bytes = build_seed_iso_bytes(
            meta_data=self.builder.run_meta_data(self, run.run_id),
            user_data=self.builder.run_user_data(self),
            network_config=self.builder.run_network_config(mac_ip_pairs),
        )
        # Filename includes the run_id so concurrent runs of the same
        # spec don't overwrite each other's phase-2 seed in the
        # shared ``local:iso/`` storage.
        phase2_filename = f"tr-{self.name[:10]}-{run.run_id[:8]}-seed.iso"
        self._phase2_seed_filename = phase2_filename
        try:
            self._upload_iso_bytes(
                client, node, "local", seed_bytes, phase2_filename,
            )
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: failed to upload phase-2 seed: {exc}"
            ) from exc

        # 2. Reconfigure the cloned VMID for the run phase:
        #    - swap CD-ROM at ide2 from the install seed to the
        #      phase-2 seed
        #    - replace net0 with the run-phase NIC (new MAC, run
        #      bridge).  ``network_entries[0]`` is the primary;
        #      ``ProxmoxVM`` only handles single-NIC VMs in v1, but
        #      future multi-NIC support extends here.
        try:
            config_updates: dict[str, Any] = {
                "ide2": f"local:iso/{phase2_filename},media=cdrom",
            }
            if network_entries:
                net_name, net_mac = network_entries[0]
                config_updates["net0"] = (
                    f"virtio={net_mac},bridge={net_name}"
                )
            client.nodes(node).qemu(vmid).config.put(**config_updates)
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: failed to reconfigure clone "
                f"VMID {vmid} for run phase: {exc}"
            ) from exc

        # 3. Start, wait for the start-task to complete.
        try:
            current = client.nodes(node).qemu(vmid).status.current.get()
            if current.get("status") != "running":
                upid = client.nodes(node).qemu(vmid).status.start.post()
                self._wait_for_task(client, node, upid, timeout=60)
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: failed to start VMID {vmid}: {exc}"
            ) from exc

        # 4. Construct the communicator.  Two paths:
        #
        #    * ``communicator='ssh'`` — wait for sshd on the
        #      configured static IP, then attach SSHCommunicator.
        #      Needs the inner-VM IP to be routable from the test
        #      runner host.
        #
        #    * ``communicator='guest-agent'`` — skip the SSH wait
        #      entirely; agent traffic hops through PVE's local
        #      virtio-serial channel so the inner VM's IP doesn't
        #      need to be reachable from the runner.  ``wait_ready``
        #      below polls ``/agent/ping`` until qemu-guest-agent
        #      inside the VM finishes starting.
        if self.communicator == "ssh":
            host = self._resolve_communicator_host(mac_ip_pairs)
            self._wait_for_ssh(host, _RUN_BOOT_TIMEOUT_S)
            self._communicator = self._make_communicator(mac_ip_pairs)
            self._communicator.wait_ready()
            _log.info(
                "vm %r: VMID %d ready; SSH communicator attached at %s",
                self.name, vmid, host,
            )
        else:
            self._communicator = self._make_communicator(mac_ip_pairs)
            self._communicator.wait_ready()
            _log.info(
                "vm %r: VMID %d ready; %s communicator attached",
                self.name, vmid, self.communicator,
            )

    def shutdown(self) -> None:
        """Stop and delete the per-run cloned VMID and its phase-2
        seed ISO.  The cached PVE template + install seed are left
        intact — they're shared cache state.

        Best-effort: errors are logged but never raised so this can
        be called from teardown paths.
        """
        if self._vmid is None or self._node is None:
            return
        # The orchestrator stashes the proxmoxer client on the VM via
        # set_client(); shutdown() can't take a context arg without
        # changing the AbstractVM contract.
        client = self._client
        if client is None:
            _log.warning(
                "vm %r: shutdown called with no client; VMID %d "
                "may leak",
                self.name, self._vmid,
            )
            return
        node = self._node
        vmid = self._vmid

        # Stop (forced — we're tearing down).
        try:
            client.nodes(node).qemu(vmid).status.stop.post()
            self._wait_for_status(client, node, vmid, "stopped", 60)
        except Exception as exc:
            _log.warning("vm %r: stop VMID %d: %s", self.name, vmid, exc)

        # Delete the clone (NOT the template — that's persistent
        # cache state).
        try:
            client.nodes(node).qemu(vmid).delete()
        except Exception as exc:
            _log.warning("vm %r: delete VMID %d: %s", self.name, vmid, exc)

        # Per-run phase-2 seed ISO.  The template's install seed
        # stays; only this run's seed gets removed.
        if self._phase2_seed_filename:
            try:
                client.nodes(node).storage("local").content(
                    f"local:iso/{self._phase2_seed_filename}",
                ).delete()
            except Exception as exc:
                _log.debug(
                    "vm %r: clean up phase-2 seed ISO %r: %s",
                    self.name, self._phase2_seed_filename, exc,
                )

        self._vmid = None
        self._node = None
        self._phase2_seed_filename = None
        # Note: ``_template_vmid`` is intentionally not cleared —
        # leaks no resources (templates persist across runs by
        # design) and lets debuggers see what we cloned from.

    # ------------------------------------------------------------------
    # Orchestrator hooks
    # ------------------------------------------------------------------

    _client: Any = None
    """proxmoxer handle, set by the orchestrator before :meth:`shutdown`
    so the VM can drive its own teardown without a context argument."""

    def set_client(self, client: Any) -> None:
        """Stash a proxmoxer client on the VM so :meth:`shutdown`
        can call REST without a context arg.  Called by the
        orchestrator on entry."""
        self._client = client

    def _make_guest_agent_communicator(self):
        """Construct the PVE-backed guest-agent communicator.

        Reached via
        :meth:`~testrange.vms.base.AbstractVM._make_communicator`
        when ``communicator='guest-agent'`` — the SSH branch lives
        in the ABC and works against any backend unchanged.

        The communicator drives qemu-guest-agent over PVE's REST
        ``/agent/`` endpoints, so the inner VM's IP does not need
        to be reachable from the test runner host — useful for
        nested topologies where SDN subnets aren't routed back to
        the outer host.
        """
        from testrange.backends.proxmox.guest_agent import (
            ProxmoxGuestAgentCommunicator,
        )
        assert self._client is not None, (
            f"VM {self.name!r}: no proxmoxer client set — "
            "set_client() must run before guest-agent attach."
        )
        assert self._node is not None, (
            f"VM {self.name!r}: node not resolved yet."
        )
        assert self._vmid is not None, (
            f"VM {self.name!r}: VMID not allocated yet."
        )
        return ProxmoxGuestAgentCommunicator(
            client=self._client,
            node=self._node,
            vmid=self._vmid,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _install_qemu_params(
        self,
        *,
        vmid: int,
        storage: str,
        import_filename: str,
        seed_filename: str,
        install_network_name: str,
        install_network_mac: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Build the JSON body for ``POST /nodes/{node}/qemu``.

        The disk is created via ``import-from`` so PVE pulls the
        qcow2 out of ``local:import/`` into the target pool as a
        proper VM disk in one shot — no separate import step.

        :param display_name: Override for the VM's PVE display name.
            When the install VMID will be promoted to a template,
            pass the template name here so the post-install
            find-template lookup succeeds without a separate rename.
        """
        memory_mib = max(self._memory_mib(), 512)
        params: dict[str, Any] = {
            "vmid": vmid,
            "name": (display_name or self.name)[:63],  # PVE name length limit
            "ostype": "l26",  # Linux 2.6+ — covers Debian 12
            "cores": self._vcpu_count(),
            "memory": memory_mib,
            "scsihw": "virtio-scsi-pci",
            "scsi0": (
                f"{storage}:0,import-from=local:import/{import_filename}"
            ),
            "ide2": f"local:iso/{seed_filename},media=cdrom",
            "boot": "order=scsi0",
            "serial0": "socket",
            "vga": "serial0",
            # NIC: prefer a configured network from the VM's
            # vNICs; fall back to the install network the
            # orchestrator passed in.  PVE SDN vnet names are also the
            # bridge names, so ``bridge=<vnet>`` works directly.
            "net0": (
                f"virtio={install_network_mac},bridge={install_network_name}"
            ),
            # Tell PVE the guest is expected to run qemu-guest-agent.
            # We don't use it for communication yet (SSH only) but
            # flipping it on is free and surfaces guest IPs in the
            # web UI for debugging.
            "agent": "enabled=1",
        }
        return params

    def _build_install_mac_ip_pairs(
        self,
        context: AbstractOrchestrator,
        install_network_name: str,
        install_network_mac: str,
    ) -> list[tuple[str, str, str, str]]:
        """Reconstruct ``(mac, ip_with_cidr, gateway, dns)`` per NIC
        for the install-phase seed's network-config.

        The orchestrator passes us the *first* NIC's network + MAC as
        ``install_network_*``; we walk this VM's
        :class:`vNIC` list and look the corresponding
        :class:`ProxmoxVirtualNetwork` up on the orchestrator to
        derive the rest.

        DNS notes
        ---------

        Unlike the libvirt backend (where each NAT network's gateway
        runs dnsmasq), PVE SDN subnets *don't* ship a DNS resolver
        unless the user explicitly configures DHCP+DNS at the SDN
        layer.  We fall back to a public resolver
        (:data:`_PUBLIC_DNS`) for ``internet=True`` networks so apt
        / dnf can resolve package mirrors during the install phase.
        For isolated networks we send no DNS — the install will fail
        if any apt repository can't be reached, which is the right
        behaviour (user opted out of internet).
        """
        from testrange.backends.proxmox.network import (
            ProxmoxVirtualNetwork,
            _mac_for_vm_network,
        )
        from testrange.devices import vNIC

        pairs: list[tuple[str, str, str, str]] = []
        nets: list[ProxmoxVirtualNetwork] = getattr(context, "_networks", [])
        net_by_logical_name = {n.name: n for n in nets}
        for ref in self._network_refs():
            if not isinstance(ref, vNIC):
                continue
            net = net_by_logical_name.get(ref.ref)
            if net is None:
                continue
            mac = _mac_for_vm_network(self.name, ref.ref)
            cidr = f"{ref.ip}/{net.prefix_len}" if ref.ip else ""
            gateway = net.gateway_ip if net.internet else ""
            dns = _PUBLIC_DNS if net.internet else ""
            pairs.append((mac, cidr, gateway, dns))
        return pairs

    def _vcpu_count(self) -> int:
        from testrange.devices import vCPU
        vcpus = [d for d in self.devices if isinstance(d, vCPU)]
        return vcpus[0].count if vcpus else 2

    def _memory_mib(self) -> int:
        from testrange.devices import Memory
        mems = [d for d in self.devices if isinstance(d, Memory)]
        if not mems:
            return 2048
        # Memory.kib gives kibibytes; convert to mebibytes for PVE.
        return mems[0].kib // 1024

    @staticmethod
    def _short_hash(path: Path) -> str:
        """Stable per-path 8-char hash so concurrent runs against
        the same base image share the import file."""
        import hashlib
        return hashlib.sha256(str(path).encode()).hexdigest()[:8]

    def _upload_disk_image(
        self,
        client: Any,
        node: str,
        storage: str,
        local_path: Path,
        target_filename: str,
    ) -> None:
        """Upload *local_path* to PVE storage as ``import`` content.

        Idempotent: if the target filename already exists on PVE
        storage, we skip the upload.  The two-tier cache ends up
        being "outer cache → PVE-side cache" — the qcow2 is staged
        once per content-hash and reused across every
        :class:`ProxmoxVM` build that resolves to the same base.
        """
        try:
            existing = client.nodes(node).storage(storage).content.get(
                content="import",
            )
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: cannot list {storage}:import on "
                f"node {node!r}: {exc}"
            ) from exc

        if any(
            entry.get("volid") == f"{storage}:import/{target_filename}"
            for entry in existing
        ):
            _log.info(
                "vm %r: import file %r already on PVE; reusing",
                self.name, target_filename,
            )
            return

        _log.info(
            "vm %r: uploading %s → %s:import/%s",
            self.name, local_path.name, storage, target_filename,
        )
        try:
            self._upload_with_target_name(
                client, node, storage,
                source_path=local_path,
                target_filename=target_filename,
                content="import",
            )
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: failed to upload {local_path} "
                f"to {storage}:import: {exc}"
            ) from exc

    def _upload_iso_bytes(
        self,
        client: Any,
        node: str,
        storage: str,
        data: bytes,
        target_filename: str,
    ) -> None:
        """Upload an in-memory bytes blob to PVE storage as ``iso`` content."""
        with tempfile.TemporaryDirectory(prefix="tr-pve-upload-") as tmpdir:
            tmp_path = Path(tmpdir) / target_filename
            tmp_path.write_bytes(data)
            try:
                self._upload_with_target_name(
                    client, node, storage,
                    source_path=tmp_path,
                    target_filename=target_filename,
                    content="iso",
                )
            except Exception as exc:
                raise VMBuildError(
                    f"VM {self.name!r}: failed to upload seed ISO to "
                    f"{storage}:iso: {exc}"
                ) from exc

    @staticmethod
    def _upload_with_target_name(
        client: Any,
        node: str,
        storage: str,
        *,
        source_path: Path,
        target_filename: str,
        content: str,
    ) -> None:
        """Upload *source_path* under *target_filename* on PVE storage.

        proxmoxer's auto-multipart only triggers on bare file handles
        (``isinstance(v, io.IOBase)``); the remote filename comes
        from ``requests.utils.guess_filename(v)`` which reads the
        file's ``.name`` attribute.  Tuple-style ``(filename, fh)``
        values are silently downgraded to data fields and PVE
        rejects them.

        We work around it by ensuring the file at ``source_path``
        already has the right name — symlink into a tempdir if the
        real file has a different name (e.g. content-hashed cache
        files), then pass the symlink as a bare file handle.
        """
        if source_path.name == target_filename:
            with open(source_path, "rb") as fh, log_duration(
                _log, f"upload {target_filename} ({content})"
            ):
                client.nodes(node).storage(storage).upload.create(
                    content=content,
                    filename=fh,
                )
            return

        with tempfile.TemporaryDirectory(prefix="tr-pve-upload-") as tmpdir:
            staged = Path(tmpdir) / target_filename
            staged.symlink_to(source_path.resolve())
            with open(staged, "rb") as fh, log_duration(
                _log, f"upload {target_filename} ({content})"
            ):
                client.nodes(node).storage(storage).upload.create(
                    content=content,
                    filename=fh,
                )

    @staticmethod
    def _wait_for_status(
        client: Any,
        node: str,
        vmid: int,
        target: str,
        timeout: float,
    ) -> None:
        """Poll ``/qemu/{vmid}/status/current`` until ``status==target``."""
        deadline = time.monotonic() + timeout
        last_status: str | None = None
        while time.monotonic() < deadline:
            try:
                current = client.nodes(node).qemu(vmid).status.current.get()
                last_status = current.get("status")
                if last_status == target:
                    return
            except Exception as exc:
                _log.debug(
                    "VMID %d status poll error (will retry): %s",
                    vmid, exc,
                )
            time.sleep(_STATUS_POLL_INTERVAL_S)
        raise VMTimeoutError(
            f"VMID {vmid} did not reach status={target!r} within "
            f"{timeout:.0f}s (last seen: {last_status!r})"
        )

    @staticmethod
    def _wait_for_task(
        client: Any,
        node: str,
        upid: str,
        timeout: float,
    ) -> None:
        """Wait for a PVE task (UPID) to finish.

        ``POST /qemu/{vmid}/status/start`` and several other
        write endpoints return a UPID immediately and run the
        actual work asynchronously.  Polling
        ``/qemu/{vmid}/status/current`` straight after the POST
        races: PVE may still report the previous (``stopped``)
        status until the start task transitions the VM.

        :raises VMBuildError: If the task itself reports a non-OK
            exit status.
        :raises VMTimeoutError: If the task hasn't finished within
            ``timeout`` seconds.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                task = client.nodes(node).tasks(upid).status.get()
            except Exception as exc:
                _log.debug("task %s status poll error: %s", upid, exc)
                time.sleep(1)
                continue
            if task.get("status") == "stopped":
                exitstatus = task.get("exitstatus", "")
                if exitstatus and exitstatus != "OK":
                    raise VMBuildError(
                        f"PVE task {upid} failed: {exitstatus}"
                    )
                return
            time.sleep(1)
        raise VMTimeoutError(
            f"PVE task {upid} didn't finish within {timeout:.0f}s"
        )

    @staticmethod
    def _wait_for_ssh(host: str, timeout: float) -> None:
        """Poll TCP 22 on *host* until it accepts a connection."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, 22), timeout=2):
                    return
            except OSError:
                time.sleep(_SSH_POLL_INTERVAL_S)
        raise VMTimeoutError(
            f"SSH (TCP 22) on {host!r} not reachable within {timeout:.0f}s"
        )

    @staticmethod
    def _best_effort_delete(client: Any, node: str, vmid: int) -> None:
        """Stop + delete *vmid*, swallowing every error.

        Used in the install-phase rollback paths so the original
        :class:`VMBuildError` reaches the caller without being
        masked by a teardown exception.
        """
        try:
            client.nodes(node).qemu(vmid).status.stop.post()
        except Exception:
            pass
        try:
            client.nodes(node).qemu(vmid).delete()
        except Exception as exc:
            _log.warning(
                "rollback delete VMID %d: %s", vmid, exc,
            )


# ---------------------------------------------------------------------------
# Module-level helpers for the PVE template cache.
#
# Templates persist across runs by design — they're the cache.  These
# helpers find an existing template by name and compute the canonical
# template name from a config hash.
# ---------------------------------------------------------------------------


_TEMPLATE_NAME_PREFIX = "tr-template-"
"""Prefix that marks a VM as a TestRange-managed PVE template.

Used by :func:`_template_name` to compute the canonical name from a
config hash, and by ``ProxmoxOrchestrator.cleanup`` to decide which
VMIDs are template cache (preserve) versus per-run clones (delete)."""


def _template_name(config_hash: str) -> str:
    """Return the canonical PVE template name for *config_hash*.

    Trims the hash to 12 chars to leave headroom under PVE's 63-char
    name limit while keeping collision risk negligible (12 hex chars
    = 48 bits)."""
    return f"{_TEMPLATE_NAME_PREFIX}{config_hash[:12]}"


def _find_template(client: Any, node: str, name: str) -> int | None:
    """Look up an existing PVE template by display name.

    Returns the VMID if a VM with display name == *name* exists on
    *node* and has ``template: 1`` in its config.  Returns ``None``
    on miss.  A name match without the template flag is treated as
    a miss (probably a half-promoted install that died); use
    :func:`_delete_orphan_templates` before rebuilding so the install
    doesn't trip over a duplicate name.
    """
    try:
        vms = client.nodes(node).qemu.get()
    except Exception as exc:
        # If we can't list VMs, treat as miss and let the install
        # path fail loudly with a more useful error.
        _log.debug("template lookup: list-VMs failed: %s", exc)
        return None
    for vm in vms or []:
        if vm.get("name") != name:
            continue
        if not vm.get("template"):
            _log.warning(
                "template lookup: VMID %s has matching name %r but is "
                "not a template (template flag not set) — treating as "
                "cache miss; install will overwrite",
                vm.get("vmid"), name,
            )
            return None
        return int(vm["vmid"])
    return None


def _delete_orphan_templates(client: Any, node: str, name: str) -> int:
    """Delete every VMID on *node* with display name *name* but no
    ``template: 1`` flag — the footprint of an install that died
    between create + promote.

    Returns the number of orphans deleted.  Best-effort: per-VMID
    failures are logged but never raise so the install can proceed.
    """
    try:
        vms = client.nodes(node).qemu.get()
    except Exception as exc:
        _log.debug("orphan sweep: list-VMs failed: %s", exc)
        return 0
    deleted = 0
    for vm in vms or []:
        if vm.get("name") != name or vm.get("template"):
            continue
        vmid = int(vm["vmid"])
        _log.warning(
            "orphan sweep: deleting half-promoted VMID %d (%r)",
            vmid, name,
        )
        try:
            # Stop first in case the orphan is somehow still running
            # (interrupt during install but before poweroff).
            try:
                client.nodes(node).qemu(vmid).status.stop.post()
            except Exception:
                pass
            client.nodes(node).qemu(vmid).delete()
            deleted += 1
        except Exception as exc:
            _log.warning(
                "orphan sweep: delete VMID %d failed: %s — install may "
                "fail with duplicate-name; clean up manually",
                vmid, exc,
            )
    return deleted


__all__ = ["ProxmoxVM"]
