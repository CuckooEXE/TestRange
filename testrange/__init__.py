"""testrange — Python plans for VM test-ranges, built as explicit dependency graphs."""

from __future__ import annotations

from testrange.hypervisor import Hypervisor
from testrange.orchestrator.runner import TestResult, run_tests
from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle
from testrange.plan import Plan

__version__ = "1.1.1"

__all__ = [
    "Hypervisor",
    "Orchestrator",
    "OrchestratorHandle",
    "Plan",
    "TestResult",
    "__version__",
    "run_tests",
]
