"""Operator-driven debugging hooks.

Single entry point :func:`pause_on_error_if_enabled` honoured by every
orchestrator's ``__enter__`` exception path AND by
:meth:`testrange.test.Test.run` around the test body.  When the env
var ``TESTRANGE_PAUSE_ON_ERROR`` is set (any non-empty value), the
process blocks on ``input()`` before teardown so an operator can SSH
into the still-alive VMs, ``cat`` log files, ``virsh dumpxml``,
``pvesh get …``, etc.

Default off; the cost of a runaway pause in CI would dwarf the
debugging value, so opt-in is the right shape.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from testrange._logging import get_logger

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator

_log = get_logger(__name__)

_ENV_VAR = "TESTRANGE_PAUSE_ON_ERROR"
"""Truthy value pauses on any orchestrator-side exception before
teardown.  Same env-var convention as ``TESTRANGE_CACHE_DIR`` /
``TESTRANGE_MEMORY_THRESHOLD`` — string presence is the toggle, the
value is unused."""


def pause_on_error_if_enabled(
    reason: str,
    orchestrator: "AbstractOrchestrator | None" = None,
) -> None:
    """Prompt the operator before teardown when the env var is set.

    No-op when ``TESTRANGE_PAUSE_ON_ERROR`` is unset (the common
    case); when set, prints *reason* + the orchestrator's keep-alive
    hints (``virsh`` / ``pvesh`` invocations the operator can use to
    inspect each provisioned resource) and blocks on ``input()``.

    EOF / Ctrl+C exits the prompt and lets teardown proceed —
    operators can ^C twice to interrupt the whole run if they
    actually want to abort.

    :param reason: One-line description of where in the orchestrator
        lifecycle the exception fired (e.g. ``"orchestrator.__enter__
        raised"``).  Surfaced in the prompt so the operator knows
        which phase they're paused at.
    :param orchestrator: Optional orchestrator to source
        :meth:`~testrange.orchestrator_base.AbstractOrchestrator.keep_alive_hints`
        from.  Hints are printed to help the operator find each
        live VM / network without re-deriving the run-id-suffixed
        names by hand.
    """
    if not os.environ.get(_ENV_VAR):
        return

    bar = "=" * 70
    sys.stderr.write(f"\n{bar}\n")
    sys.stderr.write(f"[{_ENV_VAR}] {reason}\n")
    sys.stderr.write(
        "VMs and networks are still alive.  Inspect now; teardown is\n"
        "blocked until you press Enter (or send EOF / Ctrl+C).\n"
    )
    if orchestrator is not None:
        try:
            hints = orchestrator.keep_alive_hints()
        except Exception as exc:  # pragma: no cover — best-effort
            hints = []
            _log.debug("keep_alive_hints raised during debug pause: %s", exc)
        if hints:
            sys.stderr.write("\nResources currently held:\n")
            for line in hints:
                sys.stderr.write(f"  {line}\n")
    sys.stderr.write(f"{bar}\n")
    sys.stderr.flush()

    try:
        input(f"[{_ENV_VAR}] press Enter to tear down: ")
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write(
            f"\n[{_ENV_VAR}] interrupted; proceeding with teardown\n"
        )
        sys.stderr.flush()


__all__ = ["pause_on_error_if_enabled"]
