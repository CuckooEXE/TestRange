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

    def test_nic_idx_defaults_none(self) -> None:
        assert SSHCommunicator("u").nic_idx is None

    def test_nic_idx_stored(self) -> None:
        assert SSHCommunicator("u", nic_idx=2).nic_idx == 2

    def test_nic_idx_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="nic_idx"):
            SSHCommunicator("u", nic_idx=-1)

    def test_nic_idx_non_int_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            SSHCommunicator("u", nic_idx="0")  # type: ignore[arg-type]

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

    def test_execute_unbound_raises(self) -> None:
        from testrange.exceptions import CommunicatorError

        c = SSHCommunicator("u")
        with pytest.raises(CommunicatorError, match="not bound"):
            c.execute(["uname"])

    def test_execute_empty_argv(self) -> None:
        c = SSHCommunicator("u")
        cred = PosixCred("u", password="p")
        c.bind(host="10.0.0.1", credential=cred)
        with pytest.raises(ValueError):
            c.execute([])

    def test_execute_non_str_argv(self) -> None:
        c = SSHCommunicator("u")
        cred = PosixCred("u", password="p")
        c.bind(host="10.0.0.1", credential=cred)
        with pytest.raises(TypeError):
            c.execute(["uname", 5])  # type: ignore[list-item]

    def test_bind_port_validation(self) -> None:
        c = SSHCommunicator("u")
        cred = PosixCred("u", password="p")
        with pytest.raises(ValueError):
            c.bind(host="10.0.0.1", credential=cred, port=0)
