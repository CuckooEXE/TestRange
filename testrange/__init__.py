"""testrange — declarative Python plans for VM test-ranges."""

from __future__ import annotations

from testrange.orchestrator.runtime import (
    Orchestrator,
    OrchestratorHandle,
    TestResult,
    run_tests,
)
from testrange.plan import Plan

__version__ = "0.1.0"

__all__ = [
    "Orchestrator",
    "OrchestratorHandle",
    "Plan",
    "TestResult",
    "__version__",
    "run_tests",
]
