"""Tests for SSHCommunicator Plan-time skeleton."""

from __future__ import annotations

import pytest

from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.exceptions import CommunicatorAlreadyBoundError


class TestSSHCommunicator:
    def test_construction(self) -> None:
        c = SSHCommunicator("myuser")
        assert c.username == "myuser"
        assert c.is_bound is False

    def test_empty_username(self) -> None:
        with pytest.raises(ValueError):
            SSHCommunicator("")

    def test_bind(self) -> None:
        c = SSHCommunicator("u")
        cred = PosixCred("u", password="p")
        c.bind(host="10.0.0.1", credential=cred)
        assert c.is_bound is True

    def test_bind_twice(self) -> None:
        c = SSHCommunicator("u")
        cred = PosixCred("u", password="p")
        c.bind(host="10.0.0.1", credential=cred)
        with pytest.raises(CommunicatorAlreadyBoundError):
            c.bind(host="10.0.0.2", credential=cred)

    def test_bind_username_mismatch(self) -> None:
        c = SSHCommunicator("alice")
        cred = PosixCred("bob", password="p")
        with pytest.raises(ValueError, match="does not match"):
            c.bind(host="10.0.0.1", credential=cred)

    def test_execute_not_implemented(self) -> None:
        c = SSHCommunicator("u")
        with pytest.raises(NotImplementedError):
            c.execute(["uname"])
