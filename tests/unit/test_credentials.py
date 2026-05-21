"""Tests for credentials + SSHKey.generate."""

from __future__ import annotations

import pytest

from testrange.credentials import PosixCred
from testrange.utils import SSHKey


class TestPosixCred:
    def test_password_only(self) -> None:
        c = PosixCred("root", password="x")
        assert c.username == "root"
        assert c.password == "x"
        assert c.ssh_key is None

    def test_ssh_key_only(self) -> None:
        c = PosixCred("u", ssh_key=SSHKey.generate())
        assert c.ssh_key is not None

    def test_both(self) -> None:
        c = PosixCred("u", password="p", ssh_key=SSHKey.generate(), sudo=True)
        assert c.password is not None and c.ssh_key is not None
        assert c.sudo is True

    def test_neither_auth(self) -> None:
        with pytest.raises(ValueError):
            PosixCred("u")

    def test_empty_username(self) -> None:
        with pytest.raises(ValueError):
            PosixCred("", password="x")


class TestSSHKeyGenerate:
    def test_returns_three_views(self) -> None:
        kp = SSHKey.generate(comment="t")
        # pub is PEM SubjectPublicKeyInfo, priv is OpenSSH PEM, auth_line is wire format.
        assert "-----BEGIN PUBLIC KEY-----" in kp.pub
        assert "-----END PUBLIC KEY-----" in kp.pub
        assert "PRIVATE KEY" in kp.priv
        assert "ssh-ed25519" in kp.auth_line

    def test_comment_in_auth_line(self) -> None:
        kp = SSHKey.generate(comment="my-test-key")
        assert "my-test-key" in kp.auth_line
        # Comment is OpenSSH-line metadata; it does not appear in the PEM block.
        assert "my-test-key" not in kp.pub

    def test_deterministic_for_same_comment(self) -> None:
        # The key material is determined by the comment, so the public-key
        # views (used in the cloud-init seed) are byte-equal across calls.
        # The OpenSSH private-key PEM includes a random "checkint" field,
        # so its raw text may differ even when the underlying key is the same.
        a, b = SSHKey.generate(comment="same"), SSHKey.generate(comment="same")
        assert a.pub == b.pub
        assert a.auth_line == b.auth_line

    def test_different_comment_yields_different_key(self) -> None:
        a, b = SSHKey.generate(comment="one"), SSHKey.generate(comment="two")
        assert a.priv != b.priv
        assert a.pub != b.pub
        assert a.auth_line != b.auth_line
