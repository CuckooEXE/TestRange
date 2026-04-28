"""Parity tests for the three Proxmox networking gaps closed
together: DHCP-discovery vNICs, install-vnet subnet pool, and the
``install_dns=`` orchestrator kwarg (which doubles as the run-phase
DNS for ``dns=True`` networks).

Pre-fix behaviour (covered by older tests still living in
``tests/test_proxmox_install_vnet.py``):

- every Proxmox vNIC required an explicit ``ip=``;
- the install-vnet was pinned to ``192.168.230.0/24``;
- run-phase NICs on ``dns=True`` networks set their nameserver to the
  network's gateway IP — but PVE doesn't run a resolver there, so
  ``/etc/resolv.conf`` ended up pointing at a dead address.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange import (
    Credential,
    Memory,
    NetworkError,
    OrchestratorError,
    vCPU,
    vNIC,
)
from testrange.backends.proxmox.network import ProxmoxVirtualNetwork
from testrange.backends.proxmox.orchestrator import (
    _INSTALL_SUBNET_POOL,
    ProxmoxOrchestrator,
)
from testrange.backends.proxmox.vm import ProxmoxVM


def _orch(
    *,
    vms: list[ProxmoxVM] | None = None,
    networks: list[ProxmoxVirtualNetwork] | None = None,
    install_dns: str = "1.1.1.1",
    claimed_subnets: list[str] | None = None,
) -> ProxmoxOrchestrator:
    """Build a ProxmoxOrchestrator with a stubbed client.

    ``claimed_subnets`` controls what ``cluster.sdn.subnets.get()``
    returns — the mechanism ``_pick_install_subnet`` consults to skip
    in-use pool entries.
    """
    orch = ProxmoxOrchestrator(
        host="pve.example.com",
        user="root@pam",
        password="x",
        node="pve01",
        install_dns=install_dns,
    )
    orch._vm_list = vms or []
    orch._networks = networks or []
    client = MagicMock()
    client.cluster.sdn.subnets.get.return_value = [
        {"cidr": cidr} for cidr in (claimed_subnets or [])
    ]
    orch._client = client
    return orch


def _vm(
    name: str,
    *,
    network: str = "Net",
    ip: str | None = None,
) -> ProxmoxVM:
    return ProxmoxVM(
        name=name,
        iso="https://example.com/debian-12.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1), vNIC(network, ip=ip)],
        communicator="ssh",
    )


# =====================================================================
# install_dns kwarg — defaults, override, plumbing into install MAC pairs
# =====================================================================


class TestInstallDnsKwarg:
    def test_default_is_cloudflare(self) -> None:
        # The historical baseline.  Tests for air-gapped scenarios
        # below exercise the override path.
        orch = ProxmoxOrchestrator(host="x")
        assert orch._install_dns == "1.1.1.1"

    def test_override_propagates_to_orchestrator_state(self) -> None:
        orch = ProxmoxOrchestrator(host="x", install_dns="10.0.0.53")
        assert orch._install_dns == "10.0.0.53"

    def test_install_seed_carries_orchestrator_dns(self) -> None:
        # The install-phase seed's ``mac_ip_pairs`` is what cloud-
        # init / answer.toml render into ``/etc/resolv.conf``.  Pre-
        # fix this was hardcoded ``1.1.1.1``; post-fix it follows
        # whatever the orchestrator was constructed with.
        orch = _orch(
            vms=[_vm("web", network="Net", ip="10.0.0.5")],
            install_dns="192.168.99.53",
        )
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        install_net = orch._create_install_network()
        orch._install_network = install_net

        vm = orch._vm_list[0]
        pairs = vm._build_install_mac_ip_pairs(
            orch,
            install_net.backend_name(),
            next(
                mac for vm_name, mac, _ in install_net._vm_entries
                if vm_name == "web"
            ),
        )
        assert pairs, "install-phase mac_ip_pairs unexpectedly empty"
        _mac, _cidr, _gateway, dns = pairs[0]
        assert dns == "192.168.99.53"


# =====================================================================
# Install-vnet subnet pool
# =====================================================================


class TestInstallSubnetPool:
    def test_pool_size_and_endpoints(self) -> None:
        # The pool's exact length (10 entries) is deliberate — same
        # shape as the libvirt pool, comfortable headroom for CI
        # fleet concurrency without colliding with libvirt's range
        # (192.168.240–254/24).
        assert len(_INSTALL_SUBNET_POOL) == 10
        assert _INSTALL_SUBNET_POOL[0] == "192.168.230.0/24"
        assert _INSTALL_SUBNET_POOL[-1] == "192.168.239.0/24"

    def test_picker_returns_first_when_pool_clear(self) -> None:
        orch = _orch()
        assert orch._pick_install_subnet() == _INSTALL_SUBNET_POOL[0]

    def test_picker_skips_in_use_entries(self) -> None:
        # If the first two pool entries are already claimed by other
        # SDN subnets on the cluster, the picker rolls forward.
        orch = _orch(claimed_subnets=list(_INSTALL_SUBNET_POOL[:2]))
        assert orch._pick_install_subnet() == _INSTALL_SUBNET_POOL[2]

    def test_picker_raises_when_pool_exhausted(self) -> None:
        orch = _orch(claimed_subnets=list(_INSTALL_SUBNET_POOL))
        with pytest.raises(OrchestratorError, match="every install-vnet subnet"):
            orch._pick_install_subnet()

    def test_picker_tolerates_unrelated_subnets(self) -> None:
        # A subnet that's not in our pool (the operator's own SDN
        # config, for example) doesn't count as "claimed."
        orch = _orch(claimed_subnets=["10.42.0.0/24", "172.16.0.0/24"])
        assert orch._pick_install_subnet() == _INSTALL_SUBNET_POOL[0]


# =====================================================================
# DHCP discovery — vNIC without ip= gets one from the network's subnet
# =====================================================================


class TestDhcpDiscovery:
    def _net(self, name: str = "Net", subnet: str = "10.42.0.0/24") -> ProxmoxVirtualNetwork:
        return ProxmoxVirtualNetwork(name=name, subnet=subnet)

    def test_static_ip_passes_through(self) -> None:
        # Backwards-compat: explicit ``ip=`` on a vNIC still wins.
        net = self._net()
        orch = _orch(
            vms=[_vm("web", network="Net", ip="10.42.0.5")],
            networks=[net],
        )
        orch._setup_vm_networks()
        registered = [(name, ip) for name, _, ip in net._vm_entries]
        assert registered == [("web", "10.42.0.5")]

    def test_unspecified_ip_gets_first_free_host(self) -> None:
        # ``vNIC("Net")`` with no ``ip=`` becomes the first host on
        # the subnet that isn't the gateway.
        net = self._net()
        orch = _orch(
            vms=[_vm("web", network="Net", ip=None)],
            networks=[net],
        )
        orch._setup_vm_networks()
        registered = [(name, ip) for name, _, ip in net._vm_entries]
        # ``10.42.0.1`` is the gateway → skipped.  First user host
        # is ``10.42.0.2``.
        assert registered == [("web", "10.42.0.2")]

    def test_multiple_dhcp_vms_get_sequential_addresses(self) -> None:
        # Determinism in declaration order — same VMs in the same
        # order should land on the same IPs across runs, which is
        # what makes test assertions stable.
        net = self._net()
        orch = _orch(
            vms=[
                _vm("alpha", network="Net", ip=None),
                _vm("bravo", network="Net", ip=None),
                _vm("delta", network="Net", ip=None),
            ],
            networks=[net],
        )
        orch._setup_vm_networks()
        ips = [ip for _, _, ip in net._vm_entries]
        assert ips == ["10.42.0.2", "10.42.0.3", "10.42.0.4"]

    def test_static_and_dhcp_mix_doesnt_collide(self) -> None:
        # A static vNIC at 10.42.0.5 reserves that slot; a later
        # DHCP-discovery vNIC must skip past it.
        net = self._net()
        orch = _orch(
            vms=[
                _vm("static", network="Net", ip="10.42.0.5"),
                _vm("dhcp", network="Net", ip=None),
            ],
            networks=[net],
        )
        orch._setup_vm_networks()
        registered = [(name, ip) for name, _, ip in net._vm_entries]
        # ``static`` registers first; ``dhcp`` walks the host range
        # and lands on .2 (first host not the gateway, not yet
        # taken).  Order in the ledger is registration order.
        assert registered == [("static", "10.42.0.5"), ("dhcp", "10.42.0.2")]

    def test_subnet_exhausted_raises_clearly(self) -> None:
        # Tiny /30 has 2 host addresses (.1 gateway, .2).  Two DHCP
        # VMs would need two host slots; only one is allocatable.
        net = ProxmoxVirtualNetwork(name="Tiny", subnet="10.42.0.0/30")
        orch = _orch(
            vms=[
                _vm("a", network="Tiny", ip=None),
                _vm("b", network="Tiny", ip=None),
            ],
            networks=[net],
        )
        with pytest.raises(NetworkError, match="cannot auto-allocate"):
            orch._setup_vm_networks()


# =====================================================================
# Run-phase DNS — uses install_dns, not gateway-as-DNS
# =====================================================================


class TestRunPhaseDns:
    """When a network has ``dns=True`` the orchestrator used to set
    the run-phase NIC's nameserver to the gateway IP.  PVE doesn't
    run a resolver on the gateway, so guests' ``/etc/resolv.conf``
    pointed at a dead address.  Post-fix it points at the
    orchestrator's ``install_dns``."""

    def test_dns_true_uses_install_dns_not_gateway(self) -> None:
        net = ProxmoxVirtualNetwork(name="Net", subnet="10.42.0.0/24", dns=True)
        orch = _orch(
            vms=[_vm("web", network="Net", ip="10.42.0.5")],
            networks=[net],
            install_dns="192.168.99.53",
        )
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        for n in orch._networks:
            n.bind_run(orch._run_id)
        orch._setup_vm_networks()
        _entries, mac_ip_pairs = orch._vm_network_refs(orch._vm_list[0])
        assert mac_ip_pairs, "expected one mac_ip_pair"
        _mac, cidr, _gateway, dns = mac_ip_pairs[0]
        assert dns == "192.168.99.53"
        # gateway field still names the gateway — only DNS changed.
        assert cidr == "10.42.0.5/24"

    def test_dns_false_leaves_nameserver_empty(self) -> None:
        # No DNS configured → no nameserver in the seed; cloud-init
        # falls back to whatever DHCP / image defaults produce.
        net = ProxmoxVirtualNetwork(name="Net", subnet="10.42.0.0/24", dns=False)
        orch = _orch(
            vms=[_vm("web", network="Net", ip="10.42.0.5")],
            networks=[net],
        )
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        for n in orch._networks:
            n.bind_run(orch._run_id)
        orch._setup_vm_networks()
        _entries, pairs = orch._vm_network_refs(orch._vm_list[0])
        _mac, _cidr, _gateway, dns = pairs[0]
        assert dns == ""

    def test_dhcp_vnic_cidr_uses_allocated_ip(self) -> None:
        # A vNIC that didn't pass ``ip=`` still ends up with a valid
        # CIDR in mac_ip_pairs because the orchestrator threads the
        # picker's allocation through ``_registered_ip_for``.
        net = ProxmoxVirtualNetwork(name="Net", subnet="10.42.0.0/24")
        orch = _orch(
            vms=[_vm("web", network="Net", ip=None)],
            networks=[net],
        )
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        for n in orch._networks:
            n.bind_run(orch._run_id)
        orch._setup_vm_networks()
        _entries, pairs = orch._vm_network_refs(orch._vm_list[0])
        _mac, cidr, _gateway, _dns = pairs[0]
        assert cidr == "10.42.0.2/24"
