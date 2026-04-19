"""Tour of :meth:`VM.exec` features: env vars, timeouts, exit codes, stderr.

``exec`` is intentionally a NamedTuple-returning wrapper over the
guest agent's ``guest-exec`` call.  Tests can assert on ``exit_code``,
raise with ``.check()``, decode stdout/stderr with ``stdout_text`` /
``stderr_text``, and control execution with ``env=`` / ``timeout=``.

This example touches every one of those surfaces.

Run with::

    testrange run examples/exec_features.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    VirtualNetworkRef,
    run_tests,
    vCPU,
)
from testrange.exceptions import VMTimeoutError


def exec_tour(orch: Orchestrator) -> None:
    vm = orch.vms["box"]

    # 1. Simple success: exit 0, stdout captured, stderr empty.
    r = vm.exec(["echo", "hi"])
    assert r.exit_code == 0
    assert r.stdout_text == "hi\n"
    assert r.stderr_text == ""

    # 2. Non-zero exit with stderr output.
    r = vm.exec(["sh", "-c", "echo boom >&2; exit 3"])
    assert r.exit_code == 3
    assert r.stderr_text.strip() == "boom"

    # 3. .check() raises on non-zero.
    try:
        vm.exec(["false"]).check()
    except RuntimeError as exc:
        assert "exit_code=1" in str(exc) or "1" in str(exc), str(exc)
    else:
        raise AssertionError("check() should have raised on exit 1")

    # 4. Environment variables are injected.
    r = vm.exec(
        ["sh", "-c", "printf %s \"$CUSTOM_TOKEN\""],
        env={"CUSTOM_TOKEN": "shibboleth"},
    )
    r.check()
    assert r.stdout_text == "shibboleth"

    # 5. timeout= kills the host-side wait loop and raises.
    #    The guest process keeps running; the host simply stops waiting.
    try:
        vm.exec(["sleep", "10"], timeout=2)
    except VMTimeoutError:
        pass
    else:
        raise AssertionError("expected VMTimeoutError for 10-second sleep with 2s cap")


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.17.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="box",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),  # 10 GiB OS disk
                            VirtualNetworkRef("Net"),
                        ],
                    ),
                ],
            ),
            exec_tour,
            name="exec-features",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
