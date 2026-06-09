"""The process's rich Consoles — the single owner of the terminal (ADR-0029).

Two Consoles, split by *what* is written rather than by log level:

- :func:`out_console` — **data the user asked for**, on stdout: the ``describe``
  tree, cache tables, status lines. Keeping it on stdout keeps
  ``testrange describe … | …`` pipe-friendly.
- :func:`err_console` — **diagnostics**, on stderr: log records (via
  ``RichHandler``), the ``--verbose`` live region, transfer progress, and error
  messages. Off stdout, so a piped data stream stays clean.

Both are created once, lazily, and shared; rich owns TTY detection, width,
wrapping, and control-character neutralisation. Nothing else in the package
constructs a bare ``Console``. Tests capture via ``out_console().capture()`` /
``err_console().capture()``, or pass their own ``Console`` to the renderer under
test.
"""

from __future__ import annotations

from rich.console import Console

_out: Console | None = None
_err: Console | None = None


def out_console() -> Console:
    """The shared stdout Console for user-requested data."""
    global _out
    if _out is None:
        _out = Console()
    return _out


def err_console() -> Console:
    """The shared stderr Console for diagnostics (logs, progress, errors)."""
    global _err
    if _err is None:
        _err = Console(stderr=True)
    return _err


__all__ = ["err_console", "out_console"]
