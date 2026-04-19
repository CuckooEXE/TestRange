"""Unit tests for :mod:`testrange.vms.builders.unattend`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from testrange import VM, Credential
from testrange.exceptions import CloudInitError
from testrange.packages import Winget
from testrange.vms.builders.unattend import (
    WindowsUnattendedBuilder,
    write_autounattend_iso,
)


def _win_vm(
    users: list[Credential] | None = None,
    pkgs: list | None = None,
    post: list[str] | None = None,
    name: str = "TESTVM",
) -> VM:
    """Build a Windows VM spec for autounattend tests."""
    if users is None:
        users = [
            Credential("root", "AdminPwd!"),
            Credential("alice", "Alice1!", sudo=True),
        ]
    return VM(
        name=name,
        iso="/srv/iso/Win10_21H1_English_x64.iso",
        users=users,
        pkgs=pkgs or [Winget("Git.Git")],
        post_install_cmds=post or ["Write-Host 'hello'"],
        builder=WindowsUnattendedBuilder(),
    )


@pytest.fixture
def builder() -> WindowsUnattendedBuilder:
    return WindowsUnattendedBuilder()


@pytest.fixture
def vm() -> VM:
    return _win_vm()


class TestConstruction:
    def test_defaults(self, builder: WindowsUnattendedBuilder) -> None:
        from testrange.vms.builders.unattend import _DEFAULT_PRODUCT_KEY
        assert builder.ui_language == "en-US"
        assert builder.timezone == "UTC"
        # Default is the Win10/11 Pro generic install key — lets
        # multi-edition consumer ISOs pick an edition unattended.
        assert builder.product_key == _DEFAULT_PRODUCT_KEY

    def test_product_key_explicit_none_disables_element(self) -> None:
        b = WindowsUnattendedBuilder(product_key=None)
        assert b.product_key is None

    def test_custom_locale_and_timezone(self) -> None:
        b = WindowsUnattendedBuilder(
            ui_language="de-DE",
            timezone="Central European Standard Time",
            product_key="XXXXX-YYYYY-ZZZZZ",
        )
        assert b.ui_language == "de-DE"
        assert b.timezone == "Central European Standard Time"
        assert b.product_key == "XXXXX-YYYYY-ZZZZZ"


class TestXmlOutput:
    def test_is_well_formed_xml(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert text.startswith('<?xml version="1.0"')
        ET.fromstring(text)  # raises on malformed

    def test_computer_name_set(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "<ComputerName>TESTVM</ComputerName>" in text

    def test_timezone_set(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "<TimeZone>UTC</TimeZone>" in text

    def test_administrator_password_set(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "AdminPwd!" in text  # plaintext per unattend spec

    def test_local_account_created(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "alice" in text

    def test_sudo_user_in_administrators_group(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        # alice (sudo=True) should be Administrators, not Users
        assert "<Group>Administrators</Group>" in text

    def test_winget_command_emitted(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "winget install --id Git.Git" in text

    def test_post_install_commands_emitted(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        text = builder.build_xml(vm)
        assert "Write-Host" in text

    def test_product_key_emitted_when_set(self) -> None:
        b = WindowsUnattendedBuilder(product_key="ABCDE-FGHIJ-KLMNO")
        text = b.build_xml(_win_vm())
        assert "ABCDE-FGHIJ-KLMNO" in text

    def test_product_key_absent_when_none(self) -> None:
        b = WindowsUnattendedBuilder(product_key=None)
        text = b.build_xml(_win_vm())
        assert "ProductKey" not in text

    def test_product_key_nested_inside_userdata(self) -> None:
        """Regression for the 'can't read product key from the answer
        file' Setup error: the Microsoft unattend schema requires
        ``ProductKey`` to be a child of ``UserData`` inside
        ``Microsoft-Windows-Setup``.  Placing it as a direct child of
        the component (the pre-fix location) makes Setup silently
        ignore it and prompt for a key — which, under
        ``WillShowUI=Never``, aborts the install."""
        b = WindowsUnattendedBuilder(product_key="TEST1-TEST2-TEST3")
        text = b.build_xml(_win_vm())
        root = ET.fromstring(text)
        ns = "urn:schemas-microsoft-com:unattend"
        setup_comp = root.find(
            f".//{{{ns}}}component[@name='Microsoft-Windows-Setup']"
        )
        assert setup_comp is not None
        # No direct ProductKey under the component.
        assert setup_comp.find(f"{{{ns}}}ProductKey") is None
        # ProductKey lives inside UserData.
        user_data = setup_comp.find(f"{{{ns}}}UserData")
        assert user_data is not None
        prod_key = user_data.find(f"{{{ns}}}ProductKey")
        assert prod_key is not None
        key_el = prod_key.find(f"{{{ns}}}Key")
        assert key_el is not None and key_el.text == "TEST1-TEST2-TEST3"

    def test_default_product_key_emitted(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        """Default key is the Win10 Pro generic install key — lets
        multi-edition consumer ISOs install unattended."""
        from testrange.vms.builders.unattend import _DEFAULT_PRODUCT_KEY
        text = builder.build_xml(vm)
        assert _DEFAULT_PRODUCT_KEY in text

    def test_shutdown_appended_to_first_logon_commands(
        self, builder: WindowsUnattendedBuilder, vm: VM
    ) -> None:
        """The build step relies on the autounattend powering the VM off
        so the orchestrator can snapshot the disk into the cache.
        Regression: the shutdown command must always be the final entry
        regardless of caller-supplied post_install_cmds."""
        text = builder.build_xml(vm)
        assert "shutdown /s /t 0" in text

    def test_stateless_across_different_vms(
        self, builder: WindowsUnattendedBuilder
    ) -> None:
        """One builder instance must produce correct XML for different
        VMs without leaking state between calls."""
        vm_a = _win_vm(name="ALPHA")
        vm_b = _win_vm(name="BRAVO")
        assert "<ComputerName>ALPHA</ComputerName>" in builder.build_xml(vm_a)
        assert "<ComputerName>BRAVO</ComputerName>" in builder.build_xml(vm_b)


class TestValidation:
    def test_missing_root_raises(
        self, builder: WindowsUnattendedBuilder
    ) -> None:
        vm = _win_vm(users=[Credential("alice", "pw")])
        with pytest.raises(CloudInitError):
            builder.build_xml(vm)


class TestWriteAutounattendIso:
    def test_writes_one_file_as_autounattend_xml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ISO must contain exactly one file: autounattend.xml at root."""
        # PyCdlib is imported lazily inside write_autounattend_iso via
        # ``from pycdlib import PyCdlib`` — patch the real pycdlib module
        # so the local import picks up our stub.
        import pycdlib

        iso_obj = MagicMock()
        monkeypatch.setattr(pycdlib, "PyCdlib", lambda: iso_obj)

        write_autounattend_iso(tmp_path / "unatt.iso", "<unattend/>")

        iso_obj.new.assert_called_once()
        assert iso_obj.add_fp.call_count == 1
        kwargs = iso_obj.add_fp.call_args.kwargs
        assert kwargs["joliet_path"] == "/autounattend.xml"
        assert kwargs["iso_path"] == "/AUTOUNATT.XML;1"
        iso_obj.write.assert_called_once_with(str(tmp_path / "unatt.iso"))

    def test_wraps_errors_in_cloud_init_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pycdlib

        class BadIso:
            def new(self, **_): pass
            def add_fp(self, *_, **__): raise RuntimeError("boom")
            def close(self): pass

        monkeypatch.setattr(pycdlib, "PyCdlib", lambda: BadIso())

        with pytest.raises(CloudInitError, match="boom"):
            write_autounattend_iso(tmp_path / "bad.iso", "<unattend/>")
