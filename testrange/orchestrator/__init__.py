"""Lifecycle orchestration for a Plan: preflight, install, run, test, cleanup."""

from __future__ import annotations

from testrange.orchestrator.runtime import (
    Orchestrator,
    OrchestratorHandle,
    TestResult,
    run_tests,
)

__all__ = ["Orchestrator", "OrchestratorHandle", "TestResult", "run_tests"]
