"""Talk to a VM over SSH instead of the QEMU guest agent.

TestRange's default :class:`~testrange.communication.guest_agent.GuestAgentCommunicator`
speaks to the VM over a virtio-serial channel — no network involved.
:class:`~testrange.communication.ssh.SSHCommunicator` is an alternative
that exercises the real network stack, which is useful when a test
wants to assert that SSH itself works (or when integrating with tooling
that expects a real SSH endpoint).

The orchestrator still provisions the VM via cloud-init and the guest
agent; the SSH communicator is constructed by hand inside the test
function and used directly.  Both backends are active at the same
time — this example asserts that they return the same hostname.

Auth — both paths
-----------------
This example opens **two** SSH sessions to the same VM to exercise
both authentication paths:

- Public-key: an ephemeral ed25519 pair is generated at
  ``gen_tests()`` time, the pub goes on :class:`Credential`, and
  the priv is handed to :class:`SSHCommunicator` via
  ``key_filename=``.
- Password: the :class:`Credential`'s plaintext password is re-used
  against the same VM via ``password=``.  (Password auth on non-root
  accounts works because cloud-init sets ``ssh_pwauth: True`` and
  phase-2 cloud-init re-asserts ``lock_passwd: False``.)

Key contents are deliberately excluded from the VM config cache hash
(see :func:`~testrange.cache.vm_config_hash`), so rotating the key on
every run does not invalidate the cached post-install snapshot.

Networking
----------
The VM is pinned to a static IP on a libvirt bridge with
``internet=True`` — the libvirt host sits at ``.1`` of the bridge
subnet so the test process can reach ``10.30.0.10:22`` directly
without any port-forward.

Run with::

    testrange run examples/ssh_communicator.py:gen_tests
"""

from __future__ import annotations

import atexit
import subprocess
import tempfile
from pathlib import Path

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
from testrange.communication.ssh import SSHCommunicator


def _ephemeral_ssh_key() -> tuple[Path, str]:
    """Generate an ed25519 key pair in a temp dir and return (priv_path, pub_text).

    The temp dir is scheduled for removal at process exit.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="testrange-ssh-"))
    atexit.register(lambda: subprocess.run(["rm", "-rf", str(tmpdir)], check=False))

    priv = tmpdir / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(priv)],
        check=True,
    )
    priv.chmod(0o600)
    pub = (priv.with_suffix(".pub")).read_text().strip()
    return priv, pub


# Generate once per import so the Credential used at describe-time and the
# SSHCommunicator used at test-time point at the same key.
_PRIV_KEY, _PUB_KEY = _ephemeral_ssh_key()


def _exercise(ssh: SSHCommunicator, agent_vm: object) -> None:
    """Run the full exec / hostname / env / SFTP / sudo battery over *ssh*.

    Factored out so the same assertions run against both the key-auth and
    the password-auth sessions.
    """
    # 1. exec — exit code + stdout capture over a real SSH channel.
    uname = ssh.exec(["uname", "-s"])
    assert uname.exit_code == 0, uname.stderr
    assert uname.stdout.decode().strip() == "Linux"

    # 2. hostname() convenience matches what the guest agent reports.
    assert ssh.hostname() == "ssh-demo"
    assert ssh.hostname() == agent_vm.hostname()  # type: ignore[attr-defined]

    # 3. env= is threaded through as `env K=V ...` on the remote side,
    #    so it works without fiddling with sshd's AcceptEnv.
    greeting = ssh.exec(
        ["sh", "-c", 'printf %s "$GREETING"'],
        env={"GREETING": "shibboleth"},
    )
    assert greeting.stdout.decode() == "shibboleth"

    # 4. SFTP round-trip via put_file / get_file.
    payload = bytes(range(256))  # every byte value once
    ssh.put_file("/tmp/canary.bin", payload)
    assert ssh.get_file("/tmp/canary.bin") == payload

    # 5. Sudo works because the deploy Credential was created with sudo=True.
    whoami = ssh.exec(["sudo", "-n", "whoami"])
    assert whoami.exit_code == 0, whoami.stderr
    assert whoami.stdout.decode().strip() == "root"


def ssh_round_trip(orch: Orchestrator) -> None:
    agent_vm = orch.vms["ssh-demo"]

    # --- public-key auth --------------------------------------------------
    ssh_key = SSHCommunicator(
        host="10.30.0.10",
        username="deploy",
        key_filename=_PRIV_KEY,
    )
    try:
        # Poll until sshd accepts our key.  Cloud-init finished before the
        # orchestrator returned, but sshd may still be regenerating host
        # keys on first boot.
        ssh_key.wait_ready(timeout=60)
        _exercise(ssh_key, agent_vm)
    finally:
        ssh_key.close()

    # --- password auth ----------------------------------------------------
    ssh_pw = SSHCommunicator(
        host="10.30.0.10",
        username="deploy",
        password="testrange-ssh-demo",
    )
    try:
        # sshd is already up from the previous session, so this is fast.
        ssh_pw.wait_ready(timeout=30)
        _exercise(ssh_pw, agent_vm)
    finally:
        ssh_pw.close()


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "SSHNet",
                        "10.30.0.0/24",
                        dhcp=False,      # force deterministic IP
                        internet=True,   # host <-> VM route via the bridge
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="ssh-demo",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[
                            Credential("root", "testrange-ssh-demo"),
                            Credential(
                                "deploy",
                                "testrange-ssh-demo",
                                ssh_key=_PUB_KEY,
                                sudo=True,
                            ),
                        ],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("SSHNet", ip="10.30.0.10"),
                        ],
                    ),
                ],
            ),
            ssh_round_trip,
            name="ssh-communicator",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
