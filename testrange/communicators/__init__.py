"""Communicators — runtime transports for talking to a brought-up VM."""

from __future__ import annotations

from testrange.communicators.base import Communicator, ExecResult
from testrange.communicators.qga import QGACommunicator
from testrange.communicators.ssh import SSHCommunicator

__all__ = ["Communicator", "ExecResult", "QGACommunicator", "SSHCommunicator"]
