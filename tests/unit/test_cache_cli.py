"""Tests for the cache CLI subcommands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from testrange import cli


@pytest.fixture
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate the cache root inside tmp_path."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path / "testrange" / "isos"


class TestFormatSize:
    def test_bytes_are_integers(self) -> None:
        assert cli._format_size(0) == "0 B"
        assert cli._format_size(512) == "512 B"

    def test_fractional_units_are_preserved(self) -> None:
        # Regression (CORE-45): integer floor-division collapsed every size to
        # "X.0". 1.5 KiB must render as 1.5 KiB, not 1.0 KiB.
        assert cli._format_size(1536) == "1.5 KiB"
        assert cli._format_size(1024) == "1.0 KiB"

    def test_scales_through_units(self) -> None:
        assert cli._format_size(5 * 1024**2) == "5.0 MiB"
        assert cli._format_size(int(1.6 * 1024**3)) == "1.6 GiB"
        assert cli._format_size(3 * 1024**4) == "3.0 TiB"


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


class TestCachePurge:
    def _seed(self, tmp_path: Path) -> None:
        for n, payload in (("one", b"1"), ("two", b"2")):
            src = tmp_path / f"{n}.bin"
            src.write_bytes(payload)
            cli.main(["cache", "add", str(src), "--name", n])

    def test_purge_without_yes_is_noop(
        self, cache_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path)
        capsys.readouterr()
        rc = cli.main(["cache", "purge"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--yes" in out  # tells the user how to actually do it
        assert len(list(cache_env.glob("*.bin"))) == 2  # nothing deleted

    def test_purge_dry_run_lists_without_deleting(
        self, cache_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path)
        capsys.readouterr()
        rc = cli.main(["cache", "purge", "--dry-run"])
        assert rc == 0
        assert len(list(cache_env.glob("*.bin"))) == 2  # nothing deleted

    def test_purge_yes_deletes_everything(
        self, cache_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._seed(tmp_path)
        capsys.readouterr()
        rc = cli.main(["cache", "purge", "--yes"])
        assert rc == 0
        assert "2" in capsys.readouterr().out
        assert not any(cache_env.glob("*.bin"))
        assert not any(cache_env.glob("*.json"))

    def test_purge_empty_cache(self, cache_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["cache", "purge", "--yes"])
        assert rc == 0
        assert "empty" in capsys.readouterr().out.lower()


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


class TestPushPullCli:
    def test_push_without_cache_flag_errors(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "b.bin"
        src.write_bytes(b"x")
        cli.main(["cache", "add", str(src), "--name", "thing"])
        capsys.readouterr()
        rc = cli.main(["cache", "push", "thing"])
        assert rc == 1
        assert "no HTTP cache configured" in capsys.readouterr().err

    def test_pull_without_cache_flag_errors(
        self,
        cache_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["cache", "pull", "thing"])
        assert rc == 1
        assert "no HTTP cache configured" in capsys.readouterr().err

    def test_push_uses_cache_flag_to_construct_http(
        self,
        cache_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "b.bin"
        src.write_bytes(b"x")
        cli.main(["cache", "add", str(src), "--name", "thing"])
        capsys.readouterr()
        # Mock requests so no real socket is opened. PUTs return 201; the
        # CLI's success path is exercised.
        fake_requests = MagicMock()
        ok = MagicMock(status_code=201, ok=True, text="")
        fake_requests.put.return_value = ok
        with patch("testrange.cache.http._import_requests", return_value=fake_requests):
            rc = cli.main(["--cache", "https://cache.local:8443", "cache", "push", "thing"])
        assert rc == 0
        # bin + sidecar + 1 name = 3 PUTs.
        assert fake_requests.put.call_count == 3
        urls = [call.args[0] for call in fake_requests.put.call_args_list]
        assert all(u.startswith("https://cache.local:8443/") for u in urls)


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
