"""Unit tests for :mod:`testrange.vms.builders._proxmox_prepare`.

The prep step (adding ``/auto-installer-mode.toml`` to a vanilla PVE
ISO) is the one place TestRange interacts with the PVE installer's
on-disk activation contract.  These tests pin the contract:

* file is added with the right Rock Ridge / ISO9660 / Joliet names
* the TOML body matches PVE 9.x's ``proxmox-fetch-answer`` schema
  (``mode = "partition"``, underscored ``partition_label``)
* the volume label is configurable
* ``ProxmoxPrepareError`` wraps pycdlib failures
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange.vms.builders._proxmox_prepare import (
    ProxmoxPrepareError,
    prepare_iso_bytes,
)


def _stub_iso_for(monkeypatch: pytest.MonkeyPatch, **flags: bool) -> MagicMock:
    """Patch :class:`pycdlib.PyCdlib` to a MagicMock with configurable
    ``has_*`` flags.  Returns the mock so tests can inspect calls.
    """
    import testrange.vms.builders._proxmox_prepare as pp

    iso_obj = MagicMock()
    iso_obj.has_rock_ridge.return_value = flags.get("rock_ridge", True)
    iso_obj.has_joliet.return_value = flags.get("joliet", False)
    monkeypatch.setattr(pp, "PyCdlib", lambda: iso_obj)
    return iso_obj


class TestPrepareIsoBytesAddFp:
    """The ``add_fp`` call is the entire point of the prep — these
    tests pin its arguments so a future refactor can't silently
    change the path PVE looks up."""

    def test_add_fp_called_with_iso_root_path(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iso_obj = _stub_iso_for(monkeypatch, rock_ridge=True, joliet=False)

        src = tmp_path / "vanilla.iso"
        out = tmp_path / "prepared.iso"
        src.write_bytes(b"")  # empty — pycdlib is mocked
        prepare_iso_bytes(src, out)

        iso_obj.add_fp.assert_called_once()
        kwargs = iso_obj.add_fp.call_args.kwargs
        assert kwargs["iso_path"] == "/AUTOINST.TOM;1"
        # Rock Ridge basename is what PVE 9.x's
        # ``proxmox-fetch-answer`` reads at /cdrom/auto-installer-mode.toml.
        assert kwargs["rr_name"] == "auto-installer-mode.toml"
        # No Joliet on this ISO → no joliet_path kwarg.
        assert "joliet_path" not in kwargs

    def test_add_fp_includes_joliet_when_iso_has_joliet(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iso_obj = _stub_iso_for(monkeypatch, rock_ridge=True, joliet=True)
        src = tmp_path / "vanilla.iso"
        out = tmp_path / "prepared.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out)

        kwargs = iso_obj.add_fp.call_args.kwargs
        assert kwargs.get("joliet_path") == "/auto-installer-mode.toml"

    def test_add_fp_skips_rock_ridge_when_iso_lacks_it(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iso_obj = _stub_iso_for(monkeypatch, rock_ridge=False, joliet=False)
        src = tmp_path / "vanilla.iso"
        out = tmp_path / "prepared.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out)

        kwargs = iso_obj.add_fp.call_args.kwargs
        assert "rr_name" not in kwargs


class TestPrepareIsoBytesTomlBody:
    """The TOML body sent in must match the PVE 9.x parser's expectations
    — ``mode = "partition"`` + ``partition_label = "<label>"``
    (note underscore, distinct from answer.toml's kebab-case)."""

    def test_default_partition_label(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iso_obj = _stub_iso_for(monkeypatch)
        src = tmp_path / "v.iso"
        out = tmp_path / "p.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out)

        # add_fp is called with (BytesIO, length, iso_path=, rr_name=, ...)
        positional = iso_obj.add_fp.call_args.args
        body_buf = positional[0]
        body = body_buf.getvalue().decode("utf-8")
        assert 'mode = "partition"' in body
        assert 'partition_label = "PROXMOX-AIS"' in body
        # Underscored, not kebab-case — that's the parser quirk.
        assert "partition-label" not in body

    def test_custom_partition_label(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iso_obj = _stub_iso_for(monkeypatch)
        src = tmp_path / "v.iso"
        out = tmp_path / "p.iso"
        src.write_bytes(b"")
        prepare_iso_bytes(src, out, partition_label="MY-LABEL")

        body_buf = iso_obj.add_fp.call_args.args[0]
        body = body_buf.getvalue().decode("utf-8")
        assert 'partition_label = "MY-LABEL"' in body


class TestPrepareIsoBytesLifecycle:
    def test_close_called_even_on_add_fp_failure(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pycdlib.PyCdlib`` opens a file handle to the source ISO;
        if we don't close on the error path, repeated failures leak
        FDs and eventually fail the next prep with EMFILE."""
        iso_obj = _stub_iso_for(monkeypatch)
        iso_obj.add_fp.side_effect = RuntimeError("boom")

        src = tmp_path / "v.iso"
        out = tmp_path / "p.iso"
        src.write_bytes(b"")

        with pytest.raises(ProxmoxPrepareError, match="boom"):
            prepare_iso_bytes(src, out)

        iso_obj.close.assert_called_once()

    def test_open_failure_wraps_in_proxmox_prepare_error(
        self,
        tmp_path: pytest.MonkeyPatch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A vanilla ISO that pycdlib can't open (corrupt / truncated)
        should surface as ``ProxmoxPrepareError`` with the underlying
        message — not a raw pycdlib exception."""
        import testrange.vms.builders._proxmox_prepare as pp

        iso_obj = MagicMock()
        iso_obj.open.side_effect = RuntimeError("malformed iso")
        monkeypatch.setattr(pp, "PyCdlib", lambda: iso_obj)

        src = tmp_path / "v.iso"
        out = tmp_path / "p.iso"
        src.write_bytes(b"")

        with pytest.raises(ProxmoxPrepareError, match="malformed iso"):
            prepare_iso_bytes(src, out)


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
