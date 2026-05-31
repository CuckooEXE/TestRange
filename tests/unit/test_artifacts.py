"""Per-role build-artifact cache naming (ADR-0010 §4)."""

from __future__ import annotations

import pytest

from testrange.orchestrator.artifacts import (
    built_artifact_name,
    built_artifact_roles,
    data_disk_role,
)


class TestBuiltArtifactName:
    @pytest.mark.parametrize(
        ("config_hash", "role", "expected"),
        [
            ("abc123def4567890", "os", "_built_abc123def4567890__os"),
            ("abc123def4567890", "data0", "_built_abc123def4567890__data0"),
            ("abc123def4567890", "data1", "_built_abc123def4567890__data1"),
            ("0000000000000000", "os", "_built_0000000000000000__os"),
        ],
    )
    def test_name(self, config_hash: str, role: str, expected: str) -> None:
        assert built_artifact_name(config_hash, role) == expected

    def test_distinct_hashes_distinct_names(self) -> None:
        assert built_artifact_name("aaaa", "os") != built_artifact_name("bbbb", "os")

    def test_distinct_roles_distinct_names(self) -> None:
        assert built_artifact_name("aaaa", "os") != built_artifact_name("aaaa", "data0")


class TestDataDiskRole:
    @pytest.mark.parametrize(("idx", "expected"), [(0, "data0"), (1, "data1"), (12, "data12")])
    def test_role(self, idx: int, expected: str) -> None:
        assert data_disk_role(idx) == expected


class TestBuiltArtifactRoles:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, ("os",)),
            (1, ("os", "data0")),
            (2, ("os", "data0", "data1")),
            (3, ("os", "data0", "data1", "data2")),
        ],
    )
    def test_roles(self, count: int, expected: tuple[str, ...]) -> None:
        assert built_artifact_roles(count) == expected

    def test_os_disk_always_first(self) -> None:
        assert built_artifact_roles(5)[0] == "os"
