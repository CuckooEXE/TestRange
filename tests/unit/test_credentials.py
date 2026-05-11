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
        # pub is PEM SubjectPublicKeyInfo, priv is OpenSSH PEM, auth_line is wire format.
        assert "-----BEGIN PUBLIC KEY-----" in kp.pub
        assert "-----END PUBLIC KEY-----" in kp.pub
        assert "PRIVATE KEY" in kp.priv
        assert "ssh-ed25519" in kp.auth_line

    def test_comment_in_auth_line(self) -> None:
        kp = gen_ssh_key(comment="my-test-key")
        assert "my-test-key" in kp.auth_line
        # comment is OpenSSH-line metadata; it does not appear in the PEM block.
        assert "my-test-key" not in kp.pub

    def test_deterministic_for_same_comment(self) -> None:
        # The key material is determined by the comment, so the public-key
        # views (used in the cloud-init seed) are byte-equal across calls.
        # The OpenSSH private-key PEM includes a random "checkint" field,
        # so its raw text may differ even when the underlying key is the same.
        a, b = gen_ssh_key(comment="same"), gen_ssh_key(comment="same")
        assert a.pub == b.pub
        assert a.auth_line == b.auth_line

    def test_different_comment_yields_different_key(self) -> None:
        a, b = gen_ssh_key(comment="one"), gen_ssh_key(comment="two")
        assert a.priv != b.priv
        assert a.pub != b.pub
        assert a.auth_line != b.auth_line
