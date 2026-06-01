"""Logging setup for testrange.

Stdlib ``logging`` only. A ``LoggerAdapter`` injects the active ``run_id``
into every record so all output lines are attributable to a run.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-5s [%(run_id)s] %(name)s: %(message)s"


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
    """Install a stderr StreamHandler with the standard format.

    Idempotent — calling again updates the level but does not duplicate
    handlers (the StreamHandler we installed is left in place).
    """
    root = logging.getLogger("testrange")
    root.setLevel(level.upper())
    _quiesce_firehose()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_RunIdFormatter(_DEFAULT_FORMAT))
    root.addHandler(handler)
    root.propagate = False


def _quiesce_firehose() -> None:
    """Pin the streaming-firehose loggers above the operator's log level (CORE-50).

    The build serial mirror (``…build_phase.console``, emits DEBUG) and the
    per-test stdout tee (``…runner.testout``, emits INFO) are a high-volume
    firehose of *raw guest output*, meant to be watched only through the
    ``--verbose`` live tail — which lowers them to DEBUG for its own duration
    (see :func:`testrange._tui.live_output`). Left inheriting the ``testrange``
    root level, a plain ``--log-level debug`` run would enable their DEBUG/INFO
    records and dump the whole firehose through the stderr handler, drowning the
    operator (and hijacking the terminal with embedded escapes). Pinning them to
    ``WARNING`` decouples the firehose from ``--log-level`` without touching the
    live-tail path, which overrides this level explicitly while it owns the
    terminal.

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
