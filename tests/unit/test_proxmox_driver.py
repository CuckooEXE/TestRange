"""ProxmoxDriver keystone (PVE-1): construction, lifecycle, naming, L2, preflight.

No real PVE and no proxmoxer install: a stateful fake ``ProxmoxClient`` (and a
fake proxmoxer-style chained API for the SDN calls) is injected, so the driver's
implemented surface is exercised end-to-end while the SDK stays optional.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import Communicator, NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import driver_for_name
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.proxmox import ProxmoxDriver, ProxmoxHypervisor, ProxmoxProfile
from testrange.drivers.proxmox._client import ProxmoxConn
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.build import resolve_build_switch
from testrange.vms import VMRecipe, VMSpec

_Addr = DHCPAddr | StaticAddr | None


class _FakeApi:
    """A proxmoxer-style chained API modelling just the SDN endpoints used.

    Supports ``api.cluster.sdn.vnets.post(...)`` (attribute chaining) and
    ``api.cluster.sdn.vnets(vid).delete()`` (call = path segment). Keeps zones
    and vnets in dicts so a create→destroy round-trip is observable.
    """

    def __init__(
        self,
        bridges: frozenset[str] = frozenset({"vmbr0"}),
        content: str = "images,iso,vztmpl,import",
    ) -> None:
        self.zones: dict[str, dict[str, Any]] = {}
        self.vnets: dict[str, dict[str, Any]] = {}
        self.subnets: list[dict[str, Any]] = []
        self.fw_options: list[dict[str, Any]] = []
        self.fw_rules: list[dict[str, Any]] = []
        self.bridges = bridges
        self.content = content
        self.applied = 0
        # PVE-38: model the real backend faulting. Maps an exact REST path to an
        # exception instance (a real ``proxmoxer.core.ResourceException`` in the
        # translation test) so we can prove the driver boundary converts a raw
        # proxmoxer error into ``DriverError`` instead of leaking it (PVE-39).
        self.raise_for: dict[str, BaseException] = {}

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path in self.raise_for:
            raise self.raise_for[path]
        if path.startswith("nodes/") and path.endswith("/network") and method == "get":
            return [{"iface": b, "type": "bridge"} for b in sorted(self.bridges)]
        if path.startswith("storage/") and method == "get":
            return {"content": self.content, "path": "/var/lib/vz"}
        if path == "cluster/sdn/zones" and method == "get":
            return [{"zone": z, **v} for z, v in self.zones.items()]
        if path == "cluster/sdn/zones" and method == "post":
            self.zones[kwargs["zone"]] = {"type": kwargs.get("type")}
            return None
        if path.startswith("cluster/sdn/zones/") and method == "delete":
            self.zones.pop(path.rsplit("/", 1)[1], None)
            return None
        if path == "cluster/sdn/vnets" and method == "get":
            return [{"vnet": v, **d} for v, d in self.vnets.items()]
        if path == "cluster/sdn/vnets" and method == "post":
            self.vnets[kwargs["vnet"]] = {"zone": kwargs.get("zone"), "alias": kwargs.get("alias")}
            return None
        if path.endswith("/subnets") and method == "post":
            # PVE derives the subnet id as <zone>-<network>-<mask> and exposes
            # both the id (``subnet``) and the CIDR (``cidr``) on GET; model that
            # faithfully so the driver's idempotency (by cidr) and teardown (by
            # id) paths are exercised against the real shape, not the raw POST.
            vid = path.split("/")[3]
            zone = self.vnets.get(vid, {}).get("zone")
            net, mask = kwargs["subnet"].split("/")
            self.subnets.append(
                {
                    "vnet": vid,
                    "subnet": f"{zone}-{net}-{mask}",
                    "cidr": kwargs["subnet"],
                    "gateway": kwargs.get("gateway"),
                    "type": kwargs.get("type"),
                }
            )
            return None
        if path.endswith("/subnets") and method == "get":
            return [s for s in self.subnets if s["vnet"] == path.split("/")[3]]
        if "/subnets/" in path and method == "delete":
            sid = path.rsplit("/", 1)[1]
            self.subnets = [s for s in self.subnets if s["subnet"] != sid]
            return None
        if path.endswith("/firewall/options") and method == "put":
            self.fw_options.append({"vnet": path.split("/")[3], **kwargs})
            return None
        if path.endswith("/firewall/rules") and method == "post":
            # PVE prepends each posted rule at pos 0 (confirmed live, PVE-37), so
            # fw_rules is kept in evaluation order (index 0 = first evaluated).
            self.fw_rules.insert(0, {"vnet": path.split("/")[3], **kwargs})
            return None
        if path.startswith("cluster/sdn/vnets/") and "/subnets" not in path and method == "delete":
            self.vnets.pop(path.rsplit("/", 1)[1], None)
            return None
        if path == "cluster/sdn" and method == "put":
            self.applied += 1
            return ""  # not a UPID → _sdn._apply does not wait
        raise AssertionError(f"unexpected API call: {method} {path} {kwargs}")


class _Endpoint:
    def __init__(self, api: _FakeApi, path: str) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "post", "put", "delete"):
            return lambda **kw: self._api._call(name, self._path, kw)
        return _Endpoint(self._api, f"{self._path}/{name}")

    def __call__(self, *args: Any) -> _Endpoint:
        seg = "/".join(str(a) for a in args)
        return _Endpoint(self._api, f"{self._path}/{seg}")


class _FakeClient:
    """Duck-typed stand-in for ``ProxmoxClient`` (api/node/storage/zone/...)."""

    def __init__(
        self,
        *,
        node: str = "ns1001849",
        storage: str = "local",
        bridges: frozenset[str] = frozenset({"vmbr0"}),
        content: str = "images,iso,vztmpl,import",
    ):
        self.api = _FakeApi(bridges, content)
        self._node = node
        self._storage = storage
        self.connected = False
        self.closed = False
        self.waited: list[str] = []

    @property
    def node(self) -> str:
        return self._node

    @property
    def storage(self) -> str:
        return self._storage

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def wait_task(self, upid: str, *, timeout: float = 600.0) -> None:
        self.waited.append(upid)


def _conn(**kw: Any) -> ProxmoxConn:
    return ProxmoxConn(host="pve.example", node="ns1001849", **kw)


def _driver(
    client: _FakeClient | None = None, *, uplinks: dict[str, str] | None = None
) -> ProxmoxDriver:
    # Default maps each logical uplink name to an identically-named host bridge,
    # so the bridge-existence checks (vmbr0 exists on the fake; vmbr9 does not)
    # exercise as before while the names stay "mapped" for unknown_uplink_findings.
    return ProxmoxDriver(
        _conn(),
        client=client or _FakeClient(),  # type: ignore[arg-type]
        uplinks=uplinks if uplinks is not None else {"vmbr0": "vmbr0", "vmbr9": "vmbr9"},
    )


def _vm(name: str = "web", *, addr: _Addr = None, comm: Communicator | None = None) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive("pool1", 8), NetworkIface("netA", addr=addr)],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
        ),
        communicator=comm or SSHCommunicator("u"),
    )


def _plan(switch: Switch, *, addr: _Addr = None, comm: Communicator | None = None) -> Plan:
    return Plan(
        "t",
        ProxmoxHypervisor(
            networks=[switch],
            pools=[StoragePool("pool1", 8)],
            vms=[_vm(addr=addr, comm=comm)],
        ),
    )


class TestConstruction:
    def test_satisfies_abc(self) -> None:
        assert isinstance(_driver(), HypervisorDriver)

    def test_registry_dispatch_by_name_roundtrips_uri(self) -> None:
        # The orchestrator persists the *resolved* teardown URI (driver.uri), an
        # internal serialization carrying storage/ssh so cleanup rebuilds
        # faithfully. It round-trips through from_uri/to_uri.
        uri = (
            ProxmoxProfile(host="pve.example", password="s3cret", node="ns1001849")
            .build_driver()
            .uri
        )
        d = driver_for_name("ProxmoxDriver", uri)
        assert isinstance(d, ProxmoxDriver)
        assert d.uri == uri

    def test_teardown_uri_is_proxmox_scheme(self) -> None:
        uri = ProxmoxProfile(host="pve.example").build_driver().uri
        assert uri.startswith("proxmox://")

    def test_profile_uplinks_land_on_driver(self) -> None:
        # ADR-0016: the profile's [uplinks] map rides on the driver, which resolves
        # logical Switch.uplink names to host bridges. The build switch itself is
        # portable topology on the Hypervisor now, not a profile knob.
        drv = ProxmoxProfile(host="h", uplinks={"egress": "vmbr9"}).build_driver()
        assert drv._uplinks == {"egress": "vmbr9"}


class TestLifecycle:
    def test_connect_disconnect_delegate_to_client(self) -> None:
        c = _FakeClient()
        d = _driver(c)
        d.connect()
        d.disconnect()
        assert c.connected and c.closed

    def test_connect_uses_generous_http_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-10: proxmoxer's 5s default aborts large image uploads; connect()
        # must raise the per-request timeout when it builds the real ProxmoxAPI.
        import proxmoxer

        from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn

        captured: dict[str, Any] = {}

        class _ProbeAPI:
            def __init__(self, _host: str, **kw: Any) -> None:
                captured.update(kw)

            def nodes(self, _node: str) -> _ProbeAPI:
                return self

            @property
            def status(self) -> _ProbeAPI:
                return self

            def get(self) -> dict[str, Any]:
                return {}

        monkeypatch.setattr(proxmoxer, "ProxmoxAPI", _ProbeAPI)
        ProxmoxClient(ProxmoxConn(host="h", node="n")).connect()
        assert captured["timeout"] >= 60

    def test_connect_autodetects_single_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # node="" => resolve the host's sole node at connect (authors needn't
        # name it on a single-node host).
        import proxmoxer

        from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn

        class _NodeEp:
            @property
            def status(self) -> _NodeEp:
                return self

            def get(self) -> dict[str, Any]:
                return {}

        class _Nodes:
            def get(self) -> list[dict[str, str]]:
                return [{"node": "solo"}]

            def __call__(self, _node: str) -> _NodeEp:
                return _NodeEp()

        class _API:
            def __init__(self, _host: str, **_kw: Any) -> None: ...

            @property
            def nodes(self) -> _Nodes:
                return _Nodes()

        monkeypatch.setattr(proxmoxer, "ProxmoxAPI", _API)
        client = ProxmoxClient(ProxmoxConn(host="h"))  # node="" => auto-detect
        client.connect()
        assert client.node == "solo"

    def test_connect_multi_node_without_node_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import proxmoxer

        from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn
        from testrange.exceptions import DriverError

        class _Nodes:
            def get(self) -> list[dict[str, str]]:
                return [{"node": "a"}, {"node": "b"}]

        class _API:
            def __init__(self, _host: str, **_kw: Any) -> None: ...

            @property
            def nodes(self) -> _Nodes:
                return _Nodes()

        monkeypatch.setattr(proxmoxer, "ProxmoxAPI", _API)
        with pytest.raises(DriverError, match="2 nodes"):
            ProxmoxClient(ProxmoxConn(host="h")).connect()

    def test_sftp_put_feeds_two_arg_paramiko_callback(self, tmp_path: Path) -> None:
        # PVE-23 regression: paramiko's put callback is (transferred, total) — a
        # 2-arg call. sftp_put must adapt it to ProgressReporter.update(done) and
        # not blow up with "takes 2 positional arguments but 3 were given".
        from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn

        class _FakeSftp:
            def __init__(self) -> None:
                self.put_calls: list[tuple[str, str]] = []

            def stat(self, _p: str) -> object:
                return object()  # every dir exists → no mkdir

            def put(self, local: str, remote: str, callback: Any = None) -> None:
                self.put_calls.append((local, remote))
                if callback is not None:
                    callback(48, 94)  # paramiko hands (transferred, total)
                    callback(94, 94)

            def close(self) -> None: ...

        class _FakeSSH:
            def __init__(self, sftp: _FakeSftp) -> None:
                self._sftp = sftp

            def open_sftp(self) -> _FakeSftp:
                return self._sftp

        src = tmp_path / "img.qcow2"
        src.write_bytes(b"x" * 94)
        client = ProxmoxClient(ProxmoxConn(host="h"))
        sftp = _FakeSftp()
        client._ssh = _FakeSSH(sftp)  # bypass real SSH (paramiko)
        client.sftp_put(src, "/var/lib/vz/import/img.qcow2")
        assert sftp.put_calls == [(str(src), "/var/lib/vz/import/img.qcow2")]


class TestNaming:
    def test_resource_name_is_pve_dns_label(self) -> None:
        name = _driver().compose_resource_name("abcdef1234", "vm", "Web_Server")
        assert name.replace("-", "").isalnum() and name.islower()

    def test_mac_is_locally_administered_and_stable(self) -> None:
        d = _driver()
        m1 = d.compose_mac("plan", "web", 0)
        assert m1 == d.compose_mac("plan", "web", 0)
        assert int(m1.split(":")[0], 16) & 0x02  # locally-administered bit

    def test_volume_ref_folds_in_storage(self) -> None:
        ref = _driver(_FakeClient(storage="local")).compose_volume_ref("pool1", "web.qcow2")
        assert str(ref).startswith("local:")

    def test_volume_suffix(self) -> None:
        assert _driver().volume_suffix("run_disk") == ".qcow2"
        assert _driver().volume_suffix("build_seed") == ".iso"


class TestL2:
    def test_create_switch_makes_zone_and_vnet_then_applies(self) -> None:
        c = _FakeClient()
        d = _driver(c)
        assert (
            d.create_switch(Switch("sw1", Network("a"), cidr="10.0.0.0/24"), "tr-switch-x") is None
        )
        # The zone is the driver's per-run zone (tr<hex>, <=8 chars), not an
        # author-supplied name.
        assert d._sdn_zone in c.api.zones
        assert d._sdn_zone.startswith("tr") and len(d._sdn_zone) <= 8
        assert len(c.api.vnets) == 1
        assert c.api.applied == 1

    def test_raw_proxmoxer_error_translated_to_driver_error(self) -> None:
        # PVE-38/39 (H1): a raw proxmoxer exception escaping a REST call must
        # surface as DriverError at the driver boundary. The orchestrator's
        # teardown/error handling keys on DriverError, so a leaked
        # ResourceException (a 403, a transient 595) would bypass cleanup. The
        # fake raises the *real* type so this would fail loudly if the boundary
        # ever stopped translating.
        pytest.importorskip("proxmoxer")
        from proxmoxer.core import ResourceException

        c = _FakeClient()
        c.api.raise_for["cluster/sdn/vnets"] = ResourceException(403, "Permission denied", "")
        d = _driver(c)
        with pytest.raises(DriverError):
            d.create_switch(Switch("sw1", Network("a"), cidr="10.0.0.0/24"), "tr-switch-x")

    def test_per_run_zone_is_unique_per_driver(self) -> None:
        # Two runs (two driver instances) get distinct zones — no cross-run
        # commingling in a shared zone.
        assert _driver()._sdn_zone != _driver()._sdn_zone

    def test_create_network_resolves_to_switch_vnet(self) -> None:
        d = _driver()
        sw = Switch("sw1", Network("a"), cidr="10.0.0.0/24")
        d.create_switch(sw, "tr-switch-x")
        ref = d.create_network(Network("a"), sw, "tr-net-a", switch_backend_name="tr-switch-x")
        # all networks on a switch share its vnet id
        assert isinstance(ref, str) and ref.startswith("v")

    def test_create_then_destroy_switch_roundtrips_clean(self) -> None:
        c = _FakeClient()
        d = _driver(c)
        d.create_switch(Switch("sw1", Network("a"), cidr="10.0.0.0/24"), "tr-switch-x")
        d.destroy_switch("tr-switch-x")
        assert c.api.vnets == {}
        assert c.api.zones == {}  # zone dropped once its last vnet is gone

    def test_guest_gateway_is_ssh_jump_through_the_host(self) -> None:
        # ORCH-16: guests live on isolated SDN vnets the off-box orchestrator
        # can't route to, so SSH transports jump through the PVE host (reusing
        # the SFTP host creds). QGA transports don't consult this.
        from testrange.drivers.proxmox._client import ProxmoxConn
        from testrange.gateways import SSHJumpGateway

        d = ProxmoxDriver(
            ProxmoxConn(host="pve.example", user="root@pam", password="secret"),
            client=_FakeClient(),  # type: ignore[arg-type]
        )
        gw = d.guest_gateway()
        assert isinstance(gw, SSHJumpGateway)
        assert gw.host == "pve.example"
        assert gw.username == "root"  # ssh_user default
        assert gw.password == "secret"  # ssh_password falls back to the API password

    def test_mgmt_switch_adds_subnet_with_dot2_gateway(self) -> None:
        # PVE-44 / ADR-0009(B): a mgmt=True Switch plants the host .2 adapter as
        # an SDN subnet (gateway=.2) on the vnet — the PVE analog of libvirt's
        # <ip address=.2>. No SNAT/DHCP keys: it is a pure host adapter.
        c = _FakeClient()
        d = _driver(c)
        d.create_switch(Switch("sw1", Network("a"), cidr="10.30.0.0/24", mgmt=True), "tr-switch-x")
        assert len(c.api.subnets) == 1
        sub = c.api.subnets[0]
        assert sub["cidr"] == "10.30.0.0/24"
        assert sub["gateway"] == "10.30.0.2"
        assert sub["type"] == "subnet"
        assert "snat" not in sub and "dhcp-range" not in sub

    def test_non_mgmt_switch_adds_no_subnet(self) -> None:
        c = _FakeClient()
        _driver(c).create_switch(Switch("sw1", Network("a"), cidr="10.0.0.0/24"), "tr-switch-x")
        assert c.api.subnets == []

    def test_destroy_mgmt_switch_drops_subnet_then_vnet(self) -> None:
        # PVE refuses to delete a vnet that still holds subnets, so destroy_switch
        # must drop the subnet first — and do so self-discovering (no mgmt flag).
        c = _FakeClient()
        d = _driver(c)
        d.create_switch(Switch("sw1", Network("a"), cidr="10.30.0.0/24", mgmt=True), "tr-switch-x")
        d.destroy_switch("tr-switch-x")
        assert c.api.subnets == []
        assert c.api.vnets == {}
        assert c.api.zones == {}

    def test_create_switch_mgmt_is_idempotent(self) -> None:
        # A re-entrant create_switch (crash-recovery) must not double-post the subnet.
        c = _FakeClient()
        d = _driver(c)
        sw = Switch("sw1", Network("a"), cidr="10.30.0.0/24", mgmt=True)
        d.create_switch(sw, "tr-switch-x")
        d.create_switch(sw, "tr-switch-x")
        assert len(c.api.subnets) == 1

    def test_uplink_nat_switch_returns_resolved_bridge(self) -> None:
        # ADR-0016: the sidecar's eth1 attaches to the host bridge the profile's
        # [uplinks] resolves the logical name to (here egress→vmbr0). Egress is
        # out-of-band: the bridge is operator-owned, not created or torn down.
        c = _FakeClient()
        d = _driver(c, uplinks={"egress": "vmbr0"})
        sw = Switch(
            "sw1", Network("a"), cidr="10.0.0.0/24", uplink="egress", sidecar=Sidecar(nat=True)
        )
        assert d.create_switch(sw, "tr-switch-x") == "vmbr0"
        d.destroy_switch("tr-switch-x")
        assert c.api.vnets == {}

    def test_destroy_network_is_a_noop(self) -> None:
        # Networks own no backend object; the vnet dies with the switch.
        _driver().destroy_network("tr-net-a")

    def test_create_vm_translates_nic_bridge_to_vnet_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PVE-11: the orchestrator passes composed network names in network_refs,
        # but a PVE NIC must attach to the SDN vnet id. The uplink bridge passes
        # through unchanged.
        from testrange.drivers.proxmox import _vm as vm_mod

        d = _driver()
        sw = Switch("sw1", Network("a"), cidr="10.0.0.0/24")
        d.create_switch(sw, "tr-switch-x")
        vnet = d.create_network(Network("a"), sw, "tr-net-a", switch_backend_name="tr-switch-x")

        captured: dict[str, Any] = {}
        monkeypatch.setattr(vm_mod, "create_vm", lambda *a, **kw: captured.update(kw) or "vm:1")
        spec = VMSpec(
            name="v", devices=[CPU(1), Memory(512), OSDrive("pool1", 8), NetworkIface("a")]
        )
        d.create_vm(
            "tr-vm",
            spec,
            "plan",
            os_disk_ref=VolumeRef("local:import/x"),
            seed_iso_ref=None,
            network_refs={"a": "tr-net-a", "__uplink__sw1": "vmbr0"},
        )
        assert captured["network_refs"]["a"] == vnet  # composed name → vnet id
        assert captured["network_refs"]["__uplink__sw1"] == "vmbr0"  # uplink passthrough


class TestPreflight:
    def _run(self, plan: Plan, *, build_switch: Switch | None = None, client: Any = None) -> Any:
        d = _driver(client)
        # Mirror the orchestrator: build_switch reaches preflight as a concrete
        # Switch (resolve_build_switch synthesizes the default when None).
        resolved = resolve_build_switch(build_switch)
        return d.preflight(plan, cache_manager=None, build_switch=resolved)  # type: ignore[arg-type]

    def test_isolated_static_plan_is_clean(self) -> None:
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")))
        assert bool(report), report.render()

    def test_uplink_nat_ok_when_bridge_exists(self) -> None:
        # vmbr0 is the existing host bridge the fake reports; uplink+nat is now
        # supported (sidecar eth1 bridges to it).
        sw = Switch(
            "sw1", Network("netA"), cidr="10.0.0.0/24", uplink="vmbr0", sidecar=Sidecar(nat=True)
        )
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")))
        assert bool(report), report.render()

    def test_uplink_bridge_missing_is_rejected(self) -> None:
        sw = Switch(
            "sw1", Network("netA"), cidr="10.0.0.0/24", uplink="vmbr9", sidecar=Sidecar(nat=True)
        )
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")))
        assert "proxmox-uplink-bridge-missing" in {f.code for f in report.findings}

    def test_build_switch_uplink_bridge_is_checked(self) -> None:
        # The transient build switch's uplink (resolved from build_switch) is verified too.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        build = Switch(
            "build", Network("b"), cidr="10.97.99.0/24", uplink="vmbr9", sidecar=Sidecar(nat=True)
        )
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")), build_switch=build)
        assert "proxmox-uplink-bridge-missing" in {f.code for f in report.findings}

    def test_mgmt_is_accepted(self) -> None:
        # PVE-44 / ADR-0009(B): Proxmox realizes mgmt (host .2 on the vnet), so it
        # drops the shared mgmt_unsupported gate — a mgmt=True plan preflights clean.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24", mgmt=True)
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")))
        assert "mgmt-unsupported" not in {f.code for f in report.findings}
        assert bool(report), report.render()

    def test_native_communicator_ok_with_qga(self) -> None:
        # A NativeCommunicator plan has no extra preflight gate; it passes clean.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24"), comm=NativeCommunicator()))
        assert bool(report), report.render()

    def test_dhcp_addressing_ok_with_qga(self) -> None:
        # A DHCP-addressed plan preflights clean.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        report = self._run(_plan(sw, addr=DHCPAddr()))
        assert bool(report), report.render()

    def test_import_content_missing_is_rejected(self) -> None:
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        client = _FakeClient(content="images,iso,vztmpl")  # no 'import'
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")), client=client)
        assert "proxmox-import-content-missing" in {f.code for f in report.findings}


class _CapStatusEp:
    @staticmethod
    def get() -> dict[str, Any]:
        # PVE /nodes/<node>/status: memory.total in bytes, cpuinfo.cpus = threads.
        return {"memory": {"total": 8589934592}, "cpuinfo": {"cpus": 8}}


class _CapNodeEp:
    status = _CapStatusEp()


class _CapApi:
    def nodes(self, _node: str) -> _CapNodeEp:
        return _CapNodeEp()


class _CapClient:
    node = "ns1001849"
    api = _CapApi()


class _BrokenClient:
    node = "ns1001849"

    @property
    def api(self) -> Any:
        raise RuntimeError("not connected")


class TestHostCapacity:
    """host_capacity() field extraction (CORE-84): bytes->MiB + cpuinfo.cpus."""

    def test_parses_node_status(self) -> None:
        d = ProxmoxDriver(_conn(), client=_CapClient())  # type: ignore[arg-type]
        cap = d.host_capacity()
        assert cap is not None
        assert cap.memory_mb == 8192  # 8 GiB
        assert cap.logical_cpus == 8

    def test_none_on_probe_failure(self) -> None:
        d = ProxmoxDriver(_conn(), client=_BrokenClient())  # type: ignore[arg-type]
        assert d.host_capacity() is None
