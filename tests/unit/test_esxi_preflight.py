"""ESXi preflight: the build switch is exempt from live checks in a cache-only run.

Regression for CORE-65 (found under ESXI-16). A nested ESXi run is cache-only
(``require_cache``): its build switch is realized on L0/libvirt, never on the
nested host, and the manufactured inner profile inherits libvirt's bridge-name
uplink map. The orchestrator therefore passes ``build_switch=None`` so ESXi's
live pNIC check never validates a libvirt bridge name as a vmnic on a single-pNIC
nested host.
"""

from __future__ import annotations

from testrange import Plan
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec
from tests.esxi_fakes import FakeEsxiClient
from tests.mock_driver import MockHypervisor, OriginlessBuilder


class _InstallerBuilder(OriginlessBuilder):
    """Installer-origin: no os-disk base, so preflight needs no qcow2->vmdk convert."""

    def boot_media(self) -> CacheEntry:
        return CacheEntry("installer-iso")


def _driver(*, pnics: list[str], uplinks: dict[str, str]) -> ESXiDriver:
    conn = EsxiConn(host="10.50.0.5", user="root", password="x", datastore="datastore1")
    return ESXiDriver(conn, client=FakeEsxiClient(pnics=pnics), uplinks=uplinks)  # type: ignore[arg-type]


def _plan() -> Plan:
    # Isolated run switch (no uplink, no sidecar) so the only uplink in play is the
    # build switch's; installer-origin VM so the qemu-img convert gate stays quiet.
    return Plan(
        "p",
        MockHypervisor(
            networks=[Switch("lab", Network("lab-net"), cidr="10.50.0.0/24")],
            pools=[StoragePool("pool1", 8)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="vm",
                        firmware="bios",
                        devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
                    ),
                    builder=_InstallerBuilder(),
                    communicator=SSHCommunicator("u"),
                )
            ],
        ),
    )


# The nested inner binding: 'egress' inherited from the OUTER libvirt profile maps
# to a *bridge* name, and the freshly-installed nested host has one pNIC (vmnic0).
_INHERITED = {"egress": "tr-egress"}
_NESTED_BUILD = Switch(
    "build",
    Network("build-net"),
    cidr="10.97.99.0/24",
    uplink="egress",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)


def test_build_switch_uplink_pnic_is_checked_when_present() -> None:
    # A normal run hands preflight the concrete build switch; its uplink must
    # resolve to a real vmnic. The inherited bridge name does not -> flagged.
    d = _driver(pnics=["vmnic0"], uplinks=_INHERITED)
    report = d.preflight(_plan(), cache_manager=None, build_switch=_NESTED_BUILD)  # type: ignore[arg-type]
    assert "esxi-uplink-pnic-missing" in {f.code for f in report.findings}


def test_none_build_switch_skips_pnic_check() -> None:
    # The cache-only (require_cache) path passes build_switch=None: the build
    # switch is never realized on the nested host, so its uplink is not validated.
    d = _driver(pnics=["vmnic0"], uplinks=_INHERITED)
    report = d.preflight(_plan(), cache_manager=None, build_switch=None)  # type: ignore[arg-type]
    assert "esxi-uplink-pnic-missing" not in {f.code for f in report.findings}
    assert bool(report), report.render()


def test_uplink_without_nat_skips_pnic_check() -> None:
    # ESXI-21: a switch declaring an uplink but no NAT sidecar never enslaves a
    # pNIC at runtime (`_net.create_switch` only enslaves when `uplink and
    # sidecar.nat`), so its uplink must not demand a free pNIC at preflight — a
    # plan whose VMs need no egress at all must pass even on a host with no spare
    # NIC. The uplink name is still *mapped* (so `named-uplink-resolution` stays
    # quiet); only the live pNIC requirement is conditional on NAT.
    d = _driver(pnics=["vmnic0"], uplinks={"egress": "vmnic9"})
    no_nat_build = Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True),  # nat defaults False
    )
    report = d.preflight(_plan(), cache_manager=None, build_switch=no_nat_build)  # type: ignore[arg-type]
    assert "esxi-uplink-pnic-missing" not in {f.code for f in report.findings}


def test_uplink_with_nat_still_requires_pnic() -> None:
    # The other half of ESXI-21: a NAT switch DOES enslave the pNIC, so a missing
    # one is still flagged — the check is narrowed, not removed.
    d = _driver(pnics=["vmnic0"], uplinks={"egress": "vmnic9"})
    nat_build = Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
    report = d.preflight(_plan(), cache_manager=None, build_switch=nat_build)  # type: ignore[arg-type]
    assert "esxi-uplink-pnic-missing" in {f.code for f in report.findings}


def _two_uplink_plan() -> Plan:
    # One NAT switch and one non-NAT switch, each on a distinct mapped uplink, in
    # the same run-phase topology.
    return Plan(
        "p",
        MockHypervisor(
            networks=[
                Switch(
                    "natsw",
                    Network("nat-net"),
                    cidr="10.51.0.0/24",
                    uplink="natlink",
                    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
                ),
                Switch(
                    "plainsw",
                    Network("plain-net"),
                    cidr="10.52.0.0/24",
                    uplink="plainlink",
                    sidecar=Sidecar(dhcp=True, dns=True),
                ),
            ],
            pools=[StoragePool("pool1", 8)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="vm",
                        firmware="bios",
                        devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
                    ),
                    builder=_InstallerBuilder(),
                    communicator=SSHCommunicator("u"),
                )
            ],
        ),
    )


def test_mixed_nat_and_non_nat_uplinks_flag_only_nat() -> None:
    # ESXI-21: a NAT switch and a non-NAT switch, each on a distinct uplink that
    # maps to a MISSING pNIC, coexisting in one report. Only the NAT switch enslaves
    # a pNIC at runtime, so only its uplink is flagged; the non-NAT one is exempt.
    d = _driver(pnics=["vmnic0"], uplinks={"natlink": "vmnic8", "plainlink": "vmnic9"})
    report = d.preflight(_two_uplink_plan(), cache_manager=None, build_switch=None)  # type: ignore[arg-type]
    pnic = [f for f in report.findings if f.code == "esxi-uplink-pnic-missing"]
    assert len(pnic) == 1, [f.message for f in pnic]
    assert "natlink" in pnic[0].message and "vmnic8" in pnic[0].message


class _CapCpuInfo:
    numCpuThreads = 16


class _CapHardware:
    memorySize = 17179869184  # 16 GiB, in bytes
    cpuInfo = _CapCpuInfo()


class _CapHost:
    hardware = _CapHardware()


class _CapEsxiClient:
    host = _CapHost()


def _cap_conn() -> EsxiConn:
    return EsxiConn(host="10.50.0.5", user="root", password="x", datastore="datastore1")


def test_host_capacity_parses_hardware() -> None:
    # host_capacity() field extraction (CORE-84): memorySize bytes->MiB, numCpuThreads.
    d = ESXiDriver(_cap_conn(), client=_CapEsxiClient())  # type: ignore[arg-type]
    cap = d.host_capacity()
    assert cap is not None
    assert cap.memory_mb == 16384
    assert cap.logical_cpus == 16


def test_host_capacity_none_on_probe_failure() -> None:
    # FakeEsxiClient has no .host.hardware → the probe fails soft to None.
    d = _driver(pnics=["vmnic0"], uplinks={})
    assert d.host_capacity() is None
