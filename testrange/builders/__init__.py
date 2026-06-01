"""Builders — produce install payloads (e.g., cloud-init seeds) consumed during install."""

from __future__ import annotations

from testrange.builders.base import Builder
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.builders.esxi import ESXiKickstartBuilder
from testrange.builders.proxmox import ProxmoxAnswerBuilder

__all__ = ["Builder", "CloudInitBuilder", "ESXiKickstartBuilder", "ProxmoxAnswerBuilder"]
