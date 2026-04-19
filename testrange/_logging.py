"""Structured stderr logging for TestRange.

All modules should ``from testrange._logging import get_logger`` and log
progress at ``INFO`` for user-facing steps, ``DEBUG`` for internals. The
:func:`log_duration` context manager brackets a span and reports elapsed
time on exit — use it for anything slow enough that a user might wonder
if the process is hung (VM boots, image downloads, guest-agent waits).

The CLI calls :func:`configure_root_logger` once at startup. Library
callers that embed TestRange are free to configure logging themselves;
this module never touches the root logger outside of that explicit call.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

_LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """Return the TestRange logger for *name*.

    Callers should pass ``__name__`` so the hierarchy mirrors the module
    tree (e.g. ``testrange.orchestrator``). Logging config — handlers,
    level, formatting — is inherited from the root ``testrange`` logger.

    :param name: Usually ``__name__`` of the caller.
    :returns: A :class:`logging.Logger` instance.
    """
    return logging.getLogger(name)


def configure_root_logger(level: int = logging.INFO) -> None:
    """Install a stderr handler on the ``testrange`` logger.

    Idempotent: calling more than once replaces handlers rather than
    stacking them, so repeated CLI invocations (or tests that spin the
    CLI up several times) don't produce duplicated log lines.

    :param level: Minimum level to emit. ``logging.INFO`` by default;
        pass ``logging.DEBUG`` for verbose traces.
    """
    root = logging.getLogger("testrange")
    # Drop any previously-attached handlers so re-configuring is safe.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    # Don't propagate to Python's root logger — other libraries
    # (click, requests, urllib3) configure that independently and we
    # don't want to inherit their handlers or re-emit their records.
    root.propagate = False


@contextmanager
def log_duration(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
) -> Iterator[None]:
    """Log *message* on entry and ``message + elapsed`` on successful exit.

    On exception, log the failure with the same elapsed time so the user
    can tell whether a hang became a fast failure or a slow one.

    :param logger: Logger to emit through.
    :param message: Short human-readable description of the span
        (e.g. ``"install VM 'webpublic'"``). Shown verbatim on entry;
        ``" (Xs)"`` is appended on exit.
    :param level: Log level for the success-path messages.
    """
    logger.log(level, "%s ...", message)
    start = time.monotonic()
    try:
        yield
    except BaseException:
        elapsed = time.monotonic() - start
        logger.log(
            logging.ERROR,
            "%s FAILED after %.1fs",
            message,
            elapsed,
        )
        raise
    else:
        elapsed = time.monotonic() - start
        logger.log(level, "%s done in %.1fs", message, elapsed)
