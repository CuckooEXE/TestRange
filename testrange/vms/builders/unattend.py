"""Windows unattended :class:`~testrange.vms.builders.base.Builder`.

The install phase boots the Windows installer from the supplied ISO
with an autounattend seed ISO attached.  Windows Setup partitions a
blank qcow2, installs Windows, reboots into OOBE, and the
FirstLogonCommands install virtio drivers, enable WinRM, run Winget
packages, run any caller ``post_install_cmds``, and finally
``shutdown /s /t 0`` so the orchestrator can cache the disk.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from testrange.cache import vm_config_hash
from testrange.exceptions import CloudInitError
from testrange.vms.builders.base import Builder, InstallDomain, RunDomain
from testrange.vms.images import resolve_image

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.credentials import Credential
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM as VM


def _unattend_iso_ref(run: "RunDir", vm_name: str) -> str:
    """Backend-local ref for *vm_name*'s autounattend seed ISO.

    The autounattend convention (file shape, naming) belongs to this
    builder, not to the generic per-run scratch-dir abstraction.
    """
    return run.path_for(f"{vm_name}-unattend.iso")


_NS = "urn:schemas-microsoft-com:unattend"
"""XML namespace URI for Windows autounattend documents."""

_DEFAULT_PRODUCT_KEY = "VK7JG-NPHTM-C97JM-9MPGT-3V66T"
"""Windows 10/11 **Pro** generic installation key, publicly documented
by Microsoft as the KMS-client setup key.

Its role in an unattended install is edition selection, not activation:
multi-edition ISOs (like the standard Win10 consumer media) ship
``install.wim`` with Home / Pro / Education inside, and Windows Setup
refuses to proceed silently without either a ``ProductKey`` or an
explicit ``ImageInstall/OSImage/InstallFrom/MetaData`` edition hint.
The KMS key tells Setup to install Pro and move on; the resulting VM
runs unactivated (fine for test-range use).

Callers who need a different edition or a real retail key can pass
``WindowsUnattendedBuilder(product_key=...)``; explicitly passing
``None`` omits the element entirely (valid for Enterprise eval ISOs
and custom single-edition media)."""


def _el(
    tag: str,
    text: str | None = None,
    attrib: dict[str, str] | None = None,
) -> ET.Element:
    elem = ET.Element(tag, attrib=attrib or {})
    if text is not None:
        elem.text = text
    return elem


class WindowsUnattendedBuilder(Builder):
    """Windows unattended install + run strategy.

    Stateless: VM-specific values (hostname, users, packages) are pulled
    from the VM argument each call.  Global knobs — product key, UI
    language, timezone — live on the builder and apply to every VM
    that uses this instance.

    :param product_key: Windows product key used for edition selection
        during unattended Setup.  Defaults to the publicly documented
        Windows 10/11 Pro KMS generic install key so multi-edition
        consumer ISOs proceed without a prompt.  Pass a retail key for
        activation, or ``None`` to omit the element entirely (valid for
        Enterprise-eval and single-edition ISOs).
    :param ui_language: Windows UI language code.  Defaults to
        ``'en-US'``.
    :param timezone: Windows timezone string.  Defaults to ``'UTC'``.
    """

    product_key: str | None
    """Windows product key, or ``None`` to omit the element."""

    ui_language: str
    """UI language code for the install."""

    timezone: str
    """Windows timezone string (e.g. ``'Pacific Standard Time'``)."""

    def __init__(
        self,
        product_key: str | None = _DEFAULT_PRODUCT_KEY,
        ui_language: str = "en-US",
        timezone: str = "UTC",
    ) -> None:
        self.product_key = product_key
        self.ui_language = ui_language
        self.timezone = timezone

    def default_communicator(self) -> str:
        """WinRM over HTTP 5985 — enabled by :meth:`_first_logon_commands`."""
        return "winrm"

    def needs_boot_keypress(self) -> bool:
        """Windows install ISOs under UEFI show a 5-second 'Press any
        key to boot from CD or DVD...' prompt.  No keypress = OVMF
        falls through to the empty disk and lands in the EFI shell.
        The orchestrator spams spacebars during early boot to consume
        that prompt."""
        return True

    def cache_key(self, vm: VM) -> str:
        """Same config hash the Linux path uses — iso + users + packages
        + commands + disk size.  Cached post-install disks are keyed
        identically; the only thing that changes between Linux and
        Windows cache entries is the payload, not the hash formula.
        """
        return vm_config_hash(
            iso=vm.iso,
            usernames_passwords_sudo=[
                (c.username, c.password, c.sudo) for c in vm.users
            ],
            package_reprs=[repr(p) for p in vm.pkgs],
            post_install_cmds=vm.post_install_cmds,
            disk_size=vm._primary_disk_size(),
        )

    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> InstallDomain:
        # 1. Resolve + stage the Windows ISO (outer cache), then push
        # it to whichever host the backend's hypervisor will boot from.
        local_iso = cache.stage_local_iso(resolve_image(vm.iso, cache))
        windows_iso_ref = cache.stage_source(local_iso, run.storage)

        # 2. Blank OS disk — Setup creates its own GPT partitions.
        work_disk_ref = run.create_blank_disk(
            vm.name, vm._primary_disk_size()
        )

        # 3. Autounattend seed ISO — generated in memory, written via
        # the storage backend so it lands wherever the hypervisor will
        # be reading from.
        unattend_ref = _unattend_iso_ref(run, vm.name)
        run.storage.transport.write_bytes(
            unattend_ref,
            build_autounattend_iso_bytes(self.build_xml(vm)),
        )

        # 4. virtio-win driver ISO so FirstLogonCommands can install
        # NetKVM and the qemu-guest-agent MSI.  Same stage-to-backend
        # dance as the Windows ISO.
        local_virtio = cache.get_virtio_win_iso()
        virtio_iso_ref = cache.stage_source(local_virtio, run.storage)

        return InstallDomain(
            work_disk=work_disk_ref,
            seed_iso=unattend_ref,
            extra_cdroms=(windows_iso_ref, virtio_iso_ref),
            uefi=True,
            windows=True,
            boot_cdrom=True,
        )

    def install_manifest(
        self,
        vm: VM,
        config_hash: str,
    ) -> dict[str, Any]:
        return {
            "name": vm.name,
            "iso": vm.iso,
            "users": [
                {"username": c.username, "sudo": c.sudo} for c in vm.users
            ],
            "packages": [repr(p) for p in vm.pkgs],
            "post_install_cmds": vm.post_install_cmds,
            "disk_size": vm._primary_disk_size(),
            "config_hash": config_hash,
            "windows": True,
        }

    def prepare_run_domain(
        self,
        vm: VM,
        run: RunDir,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> RunDomain:
        # Windows run boots come up with no seed ISO — FirstLogonCommands
        # already set the hostname, user accounts, and services during
        # install.  Static IPs come from the backend's DHCP
        # reservations (MAC-matched).
        return RunDomain(seed_iso=None, uefi=True, windows=True)

    # ------------------------------------------------------------------
    # Autounattend XML generation — kept public so tests and debugging
    # can inspect the exact XML without booting anything.
    # ------------------------------------------------------------------

    def build_xml(self, vm: VM) -> str:
        """Generate and return the ``autounattend.xml`` document.

        :raises CloudInitError: If required credentials (root) are
            missing from the VM.
        """
        root = ET.Element("unattend", {"xmlns": _NS})

        # ---- windowsPE pass ------------------------------------------
        pe_pass = ET.SubElement(root, "settings", {"pass": "windowsPE"})

        int_ui = ET.SubElement(pe_pass, "component", {
            "name": "Microsoft-Windows-International-Core-WinPE",
            "processorArchitecture": "amd64",
            "publicKeyToken": "31bf3856ad364e35",
            "language": "neutral",
            "versionScope": "nonSxS",
            "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
        })
        ET.SubElement(int_ui, "SetupUILanguage").append(
            _el("UILanguage", self.ui_language)
        )
        ET.SubElement(int_ui, "UILanguage").text = self.ui_language
        ET.SubElement(int_ui, "UserLocale").text = self.ui_language
        ET.SubElement(int_ui, "SystemLocale").text = self.ui_language
        ET.SubElement(int_ui, "InputLocale").text = self.ui_language

        setup_comp = ET.SubElement(pe_pass, "component", {
            "name": "Microsoft-Windows-Setup",
            "processorArchitecture": "amd64",
            "publicKeyToken": "31bf3856ad364e35",
            "language": "neutral",
            "versionScope": "nonSxS",
        })

        disk_config = ET.SubElement(setup_comp, "DiskConfiguration")
        disk = ET.SubElement(disk_config, "Disk", {
            "wcm:action": "add",
            "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
        })
        ET.SubElement(disk, "DiskID").text = "0"
        ET.SubElement(disk, "WillWipeDisk").text = "true"

        create_parts = ET.SubElement(disk, "CreatePartitions")
        esp = ET.SubElement(create_parts, "CreatePartition", {
            "wcm:action": "add",
            "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
        })
        ET.SubElement(esp, "Order").text = "1"
        ET.SubElement(esp, "Type").text = "EFI"
        ET.SubElement(esp, "Size").text = "260"
        msr = ET.SubElement(create_parts, "CreatePartition", {
            "wcm:action": "add",
            "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
        })
        ET.SubElement(msr, "Order").text = "2"
        ET.SubElement(msr, "Type").text = "MSR"
        ET.SubElement(msr, "Size").text = "128"
        primary = ET.SubElement(create_parts, "CreatePartition", {
            "wcm:action": "add",
            "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
        })
        ET.SubElement(primary, "Order").text = "3"
        ET.SubElement(primary, "Type").text = "Primary"
        ET.SubElement(primary, "Extend").text = "true"

        img_install = ET.SubElement(setup_comp, "ImageInstall")
        os_image = ET.SubElement(img_install, "OSImage")
        install_to = ET.SubElement(os_image, "InstallTo")
        ET.SubElement(install_to, "DiskID").text = "0"
        ET.SubElement(install_to, "PartitionID").text = "3"
        ET.SubElement(os_image, "WillShowUI").text = "Never"

        user_data = ET.SubElement(setup_comp, "UserData")
        # ProductKey belongs inside UserData in the windowsPE pass per
        # the Microsoft unattend schema — placing it as a direct child
        # of Microsoft-Windows-Setup makes Setup silently ignore it
        # ("can't read product key from the answer file").
        if self.product_key:
            prod_key_el = ET.SubElement(user_data, "ProductKey")
            ET.SubElement(prod_key_el, "Key").text = self.product_key
            ET.SubElement(prod_key_el, "WillShowUI").text = "Never"
        ET.SubElement(user_data, "AcceptEula").text = "true"
        ET.SubElement(user_data, "FullName").text = "TestRange"
        ET.SubElement(user_data, "Organization").text = "TestRange"

        # ---- specialize pass -----------------------------------------
        spec_pass = ET.SubElement(root, "settings", {"pass": "specialize"})
        shell_comp = ET.SubElement(spec_pass, "component", {
            "name": "Microsoft-Windows-Shell-Setup",
            "processorArchitecture": "amd64",
            "publicKeyToken": "31bf3856ad364e35",
            "language": "neutral",
            "versionScope": "nonSxS",
        })
        ET.SubElement(shell_comp, "ComputerName").text = vm.name
        ET.SubElement(shell_comp, "TimeZone").text = self.timezone

        # ---- oobeSystem pass -----------------------------------------
        oobe_pass = ET.SubElement(root, "settings", {"pass": "oobeSystem"})
        oobe_comp = ET.SubElement(oobe_pass, "component", {
            "name": "Microsoft-Windows-Shell-Setup",
            "processorArchitecture": "amd64",
            "publicKeyToken": "31bf3856ad364e35",
            "language": "neutral",
            "versionScope": "nonSxS",
        })

        admin_pwd = ET.SubElement(oobe_comp, "UserAccounts")
        admin_acct = ET.SubElement(admin_pwd, "AdministratorPassword")
        ET.SubElement(admin_acct, "Value").text = _admin_password(vm.users)
        ET.SubElement(admin_acct, "PlainText").text = "true"

        local_accounts = ET.SubElement(admin_pwd, "LocalAccounts")
        for cred in vm.users:
            if cred.is_root():
                continue
            la = ET.SubElement(local_accounts, "LocalAccount", {
                "wcm:action": "add",
                "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
            })
            pwd_el = ET.SubElement(la, "Password")
            ET.SubElement(pwd_el, "Value").text = cred.password
            ET.SubElement(pwd_el, "PlainText").text = "true"
            ET.SubElement(la, "DisplayName").text = cred.username
            ET.SubElement(la, "Group").text = (
                "Administrators" if cred.sudo else "Users"
            )
            ET.SubElement(la, "Name").text = cred.username

        oobe_cfg = ET.SubElement(oobe_comp, "OOBE")
        ET.SubElement(oobe_cfg, "HideEULAPage").text = "true"
        ET.SubElement(oobe_cfg, "HideOEMRegistrationScreen").text = "true"
        ET.SubElement(oobe_cfg, "HideOnlineAccountScreens").text = "true"
        ET.SubElement(oobe_cfg, "HideWirelessSetupInOOBE").text = "true"
        ET.SubElement(oobe_cfg, "NetworkLocation").text = "Work"
        ET.SubElement(oobe_cfg, "ProtectYourPC").text = "3"
        ET.SubElement(oobe_cfg, "SkipMachineOOBE").text = "true"
        ET.SubElement(oobe_cfg, "SkipUserOOBE").text = "true"

        flc = ET.SubElement(oobe_comp, "FirstLogonCommands")
        for idx, cmd in enumerate(
            _first_logon_commands(vm.pkgs, vm.post_install_cmds), start=1
        ):
            sc = ET.SubElement(flc, "SynchronousCommand", {
                "wcm:action": "add",
                "xmlns:wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
            })
            ET.SubElement(sc, "Order").text = str(idx)
            ET.SubElement(sc, "CommandLine").text = (
                f'powershell.exe -NoProfile -NonInteractive -Command "{cmd}"'
            )
            ET.SubElement(sc, "RequiresUserInput").text = "false"

        ET.indent(root)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            + ET.tostring(root, encoding="unicode")
        )


# ----------------------------------------------------------------------
# Module-level helpers — used to be methods on the stateful builder.
# ----------------------------------------------------------------------


def _admin_password(users: list[Credential]) -> str:
    root = next((u for u in users if u.is_root()), None)
    if root is None:
        raise CloudInitError(
            "Windows VMs require a Credential(username='root', ...) entry "
            "to set the Administrator password."
        )
    return root.password


def _first_logon_commands(
    packages: list[AbstractPackage],
    post_install_cmds: list[str],
) -> list[str]:
    """Build the PowerShell command list for FirstLogonCommands.

    Installs virtio drivers and the qemu-guest-agent MSI from the
    virtio-win ISO, opens WinRM for basic auth, runs Winget packages,
    runs caller-supplied ``post_install_cmds``, and finally powers the
    VM off so the orchestrator can snapshot the disk into the cache.
    """
    cmds: list[str] = [
        (
            "$vw = (Get-Volume | Where-Object {$_.FileSystemLabel -like 'virtio*'}"
            " | Select-Object -First 1).DriveLetter; "
            "if ($vw) { "
            "Get-ChildItem -Path \"$vw:\\\" -Recurse -Filter *.inf "
            "| ForEach-Object { pnputil /add-driver $_.FullName /install } "
            "}"
        ),
        (
            "$vw = (Get-Volume | Where-Object {$_.FileSystemLabel -like 'virtio*'}"
            " | Select-Object -First 1).DriveLetter; "
            "if ($vw -and (Test-Path \"${vw}:\\guest-agent\\qemu-ga-x86_64.msi\")) {"
            " Start-Process -Wait msiexec -ArgumentList "
            "'/i', \"${vw}:\\guest-agent\\qemu-ga-x86_64.msi\", '/qn', '/norestart' "
            "}"
        ),
        "Enable-PSRemoting -Force -SkipNetworkProfileCheck",
        (
            "Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted $true; "
            "Set-Item WSMan:\\localhost\\Service\\Auth\\Basic $true; "
            "netsh advfirewall firewall add rule name=\"WinRM-HTTP\" "
            "dir=in action=allow protocol=TCP localport=5985"
        ),
    ]

    for pkg in packages:
        if pkg.package_manager == "winget":
            cmds.extend(pkg.install_commands())

    cmds.extend(post_install_cmds)
    cmds.append("shutdown /s /t 0")
    return cmds


def build_autounattend_iso_bytes(autounattend_xml: str) -> bytes:
    """Return the bytes of an ISO 9660 seed CD containing
    ``autounattend.xml``.

    Windows Setup scans every attached FAT/NTFS/CDFS volume for a file
    named ``autounattend.xml`` at the root and uses the first match as
    the unattended answer file.  Mount this ISO as a second CD-ROM
    next to the Windows install media and the install runs unattended.

    Uses :mod:`pycdlib` — same machinery as
    :func:`testrange.vms.builders.cloud_init.build_seed_iso_bytes`, so
    no external ``genisoimage`` / ``xorriso`` dependency.  Returning
    bytes (rather than writing to a path) keeps generation backend-
    agnostic: the caller hands the bytes to whatever storage backend
    is in play.
    """
    from pycdlib import PyCdlib  # type: ignore[attr-defined]

    iso = PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="UNATTEND")

    data = autounattend_xml.encode("utf-8")
    buf = io.BytesIO()
    try:
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            iso_path="/AUTOUNATT.XML;1",
            joliet_path="/autounattend.xml",
        )
        iso.write_fp(buf)
        return buf.getvalue()
    except Exception as exc:
        raise CloudInitError(
            f"Failed to build autounattend ISO: {exc}"
        ) from exc
    finally:
        iso.close()


__all__ = ["WindowsUnattendedBuilder", "build_autounattend_iso_bytes"]
