"""Tests for credentials + gen_ssh_key."""

from __future__ import annotations

import pytest

from testrange.credentials import PosixCred, gen_ssh_key


class TestPosixCred:
    def test_password_only(self) -> None:
        c = PosixCred("root", password="x")
        assert c.username == "root"
        assert c.password == "x"
        assert c.pubkey is None

    def test_pubkey_only(self) -> None:
        c = PosixCred("u", pubkey="ssh-ed25519 AAA...")
        assert c.pubkey is not None

    def test_both(self) -> None:
        c = PosixCred("u", password="p", pubkey="ssh-ed25519 AAA...", sudo=True)
        assert c.password is not None and c.pubkey is not None
        assert c.sudo is True

    def test_neither_auth(self) -> None:
        with pytest.raises(ValueError):
            PosixCred("u")

    def test_privkey_without_pubkey(self) -> None:
        with pytest.raises(ValueError):
            PosixCred("u", privkey="-----BEGIN-----", password="x")

    def test_empty_username(self) -> None:
        with pytest.raises(ValueError):
            PosixCred("", password="x")


class TestGenSSHKey:
    def test_returns_pair(self) -> None:
        kp = gen_ssh_key(comment="t")
        assert "ssh-ed25519" in kp.public
        assert "PRIVATE KEY" in kp.private

    def test_comment_in_pubkey(self) -> None:
        kp = gen_ssh_key(comment="my-test-key")
        assert "my-test-key" in kp.public

    def test_pair_is_unique(self) -> None:
        a, b = gen_ssh_key(), gen_ssh_key()
        assert a.private != b.private
        assert a.public != b.public
