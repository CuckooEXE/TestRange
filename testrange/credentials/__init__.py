"""Credentials — pure data, consumed by Builders and Communicators."""

from __future__ import annotations

from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred

__all__ = ["Credential", "PosixCred"]
