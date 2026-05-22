# TestRange — Project Instructions

Project-level rules for `testrange`. These augment the user's global
`~/.claude/CLAUDE.md` and take precedence where they overlap.

## Working agreement (non-negotiable)

These are hard rules for this repo. They are not suggestions.

### 1. Ticket-first — every action needs a board ticket

The kanban board lives in **`ktui`** (kanban-tui), board **TestRange**. **No
work happens without a task on it.** No "hey, quickly add this feature / bugfix
/ style tweak" off the books. (`ktui` stores a local SQLite DB — it is *not* in
git; the board is the live status, the repo is the code.)

- Before starting any change, there must be a task on the board for it
  (`ktui task list --json`; create with `ktui task create`).
- If we're mid-refactor/feature and the change is **related and in-scope**, it's
  fine to create the task straight into **Doing** and proceed.
- If a requested change is **unrelated** to what's in flight, **push back**:
  say it needs its own task and (usually) shouldn't be bolted onto the
  current work. Don't silently scope-creep.
- Task shape: **title** `<type> | <ID>: Brief description`, **description** the
  detail, **category** the swimlane (`type` ∈ feat/bugfix/chore/ci/test/docs;
  `ID` uses the swimlane prefix — PVE/NET/CACHE/ORCH/CORE/COMM/BUILD/PROXY/
  DOCS/CI/BACKEND, which are the board's categories).
- Column mapping: **Ready** = backlog + to-do, **Doing** = in progress,
  **Done** = recently completed, **Archive** = older completed history.

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

### 3. PLAN.md and the ktui board are the sources of truth — keep them current

`PLAN.md` (living design) and the **`ktui` TestRange board** (what's *in flight*)
describe what the codebase *is* and what's being worked on. They must always
reflect reality:

- When a task's status changes, move it on the board (`ktui task move <id>
  <column>`: → Doing → Done). Tasks are never deleted, they flow to **Done**
  (then **Archive** for older history). Record the completion date in the task
  description (the board carries no done-date field).
- When a design decision changes, update `PLAN.md` in the same change — don't
  let it drift. If code and PLAN disagree, that's a bug to fix, not tolerate.
- A change that alters design or scope is incomplete until PLAN and the board
  are updated to match.

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
