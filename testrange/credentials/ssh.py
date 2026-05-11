"""In-memory SSH keypair generation.

``gen_ssh_key()`` produces a fresh Ed25519 keypair as text. The keypair
never touches the orchestrator host's filesystem — it lives in the
returned object and is handed to PosixCred / SSHCommunicator as bytes/text.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class SSHKeyPair:
    """Ephemeral SSH keypair, in OpenSSH text form."""

    public: str
    private: str


def gen_ssh_key(comment: str = "testrange") -> SSHKeyPair:
    """Generate a fresh Ed25519 SSH keypair.

    Returns OpenSSH-format public and private keys as text strings. The
    private key is unencrypted (no passphrase) — fine for an ephemeral,
    in-memory keypair scoped to a single run.
    """
    private = Ed25519PrivateKey.generate()
    pub_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    priv_bytes = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_text = pub_bytes.decode("ascii") + f" {comment}"
    private_text = priv_bytes.decode("ascii")
    return SSHKeyPair(public=public_text, private=private_text)
