# TestRange — Project Instructions

Project-level rules for `testrange`. These augment the user's global
`~/.claude/CLAUDE.md` and take precedence where they overlap.

## Working agreement (non-negotiable)

These are hard rules for this repo. They are not suggestions.

### 1. Ticket-first — every action needs a board ticket

The kanban board lives in **`TODO.md`** at the repo root (checked into git).
**No work happens without a task on it.** No "hey, quickly add this feature /
bugfix / style tweak" off the books. (`TODO.md` is the live status *and* the
audit trail — it versions with the code.)

- Before starting any change, there must be a task in `TODO.md` for it. Add a
  new entry under the appropriate status section and category swimlane.
- If we're mid-refactor/feature and the change is **related and in-scope**, it's
  fine to add the task straight under **Doing** and proceed.
- If a requested change is **unrelated** to what's in flight, **push back**:
  say it needs its own task and (usually) shouldn't be bolted onto the
  current work. Don't silently scope-creep.
- Task shape: a checklist item `- [ ] **<ID>** · `<type>` — Brief description`
  followed by a `>` blockquote with the detail, under the matching category
  swimlane (`type` ∈ feat/bugfix/chore/ci/test/docs/EPIC; `ID` uses the
  swimlane prefix — PVE/NET/CACHE/ORCH/CORE/COMM/BUILD/PROXY/DOCS/CI/BACKEND/
  ESXI, which are the category headings).
- Status sections: **Doing** = in progress, **Ready** = backlog + to-do,
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
pytest -m "not proxmox and not libvirt"   # unit/MockDriver only; live-backend suites run out-of-band
```

No cheap dev hacks to make a gate pass. Fix the underlying issue.

### 3. PLAN.md and TODO.md are the sources of truth — keep them current

`PLAN.md` (living design) and **`TODO.md`** (what's *in flight*) describe what
the codebase *is* and what's being worked on. They must always reflect reality:

- When a task's status changes, move its entry between sections in `TODO.md`
  (→ Doing → Done). Tasks are never deleted, they flow to **Done** (then
  **Archive** for older history) and get their checkbox ticked. Record the
  completion date inline (e.g. `_(done: 2026-06-06)_`).
- When a design decision changes, update `PLAN.md` in the same change — don't
  let it drift. If code and PLAN disagree, that's a bug to fix, not tolerate.
- A change that alters design or scope is incomplete until PLAN and TODO are
  updated to match.

### 4. New capabilities land in `examples/capabilities.py`

`examples/capabilities.py` is the canonical "everything a driver must
support" survey — the broad-coverage portable plan plus its `TESTS` list.
It's how a backend gets *certified working*.

- Any new feature / knob / capability that touches a driver-facing contract
  (devices, networks, communicators, builders, packages, credentials,
  snapshots, power state, pools, sidecar behavior, etc.) must be added to
  `examples/capabilities.py` **in the same change**: extend the plan (new
  VM, NIC, device, or builder setting) and add a corresponding entry to
  `TESTS` that verifies the capability end-to-end.
- The example stays *portable* — backend-agnostic `Hypervisor`, no host /
  credentials / build switch in the file. Backend binding happens at run
  time via `--connect`.
- Driver-specific capabilities examples (`examples/capabilities-<driver>.py`)
  will be added once a backend is *certified working* against the portable
  example. Until then, backend-specific behavior is exercised by binding
  the portable plan with `--connect <profile>`.

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
