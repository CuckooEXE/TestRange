"""Unit tests for :mod:`testrange.backends.libvirt.orchestrator`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from testrange.backends.libvirt.network import VirtualNetwork
from testrange.backends.libvirt.orchestrator import Orchestrator
from testrange.exceptions import NetworkError, OrchestratorError


class TestBuildUri:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("localhost", "qemu:///system"),
            ("127.0.0.1", "qemu:///system"),
            ("::1", "qemu:///system"),
            ("qemu+ssh://alice@vmhost/system", "qemu+ssh://alice@vmhost/system"),
            ("vmhost", "qemu+ssh://vmhost/system"),
            ("alice@vmhost", "qemu+ssh://alice@vmhost/system"),
        ],
    )
    def test_uri_resolution(self, host: str, expected: str) -> None:
        orch = Orchestrator(host=host)
        assert orch._build_uri() == expected


class TestInitialState:
    def test_defaults(self) -> None:
        o = Orchestrator()
        assert o._host == "localhost"
        assert o._networks == []
        assert o._vm_list == []
        assert o.vms == {}
        assert o._conn is None
        assert o._run is None
        assert o._install_network is None

    def test_cache_root_override(self, tmp_path: Path) -> None:
        o = Orchestrator(cache_root=tmp_path / "alt")
        assert o._cache.root == tmp_path / "alt"


class TestNameCollisions:
    """Collision checks fire at construction time, not at defineXML."""

    def _vm(self, name: str):
        from testrange import VM, Credential, HardDrive, Memory, VirtualNetworkRef, vCPU

        return VM(
            name=name,
            iso="x",
            users=[Credential("root", "pw")],
            devices=[
                vCPU(1), Memory(1), HardDrive(10),
                VirtualNetworkRef("NetA"),
            ],
        )

    def test_duplicate_vm_name_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="duplicate VM name 'a'"):
            Orchestrator(
                networks=[VirtualNetwork("NetA", "10.0.0.0/24")],
                vms=[self._vm("a"), self._vm("a")],
            )

    def test_vm_name_10char_truncation_collision_raises(self) -> None:
        # Both names truncate to 'webserver-' (10 chars).
        with pytest.raises(
            OrchestratorError,
            match="10-character truncation",
        ):
            Orchestrator(
                networks=[VirtualNetwork("NetA", "10.0.0.0/24")],
                vms=[
                    self._vm("webserver-public"),
                    self._vm("webserver-private"),
                ],
            )

    def test_duplicate_network_name_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="duplicate network name 'NetA'"):
            Orchestrator(
                networks=[
                    VirtualNetwork("NetA", "10.0.0.0/24"),
                    VirtualNetwork("NetA", "10.1.0.0/24"),
                ],
            )

    def test_network_6char_truncation_collision_raises(self) -> None:
        # 'Public_Net'[:6].lower().replace('_','') → 'public'
        # 'publicnet2'[:6].lower().replace('_','') → 'public'
        with pytest.raises(
            OrchestratorError,
            match="6-character truncation",
        ):
            Orchestrator(
                networks=[
                    VirtualNetwork("Public_Net", "10.0.0.0/24"),
                    VirtualNetwork("publicnet2", "10.1.0.0/24"),
                ],
            )

    def test_distinct_names_pass(self) -> None:
        Orchestrator(
            networks=[
                VirtualNetwork("NetA", "10.0.0.0/24"),
                VirtualNetwork("NetB", "10.1.0.0/24"),
            ],
            vms=[self._vm("a"), self._vm("b")],
        )

    def test_hypervisor_inner_vm_duplicate_raises(self) -> None:
        """Inner-VM collisions surface from Hypervisor.__init__."""
        from testrange import (
            Credential, HardDrive, Hypervisor, LibvirtOrchestrator,
            Memory, VirtualNetworkRef, vCPU,
        )

        with pytest.raises(OrchestratorError, match="duplicate VM name 'client'"):
            Hypervisor(
                name="hv",
                iso="x",
                users=[Credential("root", "pw")],
                devices=[
                    vCPU(1), Memory(2), HardDrive(20),
                    VirtualNetworkRef("Outer"),
                ],
                orchestrator=LibvirtOrchestrator,
                networks=[VirtualNetwork("Inner", "10.42.0.0/24")],
                vms=[self._vm("client"), self._vm("client")],
            )


class TestFindNetwork:
    def test_returns_matching_network(self) -> None:
        net = VirtualNetwork("NetA", "10.0.0.0/24")
        o = Orchestrator(networks=[net])
        assert o._find_network("NetA") is net

    def test_returns_none_for_unknown(self) -> None:
        o = Orchestrator(networks=[VirtualNetwork("NetA", "10.0.0.0/24")])
        assert o._find_network("Missing") is None


class TestBuildNicEntries:
    def test_static_ip_included(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("NetA", "10.0.50.0/24", internet=True, dns=True)
        net.bind_run("deadbeef")
        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("NetA", ip="10.0.50.5")]
        o = Orchestrator(networks=[net])
        entries, pairs = o._build_nic_entries(vm)
        assert len(entries) == 1
        assert entries[0][0] == net.backend_name()
        assert pairs[0][1] == "10.0.50.5/24"
        assert pairs[0][2] == "10.0.50.1"
        assert pairs[0][3] == "10.0.50.1"

    def test_isolated_network_omits_gateway(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("Isolated", "10.1.0.0/24", internet=False, dns=False)
        net.bind_run("deadbeef")
        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("Isolated", ip="10.1.0.5")]
        o = Orchestrator(networks=[net])
        _, pairs = o._build_nic_entries(vm)
        assert pairs[0][2] == ""
        assert pairs[0][3] == ""

    def test_dns_without_internet(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("Inside", "10.2.0.0/24", internet=False, dns=True)
        net.bind_run("deadbeef")
        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("Inside", ip="10.2.0.5")]
        o = Orchestrator(networks=[net])
        _, pairs = o._build_nic_entries(vm)
        assert pairs[0][2] == ""  # no gateway — isolated
        assert pairs[0][3] == "10.2.0.1"  # resolver still present

    def test_dhcp_has_empty_ip(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("NetA", "10.0.50.0/24")
        net.bind_run("deadbeef")
        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("NetA")]
        o = Orchestrator(networks=[net])
        _, pairs = o._build_nic_entries(vm)
        assert pairs[0][1] == ""

    def test_unknown_network_ref_skipped(self) -> None:
        from testrange.devices import VirtualNetworkRef

        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("Missing")]
        o = Orchestrator(networks=[])
        entries, pairs = o._build_nic_entries(vm)
        assert entries == []
        assert pairs == []


class TestSetupTestNetworks:
    def test_unknown_network_raises(self) -> None:
        from testrange.devices import VirtualNetworkRef

        vm = MagicMock()
        vm.name = "web01"
        vm._network_refs.return_value = [VirtualNetworkRef("Missing")]
        o = Orchestrator(vms=[vm])
        with pytest.raises(NetworkError):
            o._setup_test_networks("deadbeef")

    def test_auto_assigns_ips_to_successive_vms(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("NetA", "10.0.50.0/24")
        vm1 = MagicMock()
        vm1.name = "a"
        vm1._network_refs.return_value = [VirtualNetworkRef("NetA")]
        vm2 = MagicMock()
        vm2.name = "b"
        vm2._network_refs.return_value = [VirtualNetworkRef("NetA")]
        o = Orchestrator(networks=[net], vms=[vm1, vm2])
        o._setup_test_networks("deadbeef")
        assigned = {name: ip for name, _mac, ip in net._vm_entries}
        assert assigned["a"] == "10.0.50.2"
        assert assigned["b"] == "10.0.50.3"

    def test_static_ip_registered_as_is(self) -> None:
        from testrange.devices import VirtualNetworkRef

        net = VirtualNetwork("NetA", "10.0.50.0/24")
        vm = MagicMock()
        vm.name = "a"
        vm._network_refs.return_value = [VirtualNetworkRef("NetA", ip="10.0.50.99")]
        o = Orchestrator(networks=[net], vms=[vm])
        o._setup_test_networks("deadbeef")
        ips = [ip for _n, _m, ip in net._vm_entries]
        assert "10.0.50.99" in ips


class TestConnectionFailure:
    def test_connection_failure_raises_orchestrator_error(self) -> None:
        import libvirt

        o = Orchestrator()
        with patch.object(libvirt, "open", side_effect=libvirt.libvirtError("nope")):
            with pytest.raises(OrchestratorError) as excinfo:
                o.__enter__()
            assert "libvirt" in str(excinfo.value).lower()


class TestCreateInstallNetwork:
    def _orch_with_empty_libvirt(self) -> Orchestrator:
        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = []
        o._conn.listDefinedNetworks.return_value = []
        return o

    def test_network_configured_with_nat_and_dhcp(self) -> None:
        o = self._orch_with_empty_libvirt()
        net = o._create_install_network("deadbeef12345678")
        assert net.internet is True
        assert net.dhcp is True
        # DNS must be on during install so guests get a working resolver from
        # DHCP (libvirt's dnsmasq advertises itself as the DNS server).
        assert net.dns is True

    def test_install_network_name_is_short(self) -> None:
        o = self._orch_with_empty_libvirt()
        net = o._create_install_network("deadbeef12345678")
        # Name format is ``install-<4 chars>``
        assert net.name.startswith("install-")

    def test_registers_all_vms(self) -> None:
        vm1 = MagicMock()
        vm1.name = "a"
        vm1.builder.needs_install_phase.return_value = True
        vm2 = MagicMock()
        vm2.name = "b"
        vm2.builder.needs_install_phase.return_value = True
        o = Orchestrator(vms=[vm1, vm2])
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = []
        o._conn.listDefinedNetworks.return_value = []
        net = o._create_install_network("deadbeef")
        assert len(net._vm_entries) == 2


class TestPickInstallSubnet:
    """Regression: a single hardcoded install subnet (192.168.250.0/24)
    used to wedge all future runs if anything left the IP bound.  The
    orchestrator must pick from a pool and skip any subnet already in
    use by another libvirt network."""

    def _xml_with_subnet(self, cidr: str) -> str:
        import ipaddress
        net = ipaddress.IPv4Network(cidr)
        return (
            f'<network><name>n</name>'
            f'<ip address="{net.network_address + 1}" '
            f'netmask="{net.netmask}"/></network>'
        )

    def test_returns_first_pool_entry_when_nothing_conflicts(self) -> None:
        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = []
        o._conn.listDefinedNetworks.return_value = []
        assert o._pick_install_subnet() == _INSTALL_SUBNET_POOL[0]

    def test_skips_subnets_used_by_existing_networks(self) -> None:
        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = ["default", "other"]
        o._conn.listDefinedNetworks.return_value = []

        # Claim the first two subnets in the pool
        claimed = [
            self._xml_with_subnet(_INSTALL_SUBNET_POOL[0]),
            self._xml_with_subnet(_INSTALL_SUBNET_POOL[1]),
        ]
        net_mocks = [MagicMock(), MagicMock()]
        net_mocks[0].XMLDesc.return_value = claimed[0]
        net_mocks[1].XMLDesc.return_value = claimed[1]

        def lookup(name: str) -> MagicMock:
            return {"default": net_mocks[0], "other": net_mocks[1]}[name]

        o._conn.networkLookupByName.side_effect = lookup

        chosen = o._pick_install_subnet()
        assert chosen == _INSTALL_SUBNET_POOL[2]

    def test_detects_overlap_not_just_exact_match(self) -> None:
        """If a /23 network covers the /24 candidate, we must avoid it."""
        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = ["big"]
        o._conn.listDefinedNetworks.return_value = []

        # A /23 spanning 192.168.240.0 and 192.168.241.0 blocks the first two pool entries
        big = MagicMock()
        big.XMLDesc.return_value = (
            '<network><name>big</name>'
            '<ip address="192.168.240.1" netmask="255.255.254.0"/></network>'
        )
        o._conn.networkLookupByName.return_value = big

        chosen = o._pick_install_subnet()
        assert chosen not in (_INSTALL_SUBNET_POOL[0], _INSTALL_SUBNET_POOL[1])

    def test_survives_malformed_xml(self) -> None:
        """A network whose XMLDesc can't be parsed must be skipped, not
        abort subnet selection."""
        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = ["bad"]
        o._conn.listDefinedNetworks.return_value = []

        bad = MagicMock()
        bad.XMLDesc.return_value = "<not-valid-xml"
        o._conn.networkLookupByName.return_value = bad

        assert o._pick_install_subnet() == _INSTALL_SUBNET_POOL[0]

    def test_libvirt_list_failure_does_not_raise(self) -> None:
        """If listing networks fails, fall back to the first pool entry."""
        import libvirt

        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        o = Orchestrator()
        o._conn = MagicMock()
        o._conn.listNetworks.side_effect = libvirt.libvirtError("conn lost")
        assert o._pick_install_subnet() == _INSTALL_SUBNET_POOL[0]

    def test_pool_does_not_collide_with_example_subnets(self) -> None:
        """Regression guard: the pool must not overlap the subnets the
        docs example uses (10.42.1.0/24, 10.42.2.0/24) so example users
        don't hit "can't pick a subnet" errors."""
        import ipaddress

        from testrange.backends.libvirt.orchestrator import _INSTALL_SUBNET_POOL

        example_subnets = [
            ipaddress.IPv4Network("10.42.1.0/24"),
            ipaddress.IPv4Network("10.42.2.0/24"),
        ]
        for candidate in _INSTALL_SUBNET_POOL:
            cand = ipaddress.IPv4Network(candidate)
            for ex in example_subnets:
                assert not cand.overlaps(ex), f"{candidate} overlaps example {ex}"


class TestCleanupStaleInstallNetworks:
    """A crashed prior run leaves an inactive ``tr-instal-*`` definition
    holding a subnet; startup should undefine it.  Under concurrency,
    active ``tr-instal-*`` networks belong to peer runs and must be
    left alone."""

    def test_undefines_only_inactive_install_networks(self) -> None:
        inactive = MagicMock(name="inactive_stale")
        inactive.isActive.return_value = False

        conn = MagicMock()
        # listDefinedNetworks() returns inactive networks only
        conn.listDefinedNetworks.return_value = ["tr-instal-ef01"]
        conn.networkLookupByName.return_value = inactive

        o = Orchestrator()
        o._conn = conn
        o._cleanup_stale_install_networks()

        # Inactive stale network: undefined (no destroy — it's not active)
        inactive.destroy.assert_not_called()
        inactive.undefine.assert_called_once()
        # listNetworks (active) is NOT inspected — peer runs own those.
        conn.listNetworks.assert_not_called()

    def test_active_install_network_is_left_alone(self) -> None:
        """Belt-and-braces: even if an active network somehow surfaced in
        listDefinedNetworks(), it must not be touched."""
        active = MagicMock(name="active_peer")
        active.isActive.return_value = True

        conn = MagicMock()
        conn.listDefinedNetworks.return_value = ["tr-instal-peer"]
        conn.networkLookupByName.return_value = active

        o = Orchestrator()
        o._conn = conn
        o._cleanup_stale_install_networks()

        active.destroy.assert_not_called()
        active.undefine.assert_not_called()

    def test_libvirt_list_failure_does_not_raise(self) -> None:
        import libvirt

        conn = MagicMock()
        conn.listDefinedNetworks.side_effect = libvirt.libvirtError("conn lost")

        o = Orchestrator()
        o._conn = conn
        o._cleanup_stale_install_networks()  # must not raise

    def test_lookup_failure_skipped(self) -> None:
        import libvirt

        conn = MagicMock()
        conn.listDefinedNetworks.return_value = ["tr-instal-abcd"]
        conn.networkLookupByName.side_effect = libvirt.libvirtError("gone")

        o = Orchestrator()
        o._conn = conn
        o._cleanup_stale_install_networks()  # must not raise

    def test_runs_before_install_network_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering contract: cleanup must happen BEFORE the new install
        network is defined, otherwise the new network's dnsmasq fails
        to bind when a stale one is still holding the subnet IP."""
        import libvirt

        import testrange.backends.libvirt.orchestrator as orch_mod

        calls: list[str] = []
        conn = MagicMock()
        monkeypatch.setattr(libvirt, "open", lambda _uri: conn)
        monkeypatch.setattr(orch_mod, "RunDir", MagicMock())

        # The install-network codepath now runs only when at least one
        # VM has a builder that needs an install phase, so plant a stub
        # cloud-init-style VM.
        stub_vm = MagicMock()
        stub_vm.builder.needs_install_phase.return_value = True
        stub_vm.name = "cloud"
        o = Orchestrator(vms=[stub_vm])
        o._cache = MagicMock()
        monkeypatch.setattr(
            o,
            "_cleanup_stale_install_networks",
            lambda: calls.append("cleanup"),
        )
        # Short-circuit provisioning after the first step.
        def _fake_create(_run_id: str) -> MagicMock:
            calls.append("create")
            raise RuntimeError("stop here")
        monkeypatch.setattr(o, "_create_install_network", _fake_create)

        with pytest.raises(RuntimeError, match="stop here"):
            o.__enter__()

        assert calls == ["cleanup", "create"]


class TestTeardownIsIdempotent:
    def test_teardown_without_enter_is_safe(self) -> None:
        o = Orchestrator()
        o._teardown()  # must not raise even though _conn is None

    def test_teardown_clears_state(self) -> None:
        o = Orchestrator()
        o._conn = MagicMock()
        o._run = MagicMock()
        o._install_network = None
        o._teardown()
        assert o._conn is None
        assert o._run is None


class TestProvisionInstallFreeVMs:
    """Builders with needs_install_phase()=False (NoOpBuilder) skip the
    install phase; when every VM uses one, the install network is
    never created at all."""

    def _make_noop_vm(self, tmp_path, name="byoi"):
        from testrange import NoOpBuilder
        from testrange.backends.libvirt.vm import VM
        from testrange.credentials import Credential
        from testrange.devices import VirtualNetworkRef

        src = tmp_path / f"{name}.qcow2"
        src.write_bytes(b"stub")
        return VM(
            name=name,
            iso=str(src),
            users=[Credential("deploy", "pw")],
            builder=NoOpBuilder(),
            communicator="ssh",
            devices=[VirtualNetworkRef("Net", ip="10.0.0.5")],
        )

    def test_all_install_free_skips_install_network(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If every VM uses a no-install builder, _create_install_network
        must not run."""
        from testrange.backends.libvirt.network import VirtualNetwork

        net = VirtualNetwork("Net", "10.0.0.0/24", internet=True, dhcp=False)

        vm = self._make_noop_vm(tmp_path)
        # Don't actually execute a build — just record the call shape.
        vm.build = MagicMock(return_value=tmp_path / "staged.qcow2")  # type: ignore[method-assign]
        vm.start_run = MagicMock()  # type: ignore[method-assign]

        o = Orchestrator(networks=[net], vms=[vm])
        run = MagicMock()
        run.run_id = "deadbeef-byoi-prebuilt-only"
        o._conn = MagicMock()
        o._cache = MagicMock()

        # Short-circuit network start so we don't touch real libvirt.
        monkeypatch.setattr(net, "start", MagicMock())

        cleanup = MagicMock()
        create = MagicMock()
        monkeypatch.setattr(o, "_cleanup_stale_install_networks", cleanup)
        monkeypatch.setattr(o, "_create_install_network", create)

        o._provision(run)

        cleanup.assert_not_called()
        create.assert_not_called()
        # VM was built with empty install-network args (the no-install signal).
        build_kwargs = vm.build.call_args.kwargs
        assert build_kwargs["install_network_name"] == ""
        assert build_kwargs["install_network_mac"] == ""

    def test_mixed_install_network_excludes_install_free_vms(
        self,
        tmp_path: Path,
    ) -> None:
        """_create_install_network only registers VMs whose builders
        want an install phase."""
        from testrange.backends.libvirt.vm import VM
        from testrange.credentials import Credential
        from testrange.devices import VirtualNetworkRef

        noop_vm = self._make_noop_vm(tmp_path, name="byoi")
        cloud = VM(
            name="cloud",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[VirtualNetworkRef("Net")],
        )

        o = Orchestrator(
            networks=[],
            vms=[cloud, noop_vm],
        )
        o._conn = MagicMock()
        o._conn.listNetworks.return_value = []
        o._conn.listDefinedNetworks.return_value = []

        # Call the internal create method directly — it's the unit we care about.
        install_net = o._create_install_network("abcd1234")

        # Only 'cloud' should be registered; 'byoi' must be skipped.
        vm_names = [entry[0] for entry in install_net._vm_entries]
        assert vm_names == ["cloud"]


class TestMemoryPreflight:
    """Memory preflight refuses plans that would push the target host's
    RAM usage past TESTRANGE_MEMORY_THRESHOLD (default 85%).  The check
    reads the *target* host's ``/proc/meminfo`` via the storage
    transport — so for ``qemu+ssh://`` it reports the remote."""

    def _meminfo(self, total_gib: float, available_gib: float):
        from testrange.backends.libvirt._preflight import MemInfo

        return MemInfo(
            total_bytes=int(total_gib * 1024**3),
            available_bytes=int(available_gib * 1024**3),
        )

    def _vm(self, name: str, memory_gib: float):
        from testrange import (
            VM, Credential, HardDrive, Memory, VirtualNetworkRef, vCPU,
        )

        return VM(
            name=name,
            iso="x",
            users=[Credential("root", "pw")],
            devices=[
                vCPU(1), Memory(memory_gib), HardDrive(10),
                VirtualNetworkRef("NetA"),
            ],
        )

    def test_passes_when_under_threshold(self) -> None:
        from testrange.backends.libvirt._preflight import check_memory

        # 16 GiB host, 2 GiB in use, declaring 4 GiB → projected ~37%.
        check_memory(
            self._meminfo(total_gib=16, available_gib=14),
            declared_gib={"vm1": 4.0},
            threshold=0.85,
        )  # must not raise

    def test_raises_at_threshold(self) -> None:
        from testrange.backends.libvirt._preflight import check_memory

        # 16 GiB host, 1 GiB in use, declaring 13 GiB → projected ~87%.
        with pytest.raises(OrchestratorError, match="Memory preflight"):
            check_memory(
                self._meminfo(total_gib=16, available_gib=15),
                declared_gib={"big": 13.0},
                threshold=0.85,
            )

    def test_nested_hypervisor_not_double_counted(self) -> None:
        """Only the outer VM list feeds ``declared_gib_per_vm`` — inner
        VMs live inside the hypervisor's allocation so counting them
        would over-report."""
        from testrange import (
            Credential, HardDrive, Hypervisor, LibvirtOrchestrator,
            Memory, VirtualNetwork, VirtualNetworkRef, vCPU,
        )
        from testrange.backends.libvirt._preflight import declared_gib_per_vm

        inner_vms = [self._vm(f"inner{i}", 1.0) for i in range(3)]
        hv = Hypervisor(
            name="hv",
            iso="x",
            users=[Credential("root", "pw")],
            devices=[
                vCPU(2), Memory(4), HardDrive(20),
                VirtualNetworkRef("Outer"),
            ],
            orchestrator=LibvirtOrchestrator,
            networks=[VirtualNetwork("Inner", "10.42.0.0/24")],
            vms=inner_vms,
        )
        gib = declared_gib_per_vm([hv])
        # The 3 inner VMs (3 GiB) must NOT show up — only the hv itself.
        assert gib == {"hv": 4.0}

    def test_env_var_override_disables_check(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Values > 1.0 effectively disable the preflight — useful for
        debug sessions where the user accepts the risk."""
        from testrange.backends.libvirt._preflight import check_memory

        monkeypatch.setenv("TESTRANGE_MEMORY_THRESHOLD", "10")
        # 16 GiB host, already at 99% — declaring another 8 GiB.  Would
        # normally blow the default 85% check out of the water; a 10x
        # threshold lets even a grossly overcommitted plan through.
        check_memory(
            self._meminfo(total_gib=16, available_gib=0.16),
            declared_gib={"big": 8.0},
        )  # must not raise — effective bypass

    def test_env_var_bad_value_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An invalid env value must fail loud — silently running a
        different check than the user asked for is the worst option."""
        from testrange.backends.libvirt._preflight import check_memory

        monkeypatch.setenv("TESTRANGE_MEMORY_THRESHOLD", "banana")
        with pytest.raises(OrchestratorError, match="not a number"):
            check_memory(
                self._meminfo(total_gib=16, available_gib=14),
                declared_gib={"vm1": 1.0},
            )

    def test_error_message_lists_per_vm_breakdown(self) -> None:
        """The error names every VM + its GiB so the user can tell
        which one to shrink without re-reading the config."""
        from testrange.backends.libvirt._preflight import check_memory

        with pytest.raises(OrchestratorError) as excinfo:
            check_memory(
                self._meminfo(total_gib=16, available_gib=15),
                declared_gib={"sidecar": 2.0, "hv": 12.0},
                threshold=0.85,
            )
        msg = str(excinfo.value)
        assert "sidecar: 2.00 GiB" in msg
        assert "hv: 12.00 GiB" in msg
        assert "threshold 85%" in msg
        assert "TESTRANGE_MEMORY_THRESHOLD" in msg

    def test_reads_meminfo_via_transport(self) -> None:
        """The check must read the TARGET host's /proc/meminfo via the
        transport — so for ``qemu+ssh://user@host`` it reads the remote
        box, not this machine.  Regression guard on that plumbing."""
        from testrange.backends.libvirt._preflight import read_meminfo

        fake = MagicMock()
        fake.read_bytes.return_value = (
            b"MemTotal:       16000000 kB\n"
            b"MemFree:          500000 kB\n"
            b"MemAvailable:    2000000 kB\n"
            b"Buffers:          100000 kB\n"
        )
        info = read_meminfo(fake)
        fake.read_bytes.assert_called_once_with("/proc/meminfo")
        assert info.total_bytes == 16_000_000 * 1024
        assert info.available_bytes == 2_000_000 * 1024
        assert info.used_bytes == (16_000_000 - 2_000_000) * 1024

    def test_parse_meminfo_missing_fields_raises(self) -> None:
        from testrange.backends.libvirt._preflight import _parse_meminfo

        with pytest.raises(OrchestratorError, match="MemTotal and MemAvailable"):
            _parse_meminfo("Buffers: 100 kB\n")  # no MemTotal/MemAvailable
