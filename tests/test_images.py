"""Unit tests for :mod:`testrange.vms.images`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from testrange.exceptions import ImageNotFoundError
from testrange.vms.images import is_windows_image, resolve_image


class TestIsWindowsImage:
    @pytest.mark.parametrize(
        "name",
        [
            "windows-server-2022.iso",
            "Win10_22H2_English_x64.iso",
            "win11.iso",
            "W10_multi.iso",
            "server-2019.iso",
            "WINDOWS_LTSC.iso",
        ],
    )
    def test_windows_isos_detected(self, name: str) -> None:
        assert is_windows_image(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "debian-12",
            "ubuntu-24.04",
            "fedora-40",
            "/srv/images/rocky.qcow2",
            "alpine.img",
            "https://example.com/image.qcow2",
            "",
            # Regression: Linux ISOs that look Windows-ish from
            # naïve substring matches.  Bare ``"server"`` would
            # match these via the old predicate; the regex form
            # requires ``server`` followed by a 4-digit year.
            "ubuntu-22.04-live-server-amd64.iso",
            "debian-12-server.iso",
            "ubuntu-server-22.04.iso",
            # Bare ``"win"`` would match these too.
            "winetricks.iso",
            "darwin-2024.iso",
        ],
    )
    def test_non_windows_not_detected(self, name: str) -> None:
        assert is_windows_image(name) is False


class TestResolveImage:
    def test_absolute_existing_path(self, tmp_path: Path) -> None:
        iso = tmp_path / "disk.qcow2"
        iso.write_bytes(b"fake")
        cache = MagicMock()
        result = resolve_image(str(iso), cache)
        assert result == iso
        cache.get_image.assert_not_called()

    def test_tilde_expanded_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        iso = tmp_path / "disk.qcow2"
        iso.write_bytes(b"fake")
        cache = MagicMock()
        result = resolve_image("~/disk.qcow2", cache)
        assert result.resolve() == iso.resolve()

    def test_https_url_delegates_to_cache(self) -> None:
        cache = MagicMock()
        cache.get_image.return_value = Path("/cache/abc.qcow2")
        result = resolve_image("https://example.com/x.qcow2", cache)
        cache.get_image.assert_called_once_with("https://example.com/x.qcow2")
        assert result == Path("/cache/abc.qcow2")

    def test_http_url_rejected(self) -> None:
        cache = MagicMock()
        with pytest.raises(ImageNotFoundError):
            resolve_image("http://example.com/x.qcow2", cache)
        cache.get_image.assert_not_called()

    def test_unresolvable_raises(self) -> None:
        cache = MagicMock()
        with pytest.raises(ImageNotFoundError):
            resolve_image("nosuch-distro-9", cache)

    def test_alias_no_longer_resolved(self) -> None:
        cache = MagicMock()
        with pytest.raises(ImageNotFoundError):
            resolve_image("debian-12", cache)
        cache.get_image.assert_not_called()

    def test_unresolvable_error_message_is_helpful(self) -> None:
        cache = MagicMock()
        with pytest.raises(ImageNotFoundError) as excinfo:
            resolve_image("bogus-os", cache)
        assert "bogus-os" in str(excinfo.value)
        assert "https" in str(excinfo.value) or "URL" in str(excinfo.value)

    def test_nonexistent_absolute_path_raises(self) -> None:
        cache = MagicMock()
        with pytest.raises(ImageNotFoundError):
            resolve_image("/nonexistent/path/to/disk.qcow2", cache)
