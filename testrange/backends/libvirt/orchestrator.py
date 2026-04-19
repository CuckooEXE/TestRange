"""Orchestrator — the central coordinator for a test run.

The :class:`Orchestrator` owns the libvirt connection and drives the full
lifecycle of networks and VMs:

1. Open a libvirt connection (local or remote via SSH)
2. Create an ephemeral NAT network for the install phase
3. For each VM: resolve image → build (or hit cache) → create overlay
4. Create test networks with DNS/DHCP entries for all VMs
5. Start each VM, wait for its guest agent to respond
6. Expose VMs via :attr:`vms` dict for use in test functions
7. On exit: destroy VMs, destroy networks, clean up run directory

The orchestrator is designed to be used as a context manager::

    with Orchestrator(networks=[...], vms=[...]) as orch:
        do_test(orch)

It is also used directly by :class:`~testrange.test.Test`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import libvirt

from testrange._concurrency import install_subnet_lock
from testrange._logging import get_logger, log_duration
from testrange._run import RunDir
from testrange.backends.libvirt.network import (
    VirtualNetwork,
    _mac_for_vm_network,
)
from testrange.cache import CacheManager
from testrange.exceptions import NetworkError, OrchestratorError
from testrange.orchestrator_base import AbstractOrchestrator

_log = get_logger(__name__)

if TYPE_CHECKING:
    from testrange.backends.libvirt.vm import VM

_INSTALL_SUBNET_POOL = tuple(f"192.168.{o}.0/24" for o in range(240, 255))
"""Candidate subnets for the ephemeral install-phase network.

The orchestrator picks the first one not already claimed by another
libvirt network at start-up time, so stale state from a crashed prior
run (or an unrelated libvirt network) does not wedge new runs.
"""

class Orchestrator(AbstractOrchestrator):
    """libvirt / KVM / QEMU implementation of
    :class:`~testrange.orchestrator_base.AbstractOrchestrator`.

    Coordinates networks and VMs for a single test run.

    :param host: The libvirt host to connect to.  Use ``'localhost'`` or
        ``'127.0.0.1'`` for the local system, or a remote hostname /
        ``user@host`` string for an SSH-tunnelled connection.  You may also
        pass a full libvirt URI (e.g. ``'qemu+ssh://user@host/system'``).
    :param networks: Virtual networks to create for this test.
    :param vms: Virtual machines to provision and start.
    :param cache_root: Override the default cache directory.

    Example::

        orchestrator = Orchestrator(
            host="localhost",
            networks=[VirtualNetwork("TestNet", "10.1.0.0/24", internet=True)],
            vms=[VM("server", "debian-12", users=[...], devices=[vCPU(2)])],
        )
        with orchestrator as orch:
            result = orch.vms["server"].exec(["uname", "-r"])
    """

    _host: str
    """libvirt connection target: ``'localhost'``, a hostname, or a full URI."""

    _networks: list[VirtualNetwork]
    """Test networks to create for this run."""

    _vm_list: list[VM]
    """VM specifications to provision."""

    _cache: CacheManager
    """Disk-image cache manager used for this run."""

    vms: dict[str, VM]
    """Running VMs keyed by name; populated after :meth:`__enter__`."""

    _conn: libvirt.virConnect | None
    """Active libvirt connection; ``None`` before :meth:`__enter__`."""

    _run: RunDir | None
    """Scratch directory for the current test run; ``None`` outside a run."""

    _install_network: VirtualNetwork | None
    """Ephemeral NAT network used during the install phase; ``None`` outside install."""

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[VirtualNetwork] | None = None,
        vms: Sequence[VM] | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self._host = host
        self._networks = list(networks) if networks else []
        self._vm_list = list(vms) if vms else []
        self._cache = CacheManager(root=cache_root) if cache_root else CacheManager()

        # Populated after __enter__
        self.vms = {}
        self._conn = None
        self._run = None
        self._install_network = None

    @classmethod
    def backend_type(cls) -> str:
        """Return ``"libvirt"``."""
        return "libvirt"

    def _build_uri(self) -> str:
        """Translate :attr:`host` into a libvirt connection URI.

        :returns: A libvirt URI string.
        """
        if self._host in ("localhost", "127.0.0.1", "::1"):
            return "qemu:///system"
        if "://" in self._host:
            # Already a full URI
            return self._host
        return f"qemu+ssh://{self._host}/system"

    def __enter__(self) -> Orchestrator:
        """Open libvirt connection, provision all networks and VMs.

        :returns: ``self``, with :attr:`vms` fully populated.
        :raises OrchestratorError: On libvirt connection failure.
        :raises NetworkError: If a network cannot be created.
        :raises VMBuildError: If a VM install phase fails.
        """
        uri = self._build_uri()
        try:
            self._conn = libvirt.open(uri)
        except libvirt.libvirtError as exc:
            raise OrchestratorError(
                f"Cannot connect to libvirt at {uri!r}: {exc}"
            ) from exc

        self._run = RunDir()

        try:
            self._provision(self._run)
        except BaseException:
            # Best-effort cleanup on partial setup.  ``BaseException`` —
            # not ``Exception`` — so Ctrl+C during a long install wait
            # still runs teardown before the interrupt propagates.
            self._teardown()
            raise

        return self

    def __exit__(self, *_: object) -> None:
        """Destroy all VMs and networks and clean up the run directory.

        Any exception raised during teardown is swallowed so it cannot mask
        the exception that caused the ``with`` block to exit.  Returns
        ``None`` so the original exception (if any) still propagates.
        """
        try:
            self._teardown()
        except Exception:
            # _teardown() is already defensively coded to never raise; this
            # is a belt-and-braces guard against future regressions.
            pass

    def _provision(self, run: RunDir) -> None:
        """Internal provisioning sequence.

        :param run: Scratch dir for this test run.
        """
        assert self._conn is not None

        _log.info(
            "provisioning run %s: %d VM(s), %d network(s)",
            run.run_id[:8],
            len(self._vm_list),
            len(self._networks),
        )

        # Builders whose needs_install_phase() returns False (NoOp) skip
        # the install domain entirely — they hand back a ready disk.
        install_free_vms = [
            vm for vm in self._vm_list
            if not vm.builder.needs_install_phase()
        ]
        needs_install_network = any(
            vm.builder.needs_install_phase() for vm in self._vm_list
        )

        if install_free_vms:
            _log.info(
                "VMs skipping install phase: %s",
                [vm.name for vm in install_free_vms],
            )

        if needs_install_network:
            # 0. Remove any install networks left over from prior crashed
            # runs — they would collide with our new one on the
            # 192.168.24x.0/24 subnet.
            self._cleanup_stale_install_networks()

            # 1. Create and start the install-phase NAT network.
            #
            # Subnet picking is a check-then-act race: two concurrent runs
            # would both see the same pool slot free.  The file lock
            # serialises the pick + define + start across runs in this
            # process and across processes, so only the bring-up is
            # sequentialised — the rest of provisioning runs in parallel.
            with install_subnet_lock():
                self._install_network = self._create_install_network(run.run_id)
                with log_duration(
                    _log,
                    f"start install network "
                    f"{self._install_network.backend_name()!r}",
                ):
                    self._install_network.start(self)

        # 2. Build (or retrieve from cache) installed disk images
        installed_disks: dict[str, Path] = {}
        with log_duration(_log, f"install phase for {len(self._vm_list)} VM(s)"):
            for vm in self._vm_list:
                if vm.builder.needs_install_phase():
                    assert self._install_network is not None
                    install_net_name = self._install_network.backend_name()
                    install_mac = _mac_for_vm_network(vm.name, "__install__")
                else:
                    # Install-free VMs (NoOp / BYOI) don't need a NIC on
                    # the install network; pass empty strings through to
                    # keep build()'s signature stable.
                    install_net_name = ""
                    install_mac = ""
                with log_duration(_log, f"build VM {vm.name!r}"):
                    installed_disks[vm.name] = vm.build(
                        context=self,
                        cache=self._cache,
                        run=run,
                        install_network_name=install_net_name,
                        install_network_mac=install_mac,
                    )

        # 3. Stop the install network (VMs are off at this point)
        if self._install_network is not None:
            _log.debug("stopping install network")
            self._install_network.stop(self)
            self._install_network = None

        # 4. Register VMs with their test networks and assign IPs
        self._setup_test_networks(run.run_id)

        # 5. Start test networks
        for net in self._networks:
            with log_duration(_log, f"start test network {net.name!r}"):
                net.start(self)

        # 6. Start each VM and wait for guest agent
        with log_duration(_log, f"boot {len(self._vm_list)} VM(s) to ready"):
            for vm in self._vm_list:
                network_entries, mac_ip_pairs = self._build_nic_entries(vm)
                with log_duration(_log, f"start VM {vm.name!r}"):
                    vm.start_run(
                        context=self,
                        run=run,
                        installed_disk=installed_disks[vm.name],
                        network_entries=network_entries,
                        mac_ip_pairs=mac_ip_pairs,
                    )
                self.vms[vm.name] = vm
        _log.info("all VMs ready; handing off to test function")

    def _cleanup_stale_install_networks(self) -> None:
        """Undefine any *inactive* install networks left by crashed runs.

        Install networks all have the ``tr-instal-`` name prefix (the
        15-char libvirt limit truncates the full ``tr-install-<id>``).
        Any that are **not currently active** are necessarily leftovers
        from a crash — active ones belong to a peer run on the same
        host and must not be touched.

        Without this cleanup, a stale definition from a prior crash
        would keep its subnet reserved on next startup.
        """
        assert self._conn is not None
        try:
            defined = self._conn.listDefinedNetworks() or []
        except libvirt.libvirtError:
            return

        # ``listDefinedNetworks()`` only returns *inactive* networks, which
        # is exactly what we want — it will not include install networks
        # owned by a concurrent peer run.
        for name in defined:
            if not name.startswith("tr-instal-"):
                continue
            try:
                net = self._conn.networkLookupByName(name)
                if net.isActive():
                    # Paranoia: skip anything that somehow shows up active.
                    continue
                net.undefine()
            except libvirt.libvirtError:
                # Best-effort: if this fails, the next step will surface
                # a more useful error when it hits the actual conflict.
                pass

    def _pick_install_subnet(self) -> str:
        """Choose an install subnet no other libvirt network is using.

        Iterates :data:`_INSTALL_SUBNET_POOL` and returns the first
        entry whose CIDR does not overlap any existing libvirt network.
        Falls back to the first pool entry if every slot is taken (in
        which case the subsequent :meth:`~VirtualNetwork.start` will
        fail with a clearer diagnostic).

        :returns: CIDR string such as ``'192.168.240.0/24'``.
        """
        assert self._conn is not None
        used: list[ipaddress.IPv4Network] = []
        try:
            names = (self._conn.listNetworks() or []) + (
                self._conn.listDefinedNetworks() or []
            )
        except libvirt.libvirtError:
            names = []

        for name in names:
            try:
                net_obj = self._conn.networkLookupByName(name)
                root = ET.fromstring(net_obj.XMLDesc())
                ip_el = root.find("ip")
                if ip_el is None:
                    continue
                addr = ip_el.attrib.get("address")
                mask = ip_el.attrib.get("netmask", "255.255.255.0")
                if addr:
                    used.append(
                        ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
                    )
            except (libvirt.libvirtError, ET.ParseError, ValueError):
                continue

        for candidate in _INSTALL_SUBNET_POOL:
            cand_net = ipaddress.IPv4Network(candidate, strict=False)
            if not any(cand_net.overlaps(u) for u in used):
                return candidate

        return _INSTALL_SUBNET_POOL[0]

    def _create_install_network(self, run_id: str) -> VirtualNetwork:
        """Create an ephemeral NAT network for the install phase.

        :param run_id: Current run UUID.
        :returns: A configured (but not yet started) :class:`VirtualNetwork`.
        """
        subnet = self._pick_install_subnet()
        net = VirtualNetwork(
            name=f"install-{run_id[:4]}",
            subnet=subnet,
            dhcp=True,
            internet=True,
            # DNS must be on: install-phase VMs need name resolution for
            # apt/dnf to reach upstream repos. Libvirt's dnsmasq advertises
            # itself as the DHCP-handed resolver, so disabling DNS here
            # would leave guests pointed at a port that isn't listening.
            dns=True,
        )
        net.bind_run(run_id)
        # Register install-phase VMs so they get DHCP leases during install.
        # Prebuilt VMs are skipped — they never boot on this network.  The
        # install-phase MAC is derived from (vm_name, "__install__") rather
        # than (vm_name, net.name), so bypass register_vm (which computes
        # its own MAC) and use register_vm_with_mac.
        net_obj = ipaddress.IPv4Network(subnet, strict=False)
        hosts = list(net_obj.hosts())
        install_phase_vms = [
            vm for vm in self._vm_list
            if vm.builder.needs_install_phase()
        ]
        for idx, vm in enumerate(install_phase_vms):
            ip = str(hosts[idx + 1])  # skip gateway (.1)
            mac = _mac_for_vm_network(vm.name, "__install__")
            net.register_vm_with_mac(vm.name, mac, ip)
        return net

    def _setup_test_networks(self, run_id: str) -> None:
        """Bind the run ID to all test networks and register VM IPs.

        :param run_id: Current run UUID.
        """
        # Bind every network once up-front so ``backend_name()`` works below
        # and so that re-used network objects get the current run's suffix
        # rather than a stale one from a prior run.
        for net in self._networks:
            net.bind_run(run_id)

        # Per-network counter for auto-IP assignment.
        net_counters: dict[str, int] = {net.name: 0 for net in self._networks}

        for vm in self._vm_list:
            for ref in vm._network_refs():
                net = self._find_network(ref.name)
                if net is None:
                    raise NetworkError(
                        f"VM {vm.name!r} references unknown network {ref.name!r}. "
                        f"Available networks: {[n.name for n in self._networks]}"
                    )

                if ref.ip:
                    # Static IP — register with the explicit address
                    net.register_vm(vm.name, ref.ip)
                else:
                    # Auto-assign from the subnet
                    idx = net_counters[ref.name]
                    ip = net.static_ip_for_index(idx)
                    net_counters[ref.name] = idx + 1
                    net.register_vm(vm.name, ip)

    def _find_network(self, name: str) -> VirtualNetwork | None:
        """Find a network by its logical name.

        :param name: Network name to search for.
        :returns: The matching network, or ``None``.
        """
        for net in self._networks:
            if net.name == name:
                return net
        return None

    def _build_nic_entries(
        self, vm: VM
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
        """Build the NIC parameters needed for domain XML and network-config.

        :param vm: The VM whose network refs to process.
        :returns: A tuple of ``(network_entries, mac_ip_pairs)`` where:
            - ``network_entries`` is a list of ``(lv_net_name, mac)`` for
              domain XML
            - ``mac_ip_pairs`` is a list of
              ``(mac, ip_with_cidr, gateway, nameserver)`` for cloud-init
              network-config. ``gateway`` is empty unless the network has
              ``internet=True``; ``nameserver`` is empty unless ``dns=True``.
        """
        network_entries: list[tuple[str, str]] = []
        mac_ip_pairs: list[tuple[str, str, str, str]] = []

        for ref in vm._network_refs():
            net = self._find_network(ref.name)
            if net is None:
                continue
            mac = _mac_for_vm_network(vm.name, ref.name)
            lv_name = net.backend_name()
            network_entries.append((lv_name, mac))

            # Only networks with internet=True should advertise a default
            # gateway — otherwise two default routes fight for egress and
            # traffic meant for the public internet can leak onto an
            # isolated bridge. Likewise, only dns=True networks contribute
            # a resolver (dnsmasq is disabled when dns=False, so the
            # gateway IP is not a listening DNS server).
            gateway = net.gateway_ip if net.internet else ""
            nameserver = net.gateway_ip if net.dns else ""
            cidr = f"{ref.ip}/{net.prefix_len}" if ref.ip else ""
            mac_ip_pairs.append((mac, cidr, gateway, nameserver))

        return network_entries, mac_ip_pairs

    def _teardown(self) -> None:
        """Destroy every active VM, network, run artifact, and connection.

        Every step is independently guarded.  A failure in one VM shutdown
        does not stop the remaining VMs from being shut down, nor does it
        prevent networks from being destroyed, the run directory from being
        cleaned, or the libvirt connection from being closed.

        This method is declared never to raise: any bug elsewhere in the
        library that surfaces during provisioning is the *reason* teardown
        is running, and a cleanup failure must not mask the original bug.
        """
        if self._conn is None:
            return

        _log.info("teardown starting")
        for vm in self._vm_list:
            try:
                vm.shutdown()
            except Exception as exc:
                _log.debug("shutdown of VM %r raised (ignored): %s", vm.name, exc)

        for net in self._networks:
            try:
                net.stop(self)
            except Exception as exc:
                _log.debug(
                    "stop of network %r raised (ignored): %s", net.name, exc
                )

        if self._install_network is not None:
            try:
                self._install_network.stop(self)
            except Exception as exc:
                _log.debug("stop of install network raised (ignored): %s", exc)
            self._install_network = None

        if self._run is not None:
            try:
                self._run.cleanup()
            except Exception as exc:
                _log.debug("run dir cleanup raised (ignored): %s", exc)
            self._run = None

        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None
        self.vms = {}
        _log.info("teardown complete")


LibvirtOrchestrator = Orchestrator
"""Explicit alias for :class:`Orchestrator`.

Use this name in code that wants to be clear about which backend it's
asking for — e.g. when other backends also exist.  The unqualified
:class:`Orchestrator` is the documented default.
"""
