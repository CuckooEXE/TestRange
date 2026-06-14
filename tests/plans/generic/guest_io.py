"""generic/guest_io: the SSH transport contract — byte fidelity, cwd, timeouts, reconnect.

WHAT: one SSH-reached guest exercising the parts of the ``Communicator``
contract no other plan touches on this transport: a multi-megabyte binary
``write_file``/``read_file`` round-trip over SFTP (a non-periodic payload,
verified by guest-side sha256 *and* readback equality, so truncation AND
chunk-reordering both change the digest), ``execute(cwd=)``, nonzero exit codes
with the command's own diagnostic on stderr, a hung (silent) command exceeding
``timeout=`` raising ``CommunicatorError`` after roughly the requested bound
(COMM-6), file-op failures surfacing as ``CommunicatorError`` pinned to their
path (COMM-9), and ``close()`` being non-terminal — proven by connection
identity, and proven through the gateway on jump-reached backends (PROXY-3).

WHY: SFTP file I/O is called by zero plans (binary coverage exists only on the
native channel, and only to 256 KiB), so a truncating or corrupting transfer
path would certify green today; the hung-command bound and the error-wrapping
boundary are exactly the kind of contract that silently regresses until a test
hangs forever in CI.

SSH transport — not applicable on ESXi, whose host cannot jump into internal
guests (ESXI-30); like the other SSH generic plans, certify it on the
SSH-capable backends::

    testrange run --profile <name> tests/plans/generic/guest_io.py
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.exceptions import CommunicatorError
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import SSHKey

_KEY = SSHKey.generate(comment="testrange-guest-io")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
pool1 = hyp.add_pool(StoragePool("pool1", 16))
hyp.add_switch(
    Switch(
        "lab",
        Network("lab-net"),
        cidr="10.65.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
lab_net = hyp.networks["lab-net"]
hyp.vm(
    "iobox",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(lab_net, DHCPAddr())],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
    ),
    communicator=SSHCommunicator("admin"),
)

PLAN = Plan("guest-io", hyp)


def sftp_round_trips_a_multimegabyte_payload(orch: OrchestratorHandle) -> None:
    com = orch.vms["iobox"].communicator
    # 4 MiB of a counter-mode hash stream: deterministic but NON-periodic, so a
    # transfer path that reorders, duplicates, or mis-offsets 256-aligned chunks
    # (SFTP moves 32 KiB blocks) changes the digest — a periodic payload would
    # be byte-identical under exactly those faults.
    blob = b"".join(hashlib.sha256(i.to_bytes(8, "big")).digest() for i in range(131072))
    assert len(blob) == 4 * 1024 * 1024
    com.write_file("/home/admin/blob.bin", blob)
    r = com.execute(["sha256sum", "/home/admin/blob.bin"], timeout=60.0)
    assert r.ok, f"guest-side hash failed: {r}"
    guest_sha = r.stdout.decode().split()[0]
    assert guest_sha == hashlib.sha256(blob).hexdigest(), "guest received different bytes"
    readback = com.read_file("/home/admin/blob.bin")
    assert readback == blob, f"readback corrupted: wrote {len(blob)} bytes, read {len(readback)}"


def execute_honours_cwd(orch: OrchestratorHandle) -> None:
    r = orch.vms["iobox"].communicator.execute(["pwd"], cwd="/tmp")
    assert r.stdout.strip() == b"/tmp", f"cwd not honoured: {r.stdout!r}"


def nonzero_exit_and_stderr_are_captured(orch: OrchestratorHandle) -> None:
    r = orch.vms["iobox"].communicator.execute(["ls", "/no/such/path"])
    assert not r.ok and r.exit_code != 0, f"failing command reported ok: {r}"
    # Pin the capture to THIS command's diagnostic (ls echoes the offending
    # path) and to stream separation — constant stderr noise must not satisfy
    # the assert, and the diagnostic must not have leaked into stdout.
    assert b"/no/such/path" in r.stderr, f"stderr lacks the command's diagnostic: {r.stderr!r}"
    assert not r.stdout, f"ls of a missing path wrote to stdout: {r.stdout!r}"


def hung_command_times_out_on_the_callers_clock(orch: OrchestratorHandle) -> None:
    com = orch.vms["iobox"].communicator
    start = time.monotonic()
    raised: CommunicatorError | None = None
    try:
        com.execute(["sleep", "30"], timeout=5.0)
    except CommunicatorError as e:
        raised = e
    elapsed = time.monotonic() - start
    assert raised is not None, "a silent command outliving timeout= must raise CommunicatorError"
    assert "sleep" in str(raised), f"raise not pinned to the timed-out command: {raised}"
    # Both bounds matter: an implementation that rejects timeout= instantly
    # (elapsed≈0) and one that ignores it until the command ends (elapsed≈30)
    # are each broken in a different way.
    assert 4.5 <= elapsed < 20.0, f"5s budget enforced at {elapsed:.1f}s — not the caller's clock"
    assert com.execute(["true"]).ok, "channel unusable after a timed-out command"


def file_errors_surface_as_communicator_errors(orch: OrchestratorHandle) -> None:
    com = orch.vms["iobox"].communicator
    read_err: CommunicatorError | None = None
    try:
        com.read_file("/no/such/file")
    except CommunicatorError as e:
        read_err = e
    assert read_err is not None, "read_file of a missing path must raise CommunicatorError"
    assert "/no/such/file" in str(read_err), f"raise not pinned to the path: {read_err}"

    write_err: CommunicatorError | None = None
    try:
        com.write_file("/root/forbidden", b"x")
    except CommunicatorError as e:
        write_err = e
    assert write_err is not None, "write_file to a forbidden path must raise CommunicatorError"
    assert "/root/forbidden" in str(write_err), f"raise not pinned to the path: {write_err}"


def close_is_not_terminal(orch: OrchestratorHandle) -> None:
    # Last on purpose: if close() ever regresses to terminal (the PROXY-3
    # gateway latch was exactly that), the failure must not cascade into the
    # other contract certs. Connection identity pins that a genuinely NEW
    # connection was made — on one SSH transport every exec shares the client
    # port, so an unchanged port would mean close() never actually closed.
    com = orch.vms["iobox"].communicator
    before = com.execute(["sh", "-c", "echo $SSH_CONNECTION"]).stdout.split()
    com.close()
    r = com.execute(["sh", "-c", "echo $SSH_CONNECTION"])
    assert r.ok, "communicator did not reconnect after close()"
    after = r.stdout.split()
    assert before and after and before != after, (
        f"same connection identity across close() — never actually closed: {before} == {after}"
    )


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    sftp_round_trips_a_multimegabyte_payload,
    execute_honours_cwd,
    nonzero_exit_and_stderr_are_captured,
    hung_command_times_out_on_the_callers_clock,
    file_errors_surface_as_communicator_errors,
    close_is_not_terminal,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
