"""Unit tests for :mod:`testrange._run`."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from testrange._run import RunDir
from testrange.exceptions import CacheError


class TestConstruction:
    def test_creates_directory(self, tmp_path: Path) -> None:
        run = RunDir(root=tmp_path)
        assert run.path.is_dir()

    def test_run_id_is_uuid(self, tmp_path: Path) -> None:
        import uuid
        run = RunDir(root=tmp_path)
        # Must parse as UUID
        uuid.UUID(run.run_id)

    def test_each_run_has_unique_dir(self, tmp_path: Path) -> None:
        r1 = RunDir(root=tmp_path)
        r2 = RunDir(root=tmp_path)
        assert r1.run_id != r2.run_id
        assert r1.path != r2.path

    def test_mode_is_world_readable(self, tmp_path: Path) -> None:
        """Regression: mkdtemp defaults to 0o700; we widen to 0o755 so
        the qemu:///system daemon can read disk images placed inside."""
        run = RunDir(root=tmp_path)
        mode = stat.S_IMODE(run.path.stat().st_mode)
        assert mode & 0o005 == 0o005, f"mode {oct(mode)} lacks o+rx"

    def test_default_root_uses_system_tempdir(self) -> None:
        import tempfile
        run = RunDir()
        try:
            assert str(run.path).startswith(tempfile.gettempdir())
        finally:
            run.cleanup()

    def test_construction_failure_wraps_in_cache_error(
        self, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "nope"
        # A path that doesn't exist as a directory
        unwritable.write_text("this is a file, not a directory")
        with pytest.raises(CacheError):
            RunDir(root=unwritable)


class TestCreateOverlay:
    def test_invokes_qemu_img_create(
        self, tmp_path: Path, fake_qemu_img: list[list[str]]
    ) -> None:
        run = RunDir(root=tmp_path)
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"x")
        overlay = run.create_overlay("web01", base)
        assert overlay.name == "web01.qcow2"
        assert overlay.parent == run.path
        assert any(
            cmd[0] == "qemu-img" and cmd[1] == "create" and cmd[-1] == str(overlay)
            for cmd in fake_qemu_img
        )

    def test_qemu_img_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from testrange import _qemu_img

        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(
                1, ["qemu-img"], stderr="disk full"
            )
        monkeypatch.setattr(_qemu_img.subprocess, "run", _fail)

        run = RunDir(root=tmp_path)
        with pytest.raises(CacheError):
            run.create_overlay("web01", tmp_path / "missing.qcow2")


class TestCreateInstallDisk:
    def test_creates_and_resizes(
        self, tmp_path: Path, fake_qemu_img: list[list[str]]
    ) -> None:
        run = RunDir(root=tmp_path)
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"x")
        disk = run.create_install_disk("web01", base, "64G")
        assert disk.name == "web01-install.qcow2"
        assert any(cmd[1] == "create" for cmd in fake_qemu_img)
        assert any(cmd[1] == "resize" and "64G" in cmd for cmd in fake_qemu_img)


class TestSeedIsoPath:
    def test_install_variant(self, tmp_path: Path) -> None:
        run = RunDir(root=tmp_path)
        assert run.seed_iso_path("web01", install=True) == run.path / "web01-install-seed.iso"

    def test_run_variant(self, tmp_path: Path) -> None:
        run = RunDir(root=tmp_path)
        assert run.seed_iso_path("web01", install=False) == run.path / "web01-seed.iso"


class TestCleanup:
    def test_removes_directory(self, tmp_path: Path) -> None:
        run = RunDir(root=tmp_path)
        (run.path / "file.txt").write_bytes(b"x")
        run.cleanup()
        assert not run.path.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        run = RunDir(root=tmp_path)
        run.cleanup()
        run.cleanup()  # second call must not raise
