"""Lifecycle orchestration for a Plan: preflight, build, run, test, cleanup."""

from __future__ import annotations

from testrange.orchestrator.runner import TestResult, build_range, run_tests
from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle

__all__ = ["Orchestrator", "OrchestratorHandle", "TestResult", "build_range", "run_tests"]
