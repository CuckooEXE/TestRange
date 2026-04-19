"""Unit tests for :mod:`testrange.credentials`."""

from __future__ import annotations

import pytest

from testrange.credentials import Credential


class TestCredentialConstruction:
    def test_defaults(self) -> None:
        c = Credential(username="alice", password="pw")
        assert c.ssh_key is None
        assert c.sudo is False


class TestIsRoot:
    def test_root_user_detected(self) -> None:
        assert Credential(username="root", password="pw").is_root() is True

    @pytest.mark.parametrize(
        "name",
        ["alice", "Root", "ROOT", "rooot", "root2", " root", ""],
    )
    def test_non_root_usernames_rejected(self, name: str) -> None:
        """Regression: only exact lowercase ``'root'`` counts as root."""
        assert Credential(username=name, password="pw").is_root() is False
