"""Top-level network declarations: Network, Switch, Sidecar, and the build switch."""

from __future__ import annotations

from testrange.networks.base import (
    ManagedBuildSwitch,
    ManagedEgress,
    Network,
    NetworkAddressing,
    Sidecar,
    Switch,
)

__all__ = [
    "ManagedBuildSwitch",
    "ManagedEgress",
    "Network",
    "NetworkAddressing",
    "Sidecar",
    "Switch",
]
