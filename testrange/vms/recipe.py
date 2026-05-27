"""VMRecipe — VMSpec + provisioning (builder, communicator)."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.builders.base import Builder
from testrange.communicators.base import Communicator
from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class VMRecipe:
    """A VM declaration: hardware spec + how to install + how to talk to it.

    Credentials live on the ``builder`` (it's what bakes them into the
    disk). The orchestrator brokers the credential lookup to the
    communicator at bind time.
    """

    spec: VMSpec
    builder: Builder
    communicator: Communicator

    @property
    def name(self) -> str:
        return self.spec.name
