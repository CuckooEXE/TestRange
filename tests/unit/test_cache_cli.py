"""Tests for the cache CLI subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import cli


@pytest.fixture
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate the cache root inside tmp_path."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "testrange" / "isos"


class TestCacheAdd:
    def test_add_local(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "blob.bin"
        src.write_bytes(b"payload\n")
        rc = cli.main(["cache", "add", str(src), "--name", "blob"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        # sha256 of "payload\n"
        import hashlib

        assert out == hashlib.sha256(b"payload\n").hexdigest()
        assert (cache_env / f"{out}.bin").exists()
        assert (cache_env / f"{out}.json").exists()


class TestCacheList:
    def test_empty(
        self,
        cache_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["cache", "list"])
        assert rc == 0
        assert "(empty)" in capsys.readouterr().out

    def test_nonempty(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "blob.bin"
        src.write_bytes(b"data")
        cli.main(["cache", "add", str(src), "--name", "thing"])
        capsys.readouterr()
        rc = cli.main(["cache", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "thing" in out
        assert "SHA" in out


class TestCacheDelRename:
    def test_del_by_name(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "b.bin"
        src.write_bytes(b"x")
        cli.main(["cache", "add", str(src), "--name", "thing"])
        capsys.readouterr()
        rc = cli.main(["cache", "del", "thing"])
        assert rc == 0
        # Sidecar gone too:
        assert not any(cache_env.glob("*.bin"))
        assert not any(cache_env.glob("*.json"))

    def test_del_missing(
        self,
        cache_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["cache", "del", "nope"])
        assert rc == 2
        assert "no entry" in capsys.readouterr().err

    def test_rename(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "b.bin"
        src.write_bytes(b"x")
        cli.main(["cache", "add", str(src), "--name", "first"])
        capsys.readouterr()
        rc = cli.main(["cache", "rename", "first", "second"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "first" in out and "second" in out

    def test_forget(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "b.bin"
        src.write_bytes(b"x")
        cli.main(["cache", "add", str(src), "--name", "x"])
        cli.main(["cache", "rename", "x", "y"])
        capsys.readouterr()
        rc = cli.main(["cache", "forget-name", "x"])
        assert rc == 0


class TestDescribeWithCache:
    def test_describe_after_cache_add(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        examples = Path(__file__).resolve().parents[2] / "examples"
        src = tmp_path / "fake_debian.qcow2"
        src.write_bytes(b"FAKE-QCOW2-CONTENT" * 100)
        cli.main(["cache", "add", str(src), "--name", "debian-13"])
        capsys.readouterr()
        rc = cli.main(["describe", str(examples / "hello_world.py")])
        assert rc == 0
        out = capsys.readouterr().out
        # When the entry is in cache, the warning vanishes:
        assert "⚠ not in cache" not in out
        # Short sha is shown:
        assert " -> " in out
