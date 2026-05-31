"""Pluggable guest-reachability gateways (see :mod:`testrange.gateways.base`)."""

from __future__ import annotations

from testrange.gateways.base import GuestGateway
from testrange.gateways.ssh_jump import SSHJumpGateway

__all__ = ["GuestGateway", "SSHJumpGateway"]
