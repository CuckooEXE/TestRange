"""Orchestrator runtime.

Phase 0: stubs for ``OrchestratorHandle`` and ``run_tests`` so plan files
can import them. Phase 4 wires the orchestrator; Phase 5 adds the test
runner.
"""

from __future__ import annotations

from testrange.orchestrator.runtime import OrchestratorHandle, TestResult, run_tests

__all__ = ["OrchestratorHandle", "TestResult", "run_tests"]
