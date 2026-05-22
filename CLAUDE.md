# TestRange — Project Instructions

Project-level rules for `testrange`. These augment the user's global
`~/.claude/CLAUDE.md` and take precedence where they overlap.

## Working agreement (non-negotiable)

These are hard rules for this repo. They are not suggestions.

### 1. Ticket-first — every action needs a board ticket

`TODO.md` is a kanban board (swimlanes + status columns). **No work happens
without a ticket on it.** No "hey, quickly add this feature / bugfix / style
tweak" off the books.

- Before starting any change, there must be a ticket in `TODO.md` for it.
- If we're mid-refactor/feature and the change is **related and in-scope**, it's
  fine to write the ticket straight into **In Progress** and proceed.
- If a requested change is **unrelated** to what's in flight, **push back**:
  say it needs its own ticket and (usually) shouldn't be bolted onto the
  current work. Don't silently scope-creep.
- Ticket format: `<type> | <ID>: Brief description` + an indented detail line
  (`type` ∈ feat/bugfix/chore/ci/test/docs; `ID` uses the swimlane prefix —
  PVE/NET/CACHE/ORCH/CORE/COMM/BUILD/PROXY/DOCS/CI/BACKEND).

### 2. Gates always pass — no exceptions, no hacks

Every commit/push must pass the pre-commit / pre-push gates. **Always.** There
is no "I'll fix the type error later," no `# type: ignore` to dodge mypy, no
skipped tests to get green, no `--no-verify`. If a gate fails, the work isn't
done.

The gates (`.pre-commit-config.yaml` + the project standard):

```sh
ruff check .
ruff format --check .
mypy --strict testrange tests
pytest -m "not <integration-mark>"
```

No cheap dev hacks to make a gate pass. Fix the underlying issue.

### 3. PLAN.md and TODO.md are the sources of truth — keep them current

`PLAN.md` (living design) and `TODO.md` (kanban board) describe what the
codebase *is* and what's *in flight*. They must always reflect reality:

- When a ticket's status changes, move it on the board (→ In Progress → Done
  with a date stamp). Tickets are never deleted, they flow to **Done**.
- When a design decision changes, update `PLAN.md` in the same change — don't
  let it drift. If code and PLAN disagree, that's a bug to fix, not tolerate.
- A change that alters design or scope is incomplete until PLAN/TODO are
  updated to match.

## Project state (orientation)

- **Driver layer is multi-backend** (ADR-0008). `MockDriver` / `MockHypervisor`
  is the in-memory reference backend the unit suite drives end-to-end. The
  Proxmox driver is in progress (`feature/proxmox`); the libvirt driver was
  **deleted** and is slated for a rebuild against the ABC.
- **Build/run are two phases + two CLI verbs** (ADR-0010): `build` warms the
  cache; `run` auto-builds on a miss then runs tests.
- **`MockDriver` simulates the backend, not a real guest** — a live `testrange
  run` of an example to green needs a real backend; on the mock the full
  lifecycle is exercised by `tests/unit/test_orchestrator.py` with faked
  communicators. `testrange describe` is the passive CLI check.
- Conventions and design rationale live in `docs/adr/`, `PLAN.md`, and the
  user's global skills/memories.
