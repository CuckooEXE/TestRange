"""Unit tests for :mod:`testrange._run`."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from testrange._run import RunDir
from testrange.exceptions import CacheError
from testrange.storage import LocalFileTransport, LocalStorageBackend


def _make_run(cache_root: Path) -> RunDir:
    """Return a RunDir rooted at *cache_root* via LocalStorageBackend."""
    return RunDir(LocalStorageBackend(cache_root))


class TestConstruction:
    def test_creates_directory(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        assert Path(run.path).is_dir()

    def test_run_id_is_uuid(self, tmp_path: Path) -> None:
        import uuid
        run = _make_run(tmp_path)
        # Must parse as UUID
        uuid.UUID(run.run_id)

    def test_each_run_has_unique_dir(self, tmp_path: Path) -> None:
        r1 = _make_run(tmp_path)
        r2 = _make_run(tmp_path)
        assert r1.run_id != r2.run_id
        assert r1.path != r2.path

    def test_mode_is_world_readable(self, tmp_path: Path) -> None:
        """Regression: mkdtemp defaults to 0o700; we widen to 0o755 so
        the qemu:///system daemon can read disk images placed inside."""
        run = _make_run(tmp_path)
        mode = stat.S_IMODE(Path(run.path).stat().st_mode)
        assert mode & 0o005 == 0o005, f"mode {oct(mode)} lacks o+rx"

    def test_path_lives_under_backend_cache_root(
        self, tmp_path: Path,
    ) -> None:
        """Every per-run dir sits under the backend's cache root, keyed
        by run ID — so a crashed process leaves state the cache-aware
        teardown can still clean up."""
        run = _make_run(tmp_path)
        assert str(tmp_path) in run.path
        assert run.run_id in run.path

    def test_construction_failure_wraps_in_cache_error(
        self, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "nope"
        # A path that doesn't exist as a directory
        unwritable.write_text("this is a file, not a directory")
        with pytest.raises(CacheError):
            _make_run(unwritable)


class TestCreateOverlay:
    def test_invokes_qemu_img_create(
        self, tmp_path: Path, fake_qemu_img: list[list[str]]
    ) -> None:
        run = _make_run(tmp_path)
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"x")
        overlay = run.create_overlay("web01", str(base))
        overlay_path = Path(overlay)
        assert overlay_path.name == "web01.qcow2"
        assert str(overlay_path.parent) == run.path
        assert any(
            cmd[0] == "qemu-img" and cmd[1] == "create" and cmd[-1] == str(overlay)
            for cmd in fake_qemu_img
        )

    def test_qemu_img_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a qemu-img failure by having the transport return a
        # non-zero exit code.  Qcow2DiskFormat wraps that as CacheError.
        monkeypatch.setattr(
            LocalFileTransport,
            "run_tool",
            lambda self, argv, timeout=60.0: (1, b"", b"disk full"),
        )

        run = _make_run(tmp_path)
        with pytest.raises(CacheError, match="disk full"):
            run.create_overlay("web01", str(tmp_path / "missing.qcow2"))


class TestCreateInstallDisk:
    def test_creates_and_resizes(
        self, tmp_path: Path, fake_qemu_img: list[list[str]]
    ) -> None:
        run = _make_run(tmp_path)
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"x")
        disk = run.create_install_disk("web01", str(base), "64G")
        assert Path(disk).name == "web01-install.qcow2"
        assert any(cmd[1] == "create" for cmd in fake_qemu_img)
        assert any(cmd[1] == "resize" and "64G" in cmd for cmd in fake_qemu_img)


class TestSeedIsoPath:
    """The cloud-init seed-ISO and Windows autounattend-ISO path
    helpers used to live on RunDir, but the filename conventions
    belong to their respective builders.  The generic
    :meth:`RunDir.path_for` helper that backs them is exercised here
    instead — that's the contract these helpers actually need."""

    def test_install_variant(self, tmp_path: Path) -> None:
        from testrange.vms.builders.cloud_init import _seed_iso_ref
        run = _make_run(tmp_path)
        assert (
            _seed_iso_ref(run, "web01", install=True)
            == f"{run.path}/web01-install-seed.iso"
        )

    def test_run_variant(self, tmp_path: Path) -> None:
        from testrange.vms.builders.cloud_init import _seed_iso_ref
        run = _make_run(tmp_path)
        assert (
            _seed_iso_ref(run, "web01", install=False)
            == f"{run.path}/web01-seed.iso"
        )

    def test_unattend_iso_ref(self, tmp_path: Path) -> None:
        from testrange.vms.builders.unattend import _unattend_iso_ref
        run = _make_run(tmp_path)
        assert (
            _unattend_iso_ref(run, "winbox")
            == f"{run.path}/winbox-unattend.iso"
        )


class TestCleanup:
    def test_removes_directory(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        (Path(run.path) / "file.txt").write_bytes(b"x")
        run.cleanup()
        assert not Path(run.path).exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        run.cleanup()
        run.cleanup()  # second call must not raise
