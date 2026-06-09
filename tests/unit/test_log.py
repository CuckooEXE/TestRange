"""Tests for logging setup (configure idempotency + run_id injection)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from rich.logging import RichHandler

from testrange._log import configure, get_logger
from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER


@pytest.fixture(autouse=True)
def _reset_testrange_logger() -> Iterator[None]:
    """Strip handlers off the package logger so each test starts clean.

    Also restores ``propagate`` — ``configure`` sets it ``False``, which would
    otherwise stop later records from reaching ``caplog``'s root handler — and
    the firehose loggers' levels, which ``configure`` pins (CORE-50).
    """
    root = logging.getLogger("testrange")
    firehose = [logging.getLogger(CONSOLE_LOGGER), logging.getLogger(TESTOUT_LOGGER)]
    saved_handlers = list(root.handlers)
    saved_propagate = root.propagate
    saved_firehose_levels = [lg.level for lg in firehose]
    root.handlers.clear()
    root.propagate = True
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.propagate = saved_propagate
    for lg, lvl in zip(firehose, saved_firehose_levels, strict=True):
        lg.setLevel(lvl)


class TestConfigure:
    def test_installs_a_single_rich_handler(self) -> None:
        configure()
        root = logging.getLogger("testrange")
        assert sum(isinstance(h, RichHandler) for h in root.handlers) == 1
        assert root.propagate is False

    def test_idempotent_no_duplicate_handlers(self) -> None:
        configure()
        configure()
        configure()
        root = logging.getLogger("testrange")
        assert sum(isinstance(h, RichHandler) for h in root.handlers) == 1

    def test_recall_updates_level(self) -> None:
        configure(level="INFO")
        configure(level="DEBUG")
        assert logging.getLogger("testrange").level == logging.DEBUG

    def test_firehose_isolated_from_root_log_level(self) -> None:
        """``--log-level debug`` must NOT enable the serial firehose (CORE-50).

        The build serial mirror (``…console``) and per-test stdout tee
        (``…testout``) are watchable only through ``--verbose`` (the live tail
        lowers them to DEBUG for its own duration). They must never ride the
        operator's ``--log-level`` down to the plain handler, or a
        ``--log-level debug`` run drowns in raw serial chatter.
        """
        configure(level="DEBUG")
        assert logging.getLogger("testrange").level == logging.DEBUG
        # Even with the root at DEBUG, the firehose loggers stay above their
        # emit levels (console logs DEBUG, testout logs INFO), so nothing
        # reaches the installed handler.
        assert not logging.getLogger(CONSOLE_LOGGER).isEnabledFor(logging.DEBUG)
        assert not logging.getLogger(TESTOUT_LOGGER).isEnabledFor(logging.INFO)


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

    def test_rich_handler_renders_run_id_and_message(self) -> None:
        from testrange._console import err_console

        configure(level="INFO")
        log = get_logger("testrange.sample", run_id="run-7")
        with err_console().capture() as cap:
            log.info("provisioning web")
        text = cap.get()
        assert "run-7" in text
        assert "provisioning web" in text
