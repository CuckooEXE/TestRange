# Development guide

How to set up a working copy of TestRange, run the checks that CI runs,
and rebuild the documentation.

## Set up a dev environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev,docs,ssh,winrm,repl]"
```

Extras map one-to-one to optional features:

| Extra     | Pulls in                                  | When you need it                         |
| --------- | ----------------------------------------- | ---------------------------------------- |
| `dev`     | `pytest`, `pytest-cov`, `mypy`, `black`, `ruff`, `paramiko`, `pywinrm` | Always, during development.              |
| `docs`    | `sphinx`, `furo`, `sphinx-autodoc-typehints` | Building the Sphinx site.                |
| `ssh`     | `paramiko`                                | Using `SSHCommunicator` at runtime.      |
| `winrm`   | `pywinrm`                                 | Using `WinRMCommunicator` at runtime.    |
| `repl`    | `ipython`                                 | Nicer prompt for `testrange repl`.       |

System-level prerequisites for actually running VMs (not needed for
pure unit tests): `libvirt-daemon-system`, `qemu-system-x86`, `ovmf`.

## Running the test suite

TestRange ships a pytest test suite that mocks libvirt, pycdlib,
passlib, paramiko, and pywinrm so it runs anywhere — no libvirt
daemon, no KVM module, no network.

```bash
pytest                              # run everything
pytest tests/test_vm_libvirt.py     # one file
pytest -k cleanup                   # pattern match on test name
pytest -x --ff                      # stop at first failure, run failed first
pytest --cov=testrange              # with coverage (pytest-cov from [dev])
```

Suite layout is documented in `tests/tests.md` — one paragraph per
file describing scope. Register a new bug fix as a regression test:
add the assertion alongside the code it guards, then summarise it in
`tests/tests.md`.

## Linting with ruff

[`ruff`](https://docs.astral.sh/ruff/) handles **linting only** in this
repo; formatting is owned by `black`. Configuration lives in
`pyproject.toml` under `[tool.ruff]`:

- `line-length = 100`, `target-version = "py311"`.
- Rules enabled: `E` (pycodestyle errors), `F` (pyflakes), `I`
  (import order), `UP` (`pyupgrade` modernisations), `B` (bugbear
  pitfalls), `SIM` (simplifications).
- `E501` (line length) is ignored because `black` handles wrapping;
  `SIM105` is ignored because `try/except/pass` beats
  `contextlib.suppress` for short teardowns.

```bash
ruff check .                        # lint the whole tree
ruff check testrange/_cli.py        # one file
ruff check --fix .                  # auto-fix where ruff is confident
ruff check --select I --fix .       # just import ordering
```

Known pre-existing ruff/mypy findings live in `testrange/_cli.py` in
the `_print_test` function — ignore them when working in unrelated
areas (see `memory/project_tooling_debt.md` if you're using the agent
memory system).

## Formatting with black

```bash
black .                             # format in-place
black --check .                     # fail if anything is unformatted
```

`black` config is in `pyproject.toml` under `[tool.black]`:
`line-length = 100`, `target-version = ["py311", "py312", "py313"]`.
Matches ruff's line length so the two tools agree.

## Type checking with mypy

```bash
mypy testrange                      # strict mode is on in pyproject.toml
```

`mypy` config: `strict = true`, `ignore_missing_imports = true` (so
libvirt / pycdlib / passlib stubs aren't required on dev machines).
When adding a new module, prefer explicit `from __future__ import
annotations` + PEP 604 unions (`str | None`) over `Optional[str]`; the
existing code is uniform on that.

## Building the docs

The Sphinx site lives in `docs/` with the `furo` theme.

```bash
cd docs
make html                           # build into docs/_build/html
make clean                          # nuke docs/_build/
make linkcheck                      # validate external links
```

Open `docs/_build/html/index.html` in a browser. If you're editing
docstrings, the autodoc extension re-renders them on rebuild — no
separate manual step.

To host locally during editing:

```bash
cd docs/_build/html
python3 -m http.server 8000
```

Then browse to `http://localhost:8000`.

### Doc structure

- `docs/index.rst` — landing page.
- `docs/usage/` — narrative docs (quickstart, VMs, networks, caching,
  Windows, debugging, extending, etc.).
- `docs/api/` — autodoc-backed reference pages, one per major
  subsystem.
- `docs/changelog.rst` — notable changes, newest first.

New classes / public functions don't need a manual doc entry if they
live in an already-documented module; `.. autoclass::` picks them up.
New *modules* need either a fresh `.. automodule::` in an existing
`.rst` or a new `.rst` wired into the relevant `index.rst` toctree.

## Running real VMs

Unit tests cover behaviour against mocks; to shake out libvirt /
OVMF / network wiring you have to run a real orchestrator. The
`examples/` directory has working spec files you can run:

```bash
testrange run examples/hello_world.py:gen_tests
testrange run --log-level DEBUG examples/cross_network_dns.py:gen_tests
testrange describe examples/two_networks_three_vms.py:gen_tests   # preview only
testrange repl examples/hello_world.py:gen_tests                  # interactive
```

See `docs/usage/examples.rst` for a walkthrough of every example.

**First Windows run.** `examples/winrm_communicator.py` needs an ISO
on disk:

```bash
TESTRANGE_WIN_ISO=$(realpath Win10_21H1_English_x64.iso) \
    testrange run --log-level DEBUG examples/winrm_communicator.py:gen_tests
```

First run downloads `virtio-win.iso` (~800 MiB) and sits through
Windows Setup (15–30 minutes). Cache-warm runs are fast.

## Pre-commit sanity loop

Before pushing:

```bash
ruff check . && black --check . && mypy testrange && pytest && \
    (cd docs && make html)
```

All four tools are silent on success.

## Releasing

Versions live in `testrange/_version.py`. Bump both `__version__` and
`__version_info__`, update `docs/changelog.rst` (move "Unreleased"
content under a dated heading), tag the commit, and `python -m build`.
