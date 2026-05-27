"""Orchestrator runtime — Phase 0 stubs.

The real Orchestrator class lands in Phase 4; the test runner in Phase 5.
This module provides import-time stubs so plan files load on a Phase 0
install.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.plan import Plan
    from testrange.vms.handle import VMHandle


@dataclass(frozen=True)
class TestResult:
    """Outcome of one test function. Phase 5 fills the runtime side."""

    name: str
    passed: bool
    error: str | None = None
    duration: float = 0.0

    def report_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        line = f"[{status}] {self.name} ({self.duration:.2f}s)"
        if self.error:
            line += f"\n      {self.error}"
        return line


@dataclass
class OrchestratorHandle:
    """Test-code-facing handle. Phase 4/5 fill the runtime fields."""

    run_id: str
    vms: Mapping[str, VMHandle]


def run_tests(
    tests: list[Callable[[OrchestratorHandle], None]],
    plan: Plan,
) -> list[TestResult]:
    """Entry point used by ``examples/hello_world.py``.

    Phase 5 implements the real lifecycle: preflight -> install -> run ->
    each test -> cleanup. Phase 0 raises so misuse fails loud.
    """
    raise NotImplementedError("run_tests lands in Phase 5")
