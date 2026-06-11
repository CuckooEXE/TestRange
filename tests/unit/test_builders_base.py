"""Tests for builder base helpers — the race-safe prepared-ISO materializer.

``materialize_prepared`` is what makes an installer-origin builder's
``prepare_boot_media`` cache safe under the parallel build phase: two same-config
build misses derive the same ``prepared`` path, so the write must be atomic and
last-writer-wins, never torn (BUILD-25/26).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from testrange.builders.base import materialize_prepared


def test_writes_payload_atomically(tmp_path: Path) -> None:
    prepared = tmp_path / "out.iso"

    def build(tmp: Path) -> None:
        # `build` receives a fresh, non-existing path in the same directory.
        assert not tmp.exists()
        assert tmp.parent.parent == prepared.parent
        tmp.write_bytes(b"ISO-BYTES")

    materialize_prepared(prepared, build)
    assert prepared.read_bytes() == b"ISO-BYTES"
    assert list(tmp_path.glob(".tr-prep-*")) == []  # temp dir cleaned up


def _writer(payload: bytes) -> Callable[[Path], None]:
    def build(tmp: Path) -> None:
        tmp.write_bytes(payload)

    return build


def test_last_writer_wins_no_torn_file(tmp_path: Path) -> None:
    # Two same-config misses race onto the same prepared path; each builds into
    # its own temp and renames, so the final file is one complete ISO.
    prepared = tmp_path / "out.iso"
    materialize_prepared(prepared, _writer(b"A" * 100))
    materialize_prepared(prepared, _writer(b"B" * 100))
    data = prepared.read_bytes()
    assert data in (b"A" * 100, b"B" * 100)
    assert len(data) == 100  # complete, not concatenated/torn


def test_build_failure_leaves_no_residue(tmp_path: Path) -> None:
    prepared = tmp_path / "out.iso"

    def boom(tmp: Path) -> None:
        tmp.write_bytes(b"partial")
        raise RuntimeError("xorriso failed")

    with pytest.raises(RuntimeError):
        materialize_prepared(prepared, boom)
    assert not prepared.exists()  # the partial was never promoted
    assert list(tmp_path.glob(".tr-prep-*")) == []  # temp dir cleaned up
