"""Tests for SSHKey / EcdsaKey (CORE-63).

SSHKey is the deterministic Ed25519 default (cache stability); EcdsaKey is the
deterministic NIST P-256 variant for FIPS-mode peers (ESXi 8 sshd rejects
Ed25519).
"""

from __future__ import annotations

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_ssh_private_key,
)

from testrange.utils import EcdsaKey, SSHKey


class TestSSHKeyBase:
    def test_default_is_ed25519(self) -> None:
        k = SSHKey.generate(comment="x")
        assert k.algorithm == "ed25519"
        assert k.auth_line.startswith("ssh-ed25519 ")
        assert k.auth_line.endswith(" x")

    def test_deterministic(self) -> None:
        # The OpenSSH *private*-key PEM carries random check-bytes, so only the
        # public views (pub/auth_line — what lands in authorized_keys and keys the
        # cache) are stable across calls.
        a, b = SSHKey.generate(comment="same"), SSHKey.generate(comment="same")
        assert a.auth_line == b.auth_line and a.pub == b.pub
        assert SSHKey.generate(comment="a").auth_line != SSHKey.generate(comment="b").auth_line


class TestEcdsaKey:
    def test_algorithm_and_wire_type(self) -> None:
        k = EcdsaKey.generate(comment="esxi")
        assert k.algorithm == "ecdsa"
        assert isinstance(k, SSHKey)
        assert k.auth_line.startswith("ecdsa-sha2-nistp256 ")
        assert k.auth_line.endswith(" esxi")

    def test_deterministic(self) -> None:
        a, b = EcdsaKey.generate(comment="same"), EcdsaKey.generate(comment="same")
        assert a.auth_line == b.auth_line and a.pub == b.pub
        assert EcdsaKey.generate(comment="a").auth_line != EcdsaKey.generate(comment="b").auth_line

    def test_distinct_from_ed25519(self) -> None:
        # Same comment, different algorithm -> different key material.
        assert EcdsaKey.generate(comment="c").auth_line != SSHKey.generate(comment="c").auth_line

    def test_private_key_is_loadable_and_matches_public(self) -> None:
        # The emitted OpenSSH private key parses, and its derived public half
        # equals the auth_line we hand to authorized_keys.
        k = EcdsaKey.generate(comment="load")
        priv = load_ssh_private_key(k.priv.encode(), password=None)
        derived = priv.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
        # auth_line is the OpenSSH public key + " <comment>"; the key half must match.
        assert k.auth_line.startswith(derived)
        assert k.auth_line == f"{derived} load"
