"""User credential definitions for VM accounts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Credential:
    """Represents a user account to be created on a VM.

    The ``root`` account is treated as a regular user by the cloud-init
    builder — you must explicitly include a ``Credential(username='root', ...)``
    entry if you want root access configured.

    :param username: The UNIX username. Use ``'root'`` for the superuser account.
    :param password: The plaintext password. This is hashed with SHA-512-crypt
        before being written to the cloud-init ``user-data`` and never stored
        in plaintext on disk.
    :param ssh_key: An optional SSH public-key string (e.g. ``'ssh-rsa AAAA...'``).
        If provided, the key is added to ``~<username>/.ssh/authorized_keys``
        inside the VM.
    :param sudo: If ``True``, the user is granted passwordless ``sudo`` access
        (``ALL=(ALL) NOPASSWD:ALL``). Silently ignored for ``root``.
    """

    username: str
    """The UNIX username for this account (e.g. ``'root'``, ``'deploy'``)."""
    password: str
    """Plaintext password; hashed with SHA-512-crypt before use in cloud-init."""
    ssh_key: str | None = None
    """Optional SSH public-key string added to ``authorized_keys`` inside the VM."""
    sudo: bool = False
    """If ``True``, the user is granted passwordless sudo access (ignored for root)."""

    def is_root(self) -> bool:
        """Return ``True`` if this credential is for the root account.

        :returns: ``True`` when :attr:`username` equals ``'root'``.
        """
        return self.username == "root"
