"""Tests for the dedicated install-phase SDN vnet on
:class:`ProxmoxOrchestrator`.

Covers :meth:`_create_install_network`, :meth:`_teardown_install_network`,
and the build-phase wiring that uses the install vnet's
``(backend_name, mac)`` instead of the user's first declared NIC.

The motivating bug: a VM whose only declared NIC is on a network
with ``internet=False`` would attach the install-phase cloud-init
seed to a no-internet network, ``apt install`` would hang forever,
and the orchestrator's "wait for cloud-init poweroff" never returns.
The dedicated install vnet (``internet=True``) sidesteps the trap
regardless of where the VM eventually lives at run time.

No live PVE — every external interaction is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange import Credential, Memory, vCPU, vNIC
from testrange.backends.proxmox.network import ProxmoxVirtualNetwork
from testrange.backends.proxmox.orchestrator import (
    _INSTALL_SUBNET_POOL,
    _PROXMOX_INSTALL_SUBNET,
    ProxmoxOrchestrator,
)
from testrange.backends.proxmox.vm import ProxmoxVM


# =====================================================================
# Helpers
# =====================================================================


def _orch_with_vms(vms: list[ProxmoxVM]) -> ProxmoxOrchestrator:
    orch = ProxmoxOrchestrator(
        host="pve.example.com",
        user="root@pam",
        password="x",
        node="pve01",
    )
    orch._vm_list = vms
    # Stub a client whose vnet listing is empty, so
    # ``_pick_install_subnet`` walks zero vnets, finds zero claimed
    # CIDRs, and lands on the first pool entry deterministically.
    # That matches what every existing test assumed when the install
    # subnet was a fixed constant.  Tests that want to exercise
    # pool-collision picking override the vnet/subnet getters
    # locally.
    client = MagicMock()
    client.cluster.sdn.vnets.get.return_value = []
    orch._client = client
    return orch


def _vm(name: str, network_name: str = "Net") -> ProxmoxVM:
    return ProxmoxVM(
        name=name,
        iso="https://example.com/debian-12.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1), vNIC(network_name, ip="10.0.0.5")],
        communicator="ssh",
    )


# =====================================================================
# _create_install_network — shape + per-VM registrations
# =====================================================================


class TestCreateInstallNetwork:
    def test_uses_dedicated_subnet_with_internet_and_dns_on(self) -> None:
        """The install vnet must have ``internet=True`` and
        ``dns=True`` — without either, cloud-init's apt install
        step hangs forever waiting on package mirrors."""
        orch = _orch_with_vms([_vm("web")])
        orch._run_id = "abcd1234-1111-2222-3333-4444"

        net = orch._create_install_network()

        assert isinstance(net, ProxmoxVirtualNetwork)
        assert net.subnet == _PROXMOX_INSTALL_SUBNET
        assert net.internet is True
        assert net.dns is True
        assert net.dhcp is True
        assert net.name == "install"

    def test_subnet_is_outside_libvirt_pool(self) -> None:
        """Documented invariant: the proxmox install subnet must sit
        below libvirt's ``192.168.240.0/24``+ pool so concurrent
        runs of both backends on the same host can't pick the
        same subnet."""
        # Read libvirt's pool from its source of truth so the
        # invariant doesn't drift if libvirt's pool is widened.
        from testrange.backends.libvirt.orchestrator import (
            _INSTALL_SUBNET_POOL,
        )
        third_octets = [
            int(s.split(".")[2]) for s in _INSTALL_SUBNET_POOL
        ]
        proxmox_octet = int(_PROXMOX_INSTALL_SUBNET.split(".")[2])
        assert proxmox_octet < min(third_octets), (
            f"proxmox install subnet third octet {proxmox_octet} must "
            f"sit below libvirt's pool starting at {min(third_octets)}"
        )

    def test_registers_each_install_phase_vm_with_install_mac(self) -> None:
        """Each VM that needs an install phase gets a deterministic
        ``__install__``-keyed MAC pre-registered on the vnet, so
        the install-phase cloud-init seed's network-config can name
        the right NIC.  Same convention as the libvirt backend."""
        from testrange.backends.proxmox.network import _mac_for_vm_network

        orch = _orch_with_vms([_vm("web"), _vm("db")])
        orch._run_id = "abcd1234-1111-2222-3333-4444"

        net = orch._create_install_network()

        # Two VMs registered, each with the install-MAC convention.
        registered = list(net._vm_entries)
        assert len(registered) == 2
        names = [r[0] for r in registered]
        macs = [r[1] for r in registered]
        assert sorted(names) == ["db", "web"]
        # MACs use the deterministic install convention, not the
        # per-network one — so they're stable across runs.
        for name, mac in zip(names, macs, strict=True):
            assert mac == _mac_for_vm_network(name, "__install__")

    def test_skips_noop_builder_vms(self) -> None:
        """VMs whose builder doesn't need an install phase
        (NoOpBuilder) shouldn't be registered on the install vnet —
        they boot straight from a cached disk."""
        from testrange.vms.builders.noop import NoOpBuilder

        web = _vm("web")
        prebuilt = ProxmoxVM(
            name="prebuilt",
            iso="/srv/golden/debian.qcow2",  # absolute → no download
            users=[Credential("root", "pw")],
            devices=[vCPU(1), Memory(1), vNIC("Net", ip="10.0.0.6")],
            builder=NoOpBuilder(),
            communicator="ssh",
        )
        orch = _orch_with_vms([web, prebuilt])
        orch._run_id = "abcd1234-1111-2222-3333-4444"

        net = orch._create_install_network()

        names = sorted(r[0] for r in net._vm_entries)
        assert names == ["web"]  # prebuilt skipped

    def test_run_id_required(self) -> None:
        """``_create_install_network`` runs after ``run_id`` is
        bound; calling it earlier is a programmer error."""
        orch = _orch_with_vms([_vm("web")])
        orch._run_id = None  # explicit
        with pytest.raises(AssertionError, match="run_id"):
            orch._create_install_network()


# =====================================================================
# _teardown_install_network — idempotent + tolerant
# =====================================================================


class TestTeardownInstallNetwork:
    def test_noop_when_no_install_network(self) -> None:
        """Safe to call when no install vnet was ever created."""
        orch = _orch_with_vms([])
        orch._install_network = None
        orch._teardown_install_network()  # must not raise

    def test_calls_stop_and_clears_field(self) -> None:
        orch = _orch_with_vms([])
        net = MagicMock()
        orch._install_network = net

        orch._teardown_install_network()

        net.stop.assert_called_once_with(orch)
        assert orch._install_network is None

    def test_swallows_stop_errors(self) -> None:
        """A teardown failure must never raise — orchestrator
        ``__exit__`` honours the ABC's never-raise contract."""
        orch = _orch_with_vms([])
        net = MagicMock()
        net.stop.side_effect = RuntimeError("PVE 500")
        orch._install_network = net

        orch._teardown_install_network()  # must not raise
        assert orch._install_network is None  # still cleared


# =====================================================================
# _provision_vms uses the install vnet for build phase
# =====================================================================


class TestProvisionUsesInstallVnet:
    def test_build_uses_install_net_not_user_first_nic(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for the
        ``apt-install-on-internet=False-network`` hang.  Build phase
        must hand each VM the install vnet's name + the install
        MAC, never the user's first declared NIC's vnet name."""
        from testrange.backends.proxmox.network import _mac_for_vm_network

        # VM declares a single NIC on a user network.  The fix
        # makes build use the install vnet anyway.
        web = _vm("web", network_name="UserNet")
        orch = _orch_with_vms([web])
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        orch._client = MagicMock()
        orch._cache = MagicMock()
        orch._run = MagicMock()

        # Stand in for the started install vnet — backend_name returns
        # the SDN-ID we expect to land in build()'s call kwargs.
        install_net = MagicMock()
        install_net.backend_name.return_value = "instabcd"
        orch._install_network = install_net

        # ``_vm_network_refs`` walks the user's declared networks; we
        # only need it to return at least one entry so the
        # "no network refs" guard doesn't raise.  Returning a stub
        # tuple is enough because the build-phase code never reads
        # the user-network entries — only the install vnet.
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_vm_network_refs",
            lambda self, vm: ([("usernet", "52:54:00:aa:bb:cc")], []),
        )
        # Skip the actual build + start_run calls — we're asserting
        # what gets passed in, not what build does internally.
        build_mock = MagicMock(return_value="100")
        monkeypatch.setattr(ProxmoxVM, "build", build_mock)
        monkeypatch.setattr(
            ProxmoxVM, "start_run", lambda self, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "set_client", lambda self, c: None,
        )

        orch._provision_vms()

        # build() saw the install vnet's name + the install MAC,
        # NOT the user-net's name + per-network MAC.
        kwargs = build_mock.call_args.kwargs
        assert kwargs["install_network_name"] == "instabcd"
        assert kwargs["install_network_mac"] == _mac_for_vm_network(
            "web", "__install__",
        )

    def test_noop_builder_skips_install_net(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NoOpBuilder VMs don't have an install phase, so build()
        gets empty strings rather than trying to attach to the
        install vnet (which may not even exist if every VM is
        prebuilt)."""
        from testrange.vms.builders.noop import NoOpBuilder

        prebuilt = ProxmoxVM(
            name="prebuilt",
            iso="/srv/golden/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[vCPU(1), Memory(1), vNIC("Net", ip="10.0.0.5")],
            builder=NoOpBuilder(),
            communicator="ssh",
        )
        orch = _orch_with_vms([prebuilt])
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        orch._client = MagicMock()
        orch._cache = MagicMock()
        orch._run = MagicMock()
        orch._install_network = None  # NoOp — no install net

        monkeypatch.setattr(
            ProxmoxOrchestrator, "_vm_network_refs",
            lambda self, vm: ([("usernet", "52:54:00:aa:bb:cc")], []),
        )
        build_mock = MagicMock(return_value="100")
        monkeypatch.setattr(ProxmoxVM, "build", build_mock)
        monkeypatch.setattr(
            ProxmoxVM, "start_run", lambda self, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "set_client", lambda self, c: None,
        )

        orch._provision_vms()

        kwargs = build_mock.call_args.kwargs
        assert kwargs["install_network_name"] == ""
        assert kwargs["install_network_mac"] == ""

    def test_no_user_network_refs_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The "VM declared zero NICs" guard must still fire — even
        the install vnet doesn't survive past build, so a VM that
        carries no run-phase NIC has nowhere to live."""
        from testrange.exceptions import NetworkError

        web = _vm("web")
        orch = _orch_with_vms([web])
        orch._client = MagicMock()
        orch._cache = MagicMock()
        orch._run = MagicMock()
        orch._install_network = MagicMock()

        monkeypatch.setattr(
            ProxmoxOrchestrator, "_vm_network_refs",
            lambda self, vm: ([], []),  # no user NICs
        )

        with pytest.raises(NetworkError, match="no network refs"):
            orch._provision_vms()


# =====================================================================
# Install seed network-config matches the install vnet
# =====================================================================


class TestInstallSeedMatchesInstallVnet:
    """Regression for the apt-hangs-forever bug: the install-phase
    cloud-init seed's network-config has to describe the SAME NIC
    (MAC + IP + subnet) the install VM is actually attached to.

    Before the fix, ``_build_install_mac_ip_pairs`` walked the VM's
    user-declared NICs and produced a config for a MAC the install
    VM didn't have — cloud-init couldn't bring up the network,
    apt had no route to upstream, and the install hung."""

    def test_returns_single_entry_matching_install_vnet(self) -> None:
        from testrange.backends.proxmox.network import ProxmoxVirtualNetwork

        # Build the orchestrator with one VM and run the install-vnet
        # creation path so the IP gets registered the same way
        # __enter__ would.
        web = _vm("web", network_name="UserNet")
        orch = _orch_with_vms([web])
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        install_vnet = orch._create_install_network()
        # ``backend_name`` is what the orchestrator passes to
        # ``vm.build()`` as ``install_network_name``.
        install_name = install_vnet.backend_name()
        # MAC for the install NIC, same convention the orchestrator
        # uses in ``_provision_vms``.
        from testrange.backends.proxmox.network import _mac_for_vm_network
        install_mac = _mac_for_vm_network("web", "__install__")
        # Stash the vnet on the orchestrator the way __enter__ does
        # so ProxmoxVM can find it via ``context._install_network``.
        orch._install_network = install_vnet

        pairs = web._build_install_mac_ip_pairs(
            context=orch,
            install_network_name=install_name,
            install_network_mac=install_mac,
        )

        # Exactly ONE entry — the install VM has only one NIC.
        assert len(pairs) == 1
        mac, cidr, gateway, dns = pairs[0]

        # MAC matches what the orchestrator wrote into qemu's
        # ``net0=virtio=<mac>`` config.
        assert mac == install_mac
        # IP is on the install vnet's subnet (192.168.230.0/24),
        # NOT the user-declared UserNet's.
        assert cidr.startswith("192.168.230.")
        assert cidr.endswith("/24")
        # Gateway points at the install vnet's .1 — without this
        # the VM has no default route and apt hangs.
        assert gateway == "192.168.230.1"
        # DNS uses the public fallback (PVE SDN doesn't ship its
        # own resolver) so apt can resolve mirrors.
        assert dns  # non-empty

    def test_mac_mismatch_raises_loudly(self) -> None:
        """If the orchestrator passes the wrong install_network_name,
        the function refuses rather than silently producing a no-NIC
        config that would re-trigger the original install-hang."""
        from testrange.exceptions import VMBuildError

        web = _vm("web")
        orch = _orch_with_vms([web])
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        install_vnet = orch._create_install_network()
        orch._install_network = install_vnet

        with pytest.raises(VMBuildError, match="does not match"):
            web._build_install_mac_ip_pairs(
                context=orch,
                install_network_name="some-other-vnet",
                install_network_mac="52:54:00:de:ad:be",
            )

    def test_unregistered_vm_raises(self) -> None:
        """If a VM ended up in build() without being registered on
        the install vnet, surface that as a clear error rather than
        producing an empty network-config."""
        from testrange.exceptions import VMBuildError

        web = _vm("web")
        orch = _orch_with_vms([web])
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        install_vnet = orch._create_install_network()
        # Empty out the IP registration (simulate a buggy
        # _create_install_network that skipped this VM).
        install_vnet._vm_entries = []
        orch._install_network = install_vnet

        with pytest.raises(VMBuildError, match="did not register"):
            web._build_install_mac_ip_pairs(
                context=orch,
                install_network_name=install_vnet.backend_name(),
                install_network_mac="52:54:00:01:02:03",
            )
