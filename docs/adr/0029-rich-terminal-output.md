# ADR-0029: `rich` for all terminal output

Status: Accepted
Date: 2026-06-08

**Reverses the "no rich/tqdm" stance of CORE-6 / CORE-18.** CORE-6 built the
operator-facing renderers — the `--verbose` live tail (`testrange/_tui.py`) and
the transfer progress bar (`testrange/_progress.py`) — from raw ANSI escapes
"Pure stdlib … no rich/tqdm dependency", to keep the dependency surface at a
single core package (`cryptography`); CORE-18 re-affirmed that on 2026-05-27.
This ADR accepts a second core dependency, `rich`, and routes **all** terminal
output through it.

## History

A first attempt at this migration (EPIC CORE-49, tickets CORE-50…58) was
implemented and **reverted on 2026-06-01 at the user's request**; only the
stdlib firehose-isolation fix (CORE-50) was kept. This ADR is the deliberate
re-adoption (user decision, 2026-06-08), committed to `rich` as a core
dependency rather than an optional extra — superseding the reverted attempt and
adding a multi-pane live `run` dashboard (CORE-74…78) that the original epic did
not have.

## Context

Terminal I/O had grown into four independent, hand-rolled mechanisms that did
not share a notion of "the terminal":

- `testrange/_log.py` — stdlib `logging` with a single stderr `StreamHandler`.
- `testrange/_tui.py::LiveTail` — a Docker-BuildKit-style collapsing region
  hand-built from cursor-up / erase-line / erase-display escapes, a manual ring
  buffer, and bespoke SIGWINCH wiring.
- `testrange/_progress.py::ProgressReporter` — a `\r`-redrawn transfer bar with
  its own TTY/non-TTY split.
- `testrange/cli.py` — ~90 bare `print()` calls, including a hand-indented
  `describe` plan tree and hand-aligned cache tables.

Three problems followed. (1) **They fight over the terminal.** The live tail has
to forcibly remove the log handler for its duration so the two writers don't
interleave escapes. (2) **Correctness lives in escape arithmetic.** Cursor-up
counts desync on a wrapped line (`_truncate` exists solely to prevent that);
SIGWINCH, cursor restore, and re-entrancy guards are all re-implemented by hand.
(3) **The firehose rode the log level** — a raw guest-serial stream tied to
`--log-level debug` flooded the terminal (CORE-50, now fixed). Each renderer
re-solves problems (width, wrapping, TTY detection, control-char neutralisation,
markup) that a mature console library solves once.

## Decision

**Adopt `rich` as a core runtime dependency and make a single
`rich.console.Console` the one owner of the terminal.** Every output path is
expressed in rich terms:

- **One Console pair (`testrange/_console.py`).** A stdout Console for *data*
  (`describe` trees, cache tables, status) and a stderr Console for *diagnostics*
  (logs, progress, the live region, errors). TTY detection, width, and
  control-char handling are rich's job. Nothing else constructs a bare
  `Console()`.
- **Logging → `rich.logging.RichHandler`** on the `testrange` logger, TTY and
  non-TTY alike. The `run_id` field is preserved via the existing
  `_RunIdAdapter` and the handler's message format; off a terminal rich renders
  plain (no escapes), so logs stay greppable.
- **`describe` → `rich.tree.Tree`**, cache listings → `rich.table.Table`.
- **Transfer progress → `rich.progress.Progress`**, replacing the hand-rolled
  `ProgressReporter` — preserving its behaviour, including the **non-TTY
  periodic-INFO line** (the CI/build-farm visibility CORE-18 kept stdlib for).
- **The `run`/`build` live view → a multi-pane `rich.live.Live` +
  `rich.layout.Layout` dashboard** (VM lifecycle states, test pass/fail, a log
  tail, and the build serial console), replacing the single collapsing
  `LiveTail`. Off a TTY it degrades to plain `RichHandler` logging — no live
  region — preserving current non-interactive behaviour.

## Consequences

- **One more core dependency.** `rich` joins `cryptography` in
  `[project].dependencies`. `rich` is pure-Python, dependency-light
  (`markdown-it-py` + `pygments`), ships `py.typed` (so `mypy --strict` is
  unaffected), and is broadly trusted; the maintenance cost is judged well below
  that of carrying four bespoke ANSI renderers.
- **The trust boundary is unchanged.** Guest serial / test output is still
  untrusted. `testrange/_ansi.py::scrub_terminal_control` stays and is still
  applied at every guest-output sink *before* the bytes reach rich. At the
  render boundary guest-influenced strings are passed as `rich.text.Text`
  (`markup=False`) so a guest line like `[red]` cannot inject rich markup —
  defence-in-depth on top of the scrub.
- **Hand-rolled escape code is deleted**, not ported: the cursor arithmetic,
  manual SIGWINCH handler, and `\r`-bar rendering go away. Tests that asserted
  on raw `\x1b[…]` sequences are rewritten against rich's capturable Console
  (`Console(file=…, force_terminal=…)` / `Console.capture()`).
- **Log on-the-wire format changes** (RichHandler styling/layout). Non-TTY
  output stays plain text and `run_id`-tagged, so the command-log hooks (which
  parse Bash output, not testrange logs) and CI log scraping are unaffected.

## Alternatives considered

- **Keep pure stdlib (status quo).** Rejected: the bespoke renderers are exactly
  the source of the fight-over-the-terminal and escape-arithmetic bugs; growing
  them to cover trees/tables/styling and a multi-pane dashboard re-implements
  rich badly.
- **`rich` as an optional extra with a stdlib fallback.** Rejected: since *all*
  output moves to rich, a fallback means maintaining two full render paths
  forever — more surface than the bespoke code it replaces, for the sake of one
  small pure-Python dependency.
