"""Unit tests for :mod:`testrange.vms.builders._proxmox_prepare`.

The prep step (adding ``/auto-installer-mode.toml`` to a vanilla PVE
ISO) is the one place TestRange interacts with the PVE installer's
on-disk activation contract.  These tests pin the contract:

* the toml body matches PVE 9.x's ``proxmox-fetch-answer`` schema
  (``mode = "partition"``, underscored ``partition_label``)
* the volume label is configurable
* ``ProxmoxPrepareError`` wraps xorriso failures + missing-binary
* the xorriso invocation preserves boot setup via ``-boot_image any
  keep`` (the line that fixes the prepared-ISO-drops-to-grub-shell
  bug — earlier pycdlib path lost the hybrid GPT/MBR/HFS+
  infrastructure and PVE's UEFI GRUB couldn't find ``grub.cfg``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from testrange.vms.builders._proxmox_prepare import (
    ProxmoxPrepareError,
    prepare_iso_bytes,
)


def _stub_xorriso(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bin_path: str | None = "/usr/bin/xorriso",
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Patch ``shutil.which`` and ``subprocess.run`` for unit tests.

    Returns the ``subprocess.run`` mock so call args can be asserted.
    """
    import testrange.vms.builders._proxmox_prepare as pp

    monkeypatch.setattr(pp.shutil, "which", lambda _: bin_path)
    run_mock = MagicMock(
        return_value=MagicMock(returncode=returncode, stderr=stderr),
    )
    if returncode != 0:
        # ``check=True`` causes subprocess.run to raise on non-zero;
        # mirror that so the production code's except path runs.
        run_mock.side_effect = subprocess.CalledProcessError(
            returncode=returncode,
            cmd=["xorriso"],
            output="",
            stderr=stderr,
        )
    monkeypatch.setattr(pp.subprocess, "run", run_mock)
    return run_mock


class TestPrepareIsoBytesXorrisoCommand:
    """The ``xorriso`` argv is the entire production payload — these
    tests pin the flags so a future refactor can't silently drop the
    boot-preservation knob (which, when missing, produces an ISO that
    drops to ``grub>`` instead of running the installer)."""

    def test_keeps_boot_image_setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_mock = _stub_xorriso(monkeypatch)
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        prepare_iso_bytes(src, out)

        argv = run_mock.call_args.args[0]
        # ``-boot_image any keep`` is the load-bearing argument.
        # Without it xorriso re-derives the boot setup from scratch
        # and loses the hybrid GPT/MBR/HFS+ layout that PVE's UEFI
        # GRUB depends on to locate its grub.cfg.
        i = argv.index("-boot_image")
        assert argv[i + 1] == "any"
        assert argv[i + 2] == "keep"

    def test_maps_toml_to_iso_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The prepared ISO path the PVE installer reads is hardcoded
        # at ``/cdrom/auto-installer-mode.toml`` (i.e., ISO-root
        # ``/auto-installer-mode.toml``).  Anywhere else and the
        # installer drops to interactive mode.
        run_mock = _stub_xorriso(monkeypatch)
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        prepare_iso_bytes(src, out)

        argv = run_mock.call_args.args[0]
        i = argv.index("-map")
        # argv[i+1] is the local temp file; argv[i+2] is the ISO path.
        assert argv[i + 2] == "/auto-installer-mode.toml"

    def test_lifts_return_with_threshold_to_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # xorriso's default ``-return_with SORRY 32`` exits non-zero
        # on any SORRY-level event — including the benign
        # post-write re-assessment SORRY about the protective MBR's
        # partition-size field referring to the *original* image
        # size when the new image grew by one file.  We lift the
        # threshold to FAILURE so SORRY-only sessions still exit
        # zero; real write-side issues still surface as FAILURE /
        # FATAL.
        run_mock = _stub_xorriso(monkeypatch)
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        prepare_iso_bytes(src, out)

        argv = run_mock.call_args.args[0]
        i = argv.index("-return_with")
        assert argv[i + 1] == "FAILURE"
        assert argv[i + 2] == "32"

    def test_routes_through_indev_outdev(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``-indev`` (read source) + ``-outdev`` (write target) is the
        # only mode that lets ``-boot_image any keep`` preserve the
        # original boot infrastructure.  ``-dev`` (in-place) drops
        # the El Torito record entirely.
        run_mock = _stub_xorriso(monkeypatch)
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        prepare_iso_bytes(src, out)

        argv = run_mock.call_args.args[0]
        assert "-indev" in argv
        assert "-outdev" in argv
        assert "-dev" not in argv  # would silently break UEFI boot
        assert argv[argv.index("-indev") + 1] == str(src.resolve())
        assert argv[argv.index("-outdev") + 1] == str(out.resolve())


class TestPrepareIsoBytesTomlBody:
    """The TOML body sent in must match the PVE 9.x parser's expectations
    — ``mode = "partition"`` + ``partition_label = "<label>"``
    (note underscore, distinct from answer.toml's kebab-case)."""

    def _captured_toml(
        self,
        run_mock: MagicMock,
    ) -> str:
        """Fish the temp-file payload out of the recorded argv."""
        argv = run_mock.call_args.args[0]
        local = argv[argv.index("-map") + 1]
        return Path(local).read_text(encoding="utf-8")

    def test_default_partition_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The prep code unlinks the temp file in its ``finally``, so
        # snapshot the bytes via a side-effect on subprocess.run.
        captured: dict[str, str] = {}

        def _capture(argv, **kw):
            local = argv[argv.index("-map") + 1]
            captured["body"] = Path(local).read_text(encoding="utf-8")
            return MagicMock(returncode=0, stderr="")

        import testrange.vms.builders._proxmox_prepare as pp
        monkeypatch.setattr(pp.shutil, "which", lambda _: "/usr/bin/xorriso")
        monkeypatch.setattr(pp.subprocess, "run", _capture)

        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out)

        body = captured["body"]
        assert 'mode = "partition"' in body
        assert 'partition_label = "PROXMOX-AIS"' in body
        # Underscored, not kebab-case — that's the PVE parser quirk.
        assert "partition-label" not in body

    def test_custom_partition_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, str] = {}

        def _capture(argv, **kw):
            local = argv[argv.index("-map") + 1]
            captured["body"] = Path(local).read_text(encoding="utf-8")
            return MagicMock(returncode=0, stderr="")

        import testrange.vms.builders._proxmox_prepare as pp
        monkeypatch.setattr(pp.shutil, "which", lambda _: "/usr/bin/xorriso")
        monkeypatch.setattr(pp.subprocess, "run", _capture)

        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out, partition_label="MY-LABEL")

        assert 'partition_label = "MY-LABEL"' in captured["body"]


class TestPrepareIsoBytesErrorHandling:
    def test_missing_xorriso_raises_with_install_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_xorriso(monkeypatch, bin_path=None)
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        with pytest.raises(
            ProxmoxPrepareError,
            match=r"xorriso not found.*apt install xorriso",
        ):
            prepare_iso_bytes(src, out)

    def test_xorriso_failure_surfaces_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_xorriso(
            monkeypatch,
            returncode=32,
            stderr="libisofs: FAILURE : malformed input",
        )
        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")

        with pytest.raises(
            ProxmoxPrepareError,
            match=r"exit 32.*malformed input",
        ):
            prepare_iso_bytes(src, out)

    def test_temp_toml_is_cleaned_up_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Failing path must still ``unlink`` the staged TOML or
        # repeated failures pile up in /tmp.
        observed: list[Path] = []

        def _capture(argv, **kw):
            observed.append(Path(argv[argv.index("-map") + 1]))
            raise subprocess.CalledProcessError(
                returncode=1, cmd=argv, stderr="boom",
            )

        import testrange.vms.builders._proxmox_prepare as pp
        monkeypatch.setattr(pp.shutil, "which", lambda _: "/usr/bin/xorriso")
        monkeypatch.setattr(pp.subprocess, "run", _capture)

        src, out = tmp_path / "v.iso", tmp_path / "p.iso"
        src.write_bytes(b"")
        with pytest.raises(ProxmoxPrepareError):
            prepare_iso_bytes(src, out)

        assert observed, "xorriso wasn't called?"
        assert not observed[0].exists()


class TestProxmoxPrepareError:
    def test_inherits_from_testrange_error(self) -> None:
        """The exception must be catchable via the project-wide
        ``TestRangeError`` so callers can ``except TestRangeError``
        without enumerating builder-specific types."""
        from testrange.exceptions import TestRangeError
        assert issubclass(ProxmoxPrepareError, TestRangeError)

    def test_can_be_raised_with_message(self) -> None:
        with pytest.raises(ProxmoxPrepareError, match="custom"):
            raise ProxmoxPrepareError("custom message")
