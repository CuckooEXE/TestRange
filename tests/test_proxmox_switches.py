"""Tests for the Switch / VirtualNetwork two-layer model on Proxmox.

Covers:

* Generic :class:`testrange.networks.Switch` spec promotion to
  :class:`ProxmoxSwitch` at orchestrator construction.
* :class:`ProxmoxSwitch` lifecycle (zone create + idempotent reuse,
  delete, leave-alone-if-pre-existing).
* Network's ``_resolve_zone`` honouring all four switch-shape
  variants (ProxmoxSwitch instance, generic AbstractSwitch
  instance, string name, None).
* Switch-aware ``__enter__`` / ``__exit__`` ordering: zones come
  up before vnets and tear down after them.

No live PVE — every interaction is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange import Switch, VirtualNetwork
from testrange.backends.proxmox import (
    ProxmoxOrchestrator,
    ProxmoxSwitch,
    ProxmoxVirtualNetwork,
)
from testrange.exceptions import NetworkError


# =====================================================================
# Switch / generic-spec promotion
# =====================================================================


class TestSwitchPromotion:
    def test_generic_switch_promoted_at_init(self) -> None:
        """A generic ``Switch`` passed to ``switches=`` becomes a
        ``ProxmoxSwitch`` field-for-field at __init__."""
        sw = Switch("Corp", switch_type="vlan", uplinks=["eno1"])
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            switches=[sw],
        )
        assert len(orch._switches) == 1
        promoted = orch._switches[0]
        assert isinstance(promoted, ProxmoxSwitch)
        assert promoted.name == "Corp"
        assert promoted.switch_type == "vlan"
        assert promoted.uplinks == ["eno1"]

    def test_native_proxmox_switch_passes_through(self) -> None:
        """An already-ProxmoxSwitch input is reused as-is — no
        promotion overhead, no double-wrapping."""
        sw = ProxmoxSwitch("Corp", uplinks=["eno1"])
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            switches=[sw],
        )
        assert orch._switches[0] is sw

    def test_unknown_switch_type_raises(self) -> None:
        with pytest.raises(NetworkError, match="switch_type"):
            ProxmoxSwitch("oops", switch_type="bogus-type")

    def test_default_switch_type_is_simple(self) -> None:
        """No type given → simple zone (matches the existing
        TestRange default-zone behaviour)."""
        sw = ProxmoxSwitch("Mine")
        assert sw.switch_type == "simple"


# =====================================================================
# ProxmoxSwitch lifecycle
# =====================================================================


class TestProxmoxSwitchLifecycle:
    def _client_with_zones(self, existing: list[dict]) -> MagicMock:
        client = MagicMock()
        client.cluster.sdn.zones.get.return_value = existing
        return client

    def test_creates_zone_when_absent(self) -> None:
        sw = ProxmoxSwitch("Corp", switch_type="vlan", uplinks=["eno1"])
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])

        sw.start(ctx)

        ctx._client.cluster.sdn.zones.post.assert_called_once()
        kwargs = ctx._client.cluster.sdn.zones.post.call_args.kwargs
        assert kwargs["type"] == "vlan"
        assert kwargs["zone"] == sw.backend_name()
        # VLAN zone takes the first uplink as the bridge.
        assert kwargs["bridge"] == "eno1"
        # ``dhcp = "dnsmasq"`` lives at zone scope per PVE 9.x SDN
        # schema (subnets reject the field as unknown); switches must
        # set it the same way the orchestrator's default zone does
        # so per-vnet dnsmasq spawning works in user-defined zones too.
        assert kwargs["dhcp"] == "dnsmasq"
        # SDN config applied via the empty PUT.
        ctx._client.cluster.sdn.put.assert_called_once()
        assert sw._zone_created is True

    def test_idempotent_when_zone_already_exists(self) -> None:
        """If the zone is already present (from a prior run, or a
        sibling orchestrator), accept it as ours.  Don't post,
        don't mark it created — so teardown leaves it alone."""
        sw = ProxmoxSwitch("Corp")
        ctx = MagicMock()
        zone_id = sw.backend_name()
        ctx._client = self._client_with_zones([{"zone": zone_id}])

        sw.start(ctx)

        ctx._client.cluster.sdn.zones.post.assert_not_called()
        assert sw._zone_created is False

    def test_simple_zone_skips_uplink_param(self) -> None:
        """Simple zones don't take a ``bridge`` parameter — they
        don't have an uplink concept.  Even if uplinks=[...] is
        declared, drop it for the simple-zone post (the API rejects
        the field)."""
        sw = ProxmoxSwitch("Plain", switch_type="simple", uplinks=["eno1"])
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])

        sw.start(ctx)

        kwargs = ctx._client.cluster.sdn.zones.post.call_args.kwargs
        assert "bridge" not in kwargs

    def test_zone_extra_merged_into_post_body(self) -> None:
        """Free-form ``zone_extra`` knobs (for VXLAN/EVPN that
        TestRange doesn't model first-class) flow through to the
        REST body."""
        sw = ProxmoxSwitch(
            "Vxlan",
            switch_type="vxlan",
            zone_extra={"peers": "10.0.0.1,10.0.0.2"},
        )
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])

        sw.start(ctx)

        kwargs = ctx._client.cluster.sdn.zones.post.call_args.kwargs
        assert kwargs["peers"] == "10.0.0.1,10.0.0.2"

    def test_mtu_forwarded(self) -> None:
        sw = ProxmoxSwitch("Mtu", mtu=9000)
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])

        sw.start(ctx)
        kwargs = ctx._client.cluster.sdn.zones.post.call_args.kwargs
        assert kwargs["mtu"] == 9000

    def test_stop_deletes_zone_we_created(self) -> None:
        sw = ProxmoxSwitch("Corp")
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])
        sw.start(ctx)
        assert sw._zone_created is True

        sw.stop(ctx)

        ctx._client.cluster.sdn.zones.assert_any_call(sw.backend_name())
        ctx._client.cluster.sdn.zones(sw.backend_name()).delete.assert_called()
        assert sw._zone_created is False

    def test_stop_leaves_pre_existing_zone_alone(self) -> None:
        """Zone wasn't created by this start() (pre-existing) →
        stop() must NOT delete it."""
        sw = ProxmoxSwitch("Corp")
        ctx = MagicMock()
        ctx._client = self._client_with_zones(
            [{"zone": sw.backend_name()}],
        )
        sw.start(ctx)

        sw.stop(ctx)

        ctx._client.cluster.sdn.zones.return_value.delete.assert_not_called()

    def test_stop_swallows_delete_errors(self) -> None:
        """ABC contract — teardown never raises."""
        sw = ProxmoxSwitch("Corp")
        ctx = MagicMock()
        ctx._client = self._client_with_zones([])
        sw.start(ctx)
        # Make delete blow up.
        ctx._client.cluster.sdn.zones(
            sw.backend_name()
        ).delete.side_effect = RuntimeError("PVE busy")

        sw.stop(ctx)  # must not raise

    def test_backend_name_truncates_to_eight_chars(self) -> None:
        """PVE caps zone IDs at 8 chars of lowercase alphanumerics."""
        sw = ProxmoxSwitch("MyVeryLongCorporateNetwork-2025")
        bn = sw.backend_name()
        assert len(bn) <= 8
        assert bn == bn.lower()
        assert all(c.isalnum() for c in bn)


# =====================================================================
# Network's _resolve_zone honours every switch shape
# =====================================================================


class TestNetworkResolvesSwitch:
    def _orch_with_switches(
        self, switches: list[ProxmoxSwitch],
    ) -> ProxmoxOrchestrator:
        return ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            switches=switches,
        )

    def test_proxmox_switch_instance(self) -> None:
        sw = ProxmoxSwitch("Corp")
        net = ProxmoxVirtualNetwork(
            "Mgmt", "10.0.10.0/24", switch=sw,
        )
        orch = self._orch_with_switches([sw])
        # _resolve_zone returns the switch's backend zone ID.
        assert net._resolve_zone(orch) == sw.backend_name()

    def test_generic_switch_instance_looked_up_by_name(self) -> None:
        """The user passed the same generic ``Switch`` to both the
        orchestrator's ``switches=`` and the network's ``switch=``.
        Promotion creates a fresh ``ProxmoxSwitch`` on the
        orchestrator side; the network still references the
        original generic.  Resolver must look it up by name."""
        sw = Switch("Corp", switch_type="vlan", uplinks=["eno1"])
        net = ProxmoxVirtualNetwork(
            "Mgmt", "10.0.10.0/24", switch=sw,
        )
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            switches=[sw],
            networks=[net],
        )
        promoted = orch._switches[0]
        assert net._resolve_zone(orch) == promoted.backend_name()

    def test_string_name_lookup(self) -> None:
        sw = ProxmoxSwitch("Corp")
        net = ProxmoxVirtualNetwork(
            "Mgmt", "10.0.10.0/24", switch="Corp",
        )
        orch = self._orch_with_switches([sw])
        assert net._resolve_zone(orch) == sw.backend_name()

    def test_unknown_switch_name_raises(self) -> None:
        net = ProxmoxVirtualNetwork(
            "Mgmt", "10.0.10.0/24", switch="DoesNotExist",
        )
        orch = self._orch_with_switches([ProxmoxSwitch("Other")])
        with pytest.raises(NetworkError, match="not found"):
            net._resolve_zone(orch)

    def test_no_switch_falls_back_to_default_zone(self) -> None:
        """A ``VirtualNetwork`` without a ``switch=`` keeps the
        pre-Switch behaviour: lands in the orchestrator's default
        zone (``self._zone``).  This is the backwards-compat
        guarantee — every existing test that didn't know about
        switches works unchanged."""
        net = ProxmoxVirtualNetwork("Mgmt", "10.0.10.0/24")
        orch = self._orch_with_switches([])
        assert net._resolve_zone(orch) == orch._zone


# =====================================================================
# __enter__ ordering: switches up before vnets
# =====================================================================


class TestEnterStartsSwitchesFirst:
    def test_start_switches_called_before_start_networks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Vnets reference their switch's zone, so switch.start()
        must complete before any vnet.start() runs."""
        order: list[str] = []

        def _start_switches(self):
            order.append("switches")

        def _start_networks(self):
            order.append("networks")

        # Patch out everything except the ordering we care about.
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_start_switches", _start_switches,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_start_networks", _start_networks,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_setup_vm_networks", lambda self: None,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_warn_if_unroutable", lambda self: None,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_provision_vms", lambda self: None,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_enter_nested_orchestrators",
            lambda self: None,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_resolve_node",
            lambda self, nodes: setattr(self, "_node", "pve01"),
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_resolve_storage",
            lambda self: setattr(self, "_storage", "local-lvm"),
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_ensure_sdn_zone", lambda self: None,
        )
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_preflight_dnsmasq_installed",
            lambda self: None,
        )

        client = MagicMock()
        client.nodes.get.return_value = [{"node": "pve01"}]
        monkeypatch.setattr(
            "proxmoxer.ProxmoxAPI", lambda *a, **kw: client,
        )

        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
        )
        # Force the install-network path off so that codepath
        # doesn't add noise to the order list.
        orch._vm_list = []

        with orch:
            pass

        # Switches strictly before networks.
        assert order.index("switches") < order.index("networks")


# =====================================================================
# Backwards compat — pre-Switch usage still works
# =====================================================================


class TestBackwardsCompat:
    def test_orchestrator_without_switches_kwarg(self) -> None:
        """Constructing without ``switches=`` is legal (every
        existing test does this).  ``self._switches`` is empty;
        every vnet uses the default zone."""
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            networks=[ProxmoxVirtualNetwork("Mgmt", "10.0.10.0/24")],
        )
        assert orch._switches == []

    def test_virtual_network_without_switch_kwarg(self) -> None:
        """Constructing a network without ``switch=`` is legal —
        the field defaults to ``None`` and the resolver falls back
        to the orchestrator's default zone."""
        net = ProxmoxVirtualNetwork("Mgmt", "10.0.10.0/24")
        assert net.switch is None

    def test_libvirt_network_accepts_switch_kwarg_silently(self) -> None:
        """Portable test code may declare a ``switch=`` even on a
        libvirt network.  Libvirt has no separate switch concept;
        the field is preserved for inspection and ignored at
        runtime."""
        from testrange.backends.libvirt import VirtualNetwork as LVNet
        sw = Switch("Corp")
        net = LVNet("Mgmt", "10.0.10.0/24", switch=sw)
        assert net.switch is sw  # preserved on the instance
