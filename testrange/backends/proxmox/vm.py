"""Proxmox VE VM lifecycle.

First-cut implementation for **Debian-12-style cloud-init VMs reached
over SSH**.  Scope explicitly excludes:

- the QEMU guest-agent communicator (use ``communicator='ssh'``);
- the Windows installer flow;
- the outer cache (every run does a full install — PVE-template-as-cache
  is slated for a follow-up slice);
- the phase-2 seed ISO (the install-phase seed is also used at run
  start — cloud-init treats it as the same instance unless we rotate
  the instance-id, which we don't yet).

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

:meth:`build` runs once per orchestrator entry:

1. Resolve the VM's ``iso=`` URL to a local qcow2 in TestRange's
   outer cache (existing :func:`testrange.vms.images.resolve_image`).
2. Upload that qcow2 into PVE's ``local`` directory storage as
   ``import`` content.  This is the file an upcoming
   ``POST /qemu`` will pull from.
3. Render the cloud-init seed (NoCloud user-data + meta-data) via
   the existing :class:`~testrange.vms.builders.CloudInitBuilder`
   helpers, write it to a tempfile, upload as ``iso`` content.
4. Allocate a fresh VMID via ``GET /cluster/nextid``.
5. ``POST /nodes/{node}/qemu`` with ``scsi0=<storage>:0,import-from=
   local:import/<file>``, which makes PVE 7+ auto-import the qcow2
   into the target pool as a real VM disk in a single step.  No
   ``qm importdisk`` shell-out, no SSH access to the PVE node
   required.
6. Start the VMID and poll ``status/current`` until the guest powers
   itself off — that's cloud-init's ``power_state: poweroff``
   handshake, the same install-done signal the libvirt backend
   already keys on.

:meth:`start_run` re-starts the same VMID, polls for SSH
reachability on the configured static IP, and constructs an
:class:`~testrange.communication.ssh.SSHCommunicator`.

:meth:`shutdown` stops the VMID and DELETEs it so the orchestrator
exits clean.
"""

from __future__ import annotations

import socket
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    """PVE VMID once :meth:`build` has allocated it; ``None``
    until then."""

    _node: str | None
    """PVE node the VM landed on."""

    _import_filename: str | None
    """Filename in PVE's ``local:import/`` of the staged base
    qcow2 — kept so :meth:`shutdown` can clean it up."""

    _seed_filename: str | None
    """Filename in PVE's ``local:iso/`` of the cloud-init seed
    ISO — kept for cleanup."""

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
        self._node = None
        self._import_filename = None
        self._seed_filename = None

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
        """Provision a fresh VMID and run cloud-init to completion.

        Returns the allocated VMID as a stringified integer — the
        "backend-local ref" the orchestrator hands back to
        :meth:`start_run`.

        :raises VMBuildError: If any REST call fails or the resolved
            base image can't be uploaded.
        :raises VMTimeoutError: If the install-phase domain doesn't
            power off within :data:`_INSTALL_TIMEOUT_S`.
        """
        from testrange.vms.builders.cloud_init import (
            CloudInitBuilder,
            build_seed_iso_bytes,
        )
        from testrange.vms.images import resolve_image

        if not isinstance(self.builder, CloudInitBuilder):
            raise VMBuildError(
                f"VM {self.name!r}: ProxmoxVM v1 only supports "
                f"CloudInitBuilder; got "
                f"{type(self.builder).__name__}.  Pass a different "
                "builder= or use the libvirt backend in the meantime."
            )

        client = _proxmox_client(context)
        node = _proxmox_node(context)
        storage = _proxmox_storage(context)
        self._node = node

        # 1. Resolve the base qcow2 to a local file in the cache.
        base_path = resolve_image(self.iso, cache)
        _log.info(
            "vm %r: base image at %s (%.0f MiB)",
            self.name, base_path, base_path.stat().st_size / 1024 / 1024,
        )

        # 2. Upload to PVE ``local:import/``.
        import_filename = f"tr-{self.name}-{self._short_hash(base_path)}.qcow2"
        self._import_filename = import_filename
        self._upload_disk_image(client, node, "local", base_path, import_filename)

        # 3. Build + upload the cloud-init seed ISO.  Unlike the
        # libvirt backend (which uses a DHCP-enabled install network
        # for phase 1 and only configures static IPs in the phase-2
        # seed), the Proxmox SDN subnets we create don't run DHCP —
        # so the install seed has to carry the network-config too,
        # otherwise the install-phase guest comes up with no IP and
        # cloud-init can't reach package mirrors.  We synthesise
        # ``mac_ip_pairs`` from the orchestrator-derived
        # ``install_network_*`` plus this VM's own
        # :class:`VirtualNetworkRef` entries so the seed pins the
        # right IPs.
        mac_ip_pairs = self._build_install_mac_ip_pairs(
            context, install_network_name, install_network_mac,
        )
        config_hash = self.builder.cache_key(self)
        seed_bytes = build_seed_iso_bytes(
            meta_data=self.builder.install_meta_data(self, config_hash),
            user_data=self.builder.install_user_data(self),
            network_config=self.builder.run_network_config(mac_ip_pairs),
        )
        seed_filename = f"tr-{self.name}-seed.iso"
        self._seed_filename = seed_filename
        self._upload_iso_bytes(client, node, "local", seed_bytes, seed_filename)

        # 4. Allocate VMID.
        vmid = int(client.cluster.nextid.get())
        self._vmid = vmid
        _log.info("vm %r: allocated VMID %d", self.name, vmid)

        # 5. POST /qemu with the install-phase config.
        params = self._install_qemu_params(
            vmid=vmid,
            storage=storage,
            import_filename=import_filename,
            seed_filename=seed_filename,
            install_network_name=install_network_name,
            install_network_mac=install_network_mac,
        )
        # ``qemu.post`` with ``import-from`` is async: PVE returns a
        # UPID immediately and runs the import in a background task.
        # If we start the VM before that task finishes, PVE either
        # blocks the start until the import completes or fails with
        # "qcow2 still locked".  Wait for the create/import task to
        # report "stopped" (PVE's term for "done") before we start.
        try:
            create_upid = client.nodes(node).qemu.post(**params)
            with log_duration(
                _log, f"create + import-disk for VMID {vmid}",
            ):
                self._wait_for_task(
                    client, node, create_upid, timeout=600,
                )
        except Exception as exc:
            self._cleanup_partial_create(client, node)
            raise VMBuildError(
                f"VM {self.name!r}: failed to create VMID {vmid}: {exc}"
            ) from exc

        # 6. Start, wait for the start-task to complete (otherwise the
        # next status poll races against the still-pending start and
        # sees ``stopped`` from before the start kicked in), then poll
        # until the guest powers itself off via cloud-init's
        # ``power_state: poweroff``.
        try:
            start_upid = client.nodes(node).qemu(vmid).status.start.post()
            self._wait_for_task(client, node, start_upid, timeout=60)
            with log_duration(
                _log, f"VMID {vmid} install-phase poweroff"
            ):
                self._wait_for_status(
                    client, node, vmid, "stopped", _INSTALL_TIMEOUT_S,
                )
        except Exception as exc:
            self._cleanup_partial_create(client, node)
            raise VMBuildError(
                f"VM {self.name!r}: install phase failed: {exc}"
            ) from exc

        return str(vmid)

    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> None:
        """Boot the VMID, wait for SSH, attach the communicator.

        :param installed_disk: VMID returned by :meth:`build`,
            stringified.
        :param mac_ip_pairs: ``(mac, ip_with_cidr, gateway, dns)``
            per NIC.  At least one must carry a static IP — DHCP
            discovery is a future slice.
        """
        client = _proxmox_client(context)
        node = _proxmox_node(context)
        vmid = int(installed_disk)
        self._vmid = vmid
        self._node = node

        try:
            current = client.nodes(node).qemu(vmid).status.current.get()
            if current.get("status") != "running":
                upid = client.nodes(node).qemu(vmid).status.start.post()
                self._wait_for_task(client, node, upid, timeout=60)
        except Exception as exc:
            raise VMBuildError(
                f"VM {self.name!r}: failed to start VMID {vmid}: {exc}"
            ) from exc

        host = self._resolve_communicator_host(mac_ip_pairs)
        self._wait_for_ssh(host, _RUN_BOOT_TIMEOUT_S)
        self._communicator = self._make_communicator(mac_ip_pairs)
        # Force the SSH connection to establish so a busted communicator
        # surfaces here rather than at the first user-facing exec().
        self._communicator.wait_ready()
        _log.info(
            "vm %r: VMID %d ready; SSH communicator attached at %s",
            self.name, vmid, host,
        )

    def shutdown(self) -> None:
        """Stop the VMID and DELETE it.

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

        # Delete.
        try:
            client.nodes(node).qemu(vmid).delete()
        except Exception as exc:
            _log.warning("vm %r: delete VMID %d: %s", self.name, vmid, exc)

        # Best-effort: clean up the staged import file too.  Leaving it
        # behind is cheap (PVE keeps it as a reusable artifact in
        # local:import/) but uncontrolled growth bothers operators.
        if self._import_filename:
            try:
                client.nodes(node).storage("local").content(
                    f"local:import/{self._import_filename}",
                ).delete()
            except Exception as exc:
                _log.debug(
                    "vm %r: clean up import file %r: %s",
                    self.name, self._import_filename, exc,
                )
        if self._seed_filename:
            try:
                client.nodes(node).storage("local").content(
                    f"local:iso/{self._seed_filename}",
                ).delete()
            except Exception as exc:
                _log.debug(
                    "vm %r: clean up seed ISO %r: %s",
                    self.name, self._seed_filename, exc,
                )

        self._vmid = None
        self._node = None
        self._import_filename = None
        self._seed_filename = None

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
    ) -> dict[str, Any]:
        """Build the JSON body for ``POST /nodes/{node}/qemu``.

        The disk is created via ``import-from`` so PVE pulls the
        qcow2 out of ``local:import/`` into the target pool as a
        proper VM disk in one shot — no separate import step.
        """
        memory_mib = max(self._memory_mib(), 512)
        params: dict[str, Any] = {
            "vmid": vmid,
            "name": self.name[:63],  # PVE name length limit
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
            # VirtualNetworkRefs; fall back to the install network the
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
        :class:`VirtualNetworkRef` list and look the corresponding
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
        from testrange.devices import VirtualNetworkRef

        pairs: list[tuple[str, str, str, str]] = []
        nets: list[ProxmoxVirtualNetwork] = getattr(context, "_networks", [])
        net_by_logical_name = {n.name: n for n in nets}
        for ref in self._network_refs():
            if not isinstance(ref, VirtualNetworkRef):
                continue
            net = net_by_logical_name.get(ref.name)
            if net is None:
                continue
            mac = _mac_for_vm_network(self.name, ref.name)
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

    def _cleanup_partial_create(self, client: Any, node: str) -> None:
        """Best-effort tear-down of a half-created VMID + import file.

        Called from the rollback paths in :meth:`build`; never raises
        so the original error keeps propagating.
        """
        if self._vmid is None:
            return
        vmid = self._vmid
        try:
            client.nodes(node).qemu(vmid).status.stop.post()
        except Exception:
            pass
        try:
            client.nodes(node).qemu(vmid).delete()
        except Exception as exc:
            _log.warning(
                "vm %r: rollback delete VMID %d: %s",
                self.name, vmid, exc,
            )
        self._vmid = None


__all__ = ["ProxmoxVM"]
