"""Builders — produce install payloads + drive the install lifecycle.

Plan-time API exposes ``CloudInitBuilder``. The runtime guts (rendering,
seed-ISO authoring) land in Phase 3.
"""

from __future__ import annotations

from testrange.builders.base import Builder
from testrange.builders.cloudinit import CloudInitBuilder

__all__ = ["Builder", "CloudInitBuilder"]
