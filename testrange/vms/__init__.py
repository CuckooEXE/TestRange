"""VM types: VMSpec (hardware), VMRecipe (provisioning), RunningVM (runtime view)."""

from __future__ import annotations

from testrange.vms.handle import RunningVM
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec

__all__ = ["RunningVM", "VMRecipe", "VMSpec"]
