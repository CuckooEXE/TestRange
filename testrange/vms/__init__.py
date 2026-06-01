"""VM types: VMSpec (hardware), VMRecipe (provisioning), VMHandle (runtime view).

``GuestHypervisor`` is a VMRecipe that also hosts an inner plan (nested
virtualization, ADR-0021).
"""

from __future__ import annotations

from testrange.vms.nested import GuestHypervisor
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec

__all__ = ["GuestHypervisor", "VMRecipe", "VMSpec"]
