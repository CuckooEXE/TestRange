"""Tests for logging setup (configure idempotency + run_id injection)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from testrange._log import configure, get_logger


@pytest.fixture(autouse=True)
def _reset_testrange_logger() -> Iterator[None]:
    """Strip handlers off the package logger so each test starts clean.

    Also restores ``propagate`` — ``configure`` sets it ``False``, which would
    otherwise stop later records from reaching ``caplog``'s root handler.
    """
    root = logging.getLogger("testrange")
    saved_handlers = list(root.handlers)
    saved_propagate = root.propagate
    root.handlers.clear()
    root.propagate = True
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.propagate = saved_propagate


class TestConfigure:
    def test_installs_a_single_stream_handler(self) -> None:
        configure()
        root = logging.getLogger("testrange")
        assert sum(isinstance(h, logging.StreamHandler) for h in root.handlers) == 1
        assert root.propagate is False

    def test_idempotent_no_duplicate_handlers(self) -> None:
        configure()
        configure()
        configure()
        root = logging.getLogger("testrange")
        assert sum(isinstance(h, logging.StreamHandler) for h in root.handlers) == 1

    def test_recall_updates_level(self) -> None:
        configure(level="INFO")
        configure(level="DEBUG")
        assert logging.getLogger("testrange").level == logging.DEBUG


class TestRunIdInjection:
    def test_run_id_present_on_records(self, caplog: pytest.LogCaptureFixture) -> None:
        log = get_logger("testrange.sample", run_id="run-42")
        with caplog.at_level(logging.INFO, logger="testrange.sample"):
            log.info("hello")
        assert caplog.records[0].run_id == "run-42"  # type: ignore[attr-defined]

    def test_run_id_defaults_to_dash(self, caplog: pytest.LogCaptureFixture) -> None:
        log = get_logger("testrange.sample")
        with caplog.at_level(logging.INFO, logger="testrange.sample"):
            log.info("hello")
        assert caplog.records[0].run_id == "-"  # type: ignore[attr-defined]
