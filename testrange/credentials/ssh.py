"""In-memory SSH keypair generation.

``SSHKey.generate(comment=...)`` produces a deterministic Ed25519 keypair
as text. The keypair never touches the orchestrator host's filesystem — it
lives in the returned object and is handed to PosixCred / SSHCommunicator
as bytes/text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class SSHKey:
    """Ephemeral SSH keypair, in text form.

    Three views of the same Ed25519 key:

    - ``pub``: multi-line PEM ``-----BEGIN PUBLIC KEY-----`` block
      (SubjectPublicKeyInfo). The interoperable public-key encoding for
      non-SSH crypto consumers.
    - ``priv``: multi-line ``-----BEGIN OPENSSH PRIVATE KEY-----`` block.
      Unencrypted (ephemeral keypair, in-memory, single run).
    - ``auth_line``: the single-line OpenSSH public-key wire format,
      ``ssh-ed25519 AAA... comment`` — the literal string for
      ``authorized_keys``.
    """

    pub: str
    priv: str
    auth_line: str

    @classmethod
    def generate(cls, comment: str = "testrange") -> SSHKey:
        """Generate a deterministic Ed25519 SSH keypair seeded from ``comment``.

        The 32-byte Ed25519 seed is ``sha256(comment)``. Calling twice with the
        same comment produces the same keypair — that's intentional, so the
        rendered cloud-init seed (which embeds the public key into
        ``authorized_keys``) hashes the same across runs and the post-install
        cache hits.

        INSECURE BY DESIGN. The private key is fully derivable from the comment.
        Only safe for ephemeral test environments where the guest VMs are
        isolated and short-lived. Do not use this for any real authentication.
        """
        seed = hashlib.sha256(comment.encode("utf-8")).digest()
        private = Ed25519PrivateKey.from_private_bytes(seed)
        public = private.public_key()
        pub_pem = public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        auth_bytes = public.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        auth_line = auth_bytes.decode("ascii") + f" {comment}"
        priv_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        return cls(pub=pub_pem, priv=priv_pem, auth_line=auth_line)
