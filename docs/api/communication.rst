Communication
=============

Every runtime interaction with a VM — ``exec``, ``hostname``,
``get_file``/``put_file`` and the helpers built on top of them —
flows through an
:class:`~testrange.communication.base.AbstractCommunicator`.  Three
concrete backends ship in-tree:

:class:`~testrange.backends.libvirt.GuestAgentCommunicator`
    The default for Linux guests.  Speaks QEMU Guest Agent JSON over
    a ``virtio-serial`` channel.  Requires no TCP exposure from the
    VM, which is why it works on fully isolated networks.

:class:`~testrange.communication.ssh.SSHCommunicator`
    Fallback for situations where the guest-agent channel isn't
    available.  Requires a reachable IP and a provisioned SSH key.

:class:`~testrange.communication.winrm.WinRMCommunicator`
    Default for Windows guests.  Uses WinRM over HTTP (5985) with
    credentials baked in by the unattend builder.

The communicator is selected by the VM subclass at
``start_run`` time; library users rarely touch it directly.

Why guest agent?
----------------

The guest agent gives us three things SSH can't:

1. **Works before network is up.**  Tests can exec during the tiny
   window where the VM is booted but DHCP hasn't handed out a lease
   yet.  Useful for debugging cloud-init failures.

2. **No port exposure.**  Guests on ``internet=False`` networks are
   fully unreachable from the host (and everywhere else), but we can
   still talk to them.  This matters for tests that specifically
   assert network isolation.

3. **Consistent semantics across distros.**  The guest agent protocol
   is the same on Debian, Ubuntu, Fedora, Rocky, Alpine, etc.  We
   don't have to care which shell, which sudo config, or which
   systemd service quirks the guest has.

The tradeoff is that the agent only speaks a fixed command set
(``guest-exec``, ``guest-file-*``, ``guest-ping``, etc.) — there's
no streaming stdin, no pseudo-TTY, no signal delivery beyond kill.
For anything that needs those, use SSH.

ExecResult
----------

All three communicators return
:class:`~testrange.communication.base.ExecResult` — a NamedTuple with
``exit_code``, ``stdout``, ``stderr``.  Common patterns:

.. code-block:: python

    result = vm.exec(["uname", "-r"])
    result.check()                     # raise if exit_code != 0
    kernel = result.stdout_text.strip()

    # Assert message on failure, returning captured stderr:
    r = vm.exec(["curl", "-f", "https://example.com"])
    assert r.exit_code == 0, r.stderr_text

Readiness
---------

:meth:`~testrange.backends.libvirt.GuestAgentCommunicator.wait_ready`
polls ``guest-ping`` until the channel opens, with libvirt's default
"agent not connected" stderr noise suppressed during the poll loop
(real errors from other code paths are unaffected).  The orchestrator
calls this once per VM after ``domain.create()``; tests never need
to invoke it.

Reference
---------

.. autoclass:: testrange.communication.base.ExecResult
   :members:
   :show-inheritance:

.. autoclass:: testrange.communication.base.AbstractCommunicator
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.libvirt.GuestAgentCommunicator
   :members:
   :show-inheritance:

.. autoclass:: testrange.communication.ssh.SSHCommunicator
   :members:
   :show-inheritance:

.. autoclass:: testrange.communication.winrm.WinRMCommunicator
   :members:
   :show-inheritance:
