"""Run multiple tests in parallel with ``concurrency=N``.

TestRange serializes by default — ``run_tests`` runs one test at a time
so CI logs stay readable.  For larger suites, pass ``concurrency=N`` to
dispatch up to ``N`` tests to a thread pool.

Safety constraints (see ``run_tests`` docstring for full detail):

- **Install-phase subnets are auto-serialized.**  The orchestrator
  picks from a pool (``192.168.240.0/24`` – ``192.168.254.0/24``) under
  a cross-process file lock, so two concurrent install phases will
  never collide.
- **User-declared ``VirtualNetwork`` subnets are NOT auto-rewritten.**
  Each parallel test must declare its own non-overlapping subnets, or
  the second libvirt network define will fail.
- **Results come back in input order**, regardless of which test
  finished first.

This example runs three tests on three non-overlapping subnets,
verifies each one's hostname, and proves that results are returned in
input order even when later tests finish earlier than earlier ones.

Run with::

    testrange run examples/concurrency.py:gen_tests -j 3
"""

from __future__ import annotations

import time

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)


def _check(expected_hostname: str, sleep_seconds: float):
    """Return a test function that sleeps then asserts on its VM's hostname.

    The sleep stretches out the test window so the concurrency is
    observable in the log timeline: without it, every test finishes in
    ~1 second and there's no visible overlap.
    """

    def _run(orch: Orchestrator) -> None:
        vm = orch.vms[expected_hostname]
        assert vm.hostname() == expected_hostname
        time.sleep(sleep_seconds)

    _run.__name__ = f"check_{expected_hostname}"
    return _run


def _vm(name: str, subnet: str) -> tuple[VirtualNetwork, VM]:
    """Each parallel test needs its own non-overlapping subnet."""
    net = VirtualNetwork(f"Net-{name}", subnet, internet=True)
    vm = VM(
        name=name,
        iso=(
            "https://cloud.debian.org/images/cloud/bookworm/latest/"
            "debian-12-generic-amd64.qcow2"
        ),
        users=[Credential("root", "testrange")],
        devices=[
            vCPU(1),
            Memory(1),
            HardDrive(10),
            vNIC(f"Net-{name}"),
        ],
    )
    return net, vm


def gen_tests() -> list[Test]:
    tests: list[Test] = []
    # Deliberately decreasing sleeps so test[2] finishes before test[0]
    # in wall-clock, but results still come back in input order.
    for name, subnet, sleep_s in [
        ("alpha",   "10.20.1.0/24", 3.0),
        ("bravo",   "10.20.2.0/24", 2.0),
        ("charlie", "10.20.3.0/24", 1.0),
    ]:
        net, vm = _vm(name, subnet)
        tests.append(
            Test(
                Orchestrator(networks=[net], vms=[vm]),
                _check(name, sleep_s),
                name=f"concurrency-{name}",
            )
        )
    return tests


if __name__ == "__main__":
    import sys
    results = run_tests(gen_tests(), concurrency=3)
    sys.exit(0 if all(r.passed for r in results) else 1)
