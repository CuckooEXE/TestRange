"""Unit tests for :mod:`testrange._qemu_img`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from testrange import _qemu_img
from testrange.exceptions import CacheError


@pytest.fixture
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every argv passed to ``subprocess.run`` inside ``_qemu_img``."""
    calls: list[list[str]] = []

    def _fake_run(argv, *_a, **_k):
        calls.append(list(argv))
        import types as _types
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_qemu_img.subprocess, "run", _fake_run)
    return calls


class TestCreateOverlay:
    def test_argv_contains_format_and_backing(
        self, recorded_calls: list[list[str]]
    ) -> None:
        _qemu_img.create_overlay(Path("/base.qcow2"), Path("/overlay.qcow2"))
        assert recorded_calls == [
            [
                "qemu-img", "create",
                "-f", "qcow2",
                "-b", "/base.qcow2",
                "-F", "qcow2",
                "/overlay.qcow2",
            ]
        ]

    def test_subprocess_failure_raises_cache_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(
                1, ["qemu-img", "create"], stderr="no space"
            )
        monkeypatch.setattr(_qemu_img.subprocess, "run", _fail)
        with pytest.raises(CacheError) as excinfo:
            _qemu_img.create_overlay(Path("/a"), Path("/b"))
        assert "qemu-img create failed" in str(excinfo.value)
        assert "no space" in str(excinfo.value)


class TestResize:
    def test_argv(self, recorded_calls: list[list[str]]) -> None:
        _qemu_img.resize(Path("/disk.qcow2"), "64G")
        assert recorded_calls == [
            ["qemu-img", "resize", "/disk.qcow2", "64G"]
        ]

    def test_subprocess_failure_raises_cache_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(
                1, ["qemu-img", "resize"], stderr="file busy"
            )
        monkeypatch.setattr(_qemu_img.subprocess, "run", _fail)
        with pytest.raises(CacheError) as excinfo:
            _qemu_img.resize(Path("/disk"), "64G")
        assert "qemu-img resize failed" in str(excinfo.value)


class TestConvertCompressed:
    def test_argv_includes_compress_flag(
        self, recorded_calls: list[list[str]]
    ) -> None:
        _qemu_img.convert_compressed(Path("/src.qcow2"), Path("/dst.qcow2"))
        assert recorded_calls == [
            [
                "qemu-img", "convert",
                "-f", "qcow2",
                "-O", "qcow2",
                "-c",
                "/src.qcow2",
                "/dst.qcow2",
            ]
        ]

    def test_subprocess_failure_raises_cache_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(
                1, ["qemu-img", "convert"], stderr="bad magic"
            )
        monkeypatch.setattr(_qemu_img.subprocess, "run", _fail)
        with pytest.raises(CacheError) as excinfo:
            _qemu_img.convert_compressed(Path("/a"), Path("/b"))
        assert "qemu-img convert failed" in str(excinfo.value)
        assert "bad magic" in str(excinfo.value)


class TestPrivateRunHelper:
    def test_success_is_silent(
        self, recorded_calls: list[list[str]]
    ) -> None:
        _qemu_img._run(["qemu-img", "info", "/x"])
        assert recorded_calls == [["qemu-img", "info", "/x"]]

    def test_error_message_includes_subcommand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a, **_k):
            raise subprocess.CalledProcessError(
                1, ["qemu-img", "snapshot"], stderr="nope"
            )
        monkeypatch.setattr(_qemu_img.subprocess, "run", _fail)
        with pytest.raises(CacheError) as excinfo:
            _qemu_img._run(["qemu-img", "snapshot", "-a", "s1", "/x"])
        # The error message should name the subcommand so the cause is obvious
        assert "qemu-img snapshot" in str(excinfo.value)
