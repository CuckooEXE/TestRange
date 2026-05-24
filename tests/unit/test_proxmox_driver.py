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
from testrange.drivers import driver_for, driver_for_name
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.proxmox import ProxmoxDriver, ProxmoxHypervisor
from testrange.drivers.proxmox._client import ProxmoxConn
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

_Addr = DHCPAddr | StaticAddr | None


# --- fakes ----------------------------------------------------------------


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
        self.bridges = bridges
        self.content = content
        self.applied = 0

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
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
        if path.startswith("cluster/sdn/vnets/") and method == "delete":
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


def _driver(client: _FakeClient | None = None) -> ProxmoxDriver:
    return ProxmoxDriver(_conn(), client=client or _FakeClient())  # type: ignore[arg-type]


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
        ProxmoxHypervisor(
            host="pve.example",
            password="pw",
            node="ns1001849",
            networks=[switch],
            pools=[StoragePool("pool1", 8)],
            vms=[_vm(addr=addr, comm=comm)],
        ),
        name="t",
    )


# --- construction & registry ---------------------------------------------


class TestConstruction:
    def test_satisfies_abc(self) -> None:
        assert isinstance(_driver(), HypervisorDriver)

    def test_registry_dispatch_by_hypervisor_type(self) -> None:
        hyp = ProxmoxHypervisor(host="pve.example", password="pw")
        assert isinstance(driver_for(hyp), ProxmoxDriver)

    def test_registry_dispatch_by_name_roundtrips_uri(self) -> None:
        # The orchestrator persists the *resolved* teardown URI (driver_uri), an
        # internal serialization carrying storage/ssh so cleanup rebuilds
        # faithfully. It round-trips through from_uri/to_uri.
        uri = ProxmoxHypervisor(host="pve.example", password="s3cret", node="ns1001849").driver_uri
        d = driver_for_name("ProxmoxDriver", uri)
        assert isinstance(d, ProxmoxDriver)
        assert d.uri == uri

    def test_teardown_uri_is_proxmox_scheme(self) -> None:
        uri = ProxmoxHypervisor(host="pve.example").driver_uri
        assert uri.startswith("proxmox://")

    def test_requires_host(self) -> None:
        with pytest.raises(ValueError, match="host"):
            ProxmoxHypervisor(host="")

    def test_ssh_defaults_to_api_creds(self) -> None:
        # SSH (download_from_pool only) reuses the API user/password by default;
        # root@pam -> root for the system login.
        conn = ProxmoxHypervisor(host="pve.example", password="p@ss:w/rd").conn()
        assert conn.password == "p@ss:w/rd"
        assert conn.ssh_user == "root" and conn.ssh_password == "p@ss:w/rd"

    def test_bare_username_defaults_to_pam_realm(self) -> None:
        # PVE-21: a realm-less user must auth as root@pam, not root (which 401s);
        # the default user is already root@pam. SSH still logs in as the bare user.
        assert ProxmoxHypervisor(host="pve.example", user="root").conn().user == "root@pam"
        assert ProxmoxHypervisor(host="pve.example").conn().user == "root@pam"

    def test_explicit_realm_is_preserved(self) -> None:
        assert ProxmoxHypervisor(host="pve.example", user="svc@pve").conn().user == "svc@pve"

    def test_node_defaults_to_autodetect(self) -> None:
        # No node => conn carries "" (the client resolves it at connect).
        assert ProxmoxHypervisor(host="pve.example").conn().node == ""

    def test_build_uplink_addr_threads_into_build_switch(self) -> None:
        # NET-7: a static build-uplink address on the hypervisor flows into the
        # synthesized build switch's uplink_addr (so the build sidecar's eth1 is
        # static — for host-NAT'd uplinks that won't DHCP the sidecar).
        from testrange.orchestrator.build import _build_switch

        addr = StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",))
        hyp = ProxmoxHypervisor(host="h", build_uplink="vmbr9", build_uplink_addr=addr)
        assert hyp.build_uplink_addr is addr
        sw = _build_switch(hyp.build_uplink, hyp.build_uplink_addr)
        assert sw.sidecar is not None and sw.sidecar.addr is addr and sw.sidecar.nat is True


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


# --- L2 (delegates to _sdn) ----------------------------------------------


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

    def test_uplink_nat_switch_returns_existing_bridge(self) -> None:
        # The sidecar's eth1 attaches to the existing host bridge named by uplink.
        c = _FakeClient()
        d = _driver(c)
        sw = Switch(
            "sw1", Network("a"), cidr="10.0.0.0/24", uplink="vmbr0", sidecar=Sidecar(nat=True)
        )
        assert d.create_switch(sw, "tr-switch-x") == "vmbr0"
        # The bridge is operator-owned: not created as a vnet, not torn down.
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


# --- preflight ------------------------------------------------------------


class TestPreflight:
    def _run(self, plan: Plan, *, build_switch: Switch | None = None, client: Any = None) -> Any:
        d = _driver(client)
        return d.preflight(plan, cache_manager=None, build_switch=build_switch)  # type: ignore[arg-type]

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
        assert "proxmox-uplink-bridge-missing" in {f.code for f in report.errors}

    def test_build_switch_uplink_bridge_is_checked(self) -> None:
        # The transient build switch's uplink (hyp.build_uplink) is verified too.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        build = Switch(
            "build", Network("b"), cidr="10.97.99.0/24", uplink="vmbr9", sidecar=Sidecar(nat=True)
        )
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")), build_switch=build)
        assert "proxmox-uplink-bridge-missing" in {f.code for f in report.errors}

    def test_mgmt_is_rejected(self) -> None:
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24", mgmt=True)
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")))
        assert "mgmt-unsupported" in {f.code for f in report.errors}

    def test_native_communicator_ok_with_qga(self) -> None:
        # PVE-4 wired QGA → full native caps, so a NativeCommunicator passes.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24"), comm=NativeCommunicator()))
        assert bool(report), report.render()

    def test_dhcp_addressing_ok_with_qga(self) -> None:
        # read_file capability (QGA) lets the sidecar lease file be read.
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        report = self._run(_plan(sw, addr=DHCPAddr()))
        assert bool(report), report.render()

    def test_native_capabilities_full_with_qga(self) -> None:
        assert _driver().native_guest_capabilities() == frozenset(
            {"execute", "read_file", "write_file"}
        )

    def test_import_content_missing_is_rejected(self) -> None:
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        client = _FakeClient(content="images,iso,vztmpl")  # no 'import'
        report = self._run(_plan(sw, addr=StaticAddr("10.0.0.10/24")), client=client)
        assert "proxmox-import-content-missing" in {f.code for f in report.errors}
