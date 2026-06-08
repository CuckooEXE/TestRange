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
_KICKSTART_KERNELOPT = "runweasel ks=cdrom:/ks.cfg logPort=com1 gdbPort=none"
_KICKSTART_FILENAME = "ks.cfg"


class EsxiPrepareError(BuilderError):
    """Raised when preparing an ESXi installer ISO fails (missing ``xorriso``,
    a corrupt/unrecognized vanilla ISO, or a non-zero ``xorriso`` exit)."""


def render_kickstart(
    *, root_password: str, ssh_key: str | None = None, license: str | None = None
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
    return "\n".join([*install, *result, *_firstboot(ssh_key)]) + "\n"


def _firstboot(ssh_key: str | None) -> list[str]:
    """Run-phase ``%firstboot`` provisioning.

    Runs once when the *captured* disk is first booted for a run (``%post`` powers
    the installer off before any real boot). It seeds ``/etc/rc.local.d/local.sh``,
    which runs late on *every* boot, after hostd — where ``vim-cmd``/``esxcli``
    work (calling them in ``%firstboot`` itself runs before hostd and hangs).

    **vmk0 MAC follow (ESXI-18), sentinel-guarded one-shot reboot — ALWAYS.** ESXi
    pins ``vmk0``'s MAC to the pNIC present at *install* (the build NIC) and keeps
    it (``Net.FollowHardwareMac=0`` default), so when the captured disk is booted on
    a different *run* NIC, ``vmk0`` DHCPs under the stale build MAC and the
    orchestrator's lease discovery — keyed on the run NIC's MAC — misses.
    ``FollowHardwareMac`` is read only at ``vmk0`` *creation* and ``vmk0`` is already
    up by the time ``local.sh`` runs, and a live down/up does NOT re-MAC it — so the
    only lever is a reboot. On the first run boot the block sets the flag, persists
    it (``auto-backup.sh``), drops a sentinel, and ``reboot``\\ s. The sentinel
    (``/etc/vmware/.trfollowhwmac``, persisted by that same ``auto-backup.sh``) makes
    it one-shot — the second boot skips the block. That second boot re-creates
    ``vmk0`` under the run NIC's hardware MAC, so the lease lands under the polled
    MAC. (``local.sh``'s plain reboot is what works here — a detached reboot from
    ``%firstboot`` was tried and did not fire. Run-phase lease discovery must
    tolerate the two boots.) This is independent of the transport: the orchestrator
    DHCP-discovers the host however it is later reached, so it is always emitted.

    **SSH (only when SSH is the transport, i.e. ``ssh_key`` is set; ESXI-19).** Drop
    the root key (pure filesystem) before the MAC block, and enable sshd *after* the
    ``fi`` — so on the first boot the MAC block reboots before reaching it, and on
    the post-reboot boot (sentinel present) the block is skipped and sshd comes up.
    When ``ssh_key`` is ``None`` the key write and sshd enable are omitted entirely
    and the host gets no open sshd.

    Flat layout (no indentation): busybox only closes a plain ``cat <<'EOF'``
    heredoc on a column-0 terminator, so an indented body would let the heredoc
    swallow the rest of the script — so the appended ``local.sh`` block (``if``
    body included) is written unindented.
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
        ]
    lines += [
        "cat >> /etc/rc.local.d/local.sh <<'RCEOF'",
        "if [ ! -f /etc/vmware/.trfollowhwmac ]; then",
        "esxcli system settings advanced set -o /Net/FollowHardwareMac -i 1",
        "touch /etc/vmware/.trfollowhwmac",
        "/sbin/auto-backup.sh",
        "reboot",
        "fi",
    ]
    if ssh_key:
        lines += [
            "vim-cmd hostsvc/enable_ssh",
            "vim-cmd hostsvc/start_ssh",
            "esxcli network firewall ruleset set --enabled true --ruleset-id sshServer",
        ]
    lines += ["RCEOF"]
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
