"""Logging setup for testrange.

Records are rendered by :class:`rich.logging.RichHandler` onto the shared stderr
Console (ADR-0029); RichHandler owns the time and level columns, so the
formatter carries only the ``run_id`` and logger name. A ``LoggerAdapter``
injects the active ``run_id`` into every record so all output lines are
attributable to a run.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

from rich.logging import RichHandler

from testrange._console import err_console

# RichHandler renders the timestamp and level; the formatted message carries
# only what rich does not: the run_id correlator and the logger name.
_MESSAGE_FORMAT = "[%(run_id)s] %(name)s: %(message)s"


class _RunIdAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Inject a ``run_id`` field into every record."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra_raw = kwargs.get("extra")
        extra: dict[str, Any] = dict(extra_raw) if extra_raw else {}
        default_rid = self.extra.get("run_id", "-") if self.extra else "-"
        extra.setdefault("run_id", default_rid)
        kwargs["extra"] = extra
        return msg, kwargs


def configure(level: str = "INFO") -> None:
    """Install a :class:`RichHandler` on the ``testrange`` logger (ADR-0029).

    Idempotent — calling again updates the level but does not duplicate
    handlers (the RichHandler we installed is left in place).
    """
    root = logging.getLogger("testrange")
    root.setLevel(level.upper())
    _quiesce_firehose()
    if any(isinstance(h, RichHandler) for h in root.handlers):
        return
    # markup=False: messages carry literal ``[run_id]`` brackets and, on the
    # fail path, raw guest output — neither must be parsed as rich markup.
    handler = RichHandler(
        console=err_console(),
        show_path=False,
        markup=False,
        rich_tracebacks=True,
    )
    handler.setFormatter(_RunIdFormatter(_MESSAGE_FORMAT))
    root.addHandler(handler)
    root.propagate = False


def _quiesce_firehose() -> None:
    """Pin the streaming-firehose loggers above the operator's log level (CORE-50).

    The build serial mirror (``…vm_build.console``, emits DEBUG) and the
    per-test stdout tee (``…runner.testout``, emits INFO) are a high-volume
    firehose of *raw guest output*. They are meant to be surfaced only on demand
    — through the dashboard's Serial pane or as ``--verbose`` log lines, both of
    which lower them to DEBUG for their own duration (see
    :func:`testrange._dashboard.run_dashboard`). Left inheriting the
    ``testrange`` root level, a plain ``--log-level debug`` run would enable
    their DEBUG/INFO records and dump the whole firehose through this stderr
    handler, drowning the operator (and hijacking the terminal with embedded
    escapes). Pinning them to ``WARNING`` decouples the firehose from
    ``--log-level`` without touching the live-tail path, which overrides this
    level explicitly while it owns the terminal.

    Imported lazily so this low-level module stays free of a static dependency
    on the UI layer that owns the firehose logger names.
    """
    from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER

    for name in (CONSOLE_LOGGER, TESTOUT_LOGGER):
        logging.getLogger(name).setLevel(logging.WARNING)


class _RunIdFormatter(logging.Formatter):
    """Formatter that tolerates records missing ``run_id``."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        return super().format(record)


def get_logger(name: str, run_id: str | None = None) -> _RunIdAdapter:
    """Return a logger that auto-injects ``run_id`` into every record."""
    base = logging.getLogger(name if name.startswith("testrange") else f"testrange.{name}")
    return _RunIdAdapter(base, {"run_id": run_id or "-"})
