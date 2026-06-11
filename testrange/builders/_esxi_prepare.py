"""Prepare an ESXi installer ISO for unattended (kickstart) install (ADR-0022).

ESXi's weasel installer activates kickstart mode when the kernel command line
carries ``ks=<source>``. We use ``ks=cdrom:/ks.cfg`` (single-CDROM mode): weasel
walks every attached CD-ROM for ``/ks.cfg``, and we inject it into the booted
installer ISO itself. Two-pass xorriso:

1. Extract ``/BOOT.CFG`` (BIOS, every ESXi 5+) and ``/EFI/BOOT/BOOT.CFG`` (UEFI,
   ESXi 7+ only — tolerate absence) and patch each ``kernelopt=`` line to
   ``runweasel ks=cdrom:/ks.cfg logPort=com1 gdbPort=none`` (DROP the original
   ``cdromBoot`` — it makes weasel handle the boot CD specially and skip the ks=
   search). ``logPort=com1`` makes the **installer's** vmkernel stream its log out
   COM1; ``%post`` injects the build-result record into that vmkernel log via
   ``vsish`` (see :func:`render_kickstart`), which is the only userspace→serial
   channel ESXi has — the build VM's serial sink reads it host-side.
2. Re-emit the ISO with the patched configs back at their paths + ``ks.cfg`` at
   the root.

Load-bearing xorriso flags (from the proven ``esxi-kickstart.sh`` flow):

- ``-rockridge off`` — ESXi's stripped cdfs is pure ISO9660 and REJECTS
  Rock-Ridge-decorated entries; with RR on, ``ks.cfg`` is invisible to weasel.
- ``-compliance lowercase`` — strict ISO9660 uppercases filenames, so without
  this xorriso writes ``KS.CFG`` and weasel's case-sensitive ``ks=cdrom:/ks.cfg``
  lookup ENOENTs. Relaxing compliance keeps the lowercase entry (``BOOT.CFG``
  is authored uppercase on the source and stays that way).
- ``-boot_image any patch`` (NOT ``keep``) — updates ISOLINUX's boot-info-table
  self-LBA checksum after the injected files shift the layout; ``keep`` leaves a
  stale checksum and SeaBIOS bombs with "ISOLINUX: Image checksum error" before
  the kernel loads.
- ``-return_with FAILURE 32`` — same benign post-write MBR-size SORRY workaround
  as the PVE prep.

``ks.cfg`` is lowercase at the root: cdfs is case-sensitive, so
``ks=cdrom:/KS.CFG`` returns ENOENT against a lowercase entry.

This is a sanctioned ``subprocess`` use — see ADR-0022 (shared with the PVE prep
module); the project-wide ban (ADR-0001) carves out exactly these two modules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import BuilderError

_log = get_logger(__name__)

# xorriso is run under a fixed C locale so the diagnostic strings _extract()
# matches on ("not found", "no such file", ...) stay stable regardless of the
# host's $LANG/$LC_MESSAGES.
_C_LOCALE_ENV = {**os.environ, "LC_ALL": "C"}

_BOOTCFG_BIOS = "/BOOT.CFG"
_BOOTCFG_UEFI = "/EFI/BOOT/BOOT.CFG"
# logPort=com1 streams the installer vmkernel log out COM1 so the %post
# build-result (injected via `vsish -e set /system/log`) reaches the build VM's
# serial sink; gdbPort=none keeps the kernel debugger off the UARTs.
#
# systemMediaSize=min caps ESX-OSData at the ~33 GiB minimum so the installer
# leaves the rest of the disk for a local VMFS datastore (ESXI-20). WITHOUT it,
# ESX-OSData expands to fill a modest disk and the installer creates NO local
# datastore — and a nested lab node with no datastore can host nothing (the ESXi
# driver's create_pool needs a datastore to fold a pool into). With it, a node
# sized >= ~70 GiB comes up with a usable "datastore1" the driver can target.
_KICKSTART_KERNELOPT = "runweasel ks=cdrom:/ks.cfg logPort=com1 gdbPort=none systemMediaSize=min"
_KICKSTART_FILENAME = "ks.cfg"


class EsxiPrepareError(BuilderError):
    """Raised when preparing an ESXi installer ISO fails (missing ``xorriso``,
    a corrupt/unrecognized vanilla ISO, or a non-zero ``xorriso`` exit)."""


def render_kickstart(
    *,
    root_password: str,
    ssh_key: str | None = None,
    license: str | None = None,
    allow_tcp_forwarding: bool = False,
) -> str:
    """Build a ks.cfg that installs ESXi unattended and reports a build result.

    Build-result over serial, ESXi-style (ADR-0012). ESXi has **no userspace
    serial write path** — ``/dev/char/serial/uart*`` is held by the vmkernel and
    is not a tty, so the Linux-guest idiom ``echo … > /dev/ttyS0`` is silently
    swallowed. The one channel that works: the **installer's** vmkernel streams
    its log out COM1 (``logPort=com1``, patched into the installer BOOT.CFG by
    :func:`_patch_bootcfg`), and ``vsish -e set /system/log "<text>"`` injects an
    arbitrary line into that vmkernel log. So the record is emitted from ``%post``
    — which runs in the installer, *after* weasel has written the disk — straight
    onto the build VM's serial sink.

    Reaching ``%post`` means the install succeeded (weasel halts before ``%post``
    on a failed install), so ``%post`` emits ``ok`` unconditionally, then powers
    the installer off with ``poweroff -f`` and the orchestrator captures the disk.
    A failed install never reaches ``%post``: the console closes without ``ok``
    and the orchestrator raises ``BuildFailedError`` (the silent-corrupt-cache
    guard). ``poweroff -f`` is the *only* hostd-free poweroff that works in the
    installer — ``esxcli``/``localcli system shutdown`` need hostd, which is the
    original ESXI-17 hang; the install is already finalized when ``%post`` runs, so
    a hard poweroff is safe.

    The marker is assembled from shell variables (``${_t}-${_r}``) so the literal
    ``TESTRANGE-RESULT:`` never appears in the ks.cfg **source**: weasel echoes
    every section body to the same serial at parse time, and a verbatim marker
    there would false-trigger the orchestrator's parser before the real emission.

    ``%firstboot`` carries run-phase provisioning only — the vmk0 MAC follow
    (ESXI-18), always; plus the SSH key + sshd enable when ``ssh_key`` is set
    (ESXI-19). It runs when the *captured* disk is booted for a run, not during the
    build, so a provisioning hiccup there can't corrupt the build-result signal.

    Args:
      root_password: plaintext root password (``rootpw``). ESXi enforces
        complexity (>=8 chars, mixed classes); the caller's Credential is the
        source of truth, so we only reject an empty password here.
      ssh_key: the root OpenSSH public key, or ``None`` when SSH is not the
        transport (ESXI-19). When set, ``%firstboot`` writes it to
        ``/etc/ssh/keys-root/authorized_keys`` (ESXi's path, NOT ``~root/.ssh``)
        and enables sshd via ``/etc/rc.local.d/local.sh`` (which runs late, after
        hostd — ``vim-cmd`` in ``%firstboot`` itself runs too early and hangs). The
        vmk0 MAC-follow fix rides that same ``local.sh`` regardless of this arg.
        NOTE: ESXi 8 sshd runs in FIPS mode and rejects Ed25519 keys; use RSA/ECDSA.
      license: optional ESXi license key. When set, weasel applies it at install
        time via the top-level ``serialnum --esx=<key>`` directive, so the node
        boots licensed instead of on the read-only free/evaluation edition.
        ``None`` leaves the default evaluation license in place.
      allow_tcp_forwarding: when True (and ``ssh_key`` is set), ``%firstboot``
        appends ``AllowTcpForwarding yes`` to ``/etc/ssh/sshd_config``. ESXi's
        ``guest_gateway`` SSH-jumps to guests over a ``direct-tcpip`` channel,
        which sshd refuses without it (ESXI-22); the default leaves sshd's
        stock policy in place. Inert without a key (no sshd is provisioned).
    """
    if not root_password:
        raise EsxiPrepareError(
            "ESXi kickstart requires a non-empty root_password "
            "(ESXi has no installable system without one)."
        )
    install = [
        "# Generated by ESXiKickstartBuilder — do not edit by hand.",
        "accepteula",
        # serialnum is a top-level weasel directive (alongside accepteula/rootpw),
        # applied during install — not a %firstboot esxcli/vim-cmd call.
        *([f"serialnum --esx={license}"] if license else []),
        f"rootpw {root_password}",
        # --firstdisk picks the first non-install-source disk; --overwritevmfs
        # clears any prior VMFS so a re-run is idempotent.
        "install --firstdisk --overwritevmfs",
        "network --bootproto=dhcp --device=vmnic0",
        "reboot",
        "",
    ]
    # %post (installer env): the install is done and the installer vmkernel is
    # streaming its log out COM1, so inject the build-result into that log via
    # vsish and power the installer off. _t/_r keep the literal marker out of the
    # ks.cfg source (weasel echoes section bodies to the serial at parse time).
    result = [
        "%post --interpreter=busybox --ignorefailure=true",
        "_t=TESTRANGE",
        "_r=RESULT",
        'vsish -e set /system/log "${_t}-${_r}: ok"',
        # let the record drain out the serial before the hard poweroff
        "sleep 2",
        "poweroff -f",
    ]
    return "\n".join([*install, *result, *_firstboot(ssh_key, allow_tcp_forwarding)]) + "\n"


def _firstboot(ssh_key: str | None, allow_tcp_forwarding: bool) -> list[str]:
    """Run-phase ``%firstboot`` provisioning.

    Runs once when the *captured* disk is first booted for a run (``%post`` powers
    the installer off before any real boot). It seeds ``/etc/rc.local.d/local.sh``,
    which runs late on *every* boot, after hostd — where ``vim-cmd``/``esxcli``
    work (calling them in ``%firstboot`` itself runs before hostd and hangs).

    **vmk0 MAC follow is solved at the BUILD NIC, not here (ESXI-18).** ESXi pins
    ``vmk0``'s MAC to the pNIC present at *install* and restores it from
    ``esx.conf`` on every later boot (``FollowHardwareMac`` is consulted only at
    vmk *creation*, so the flag-plus-reboot ``local.sh`` block this section used
    to emit was live-disproven — a reboot restores the pinned MAC rather than
    re-creating ``vmk0``). The fix lives upstream: an installer-origin build's
    dedicated build NIC wears the MAC of the VM's first *declared* NIC
    (``_build_nic_for``, orchestrator/build_phase.py), so the install pins
    ``vmk0`` to exactly the identity the run-phase VM wakes up with and the
    orchestrator's lease discovery polls. One boot, no guest-side surgery.

    **SSH (only when SSH is the transport, i.e. ``ssh_key`` is set; ESXI-19).**
    Drop the root key (pure filesystem), and enable + start sshd from
    ``local.sh`` — which runs late on every boot, after hostd, where
    ``vim-cmd``/``esxcli`` work. When ``ssh_key`` is ``None`` the key write and
    sshd enable are omitted entirely and the host gets no open sshd.
    ``allow_tcp_forwarding`` (ESXI-22) rides ``local.sh`` too, as a REPLACE of
    the shipped ``AllowTcpForwarding no`` line: ESXi regenerates
    ``/etc/ssh/sshd_config`` from the ConfigStore after ``%firstboot``, so a
    firstboot edit is reverted — and a trailing append would lose anyway (sshd
    takes the first match). ``local.sh`` then restarts sshd so the edit is live
    (ESXI-36).

    ``%firstboot`` ends with ``/sbin/auto-backup.sh`` and a sentinel-guarded
    one-shot ``reboot``: ESXi's ``/etc`` is a ramdisk overlay, so the key file
    and ``local.sh`` written here survive a later boot only if backed up into
    the persisted state archive — and ``rc.local.d`` has ALREADY run by the
    time ``%firstboot`` fires, so the ``local.sh`` content written above
    executes from the NEXT boot only (live-found twice: boot 1 always settles
    at the DCUI sshd-less). The reboot makes boot 2 — sshd up, config live —
    deterministic; the sentinel (persisted by the same backup) makes it
    one-shot even if ``%firstboot`` ever re-ran (ESXI-36).

    Flat layout (no indentation): busybox only closes a plain ``cat <<'EOF'``
    heredoc on a column-0 terminator, so an indented body would let the heredoc
    swallow the rest of the script — so the appended ``local.sh`` block is
    written unindented.
    """
    lines = ["", "%firstboot --interpreter=busybox"]
    if ssh_key:
        lines += [
            "mkdir -p /etc/ssh/keys-root",
            "cat > /etc/ssh/keys-root/authorized_keys <<'KEYEOF'",
            ssh_key,
            "KEYEOF",
            "chmod 600 /etc/ssh/keys-root/authorized_keys",
            "chown root:root /etc/ssh/keys-root/authorized_keys",
            # REPLACE local.sh (cat >), never append: the stock ESXi local.sh
            # ends with `exit 0`, so appended lines are dead code on every boot
            # — the live-found reason sshd stayed off across three standup
            # attempts while every prior design "appended" its activation
            # (ESXI-36; esxi-manager.sh proved the replace shape).
            "cat > /etc/rc.local.d/local.sh <<'RCEOF'",
            "#!/bin/sh",
        ]
        if allow_tcp_forwarding:
            lines += [
                "grep -vi '^AllowTcpForwarding' /etc/ssh/sshd_config > /etc/ssh/sshd_config.tr",
                "echo 'AllowTcpForwarding yes' >> /etc/ssh/sshd_config.tr",
                "mv /etc/ssh/sshd_config.tr /etc/ssh/sshd_config",
            ]
        lines += [
            "vim-cmd hostsvc/enable_ssh",
            "esxcli network firewall ruleset set --enabled true --ruleset-id sshServer",
            "/etc/init.d/SSH restart",
            "exit 0",
            "RCEOF",
            "chmod +x /etc/rc.local.d/local.sh",
        ]
    lines += [
        "if [ ! -f /etc/vmware/.tr-rebooted ]; then",
        "touch /etc/vmware/.tr-rebooted",
        "/sbin/auto-backup.sh",
        "reboot",
        "fi",
        "/sbin/auto-backup.sh",
    ]
    return lines


def prepare_iso(vanilla_iso: Path, out_path: Path, *, kickstart: str) -> None:
    """Write a kickstart-patched copy of *vanilla_iso* to *out_path* (two-pass).

    Args:
      vanilla_iso: existing ESXi installer ISO on disk.
      out_path: where to write the prepared ISO (overwritten if present).
      kickstart: ks.cfg body (see :func:`render_kickstart`).

    Raises:
      EsxiPrepareError: ``xorriso`` missing, the vanilla ISO is not a recognized
        ESXi image (no ``/BOOT.CFG``), or ``xorriso`` returns non-zero.
    """
    vanilla_iso = vanilla_iso.expanduser().resolve()
    out_path = out_path.expanduser().resolve()
    xorriso = shutil.which("xorriso")
    if xorriso is None:
        raise EsxiPrepareError(
            "xorriso not found on $PATH — install it with `apt install xorriso` "
            "(Debian/Ubuntu), `dnf install xorriso` (Fedora/RHEL), or `brew install "
            "xorriso` (macOS). Required to inject /ks.cfg + patch the ESXi boot "
            "configs while preserving the hybrid boot setup (ADR-0022)."
        )
    _log.info("preparing ESXi ISO %s -> %s", vanilla_iso, out_path)
    # xorriso's -outdev refuses to clobber an existing image (exits non-zero), so
    # clear any stale prepared ISO first — re-preparing the same target is normal.
    out_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="testrange-esxi-prep-") as td:
        tmp = Path(td)
        ks_path = tmp / _KICKSTART_FILENAME
        ks_path.write_text(kickstart, encoding="utf-8")

        bios_cfg = tmp / "BOOT.CFG"
        if not _extract(xorriso, vanilla_iso, _BOOTCFG_BIOS, bios_cfg):
            raise EsxiPrepareError(
                f"vanilla ESXi ISO {vanilla_iso} has no {_BOOTCFG_BIOS} — "
                "not a recognizable ESXi installer image."
            )
        _patch_bootcfg(bios_cfg)

        uefi_cfg = tmp / "BOOT.EFI.CFG"
        have_efi = _extract(xorriso, vanilla_iso, _BOOTCFG_UEFI, uefi_cfg)
        if have_efi:
            _patch_bootcfg(uefi_cfg)
        else:
            _log.info(
                "%s absent in vanilla ISO; assuming legacy-only (pre-7 ESXi) — "
                "UEFI boot will not work on the prepared ISO.",
                _BOOTCFG_UEFI,
            )

        cmd = [
            xorriso,
            "-return_with",
            "FAILURE",
            "32",
            # Keep ks.cfg LOWERCASE on the ISO: ESXi's cdfs is case-sensitive and
            # weasel looks up the exact ``ks=cdrom:/ks.cfg``, but strict ISO9660
            # uppercases names (xorriso would write ``KS.CFG`` and weasel'd ENOENT).
            # Relaxing compliance preserves the lowercase entry; ``BOOT.CFG`` stays
            # uppercase because that is how it is authored on the source.
            "-compliance",
            "lowercase",
            "-indev",
            str(vanilla_iso),
            "-rockridge",
            "off",
            "-outdev",
            str(out_path),
            "-boot_image",
            "any",
            "patch",
            "-map",
            str(ks_path),
            "/ks.cfg",
            "-map",
            str(bios_cfg),
            _BOOTCFG_BIOS,
        ]
        if have_efi:
            cmd.extend(["-map", str(uefi_cfg), _BOOTCFG_UEFI])
        cmd.append("-commit")
        _run_xorriso(cmd)


def _run_xorriso(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True, env=_C_LOCALE_ENV)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise EsxiPrepareError(
            f"xorriso failed (exit {exc.returncode}): {stderr or '(no stderr)'}"
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover — racy with which()
        raise EsxiPrepareError(f"xorriso disappeared between which() and exec(): {exc}") from exc


def _extract(xorriso: str, iso: Path, iso_path: str, out: Path) -> bool:
    """Extract *iso_path* from *iso* to *out*; return whether the file existed.

    A missing file is not an error (pre-7 ESXi ISOs lack the UEFI variant);
    anything else raises.
    """
    proc = subprocess.run(
        [xorriso, "-osirrox", "on", "-indev", str(iso), "-extract", iso_path, str(out)],
        capture_output=True,
        text=True,
        check=False,
        env=_C_LOCALE_ENV,
    )
    if proc.returncode != 0:
        # A genuinely-absent source (the UEFI BOOT.CFG on a legacy-only ISO) is
        # not an error. xorriso words this several ways depending on version:
        # "not found", "No such file or directory", "Cannot determine attributes
        # of (ISO) source file". Match all so absence is tolerated; anything else
        # is a real failure — including a non-zero exit that left a *partial*
        # file behind, which must not be mistaken for a clean extraction.
        stderr = (proc.stderr or "").lower()
        if any(s in stderr for s in ("not found", "no such file", "cannot determine attributes")):
            return False
        out.unlink(missing_ok=True)  # drop any partial extraction before failing
        raise EsxiPrepareError(
            f"xorriso -extract {iso_path!r} failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip() or '(no stderr)'}"
        )
    if out.exists():
        # osirrox preserves the ISO's (often read-only) mode on the extracted
        # copy; make our temp file writable so _patch_bootcfg can rewrite it.
        out.chmod(0o644)
    return out.exists()


def _patch_bootcfg(path: Path) -> None:
    """Rewrite *path*'s ``kernelopt=`` line so weasel runs unattended.

    Idempotent (a config already carrying ``ks=cdrom:/ks.cfg`` is left alone); a
    config with no ``kernelopt=`` line gets one appended. Preserves the source's
    line endings (BOOT.CFGs are often CRLF on the wire) — read/write in BYTES, not
    ``read_text`` (whose universal-newline translation strips the ``\\r`` so the
    CRLF could never be detected).
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if "ks=cdrom:/ks.cfg" in text:
        return
    new_lines: list[str] = []
    seen = False
    for line in text.splitlines():
        if line.startswith("kernelopt="):
            new_lines.append(f"kernelopt={_KICKSTART_KERNELOPT}")
            seen = True
        else:
            new_lines.append(line)
    if not seen:
        new_lines.append(f"kernelopt={_KICKSTART_KERNELOPT}")
    sep = "\r\n" if b"\r\n" in raw else "\n"
    path.write_bytes((sep.join(new_lines) + sep).encode("utf-8"))


__all__ = ["EsxiPrepareError", "prepare_iso", "render_kickstart"]
