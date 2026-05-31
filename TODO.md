# TODO — moved to ktui

The kanban board is no longer kept in this file. It now lives in **`ktui`**
(kanban-tui), board **TestRange** — see the working agreement in
[`CLAUDE.md`](CLAUDE.md) §1/§3.

```sh
ktui task list --json          # all tickets
ktui task list --actionable    # unblocked, ready to pick up
ktui                           # interactive TUI (exit ctrl+q)
```

- **Columns:** Ready (backlog + to-do) · Doing (in progress) · Done · Archive
  (older history).
- **Categories** are the old swimlanes: PVE · BACKEND · NET · CACHE · ORCH ·
  BUILD · COMM · PROXY · CORE · CI.

The full ticket history as of the migration (2026-05-22) is preserved in this
file's git history.
