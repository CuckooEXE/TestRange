# TestRange

A pip-installable Python package for orchestrating hypervisor-backed
virtual-machine environments for integration testing, version-
compatibility testing, and anything else that needs a real OS.

Each shipped hypervisor backend is a peer implementation of the same
abstract surface — pick one and the rest of your test code stays
hypervisor-neutral.  See `testrange.backends` for the full list and
each backend's prerequisites.

```python
from testrange import (
    Test, Orchestrator, VM, VirtualNetwork, Credential, Apt, vCPU, Memory,
    vNIC, HardDrive, run_tests,
)

def smoke(orch):
    web = orch.vms["web"]
    assert b"nginx" in web.exec(["systemctl", "status", "nginx"]).stdout

def gen_tests():
    return [Test(
        Orchestrator(
            networks=[VirtualNetwork("Net", "10.0.1.0/24", internet=True, dhcp=True)],
            vms=[VM(
                name="web",
                iso="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
                users=[Credential("root", "Password123!")],
                pkgs=[Apt("nginx")],
                post_install_cmds=["systemctl enable --now nginx"],
                devices=[vCPU(2), Memory(2), vNIC("Net"), HardDrive(20)],
            )],
        ),
        smoke,
    )]

if __name__ == "__main__":
    run_tests(gen_tests())
```

## Install

Each backend has its own host-side prerequisites — see the matching
backend module's docstring under `testrange.backends` for the
package list, daemon-enable commands, and group memberships your
host needs.  Skip these if you only want to run the unit tests.

Then the package itself:

```bash
pip install -e ".[ssh]"
```

Optional extras (combine with commas — `pip install -e ".[ssh,winrm]"`):

| Extra   | When you need it                                  |
| ------- | ------------------------------------------------- |
| `ssh`   | Using `SSHCommunicator` at runtime                |
| `winrm` | Using `WinRMCommunicator` for Windows VMs         |
| `repl`  | Nicer prompt for `testrange repl`                 |
| `docs`  | Building the Sphinx documentation (below)         |
| `dev`   | Running the test suite + linters                  |

See [`DEVELOPMENT.md`](DEVELOPMENT.md) for the full development
workflow (running tests, linting, type-checking, contributing).

## Run a test

```bash
testrange run ./my_tests.py:gen_tests
```

`MODULE[:FACTORY]` accepts either a path to a Python file or a dotted
module name; `FACTORY` defaults to `gen_tests`.  Other useful commands:

```bash
testrange describe ./my_tests.py:gen_tests   # preview without booting
testrange repl     ./my_tests.py:gen_tests   # interactive after provisioning
testrange cleanup  ./my_tests.py:gen_tests <runid>   # SIGKILL recovery
testrange cache-list                         # what's in the local cache
```

## Build the documentation

The Sphinx source lives in `docs/`.  Install the docs extra and run
`make html`:

```bash
pip install -e ".[docs]"
cd docs
make html        # output lands in docs/_build/html/
```

Open `docs/_build/html/index.html` in a browser, or serve the tree
locally:

```bash
python -m http.server -d docs/_build/html
```

`make clean` removes the entire `_build/` directory if you want a
fresh rebuild.

## License

MIT.
