Communication
=============

Every interaction with a running VM flows through a
:class:`~testrange.communication.base.AbstractCommunicator`.  This
page covers the three shipped backends, the primitives they expose,
and the file helpers layered on top.

The default: QEMU Guest Agent
-----------------------------

Linux VMs default to
:class:`~testrange.backends.libvirt.GuestAgentCommunicator`,
which speaks the QEMU Guest Agent JSON protocol over a
``virtio-serial`` channel.  This has three useful properties:

1. **No TCP exposure.**  The agent doesn't listen on any port — host
   and guest talk through a Unix socket managed by libvirt.  Tests
   can assert network isolation without losing the ability to inspect
   the isolated VMs.

2. **Works before DHCP.**  The channel is up as soon as the kernel
   initialises the virtio drivers, which happens well before network
   config.  Early-boot debugging works without guessing at IPs.

3. **Distribution-agnostic.**  The same JSON commands work on Debian,
   Ubuntu, Fedora, Rocky, Alpine, Arch.  TestRange doesn't need to
   know which shell, which sudo config, or which service manager the
   guest uses.

The tradeoff is that the agent exposes a fixed command set
(``guest-exec``, ``guest-file-open/read/write/close``, ``guest-ping``,
etc.).  No streaming stdin, no pseudo-TTY, no signal delivery beyond
``kill``.  If your test needs those, fall back to SSH.

Running commands
----------------

:meth:`~testrange.vms.base.AbstractVM.exec` takes an argv list and
returns :class:`~testrange.communication.base.ExecResult`:

.. code-block:: python

    r = vm.exec(["systemctl", "is-active", "nginx"])

    r.exit_code        # int
    r.stdout           # bytes
    r.stderr           # bytes
    r.stdout_text      # UTF-8 decoded, replacement-on-error
    r.stderr_text      # same
    r.check()          # raises if exit_code != 0

``ExecResult`` is a NamedTuple, so destructuring is natural:

.. code-block:: python

    exit_code, stdout, stderr = vm.exec(["whoami"])

Environment variables can be passed explicitly:

.. code-block:: python

    vm.exec(
        ["my-tool", "--config=/etc/m.yml"],
        env={"LOG_LEVEL": "debug"},
        timeout=30,
    )

File operations
---------------

The primitives are bytes in, bytes out:

.. code-block:: python

    contents = vm.get_file("/etc/os-release")    # bytes
    vm.put_file("/tmp/payload", b"\\x00\\x01...")  # bytes

On top of those, four ergonomic helpers:

.. code-block:: python

    # Text round-trip — UTF-8 by default, encoding keyword optional
    motd = vm.read_text("/etc/motd")
    vm.write_text("/tmp/note.txt", "hello\\n")

    # Host ↔ VM file copies
    vm.download("/var/log/syslog", tmp_path / "syslog")  # VM → host
    vm.upload(local_config, "/etc/myapp.conf")           # host → VM

``download`` auto-creates the destination's parent directory and
returns the resolved :class:`~pathlib.Path`.  ``upload`` validates the
host file exists before touching the VM — if it doesn't, you get
:class:`FileNotFoundError` with no half-done transfer on the guest.

Guest agent readiness
---------------------

The orchestrator calls
:meth:`~testrange.backends.libvirt.GuestAgentCommunicator.wait_ready`
once per VM after ``domain.create()``, polling ``guest-ping`` until
the channel opens.  Libvirt's stderr warnings during that window
(``Guest agent is not responding``) are suppressed only for the
duration of the poll; real errors from other code paths still print
normally.

You can see the readiness timing in the ``testrange`` logs::

    wait for guest agent on 'webpublic' ...
    wait for guest agent on 'webpublic' done in 9.0s

For long-running setups where the agent takes longer than the default
300 s to come up, watch those log lines — they're the source of truth
for whether a VM is hung or just slow.

Fallbacks: SSH and WinRM
-------------------------

:class:`~testrange.communication.ssh.SSHCommunicator` is available
for cases where the guest agent isn't an option (e.g. custom base
images without ``qemu-guest-agent`` installed) or when a test wants
to exercise the real network stack.  It needs a reachable IP, plus
either a provisioned SSH key (``key_filename=``) or the account's
password (``password=``).  Password auth on non-root users works
out of the box: cloud-init's ``ssh_pwauth: True`` is set both at
install and at every run-phase boot, and phase-2 user-data
re-asserts ``lock_passwd: False`` so the password doesn't get
re-locked between reboots.

See ``examples/ssh_communicator.py`` for an end-to-end example that
opens both a key-auth and a password-auth session to the same VM.

Selecting the backend
~~~~~~~~~~~~~~~~~~~~~

Pass ``communicator=`` to :class:`~testrange.backends.libvirt.VM` to pick
the backend the orchestrator wires up on start.  Three values are
accepted:

- ``"guest-agent"`` — virtio-serial; requires ``qemu-guest-agent`` in
  the image.  Cloud-init-built Linux VMs get it for free; Windows VMs
  get it once the autounattend FirstLogonCommands install the MSI
  from the virtio-win ISO.
- ``"ssh"`` — uses the first
  :class:`~testrange.devices.vNIC` with a static ``ip=``
  as the host, and the first :class:`~testrange.credentials.Credential`
  as the login (preferring one with an ``ssh_key`` set).
- ``"winrm"`` — WinRM on HTTP 5985 with basic auth.  The ``root``
  credential is mapped to the built-in ``Administrator`` account
  (matching the :class:`~testrange.vms.builders.WindowsUnattendedBuilder`
  convention); if there is no ``root`` credential the first
  credential is used as-is.

Defaults:

- Linux images (cloud qcow2, URL, or local .qcow2/.img) →
  ``"guest-agent"``.
- Windows install ISOs (detected by
  :func:`~testrange.vms.images.is_windows_image`) → ``"winrm"``.
- BYOI (``builder=NoOpBuilder()``) → ``"guest-agent"`` by default,
  or ``"winrm"`` when ``NoOpBuilder(windows=True)``.  Pass
  ``communicator="ssh"`` explicitly if your prebuilt Linux image
  doesn't ship the guest agent.

``"ssh"`` and ``"winrm"`` raise
:class:`~testrange.exceptions.VMBuildError` at start time if no
:class:`~testrange.devices.vNIC` carries a static
``ip=``.  v1 doesn't do DHCP-lease discovery — the resolver hook
(``_resolve_communicator_host``) is factored out so that's a
drop-in addition later.

All three backends return the same
:class:`~testrange.communication.base.ExecResult` shape, so test
code is portable across guests.
