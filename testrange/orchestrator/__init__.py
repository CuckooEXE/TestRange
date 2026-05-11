"""Orchestrator runtime.

Phase 4: brings the range up via the install + run phases and tears it
down on exit. Phase 5 will wire the test execution loop.
"""

from __future__ import annotations

from testrange.orchestrator.runtime import (
    Orchestrator,
    OrchestratorHandle,
    TestResult,
    run_tests,
)

__all__ = ["Orchestrator", "OrchestratorHandle", "TestResult", "run_tests"]
