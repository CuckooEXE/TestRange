"""testrange — declarative Python plans for VM test-ranges."""

from __future__ import annotations

from testrange.orchestrator.runtime import OrchestratorHandle, TestResult, run_tests
from testrange.plan import Plan

__version__ = "0.1.0"

__all__ = [
    "OrchestratorHandle",
    "Plan",
    "TestResult",
    "__version__",
    "run_tests",
]
