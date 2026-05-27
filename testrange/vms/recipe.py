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

    def __post_init__(self) -> None:
        if not isinstance(self.spec, VMSpec):
            raise TypeError(f"VMRecipe.spec must be a VMSpec, got {type(self.spec).__name__}")
        if not isinstance(self.builder, Builder):
            raise TypeError(
                f"VMRecipe.builder must be a Builder, got {type(self.builder).__name__}"
            )
        if not isinstance(self.communicator, Communicator):
            raise TypeError(
                f"VMRecipe.communicator must be a Communicator, "
                f"got {type(self.communicator).__name__}"
            )

    @property
    def name(self) -> str:
        return self.spec.name
