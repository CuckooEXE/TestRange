"""Parity tests for the Proxmox networking surface: DHCP-discovery
vNICs, install-vnet subnet pool, and the IPAM + dnsmasq integration
that gives PVE guests libvirt-style DHCP + ``<vm>.<vnet>`` DNS.

Pre-fix behaviour (preserved as historical context — earlier tests
still live in ``tests/test_proxmox_install_vnet.py``):

- every Proxmox vNIC required an explicit ``ip=``;
- the install-vnet was pinned to ``192.168.230.0/24``;
- run-phase NICs on ``dns=True`` networks set their nameserver to the
  network's gateway IP — but PVE didn't run a resolver there, so
  ``/etc/resolv.conf`` ended up pointing at a dead address.  The
  earlier ``install_dns=`` kwarg worked around that by pinning a
  public resolver across both phases; with PVE's per-vnet dnsmasq
  now serving DNS, the gateway IS the resolver and the workaround
  was retired.
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
    claimed_subnets: list[str] | None = None,
) -> ProxmoxOrchestrator:
    """Build a ProxmoxOrchestrator with a stubbed client.

    ``claimed_subnets`` controls what cluster-wide subnets the
    install-vnet picker sees as "in use".  PVE 9.x's API has no
    ``GET /cluster/sdn/subnets`` endpoint (returns 501); the picker
    walks ``GET /cluster/sdn/vnets`` and per-vnet
    ``GET /cluster/sdn/vnets/{vnet}/subnets`` to enumerate.  The
    stub here parks each claimed CIDR in its own synthetic vnet so
    the walk picks them up the same way it would on a real cluster.
    """
    orch = ProxmoxOrchestrator(
        host="pve.example.com",
        user="root@pam",
        password="x",
        node="pve01",
    )
    orch._vm_list = vms or []
    orch._networks = networks or []
    client = MagicMock()

    cidrs = list(claimed_subnets or [])
    # ``cluster.sdn.vnets.get()`` returns one synthetic vnet per
    # claimed CIDR; the picker calls ``cluster.sdn.vnets(name).subnets.get()``
    # for each, and we route those through a side_effect so each
    # synthetic vnet returns its own CIDR.
    vnet_names = [f"vnet{i}" for i, _ in enumerate(cidrs)]
    client.cluster.sdn.vnets.get.return_value = [
        {"vnet": name} for name in vnet_names
    ]
    cidr_by_vnet = dict(zip(vnet_names, cidrs))

    def _subnets_for(name: object) -> MagicMock:
        result = MagicMock()
        cidr = cidr_by_vnet.get(str(name))
        result.subnets.get.return_value = (
            [{"cidr": cidr, "subnet": f"tr-{cidr}"}] if cidr else []
        )
        return result

    client.cluster.sdn.vnets.side_effect = _subnets_for
    orch._client = client
    # ``_setup_vm_networks`` asserts a bound run_id (it calls
    # ``bind_run`` on every network at the top, so a missing run_id
    # is a clear contract violation).  Stamp a deterministic ID
    # here so test cases that exercise the IP-allocation path don't
    # need to set it themselves.
    orch._run_id = "abcd1234-1111-2222-3333-4444"
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
# dnsmasq preflight on the PVE node
# =====================================================================


class TestDnsmasqPreflight:
    """Top-level orchestrator must check dnsmasq is installed on the
    target PVE node before any subnet hits ``dhcp = "dnsmasq"`` —
    otherwise the dnsmasq instance never spawns and guests time out.

    Probe shape: ``GET /nodes/{node}/apt/changelog?name=dnsmasq``.
    That endpoint succeeds when the package is installed and errors
    when it isn't — much more reliable than ``/apt/versions``, which
    PVE hardcodes to a curated "important Proxmox packages" list
    that never includes ``dnsmasq``.
    """

    def test_passes_when_changelog_lookup_succeeds(self) -> None:
        orch = _orch()
        # A successful changelog lookup returns the changelog text
        # (or an empty string on some PVE versions); both count as
        # "installed".
        orch._client.nodes.return_value.apt.changelog.get.return_value = (
            "dnsmasq (2.90-1) unstable; urgency=medium\n  * release\n"
        )
        orch._preflight_dnsmasq_installed()
        # Verify we hit the right endpoint with the right query.
        orch._client.nodes.return_value.apt.changelog.get.assert_called_once_with(
            name="dnsmasq",
        )

    def test_raises_when_changelog_errors(self) -> None:
        # Package not installed → PVE returns 500 from
        # ``apt-get changelog dnsmasq`` which proxmoxer surfaces as
        # an exception.
        orch = _orch()
        orch._client.nodes.return_value.apt.changelog.get.side_effect = (
            RuntimeError("E: Unable to find a source package for dnsmasq")
        )
        with pytest.raises(OrchestratorError, match="dnsmasq.*not.*installed"):
            orch._preflight_dnsmasq_installed()

    def test_error_includes_install_hint_and_disable_step(self) -> None:
        orch = _orch()
        orch._client.nodes.return_value.apt.changelog.get.side_effect = (
            RuntimeError("nope")
        )
        with pytest.raises(OrchestratorError) as exc:
            orch._preflight_dnsmasq_installed()
        msg = str(exc.value)
        assert "apt-get install" in msg
        assert "dnf install" in msg
        # Per PVE SDN docs, the systemd dnsmasq.service must be
        # disabled after install (PVE owns the per-vnet instances);
        # the error message should mention this so manual installers
        # don't hit the same port-conflict footgun.
        assert "systemctl disable" in msg


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

    def test_picked_ip_is_stamped_back_onto_vnic(self) -> None:
        # Downstream consumers (ProxmoxAnswerBuilder._network_block,
        # cloud-init network-config readers) inspect ``vNIC.ip``
        # directly.  The orchestrator stamps the picked IP back onto
        # the vNIC after allocation so they see a unified static-IP
        # view — without this, the answer builder falls back to
        # ``from-dhcp`` mode and the PVE installer freezes the wrong
        # (install-phase) lease into /etc/network/interfaces.
        net = self._net()
        vm = _vm("web", network="Net", ip=None)
        nic = vm.devices[2]  # vCPU, Memory, vNIC
        assert nic.ip is None
        orch = _orch(vms=[vm], networks=[net])
        orch._setup_vm_networks()
        assert nic.ip == "10.42.0.2"

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
# Run-phase DNS — gateway IS the dnsmasq, libvirt-style
# =====================================================================


class TestRunPhaseDns:
    """Each SDN subnet ships ``dhcp = "dnsmasq"`` so the gateway
    address is also the DNS server.  Run-phase NICs on ``dns=True``
    networks point at the gateway directly — no separate
    install_dns workaround needed."""

    def test_dns_true_uses_gateway(self) -> None:
        net = ProxmoxVirtualNetwork(name="Net", subnet="10.42.0.0/24", dns=True)
        orch = _orch(
            vms=[_vm("web", network="Net", ip="10.42.0.5")],
            networks=[net],
        )
        orch._run_id = "abcd1234-1111-2222-3333-4444"
        for n in orch._networks:
            n.bind_run(orch._run_id)
        orch._setup_vm_networks()
        _entries, mac_ip_pairs = orch._vm_network_refs(orch._vm_list[0])
        assert mac_ip_pairs, "expected one mac_ip_pair"
        _mac, cidr, _gateway, dns = mac_ip_pairs[0]
        assert dns == net.gateway_ip == "10.42.0.1"
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


# =====================================================================
# SDN subnet creation flips to dhcp = "dnsmasq" + IPAM push
# =====================================================================


class TestSubnetDnsmasq:
    """Every SDN subnet TestRange creates carries ``dhcp = "dnsmasq"``
    + a ``dhcp-range`` covering the high half of the host range.
    Each :meth:`register_vm` entry then flows through to PVE's IPAM
    via ``POST /cluster/sdn/vnets/{vnet}/ips``, which is what gives
    us deterministic DHCP leases AND ``<vm>.<vnet>`` DNS."""

    def _started_net(
        self, register: list[tuple[str, str, str]] | None = None,
    ) -> tuple[ProxmoxVirtualNetwork, MagicMock]:
        net = ProxmoxVirtualNetwork(name="OuterNet", subnet="10.0.0.0/24")
        net.bind_run("abcd1234-1111-2222-3333-4444")
        for vm_name, mac, ip in register or []:
            net.register_vm_with_mac(vm_name, mac, ip)
        client = MagicMock()
        client.cluster.sdn.vnets.return_value.subnets.get.return_value = [
            {"subnet": "tr-10.0.0.0-24"},
        ]
        ctx = MagicMock(_client=client, _zone="tr", _switches=[])
        net.start(ctx)
        return net, client

    def test_subnet_post_carries_dhcp_range_only(self) -> None:
        _net, client = self._started_net()
        subnet_call = client.cluster.sdn.vnets.return_value.subnets.post
        kwargs = subnet_call.call_args.kwargs
        # ``dhcp = "dnsmasq"`` is set at ZONE scope (see
        # :meth:`ProxmoxOrchestrator._ensure_sdn_zone` /
        # :meth:`ProxmoxSwitch.start`), NOT at subnet scope.  PVE
        # 9.x's subnet schema rejects ``dhcp`` as an unknown
        # property: the subnet only carries the dhcp-range.
        assert "dhcp" not in kwargs
        # Range starts past the reserved head (.1 gateway + 9 IPAM
        # static slots) so static reservations and dynamic leases
        # don't fight over the low end.
        assert kwargs["dhcp-range"] == [
            "start-address=10.0.0.11,end-address=10.0.0.254",
        ]

    def test_register_vm_pushes_ipam_entry_with_zone(self) -> None:
        # PVE IPAM endpoint requires ``ip`` + ``zone``; ``mac`` is
        # optional but always supplied by us (deterministic-MAC
        # scheme).  ``hostname`` is NOT a valid field on this
        # endpoint — earlier slices passed one and got 400s.
        _net, client = self._started_net(
            register=[
                ("web", "52:54:00:11:22:33", "10.0.0.5"),
                ("db",  "52:54:00:44:55:66", "10.0.0.6"),
            ],
        )
        ips_post = client.cluster.sdn.vnets.return_value.ips.post
        assert ips_post.call_count == 2
        first = ips_post.call_args_list[0].kwargs
        assert first == {
            "ip": "10.0.0.5",
            "mac": "52:54:00:11:22:33",
            "zone": "tr",
        }
        second = ips_post.call_args_list[1].kwargs
        assert second == {
            "ip": "10.0.0.6",
            "mac": "52:54:00:44:55:66",
            "zone": "tr",
        }
        # Regression guard: the endpoint rejects ``hostname`` as
        # an unknown property; never put it back without first
        # verifying the schema accepts it.
        for call in ips_post.call_args_list:
            assert "hostname" not in call.kwargs

    def test_subnet_too_small_for_reservation_slice_raises(self) -> None:
        # /30 has 2 hosts.  Reserved-head is 10 → no dynamic range
        # left, picker raises clearly rather than producing an
        # inverted dhcp-range.
        net = ProxmoxVirtualNetwork(name="Tiny", subnet="10.0.0.0/30")
        net.bind_run("abcd1234-1111-2222-3333-4444")
        client = MagicMock()
        ctx = MagicMock(_client=client, _zone="tr", _switches=[])
        with pytest.raises(NetworkError, match="too small"):
            net.start(ctx)


class TestZoneCreationCarriesDhcpDnsmasq:
    """The ``dhcp = "dnsmasq"`` selector lives at zone scope per the
    PVE 9.x SDN schema.  Both the orchestrator's default zone
    (:meth:`ProxmoxOrchestrator._ensure_sdn_zone`) and user-defined
    :class:`ProxmoxSwitch` zones must set it; otherwise PVE doesn't
    spawn the per-vnet dnsmasq instances and DHCP/DNS silently does
    nothing inside the vnet."""

    def test_default_zone_create_includes_dhcp_dnsmasq(self) -> None:
        orch = _orch()
        # No existing zone — should POST a new one with the dhcp field.
        orch._client.cluster.sdn.zones.get.return_value = []
        orch._ensure_sdn_zone()
        post = orch._client.cluster.sdn.zones.post
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        assert kwargs["type"] == "simple"
        assert kwargs["zone"] == "tr"
        assert kwargs["dhcp"] == "dnsmasq"

    def test_default_zone_already_present_with_dhcp_is_noop(self) -> None:
        orch = _orch()
        orch._client.cluster.sdn.zones.get.return_value = [
            {"zone": "tr", "type": "simple", "dhcp": "dnsmasq"},
        ]
        orch._ensure_sdn_zone()
        # No POST and no PUT against the zone itself — just the
        # cluster-wide apply at the end.
        orch._client.cluster.sdn.zones.post.assert_not_called()
        orch._client.cluster.sdn.zones.assert_not_called()

    def test_default_zone_present_without_dhcp_gets_upgraded(self) -> None:
        # Pre-existing zone from an earlier TestRange version that
        # didn't set ``dhcp`` — must be upgraded in place via PUT
        # so the existing zone starts spawning dnsmasq for any
        # subsequent subnet create.
        orch = _orch()
        orch._client.cluster.sdn.zones.get.return_value = [
            {"zone": "tr", "type": "simple"},  # no dhcp
        ]
        orch._ensure_sdn_zone()
        # PUT against the specific zone with dhcp=dnsmasq.
        orch._client.cluster.sdn.zones.assert_any_call("tr")
        zone_handle = orch._client.cluster.sdn.zones.return_value
        zone_handle.put.assert_called_once_with(dhcp="dnsmasq")
