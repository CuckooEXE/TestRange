"""VM types: VMSpec (hardware), VMRecipe (provisioning), VMHandle (runtime view)."""

from __future__ import annotations

from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec

__all__ = ["VMRecipe", "VMSpec"]
