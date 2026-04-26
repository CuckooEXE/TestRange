"""Tests for the PVE-template admin surface on ProxmoxOrchestrator.

Covers :meth:`ProxmoxOrchestrator.list_templates`,
:meth:`ProxmoxOrchestrator.prune_templates`, and the matching CLI
commands (``proxmox-list-templates`` / ``proxmox-prune-templates``).

No real PVE — proxmoxer.ProxmoxAPI is patched with a recording fake.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from testrange.backends.proxmox.orchestrator import ProxmoxOrchestrator


# =====================================================================
# Helpers
# =====================================================================


def _patch_proxmox_api(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Patch proxmoxer.ProxmoxAPI to return *client*.  Patch both the
    real import and the in-module reference so any ``from proxmoxer
    import ProxmoxAPI`` lands on our mock."""
    monkeypatch.setattr(
        "proxmoxer.ProxmoxAPI", lambda *a, **kw: client,
    )


def _orch(monkeypatch: pytest.MonkeyPatch) -> ProxmoxOrchestrator:
    """Build a bare orchestrator with `_resolve_node` neutralised so
    tests don't need to wire a full nodes-listing fake."""
    orch = ProxmoxOrchestrator(
        host="pve.example.com",
        user="root@pam",
        password="x",
        node="pve01",
    )
    monkeypatch.setattr(
        ProxmoxOrchestrator, "_resolve_node",
        lambda self, nodes: setattr(self, "_node", "pve01"),
    )
    return orch


# =====================================================================
# list_templates
# =====================================================================


class TestListTemplates:
    def test_returns_only_tr_template_prefixed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Filter is two-pronged: ``template`` flag set AND name
        starts with the testrange prefix.  Operator-managed templates
        with names like ``debian12-base`` must not show up."""
        client = MagicMock()
        client.nodes.get.return_value = [{"node": "pve01"}]
        client.nodes.return_value.qemu.get.return_value = [
            {"vmid": 100, "name": "tr-template-aaaa", "template": 1},
            {"vmid": 101, "name": "tr-template-bbbb", "template": 1},
            {"vmid": 102, "name": "debian12-base", "template": 1},  # operator
            {"vmid": 103, "name": "tr-web-runabcd"},  # clone, not template
            {"vmid": 104, "name": "tr-template-cccc"},  # missing flag
        ]
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        result = orch.list_templates()

        names = sorted(t["name"] for t in result)
        assert names == ["tr-template-aaaa", "tr-template-bbbb"]
        assert all(isinstance(t["vmid"], int) for t in result)

    def test_returns_empty_when_no_templates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MagicMock()
        client.nodes.get.return_value = [{"node": "pve01"}]
        client.nodes.return_value.qemu.get.return_value = []
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        assert orch.list_templates() == []

    def test_list_failure_raises_orchestrator_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange.exceptions import OrchestratorError

        client = MagicMock()
        client.nodes.get.return_value = [{"node": "pve01"}]
        client.nodes.return_value.qemu.get.side_effect = RuntimeError("PVE down")
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        with pytest.raises(OrchestratorError):
            orch.list_templates()


# =====================================================================
# prune_templates
# =====================================================================


class TestPruneTemplates:
    def _client_with_templates(
        self, templates: list[dict[str, Any]],
    ) -> Any:
        client = MagicMock()
        client.nodes.get.return_value = [{"node": "pve01"}]
        client.nodes.return_value.qemu.get.return_value = templates
        return client

    def test_prunes_all_templates_when_names_omitted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = self._client_with_templates([
            {"vmid": 100, "name": "tr-template-aaaa", "template": 1},
            {"vmid": 101, "name": "tr-template-bbbb", "template": 1},
        ])
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        deleted = orch.prune_templates()

        assert deleted == 2
        # Both VMIDs received a delete call.
        deleted_vmids = {
            call.args[0]
            for call in client.nodes.return_value.qemu.call_args_list
            if call.args
            and isinstance(call.args[0], int)
            and call.args[0] in (100, 101)
        }
        assert deleted_vmids == {100, 101}

    def test_filters_to_named_templates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = self._client_with_templates([
            {"vmid": 100, "name": "tr-template-aaaa", "template": 1},
            {"vmid": 101, "name": "tr-template-bbbb", "template": 1},
            {"vmid": 102, "name": "tr-template-cccc", "template": 1},
        ])
        # Record which VMIDs delete() is called against.  All shared
        # child mocks collapse onto one .delete object, so we sniff
        # ``qemu(vmid)`` call args around delete invocations instead.
        deleted_vmids: list[int] = []
        delete_mock = client.nodes.return_value.qemu.return_value.delete

        def _record_delete(*_a, **_kw):
            # Last qemu(vmid) call is the target of this delete.
            qemu_calls = client.nodes.return_value.qemu.call_args_list
            int_calls = [c for c in qemu_calls if c.args and isinstance(c.args[0], int)]
            deleted_vmids.append(int_calls[-1].args[0])

        delete_mock.side_effect = _record_delete
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        deleted = orch.prune_templates(names=["tr-template-bbbb"])

        assert deleted == 1
        assert deleted_vmids == [101]

    def test_skips_non_template_vms(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even when prune is unrestricted, VMs without the
        ``template`` flag must not be deleted — those are clones in
        active use."""
        client = self._client_with_templates([
            {"vmid": 100, "name": "tr-template-aaaa", "template": 1},
            {"vmid": 999, "name": "tr-web-runabcd"},  # active clone
        ])
        deleted_vmids: list[int] = []
        delete_mock = client.nodes.return_value.qemu.return_value.delete

        def _record_delete(*_a, **_kw):
            qemu_calls = client.nodes.return_value.qemu.call_args_list
            int_calls = [c for c in qemu_calls if c.args and isinstance(c.args[0], int)]
            deleted_vmids.append(int_calls[-1].args[0])

        delete_mock.side_effect = _record_delete
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        deleted = orch.prune_templates()

        assert deleted == 1
        # The non-template clone VMID was never targeted.
        assert 999 not in deleted_vmids
        assert deleted_vmids == [100]

    def test_per_vmid_failure_does_not_stop_prune(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One template stuck (locked, missing on disk, etc.) must not
        block the rest of the eviction sweep."""
        client = self._client_with_templates([
            {"vmid": 100, "name": "tr-template-aaaa", "template": 1},
            {"vmid": 101, "name": "tr-template-bbbb", "template": 1},
        ])
        # First delete raises; second succeeds.
        delete_mock = client.nodes.return_value.qemu.return_value.delete
        delete_mock.side_effect = [RuntimeError("PVE busy"), None]
        _patch_proxmox_api(monkeypatch, client)

        orch = _orch(monkeypatch)
        deleted = orch.prune_templates()

        assert deleted == 1  # the one that succeeded


# =====================================================================
# CLI: proxmox-list-templates / proxmox-prune-templates
# =====================================================================


class TestListTemplatesCLI:
    def test_no_templates_prints_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        # Build a fake orchestrator so the CLI doesn't call PVE.
        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = []
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-list-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01"],
        )
        assert result.exit_code == 0
        assert "No TestRange templates" in result.output

    def test_lists_templates_with_vmid_and_name(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = [
            {"vmid": 100, "name": "tr-template-aaaa"},
            {"vmid": 101, "name": "tr-template-bbbb"},
        ]
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-list-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01"],
        )
        assert result.exit_code == 0
        assert "VMID 100" in result.output
        assert "tr-template-aaaa" in result.output
        assert "VMID 101" in result.output
        assert "tr-template-bbbb" in result.output


class TestPruneTemplatesCLI:
    def test_nothing_to_prune_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = []
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-prune-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01"],
        )
        assert result.exit_code == 0
        assert "Nothing to prune" in result.output
        # No prune call when there's nothing to prune.
        fake_orch.prune_templates.assert_not_called()

    def test_yes_skips_confirmation_and_prunes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = [
            {"vmid": 100, "name": "tr-template-aaaa"},
        ]
        fake_orch.prune_templates.return_value = 1
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-prune-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01",
             "--yes"],
        )
        assert result.exit_code == 0
        # No confirmation prompt when --yes.
        assert "Proceed?" not in result.output
        assert "Pruned 1 template(s)" in result.output
        fake_orch.prune_templates.assert_called_once_with(names=None)

    def test_name_filter_passed_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = [
            {"vmid": 100, "name": "tr-template-aaaa"},
            {"vmid": 101, "name": "tr-template-bbbb"},
        ]
        fake_orch.prune_templates.return_value = 1
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-prune-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01",
             "--name", "tr-template-aaaa",
             "--yes"],
        )
        assert result.exit_code == 0
        fake_orch.prune_templates.assert_called_once_with(
            names=["tr-template-aaaa"],
        )

    def test_confirmation_abort_does_not_prune(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._cli import main

        fake_orch = MagicMock()
        fake_orch.list_templates.return_value = [
            {"vmid": 100, "name": "tr-template-aaaa"},
        ]
        monkeypatch.setattr(
            "testrange._cli._build_proxmox_orchestrator",
            lambda url: fake_orch,
        )

        result = CliRunner().invoke(
            main,
            ["proxmox-prune-templates",
             "--orchestrator", "proxmox://root:pw@pve/pve01"],
            input="n\n",
        )
        # click.confirm(abort=True) exits non-zero on "no".
        assert result.exit_code != 0
        fake_orch.prune_templates.assert_not_called()
