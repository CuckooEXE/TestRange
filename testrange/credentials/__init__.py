"""Credentials — pure data, consumed by Builders and Communicators."""

from __future__ import annotations

from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.credentials.ssh import SSHKeyPair, gen_ssh_key

__all__ = ["Credential", "PosixCred", "SSHKeyPair", "gen_ssh_key"]
