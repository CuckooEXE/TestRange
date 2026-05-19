"""Lifecycle orchestration for a Plan: preflight, install, run, test, cleanup."""

from __future__ import annotations

from testrange.orchestrator.runner import TestResult, run_tests
from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle

__all__ = ["Orchestrator", "OrchestratorHandle", "TestResult", "run_tests"]
