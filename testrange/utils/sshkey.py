"""In-memory SSH keypair generation.

``SSHKey.generate(comment=...)`` produces a deterministic **Ed25519** keypair as
text; the :class:`EcdsaKey` subclass produces a deterministic **NIST P-256**
(``ecdsa-sha2-nistp256``) keypair for FIPS-mode peers that reject Ed25519 —
notably ESXi 8's sshd, which silently denies Ed25519 pubkeys (CORE-63). The
keypair never touches the orchestrator host's filesystem — it lives in the
returned object and is passed around as bytes/text (e.g. into a ``PosixCred`` or
an ``SSHCommunicator``). This is a standalone value type with no dependency on
the credential ABC, hence its home under ``testrange.utils`` rather than
``testrange.credentials``.

The algorithm is the *type*: ``SSHKey`` is Ed25519, ``EcdsaKey`` is P-256. Both
share one encoder; a subclass only supplies its private key from the seed.
``.algorithm`` reports the choice for callers that must pick a FIPS-approved key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import ClassVar

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes


@dataclass(frozen=True)
class SSHKey:
    """Ephemeral SSH keypair, in text form (Ed25519 unless a subclass overrides).

    Three views of the same key:

    - ``pub``: multi-line PEM ``-----BEGIN PUBLIC KEY-----`` block
      (SubjectPublicKeyInfo). The interoperable public-key encoding for
      non-SSH crypto consumers.
    - ``priv``: multi-line OpenSSH private-key PEM block (the standard
      ``OPENSSH PRIVATE KEY`` banner). Unencrypted (ephemeral keypair,
      in-memory, single run).
    - ``auth_line``: the single-line OpenSSH public-key wire format,
      ``ssh-ed25519 AAA... comment`` (or ``ecdsa-sha2-nistp256 …`` for
      :class:`EcdsaKey`) — the literal string for ``authorized_keys``.

    ``algorithm`` is the OpenSSH key-type family; check it when a peer requires a
    FIPS-approved algorithm (use :class:`EcdsaKey` for ESXi).
    """

    pub: str
    priv: str
    auth_line: str

    algorithm: ClassVar[str] = "ed25519"

    @classmethod
    def _private_key(cls, seed: bytes) -> PrivateKeyTypes:
        """The algorithm's private key derived from a 32-byte seed.

        Base: Ed25519, whose 32-byte seed *is* the key. Subclasses override to
        derive their own algorithm's key deterministically from the same seed.
        """
        return Ed25519PrivateKey.from_private_bytes(seed)

    @classmethod
    def generate(cls, comment: str = "testrange") -> SSHKey:
        """Generate a deterministic SSH keypair seeded from ``comment``.

        The 32-byte seed is ``sha256(comment)``. Calling twice with the same
        comment (and same class) produces the same keypair — intentional, so the
        rendered seed that embeds the public key into ``authorized_keys`` hashes
        the same across runs and the post-install cache hits.

        INSECURE BY DESIGN. The private key is fully derivable from the comment.
        Only safe for ephemeral test environments where the guest VMs are
        isolated and short-lived. Do not use this for real authentication.
        """
        seed = hashlib.sha256(comment.encode("utf-8")).digest()
        private = cls._private_key(seed)
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


class EcdsaKey(SSHKey):
    """Deterministic NIST P-256 (``ecdsa-sha2-nistp256``) keypair.

    For FIPS-mode SSH peers that reject Ed25519 — ESXi 8's sshd runs in FIPS mode
    and silently denies Ed25519 pubkeys, but accepts ECDSA P-256 and RSA
    (CORE-63). Same deterministic-from-``comment`` contract as the base.
    """

    algorithm: ClassVar[str] = "ecdsa"

    @classmethod
    def _private_key(cls, seed: bytes) -> PrivateKeyTypes:
        # The P-256 private key is a scalar in [1, n-1]. Take the top 255 bits of
        # the seed: that is < 2**255 < n (the curve order is just under 2**256),
        # so it is always in range without hardcoding n; `or 1` rules out 0. This
        # discards one bit of entropy, which is irrelevant for an insecure-by-
        # design ephemeral key.
        scalar = (int.from_bytes(seed, "big") >> 1) or 1
        return ec.derive_private_key(scalar, ec.SECP256R1())


__all__ = ["EcdsaKey", "SSHKey"]
