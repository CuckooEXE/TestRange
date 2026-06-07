"""Standalone helpers with no dependency on testrange's core abstractions."""

from __future__ import annotations

from testrange.utils.fsutil import durable_replace
from testrange.utils.sshkey import EcdsaKey, SSHKey

__all__ = ["EcdsaKey", "SSHKey", "durable_replace"]
