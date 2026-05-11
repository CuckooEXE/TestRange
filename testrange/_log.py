"""Logging setup for testrange.

Stdlib ``logging`` only. A ``LoggerAdapter`` injects the active ``run_id``
into every record so all output lines are attributable to a run.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s %(levelname)-5s [%(run_id)s] %(name)s: %(message)s"


class _RunIdAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Inject a ``run_id`` field into every record."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra_raw = kwargs.get("extra")
        extra: dict[str, Any] = dict(extra_raw) if extra_raw else {}
        default_rid = (
            self.extra.get("run_id", "-") if self.extra else "-"
        )
        extra.setdefault("run_id", default_rid)
        kwargs["extra"] = extra
        return msg, kwargs


def configure(level: str = "INFO") -> None:
    """Install a stderr StreamHandler with the standard format.

    Idempotent — calling again replaces the handler config but does not
    duplicate handlers.
    """
    global _CONFIGURED
    root = logging.getLogger("testrange")
    if _CONFIGURED:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_RunIdFormatter(_DEFAULT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    _CONFIGURED = True


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
