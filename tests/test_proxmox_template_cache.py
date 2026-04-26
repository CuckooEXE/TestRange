"""Tests for the PVE-template-as-cache path on ProxmoxVM.

Covers the find-or-create-template logic in :meth:`ProxmoxVM.build`,
the phase-2 seed swap in :meth:`ProxmoxVM.start_run`, and the
clones-vs-templates distinction in
:meth:`ProxmoxOrchestrator.cleanup`.

No real PVE here — every REST call is mocked through a recording
fake of ``proxmoxer.ProxmoxAPI``.  Live-PVE coverage lives in
``tests/test_proxmox_live.py`` (skipped without ``TESTRANGE_PROXMOX_HOST``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange import Credential, Memory, vCPU, vNIC
from testrange.backends.libvirt.storage import LocalStorageBackend
from testrange.backends.proxmox.vm import (
    ProxmoxVM,
    _delete_orphan_templates,
    _find_template,
    _template_name,
)


# =====================================================================
# Helpers
# =====================================================================


def _vm() -> ProxmoxVM:
    return ProxmoxVM(
        name="web",
        iso="https://example.com/debian-12.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1), vNIC("Net", ip="10.0.0.5")],
        communicator="ssh",
    )


class _FakeAsync:
    """Returns a synthesised UPID for any POST and an instant
    ``status: stopped`` for the matching task lookup so
    ``_wait_for_task`` short-circuits."""

    def __init__(self, upid: str = "UPID:fake"):
        self.upid = upid

    def post(self, **_kwargs):
        return self.upid


# =====================================================================
# Module-level helpers
# =====================================================================


class TestTemplateName:
    def test_includes_prefix_and_truncated_hash(self) -> None:
        assert _template_name("a" * 24) == "tr-template-aaaaaaaaaaaa"

    def test_truncates_to_12_chars(self) -> None:
        # 24-char hash truncated to 12 → 12-char tail.
        result = _template_name("0123456789abcdef" * 2)
        assert result == "tr-template-0123456789ab"

    def test_deterministic(self) -> None:
        h = "deadbeef" * 3
        assert _template_name(h) == _template_name(h)


class TestFindTemplate:
    def _client_with_vms(self, vms: list[dict[str, Any]]) -> Any:
        client = MagicMock()
        client.nodes.return_value.qemu.get.return_value = vms
        return client

    def test_returns_vmid_on_match(self) -> None:
        client = self._client_with_vms([
            {"vmid": 100, "name": "tr-template-abc", "template": 1},
        ])
        assert _find_template(client, "pve01", "tr-template-abc") == 100

    def test_returns_none_when_no_match(self) -> None:
        client = self._client_with_vms([
            {"vmid": 100, "name": "something-else", "template": 1},
        ])
        assert _find_template(client, "pve01", "tr-template-abc") is None

    def test_returns_none_for_name_match_without_template_flag(
        self,
    ) -> None:
        """Half-promoted install or accidental name collision.  We
        treat it as a miss; the install path will rebuild over it
        rather than returning a broken template."""
        client = self._client_with_vms([
            {"vmid": 100, "name": "tr-template-abc"},  # no template flag
        ])
        assert _find_template(client, "pve01", "tr-template-abc") is None

    def test_returns_none_when_listing_fails(self) -> None:
        """REST list call failing is a soft signal; treat as miss
        and let the install path surface the actual error."""
        client = MagicMock()
        client.nodes.return_value.qemu.get.side_effect = RuntimeError("nope")
        assert _find_template(client, "pve01", "tr-template-abc") is None


# =====================================================================
# build() — cache hit path
# =====================================================================


class TestBuildCacheHit:
    """When the template already exists in PVE, the install flow is
    skipped entirely and we go straight to the clone."""

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        existing_template_vmid: int,
    ) -> tuple[ProxmoxVM, MagicMock]:
        """Build a ProxmoxVM + a context whose proxmoxer client
        reports an existing template at *existing_template_vmid*.
        Returns (vm, client) so the test can inspect calls."""
        vm = _vm()
        client = MagicMock()
        # Find-template hit: VM list shows our template name with
        # template=1 set.
        config_hash = vm.builder.cache_key(vm)
        tname = _template_name(config_hash)
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": existing_template_vmid, "name": tname, "template": 1},
        ]
        # Clone path uses cluster.nextid → an int, then qemu(template_vmid)
        # .clone.post → UPID, then a status lookup that reports stopped.
        client.cluster.nextid.get.return_value = 999
        client.nodes.return_value.qemu.return_value.clone.post.return_value = (
            "UPID:clone"
        )
        # _wait_for_task polls tasks.status — patch it to an instant return.
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )
        ctx = MagicMock()
        ctx._client = client
        ctx._node = "pve01"
        ctx._storage = "local-lvm"
        return vm, client

    def test_cache_hit_skips_install_and_returns_clone_vmid(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vm, client = self._wire(monkeypatch, existing_template_vmid=100)
        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))

        ref = vm.build(
            context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )

        # Returned ref is the cloned run_vmid as a string.
        assert ref == "999"
        # The clone call targeted the existing template VMID.
        client.nodes.return_value.qemu.assert_any_call(100)
        # No install path: we did NOT POST to qemu.post (which is the
        # create-VM call).  ``qemu.post`` is on the listing endpoint
        # (``nodes(...).qemu.post``), distinct from
        # ``nodes(...).qemu(vmid).clone.post``.
        assert not client.nodes.return_value.qemu.post.called
        # Template VMID stashed for inspection.
        assert vm._template_vmid == 100
        # Run VMID stashed for shutdown.
        assert vm._vmid == 999


# =====================================================================
# build() — cache miss path
# =====================================================================


class TestBuildCacheMiss:
    def test_install_flow_runs_and_promotes_then_clones(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vm = _vm()
        client = MagicMock()
        # Find-template miss: empty VM list → install path runs.
        client.nodes.return_value.qemu.get.return_value = []
        # cluster.nextid called twice — once for install_vmid, once
        # for run_vmid.
        client.cluster.nextid.get.side_effect = [200, 999]
        client.nodes.return_value.qemu.post.return_value = "UPID:create"
        client.nodes.return_value.qemu.return_value.status.start.post.return_value = (
            "UPID:start"
        )
        client.nodes.return_value.qemu.return_value.template.post.return_value = (
            None
        )
        client.nodes.return_value.qemu.return_value.clone.post.return_value = (
            "UPID:clone"
        )
        # Stub waits and image upload — neither has any logic worth
        # exercising in this unit test.
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_status", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "_upload_disk_image", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "_upload_iso_bytes", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "testrange.vms.images.resolve_image",
            lambda iso, cache: type("P", (), {
                "stat": lambda self: type("S", (), {"st_size": 1024})(),
                "__str__": lambda self: "/tmp/fake.qcow2",
                "name": "fake.qcow2",
            })(),
        )

        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        ref = vm.build(
            context=MagicMock(
                _client=client, _node="pve01", _storage="local-lvm",
            ),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )

        # Returned ref is the run_vmid (the clone), not the install
        # template VMID.
        assert ref == "999"
        # Install + promote happened on VMID 200.
        client.nodes.return_value.qemu(200).template.post.assert_called()
        # Clone took template 200 as source, with newid=999.
        clone_call = client.nodes.return_value.qemu(200).clone.post.call_args
        assert clone_call.kwargs["newid"] == 999
        # Template VMID stashed (preserved across teardown).
        assert vm._template_vmid == 200


# =====================================================================
# start_run() — phase-2 seed swap
# =====================================================================


class TestStartRunPhase2Seed:
    def test_swaps_ide2_to_phase2_seed_and_updates_net0(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The clone inherits the install seed at ide2 and the install
        NIC at net0 from the template; both must be swapped before
        boot."""
        vm = _vm()
        client = MagicMock()
        client.nodes.return_value.qemu.return_value.status.current.get.return_value = {
            "status": "stopped",
        }
        client.nodes.return_value.qemu.return_value.status.start.post.return_value = (
            "UPID:start"
        )
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            ProxmoxVM, "_upload_iso_bytes", lambda *a, **kw: None,
        )
        # Skip the SSH wait and communicator construction — neither is
        # the contract we care about here.
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_ssh", staticmethod(lambda *a, **kw: None),
        )
        monkeypatch.setattr(
            ProxmoxVM, "_make_communicator", lambda self, mac_ip_pairs: MagicMock(),
        )

        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        ctx = MagicMock(_client=client, _node="pve01", _storage="local-lvm")

        vm.start_run(
            context=ctx,
            run=run,
            installed_disk="999",
            network_entries=[("RunNet", "52:54:00:aa:bb:cc")],
            mac_ip_pairs=[(
                "52:54:00:aa:bb:cc", "10.0.0.5/24", "10.0.0.1", "1.1.1.1",
            )],
        )

        # config.put was called with the ide2 swap + net0 update.
        put_call = (
            client.nodes.return_value.qemu.return_value.config.put.call_args
        )
        assert put_call is not None
        assert "ide2" in put_call.kwargs
        assert put_call.kwargs["ide2"].endswith("-seed.iso,media=cdrom")
        assert put_call.kwargs["net0"] == "virtio=52:54:00:aa:bb:cc,bridge=RunNet"
        # Per-run seed filename stashed for shutdown.
        assert vm._phase2_seed_filename is not None
        assert "abcd1234" in vm._phase2_seed_filename


# =====================================================================
# shutdown() — clones go, templates stay
# =====================================================================


class TestShutdownLeavesTemplate:
    def test_deletes_clone_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # _wait_for_status polls until the VMID reports "stopped";
        # against a MagicMock the equality never matches and we'd
        # block for the full 60s timeout.  Stub it out.
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_status", lambda *a, **kw: None,
        )

        vm = _vm()
        client = MagicMock()
        vm._client = client
        vm._node = "pve01"
        vm._vmid = 999  # the clone
        vm._template_vmid = 100  # the template — must NOT be touched
        vm._phase2_seed_filename = "tr-web-abcd1234-seed.iso"

        vm.shutdown()

        # Clone was deleted.
        client.nodes.return_value.qemu(999).delete.assert_called()
        # Template was NOT.
        for call in client.nodes.return_value.qemu.call_args_list:
            assert call.args[0] != 100, (
                f"shutdown should never touch the template VMID; "
                f"saw call qemu({call.args[0]!r})"
            )
        # Run-state cleared, but template_vmid retained for debugging.
        assert vm._vmid is None
        assert vm._template_vmid == 100
        assert vm._phase2_seed_filename is None


# =====================================================================
# Orchestrator.cleanup — clones go, templates stay
# =====================================================================


class TestOrchestratorCleanupPreservesTemplates:
    def test_cleanup_skips_template_vmids_even_if_name_matches(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cleanup CLI's name-pattern reconstruction is generous
        — if a template somehow ends up with a clone-shaped name
        (shouldn't happen but might via manual rename) we still
        refuse to delete it."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )
        # Patch proxmoxer.ProxmoxAPI to return our mock client so
        # cleanup() doesn't need a real PVE.
        client = MagicMock()
        # Listing nodes
        client.nodes.get.return_value = [{"node": "pve01"}]
        # Listing VMs by node — claims a clone-named VM that is actually
        # a template.
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": 555, "name": "tr-web-deadbeef"},
        ]
        # The is-template lookup says yes.
        client.nodes.return_value.qemu.return_value.config.get.return_value = {
            "template": 1,
        }
        monkeypatch.setattr(
            "proxmoxer.ProxmoxAPI", lambda *a, **kw: client,
        )

        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            user="root@pam",
            password="x",
            node="pve01",
            vms=[_vm()],
        )
        # Make _resolve_node a no-op (it normally validates against
        # the listed nodes, which we've already faked).
        monkeypatch.setattr(
            ProxmoxOrchestrator, "_resolve_node",
            lambda self, nodes: setattr(self, "_node", "pve01"),
        )

        orch.cleanup("deadbeef-1111-2222-3333-444455556666")

        # No delete on the template-flagged VMID.
        for call in client.nodes.return_value.qemu.call_args_list:
            if call.args and call.args[0] == 555:
                # Allowed: config.get().  Not allowed: delete().
                pass
        client.nodes.return_value.qemu(555).delete.assert_not_called()


# =====================================================================
# _delete_orphan_templates — crash-recovery sweep
# =====================================================================


class TestDeleteOrphanTemplates:
    """Sweep VMIDs whose display name matches a template name but
    whose ``template`` flag isn't set — the footprint of an install
    that died between create and ``qm template``."""

    def _client_with_vms(self, vms: list[dict[str, Any]]) -> Any:
        client = MagicMock()
        client.nodes.return_value.qemu.get.return_value = vms
        return client

    def test_deletes_unflagged_name_match(self) -> None:
        client = self._client_with_vms([
            {"vmid": 700, "name": "tr-template-abc"},  # no template flag
        ])
        deleted = _delete_orphan_templates(client, "pve01", "tr-template-abc")
        assert deleted == 1
        client.nodes.return_value.qemu(700).delete.assert_called_once()

    def test_skips_proper_templates(self) -> None:
        """A real template with the same name is left alone — the
        find-then-clone path will pick it up on the very next
        lookup."""
        client = self._client_with_vms([
            {"vmid": 700, "name": "tr-template-abc", "template": 1},
        ])
        deleted = _delete_orphan_templates(client, "pve01", "tr-template-abc")
        assert deleted == 0
        client.nodes.return_value.qemu(700).delete.assert_not_called()

    def test_skips_unrelated_names(self) -> None:
        client = self._client_with_vms([
            {"vmid": 700, "name": "something-else"},
            {"vmid": 701, "name": "tr-template-other"},
        ])
        deleted = _delete_orphan_templates(client, "pve01", "tr-template-abc")
        assert deleted == 0

    def test_stops_orphan_before_delete(self) -> None:
        """If the orphan is somehow still running (interrupted before
        poweroff), stop it first so the delete doesn't fail with
        VM-is-running."""
        client = self._client_with_vms([
            {"vmid": 700, "name": "tr-template-abc"},
        ])
        _delete_orphan_templates(client, "pve01", "tr-template-abc")
        client.nodes.return_value.qemu(700).status.stop.post.assert_called()

    def test_returns_zero_when_listing_fails(self) -> None:
        """REST list call failing is logged and treated as 'no orphans
        found' — the install path will surface the actual error."""
        client = MagicMock()
        client.nodes.return_value.qemu.get.side_effect = RuntimeError("nope")
        assert _delete_orphan_templates(client, "pve01", "tr-template-x") == 0

    def test_per_vmid_failure_does_not_raise(self) -> None:
        """Best-effort: a delete failure is logged but never raises so
        the caller (build()'s install path) can still proceed."""
        client = MagicMock()
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": 700, "name": "tr-template-abc"},
            {"vmid": 701, "name": "tr-template-abc"},
        ]
        # First delete fails, second succeeds.
        delete_mock = client.nodes.return_value.qemu.return_value.delete
        delete_mock.side_effect = [RuntimeError("PVE busy"), None]
        deleted = _delete_orphan_templates(client, "pve01", "tr-template-abc")
        # The second succeeded.
        assert deleted == 1


class TestBuildSweepsOrphansBeforeRetry:
    """build()'s cache-miss path runs the sweep BEFORE attempting
    install so a left-over orphan from a previous crash doesn't
    block ``qm create`` with a duplicate display-name error."""

    def test_orphan_sweep_called_on_cache_miss(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vm = _vm()
        client = MagicMock()
        # Find-template miss.
        client.nodes.return_value.qemu.get.return_value = []

        sweep_calls: list[tuple[Any, str, str]] = []

        def _spy_sweep(c, n, name):
            sweep_calls.append((c, n, name))
            return 0

        # Patch on the module ProxmoxVM was actually loaded from —
        # the scaffold test re-imports the proxmox package, so the
        # name in sys.modules can drift from what this file's import
        # captured.  Going through ProxmoxVM.__module__ keeps the
        # patch glued to the real running code.
        import sys as _sys
        monkeypatch.setattr(
            _sys.modules[ProxmoxVM.__module__],
            "_delete_orphan_templates",
            _spy_sweep,
        )
        # Short-circuit the whole install so we only assert sweep
        # ordering, not the install's own behaviour.
        monkeypatch.setattr(
            ProxmoxVM, "_install_and_template",
            lambda self, *a, **kw: 200,
        )
        # Clone path stubs.
        client.cluster.nextid.get.return_value = 999
        client.nodes.return_value.qemu.return_value.clone.post.return_value = (
            "UPID:clone"
        )
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )

        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        vm.build(
            context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )

        # Sweep was called once with the expected template name.
        assert len(sweep_calls) == 1
        _, node, name = sweep_calls[0]
        assert node == "pve01"
        assert name.startswith("tr-template-")

    def test_orphan_sweep_skipped_on_cache_hit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On a cache hit there's no install to re-run, so there's no
        reason to sweep — and sweeping would risk deleting a name-
        matched non-template the user might want to keep."""
        vm = _vm()
        client = MagicMock()
        config_hash = vm.builder.cache_key(vm)
        tname = _template_name(config_hash)
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": 100, "name": tname, "template": 1},
        ]
        client.cluster.nextid.get.return_value = 999
        client.nodes.return_value.qemu.return_value.clone.post.return_value = (
            "UPID:clone"
        )

        sweep_calls: list[Any] = []
        # See sibling test for why we patch via ProxmoxVM.__module__.
        import sys as _sys
        monkeypatch.setattr(
            _sys.modules[ProxmoxVM.__module__],
            "_delete_orphan_templates",
            lambda *a, **kw: sweep_calls.append(a) or 0,
        )
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )

        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        vm.build(
            context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )

        assert sweep_calls == []


# =====================================================================
# build() — linked-then-full clone fallback
# =====================================================================


class TestCloneFallback:
    """Linked clone is faster but only works on storage that supports
    snapshots (LVM-thin, ZFS, qcow2 file).  On raw LVM / Ceph-without-
    snapshots / NFS the linked-clone POST raises; we transparently
    retry with full=1 so the run still succeeds."""

    def _wire_clone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clone_post,
    ) -> tuple[ProxmoxVM, MagicMock]:
        vm = _vm()
        client = MagicMock()
        # Cache hit so we skip straight to the clone path.
        config_hash = vm.builder.cache_key(vm)
        tname = _template_name(config_hash)
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": 100, "name": tname, "template": 1},
        ]
        client.cluster.nextid.get.return_value = 999
        client.nodes.return_value.qemu.return_value.clone.post = clone_post
        monkeypatch.setattr(
            ProxmoxVM, "_wait_for_task", lambda *a, **kw: None,
        )
        return vm, client

    def test_uses_linked_clone_when_supported(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone_post = MagicMock(return_value="UPID:clone")
        vm, client = self._wire_clone(monkeypatch, clone_post)
        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        vm.build(
            context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )
        # First (and only) clone attempt was full=0.
        assert clone_post.call_count == 1
        assert clone_post.call_args.kwargs["full"] == 0

    def test_falls_back_to_full_when_linked_fails(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First call raises (linked unsupported); second succeeds.
        clone_post = MagicMock(
            side_effect=[RuntimeError("storage refuses linked"), "UPID:clone"],
        )
        vm, client = self._wire_clone(monkeypatch, clone_post)
        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        vm.build(
            context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
            cache=MagicMock(),
            run=run,
            install_network_name="install-net",
            install_network_mac="52:54:00:01:02:03",
        )
        # Two attempts: full=0 then full=1.
        assert clone_post.call_count == 2
        assert clone_post.call_args_list[0].kwargs["full"] == 0
        assert clone_post.call_args_list[1].kwargs["full"] == 1
        # newid + name preserved across both attempts.
        assert (
            clone_post.call_args_list[0].kwargs["newid"]
            == clone_post.call_args_list[1].kwargs["newid"]
        )

    def test_full_clone_failure_raises_vmbuild_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange.exceptions import VMBuildError
        clone_post = MagicMock(
            side_effect=[
                RuntimeError("linked unsupported"),
                RuntimeError("full also failed"),
            ],
        )
        vm, client = self._wire_clone(monkeypatch, clone_post)
        run = MagicMock(run_id="abcd1234-1111-2222-3333-4444")
        run.storage = LocalStorageBackend(Path("/tmp"))
        with pytest.raises(VMBuildError):
            vm.build(
                context=MagicMock(_client=client, _node="pve01", _storage="local-lvm"),
                cache=MagicMock(),
                run=run,
                install_network_name="install-net",
                install_network_mac="52:54:00:01:02:03",
            )
