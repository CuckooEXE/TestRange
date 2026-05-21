"""VMRecipe — VMSpec + provisioning (builder, communicator)."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.builders.base import Builder
from testrange.communicators.base import Communicator
from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class VMRecipe:
    """A VM declaration: hardware spec + how to install + how to talk to it."""

    spec: VMSpec
    builder: Builder
    communicator: Communicator

    @property
    def name(self) -> str:
        return self.spec.name
