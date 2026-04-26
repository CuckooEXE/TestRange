"""Tests for ``Orchestrator.cleanup(run_id)`` — the leaked-run recovery
path used by ``testrange cleanup MODULE[:FACTORY] RUN_ID``.

These cover the deterministic name reconstruction (the whole point of
the feature) and the idempotent / best-effort behaviour callers rely
on when a previous SIGKILL left a partial mess.  No real libvirt
connection is opened — we patch ``libvirt.open`` to hand back a fake
``virConnect`` that records lookup / destroy / undefine calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import libvirt
import pytest

from testrange import VM, Credential, Memory, Orchestrator, vCPU, vNIC
from testrange.backends.libvirt.network import VirtualNetwork


def _vm(name: str = "web") -> VM:
    return VM(
        name=name,
        iso="https://example.com/x.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1), vNIC("Net")],
    )


def _net(name: str = "Net") -> VirtualNetwork:
    return VirtualNetwork(name=name, subnet="10.0.0.0/24")


class _FakeDomain:
    def __init__(self) -> None:
        self.destroyed = False
        self.undefined = False

    def isActive(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True

    def undefineFlags(self, _flags: int) -> None:
        # Domains: cleanup uses undefineFlags first, falls back to undefine.
        self.undefined = True

    def undefine(self) -> None:
        self.undefined = True


class _FakeNet(_FakeDomain):
    """Networks have only undefine() (no undefineFlags)."""


class _FakeConn:
    """Records every lookup / destroy / undefine call.

    By default lookups raise libvirtError (resource not found).  Tests
    seed ``self.domains`` / ``self.networks`` with names that *are*
    present to exercise the destroy path."""

    def __init__(self) -> None:
        self.domains: dict[str, _FakeDomain] = {}
        self.networks: dict[str, _FakeNet] = {}
        self.closed = False

    def lookupByName(self, name: str) -> _FakeDomain:
        if name in self.domains:
            return self.domains[name]
        raise libvirt.libvirtError(f"no domain {name}")

    def networkLookupByName(self, name: str) -> _FakeNet:
        if name in self.networks:
            return self.networks[name]
        raise libvirt.libvirtError(f"no network {name}")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    """Patch libvirt.open to return a fresh fake connection."""
    conn = _FakeConn()
    monkeypatch.setattr(libvirt, "open", lambda _uri: conn)
    return conn


class TestCleanupReconstructsNames:
    """The whole feature pivots on computing the same names __enter__
    would have created.  These verify the name-construction matches
    what the orchestrator would build at run time."""

    def test_run_phase_domain_name(self, fake_conn: _FakeConn) -> None:
        orch = Orchestrator(vms=[_vm("web")])
        run_id = "deadbeef-1111-2222-3333-444455556666"
        # Seed the run-phase domain so cleanup finds + tears it down.
        dom = _FakeDomain()
        fake_conn.domains[f"tr-web-{run_id[:8]}"] = dom

        orch.cleanup(run_id)

        assert dom.destroyed
        assert dom.undefined

    def test_install_phase_domain_name(self, fake_conn: _FakeConn) -> None:
        """Install-phase domains are tr-build-<vm[:10]>-<runid[:8]>;
        cleanup must check this name too because a SIGKILL during the
        build would leave the install domain orphaned."""
        orch = Orchestrator(vms=[_vm("dbserver01")])
        run_id = "abc12345-aaaa-bbbb-cccc-dddddddddddd"
        install_dom = _FakeDomain()
        fake_conn.domains[f"tr-build-dbserver01-{run_id[:8]}"] = install_dom

        orch.cleanup(run_id)

        assert install_dom.destroyed
        assert install_dom.undefined

    def test_vm_name_truncated_to_10_chars(self, fake_conn: _FakeConn) -> None:
        """libvirt domain names use vm.name[:10]; cleanup must match."""
        orch = Orchestrator(vms=[_vm("verylongvmname12345")])
        run_id = "abc12345-aaaa-bbbb-cccc-dddddddddddd"
        # First 10 chars of the VM name.
        dom = _FakeDomain()
        fake_conn.domains[f"tr-verylongvm-{run_id[:8]}"] = dom

        orch.cleanup(run_id)

        assert dom.destroyed

    def test_test_network_name(self, fake_conn: _FakeConn) -> None:
        """Per-test networks are tr-<net[:6]>-<runid[:4]> with name
        normalisation (lowercase, strip underscores)."""
        orch = Orchestrator(networks=[_net("My_Net")], vms=[])
        run_id = "ab12cd34-aaaa-bbbb-cccc-dddddddddddd"
        # "My_Net"[:6].lower().replace("_","") = "mynet"
        net = _FakeNet()
        fake_conn.networks[f"tr-mynet-{run_id[:4]}"] = net

        orch.cleanup(run_id)

        assert net.destroyed
        assert net.undefined

    def test_install_network_name(self, fake_conn: _FakeConn) -> None:
        """The ephemeral install network is named install-<runid[:4]>
        (logical) which truncates to tr-instal-<runid[:4]> (libvirt)."""
        orch = Orchestrator(vms=[_vm()])
        run_id = "ef98abcd-aaaa-bbbb-cccc-dddddddddddd"
        net = _FakeNet()
        fake_conn.networks[f"tr-instal-{run_id[:4]}"] = net

        orch.cleanup(run_id)

        assert net.destroyed
        assert net.undefined


class TestCleanupBestEffort:
    """Cleanup must never raise on missing resources or per-resource
    failures — leaked-run recovery is a best-effort sweep, not a
    transactional teardown."""

    def test_missing_resources_silently_skipped(
        self, fake_conn: _FakeConn,
    ) -> None:
        """Idempotency: nothing exists, cleanup should still return cleanly."""
        orch = Orchestrator(
            vms=[_vm("web"), _vm("db")],
            networks=[_net("a"), _net("b")],
        )
        # No seeded domains / networks → all lookups raise.
        orch.cleanup("00000000-0000-0000-0000-000000000000")
        assert fake_conn.closed

    def test_destroy_failure_does_not_stop_subsequent_cleanups(
        self, fake_conn: _FakeConn, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One bad teardown shouldn't block the rest — a leaked-run
        sweep that aborts halfway through is exactly what the user is
        running cleanup to avoid."""
        run_id = "abc12345-aaaa-bbbb-cccc-dddddddddddd"

        bad_dom = MagicMock(spec=_FakeDomain)
        bad_dom.isActive.return_value = True
        bad_dom.destroy.side_effect = libvirt.libvirtError("permission denied")
        bad_dom.undefineFlags.side_effect = libvirt.libvirtError("nope")

        good_dom = _FakeDomain()
        fake_conn.domains[f"tr-bad-{run_id[:8]}"] = bad_dom
        fake_conn.domains[f"tr-good-{run_id[:8]}"] = good_dom

        orch = Orchestrator(vms=[_vm("bad"), _vm("good")])
        orch.cleanup(run_id)

        # Good VM was still cleaned up despite the bad one failing.
        assert good_dom.destroyed
        assert good_dom.undefined


class TestCleanupRunDir:
    def test_removes_run_scratch_dir(
        self,
        fake_conn: _FakeConn,
        tmp_path: Path,
    ) -> None:
        run_id = "abc12345-aaaa-bbbb-cccc-dddddddddddd"
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "leftover.qcow2").write_bytes(b"x")

        orch = Orchestrator(vms=[_vm()], cache_root=tmp_path)
        orch.cleanup(run_id)

        assert not run_dir.exists()

    def test_missing_run_dir_does_not_raise(
        self,
        fake_conn: _FakeConn,
        tmp_path: Path,
    ) -> None:
        orch = Orchestrator(vms=[_vm()], cache_root=tmp_path)
        # No run dir created → must still return cleanly.
        orch.cleanup("00000000-0000-0000-0000-000000000000")


class TestProxmoxCleanupCredentialFailure:
    """ProxmoxOrchestrator.cleanup is implemented but raises
    OrchestratorError when invoked without enough credentials to
    open a PVE connection — same failure shape its ``__enter__``
    uses, so callers get a consistent error to handle.

    (Live behaviour — clones-vs-templates distinction, name
    reconstruction — is exercised in
    tests/test_proxmox_template_cache.py against a mocked client.)"""

    def test_raises_orchestrator_error_without_credentials(self) -> None:
        from testrange.backends.proxmox.orchestrator import ProxmoxOrchestrator
        from testrange.exceptions import OrchestratorError

        orch = ProxmoxOrchestrator(host="x")
        with pytest.raises(OrchestratorError, match="credentials"):
            orch.cleanup("00000000-0000-0000-0000-000000000000")
