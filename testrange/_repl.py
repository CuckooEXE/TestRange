"""Interactive Python REPL for poking at a provisioned test plan.

Loaded by the ``testrange repl`` CLI command (see :mod:`testrange._cli`).
The orchestrator is already started before :func:`start_repl` is called,
so :attr:`Orchestrator.vms` is fully populated and every :class:`VM`
exposes its full runtime API (:meth:`~testrange.vms.base.AbstractVM.exec`,
:meth:`~testrange.vms.base.AbstractVM.upload`, etc.).

The REPL prefers IPython if installed (nicer tab-completion and pretty
printing) and falls back to :class:`code.InteractiveConsole` from the
standard library so the feature works on any installation.
"""

from __future__ import annotations

import atexit
import builtins
import code
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator as Orchestrator


_HISTORY_PATH = Path.home() / ".cache" / "testrange" / "repl_history"
"""Persistent history file for the stdlib REPL fallback."""


def start_repl(orch: Orchestrator, test_name: str) -> None:
    """Launch an interactive Python REPL bound to *orch*'s VMs.

    Builds a locals dict containing ``orch``, ``vms``, and one binding
    per VM (named after the VM) so the user can type ``web.exec([...])``
    instead of ``orch.vms["web"].exec([...])``.

    :param orch: An already-entered
        :class:`~testrange.orchestrator_base.AbstractOrchestrator`.
    :param test_name: Name of the :class:`~testrange.test.Test` whose
        configuration produced *orch*; shown in the REPL banner.
    """
    ns = _build_locals(orch)
    banner = _build_banner(orch, test_name, ns)
    _interact(ns, banner)


def _build_locals(orch: Orchestrator) -> dict[str, Any]:
    """Build the locals dict exposed to the interactive session.

    :param orch: Started orchestrator with ``vms`` populated.
    :returns: Mapping suitable for ``IPython.embed(user_ns=...)`` or
        :class:`code.InteractiveConsole`.
    """
    ns: dict[str, Any] = {
        "orch": orch,
        "vms": list(orch.vms.values()),
    }
    # Expose every VM by its name so the user can type `web.exec(...)`.
    # Skip names that would shadow our own bindings or any standard builtin.
    reserved = {"orch", "vms"} | set(dir(builtins))
    for name, vm in orch.vms.items():
        if name.isidentifier() and name not in reserved:
            ns[name] = vm
    return ns


def _build_banner(
    orch: Orchestrator, test_name: str, ns: dict[str, Any]
) -> str:
    """Return the multi-line banner shown when the REPL starts.

    :param orch: Started orchestrator.
    :param test_name: Name of the test whose config produced *orch*.
    :param ns: Locals dict built by :func:`_build_locals`.
    :returns: Banner string.
    """
    vm_names = sorted(orch.vms.keys())
    bound_as_locals = [n for n in vm_names if n in ns]
    skipped = [n for n in vm_names if n not in bound_as_locals]

    lines = [
        f"TestRange REPL — test {test_name!r}",
        "  orch          Orchestrator",
        f"  vms           list[VM] ({len(vm_names)})",
    ]
    for n in bound_as_locals:
        lines.append(f"  {n:<13} VM")
    if skipped:
        lines.append(
            f"  (not bound as locals — name collision: {', '.join(skipped)};"
            f" use orch.vms[name])"
        )
    if bound_as_locals:
        sample = bound_as_locals[0]
        lines.append("")
        lines.append(f"Try:  {sample}.exec(['uname', '-r']).stdout_text")
    lines.append("Ctrl-D or exit() to quit.")
    return "\n".join(lines)


def _interact(ns: dict[str, Any], banner: str) -> None:
    """Hand control to IPython if available, otherwise to the stdlib REPL.

    :param ns: Locals dict built by :func:`_build_locals`.
    :param banner: Banner string built by :func:`_build_banner`.
    """
    try:
        from IPython import embed  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        _interact_stdlib(ns, banner)
        return
    embed(  # type: ignore[no-untyped-call]
        user_ns=ns, banner1=banner, colors="neutral"
    )


def _interact_stdlib(ns: dict[str, Any], banner: str) -> None:
    """Run :class:`code.InteractiveConsole` with optional readline history.

    :param ns: Locals dict.
    :param banner: Banner string.
    """
    _enable_readline_history()
    console = code.InteractiveConsole(locals=ns)
    try:
        console.interact(banner=banner, exitmsg="")
    except SystemExit:
        # `exit()` / `quit()` inside the REPL raise SystemExit. Catch it
        # so the CLI's finally-block runs the orchestrator teardown.
        pass


def _enable_readline_history() -> None:
    """Wire up persistent history under :data:`_HISTORY_PATH` if possible.

    Silently no-ops on platforms where ``readline`` is unavailable
    (notably Windows) — the REPL still works, just without history.
    """
    try:
        import readline
    except ImportError:
        return

    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    if _HISTORY_PATH.exists():
        try:
            readline.read_history_file(str(_HISTORY_PATH))
        except OSError:
            pass

    atexit.register(_save_history, readline)


def _save_history(readline_mod: Any) -> None:
    """Persist readline history; swallow any I/O errors at shutdown."""
    try:
        readline_mod.write_history_file(str(_HISTORY_PATH))
    except OSError:
        pass


def print_keep_summary(orch: Orchestrator) -> None:
    """Print cleanup hints when the user passes ``--keep`` to ``testrange repl``.

    Delegates backend-specific command generation to
    :meth:`~testrange.orchestrator_base.AbstractOrchestrator.keep_alive_hints`
    — the REPL itself knows nothing about the hypervisor's native
    cleanup CLI.

    :param orch: The orchestrator being intentionally left running.
    """
    run_dir = (
        str(orch._run.path)
        if getattr(orch, "_run", None)
        else "(none)"
    )

    hints = orch.keep_alive_hints()

    lines = ["", "Run kept alive. To clean up manually:"]
    lines.append(f"  Run dir:  {run_dir}")
    if hints:
        lines.append("Suggested:")
        for cmd in hints:
            lines.append(f"  {cmd}")
        if getattr(orch, "_run", None):
            lines.append(f"  rm -rf {run_dir}")

    # stdout, not the logger, so it always shows even at --log-level ERROR.
    print(os.linesep.join(lines))
