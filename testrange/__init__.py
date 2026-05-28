"""testrange — declarative Python plans for VM test-ranges."""

from __future__ import annotations

from testrange.hypervisor import Hypervisor
from testrange.orchestrator.runner import TestResult, run_tests
from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle
from testrange.plan import Plan

__version__ = "0.2.0"

__all__ = [
    "Hypervisor",
    "Orchestrator",
    "OrchestratorHandle",
    "Plan",
    "TestResult",
    "__version__",
    "run_tests",
]
