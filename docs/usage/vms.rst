Virtual Machines
================

This page walks through building a VM spec end-to-end: picking an
image, configuring users and packages, describing hardware, and
talking to the VM once it's booted.  Examples use Linux guests; the
Windows build path differs in several ways and has its own page
(:doc:`windows`).  Anything in this document marked **Linux** does
not apply to Windows VMs.

Picking an image
----------------

The ``iso=`` argument accepts three things:

1. **An absolute path to a local ``.qcow2`` / ``.img``** —
   ``"/srv/images/my-golden.qcow2"``.  Useful for custom or
   pre-hardened Linux bases.  TestRange will not modify the file; it
   will create an overlay during the install phase and copy the
   resulting post-install image into the cache.

2. **An ``https://`` URL pointing at a cloud image** — downloaded
   once and cached under ``<cache_root>/images/`` keyed by the URL
   hash; subsequent VMs that use the same URL skip the download.
   Linux only — Microsoft does not publish stable Windows image URLs.

3. **An absolute path to a Windows install ``.iso``** — triggers the
   Windows install path (autounattend, OVMF, virtio-win).  See
   :doc:`windows` for the details.

Common upstream cloud-image URLs (one-off lookups; TestRange doesn't
track them):

- Debian 12 — ``https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2``
- Ubuntu 24.04 — ``https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img``
- Fedora 40 — ``https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2``
- Rocky 9 — ``https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2``

Builders
--------

Every VM has a :class:`~testrange.vms.builders.base.Builder`: a
strategy object that encodes how the VM gets from ``iso=`` to a
runnable disk image.  Three concrete builders ship today:

- :class:`~testrange.vms.builders.CloudInitBuilder` — boots a Linux
  cloud image under a NoCloud seed ISO and lets cloud-init customise
  it.  Default for ``.qcow2`` / ``.img`` / ``https://`` inputs.
- :class:`~testrange.vms.builders.WindowsUnattendedBuilder` — boots a
  Windows installer with an autounattend seed and lets Setup +
  FirstLogonCommands run to completion.  Default for ``.iso`` inputs
  that look like Windows install media (see :doc:`windows`).
- :class:`~testrange.vms.builders.NoOpBuilder` — no install phase;
  the user's qcow2 is already ready (BYOI, see :ref:`byoi`).

The builder is auto-selected from ``iso=`` when you don't pass one.
Override with ``builder=`` when you need to:

.. code-block:: python

    VM(
        iso="/srv/golden/debian.qcow2",
        users=[...],
        # No install phase — stage the qcow2 into the cache and boot it.
        builder=NoOpBuilder(),
    )

    VM(
        iso="/srv/iso/Win10.iso",
        users=[...],
        # Override the locale / product key the autounattend uses.
        builder=WindowsUnattendedBuilder(
            ui_language="de-DE",
            timezone="W. Europe Standard Time",
        ),
    )

Subclass :class:`~testrange.vms.builders.base.Builder` to support a new
install pipeline (preseed, Kickstart, Ignition, sysprep'd Windows…)
without touching the VM or orchestrator code — they only speak to the
builder through the abstract interface.

.. _byoi:

Bring your own image (BYOI)
---------------------------

For images you already build elsewhere — Packer, Buildroot, a manual
golden build — pass ``builder=NoOpBuilder()`` alongside the local path.
TestRange skips the install phase entirely: no users are created, no
packages installed, no post-install commands run.  The qcow2 is staged
into the cache and booted from an overlay as-is.

.. code-block:: python

    VM(
        name="byoi",
        iso="/var/tmp/my-golden.qcow2",
        builder=NoOpBuilder(),          # no install phase
        communicator="ssh",
        users=[Credential("deploy", "alreadyset", sudo=True)],
        devices=[
            vCPU(2), Memory(4), HardDrive(40),
            VirtualNetworkRef("Net", ip="10.40.0.10"),
        ],
    )

Rules for the :class:`NoOpBuilder`:

- ``pkgs`` and ``post_install_cmds`` are silently ignored — the builder
  has no install phase to plug them into.
- ``iso`` must be a local path (absolute or ``~``-relative).  Use the
  cloud-init builder with an ``https://`` URL if you want TestRange to
  download instead.
- ``users=[...]`` is *informational*.  TestRange does not create those
  accounts — they must already exist in the image.  The credentials
  are forwarded to the selected communicator so
  :meth:`~testrange.vms.base.AbstractVM.exec` and the file helpers
  continue to work.
- ``communicator="ssh"`` and ``communicator="winrm"`` (see
  :doc:`communication`) require at least one
  :class:`~testrange.devices.VirtualNetworkRef` with a static ``ip=``.
  The orchestrator registers that IP as a libvirt DHCP reservation on
  the MAC it assigns the NIC, so the VM comes up at the address you
  declared.
- ``communicator="guest-agent"`` (the Linux default) still works if
  you installed ``qemu-guest-agent`` in the image — same interface as
  a cloud-init VM, no network required.
- Pass ``NoOpBuilder(windows=True)`` for a pre-built Windows qcow2:
  the run-phase domain gets UEFI firmware + SATA primary disk +
  e1000e NIC (matching what the Windows install path would have used)
  and the default communicator flips to ``"winrm"``.

How the cache behaves
~~~~~~~~~~~~~~~~~~~~~

The qcow2 you hand over is content-hashed (SHA-256) and staged into
``<cache_root>/vms/byoi-<hash>.qcow2`` on first use.  Subsequent runs
with the same file skip the copy.  Files already under the cache
root are used in place.

See ``examples/bring_your_own_image.py`` for an end-to-end walkthrough
that bakes a golden image once and reuses it for BYOI runs.

Credentials
-----------

Every VM must have at least one :class:`~testrange.credentials.Credential`
with ``username="root"``.  Additional users can be non-privileged or
passwordless-sudo:

.. code-block:: python

    users=[
        Credential("root", "rootpw"),
        Credential("alice", "alicepw", sudo=True),
        Credential("readonly", "viewpw"),
    ]

Linux
  Passwords are hashed with SHA-512 crypt before landing in the
  cloud-init seed ISO — they're never stored in plaintext on disk.
  Optional SSH keys are injected per-user and rotated on each run
  (they're excluded from the cache hash, so swapping keys doesn't
  invalidate cached images).

Windows
  The ``root`` credential's password sets the **built-in
  Administrator** account; other credentials become local accounts,
  and ``sudo=True`` promotes them to the ``Administrators`` group.
  Passwords land in the autounattend XML in plain text (Windows
  Setup expects ``PlainText=true``); treat the autounattend ISO as
  a secret.  SSH keys are ignored.  See :doc:`windows` for more.

Packages
--------

Add packages with a list of :class:`~testrange.packages.Apt`,
:class:`~testrange.packages.Dnf`, :class:`~testrange.packages.Pip`,
:class:`~testrange.packages.Homebrew`, or
:class:`~testrange.packages.Winget` instances:

.. code-block:: python

    pkgs=[
        Apt("nginx"),
        Apt("postgresql"),
        Pip("requests"),
        Pip("sqlalchemy", version="2.0.30"),
    ]

Linux
  The cloud-init builder splits these automatically between the fast
  native path (apt/dnf in ``packages:``) and shell ``runcmd`` (pip,
  brew).

Windows
  Only :class:`~testrange.packages.Winget` entries are honoured —
  they run via ``winget install`` inside the autounattend's
  FirstLogonCommands.  Apt/Dnf/Pip/Homebrew are silently ignored.

See :doc:`packages` for details.

Post-install commands
---------------------

Use ``post_install_cmds=[...]`` for one-liners that need to run after
packages are installed, during the install phase only.  These changes
are baked into the cached image:

.. code-block:: python

    # Linux — commands are shell strings (runcmd)
    post_install_cmds=[
        "rm -f /var/www/html/index.nginx-debian.html",
        "echo '<h1>hello</h1>' > /var/www/html/index.html",
        "systemctl enable --now nginx",
    ]

    # Windows — commands are PowerShell (FirstLogonCommands)
    post_install_cmds=[
        "New-LocalGroup -Name 'CI' -ErrorAction SilentlyContinue",
        "Add-LocalGroupMember -Group 'CI' -Member 'deploy'",
    ]

On Windows the commands are invoked via
``powershell.exe -NoProfile -NonInteractive -Command``, not
``cmd.exe``.  Escape quoting accordingly.

Hardware (devices)
------------------

``devices=[...]`` is a flat list.  Defaults fill in anything omitted:
2 vCPU, 2 GiB RAM, 20 GB disk, no NICs.  A typical production-like VM:

.. code-block:: python

    devices=[
        vCPU(4),
        Memory(8),
        HardDrive(200),                # 200 GiB OS disk
        HardDrive(500),                # 500 GiB data disk → /dev/vdb
        VirtualNetworkRef("Public"),
        VirtualNetworkRef("Private", ip="10.0.2.10"),
    ]

``HardDrive`` accepts a number (interpreted as GiB) or a size string
like ``"200GB"`` / ``"1.5TiB"``.  **The first entry is always the OS
disk** — the installer writes to it and the post-install snapshot is
what the cache stores.  Any additional ``HardDrive`` entries are
empty qcow2 data volumes, ephemeral per-run.

Bus mapping:

- **Linux** — primary disk on ``virtio`` (``/dev/vda``); extra drives
  follow as ``/dev/vdb``, ``/dev/vdc``, or ``nvme1n1`` + friends when
  ``nvme=True``.
- **Windows** — primary disk on ``sata`` (``sda``) so Setup can see it
  without the virtio-blk driver; CD-ROMs take the remaining SATA
  slots.  Extra drives still use virtio and come online after
  first-logon installs the drivers.

Multiple ``VirtualNetworkRef`` entries add NICs — the first is
typically the one with internet access.

Talking to a running VM
-----------------------

Once the orchestrator has booted everything, your test function
receives the running VMs via ``orchestrator.vms[name]``.  Every call
is synchronous and returns typed results:

.. code-block:: python

    def my_test(orchestrator):
        web = orchestrator.vms["webserver"]

        # Run a command
        r = web.exec(["systemctl", "is-active", "nginx"])
        assert r.exit_code == 0, r.stderr_text

        # Read/write text
        motd = web.read_text("/etc/motd")
        web.write_text("/tmp/flag.txt", "ok\n")

        # Copy bytes to/from the host
        web.download("/var/log/nginx/access.log", tmp_path / "access.log")
        web.upload(local_config, "/etc/nginx/sites-enabled/test.conf")

        # Restart a service after dropping in a new config
        web.exec(["systemctl", "restart", "nginx"], timeout=30).check()

Previewing the spec
-------------------

``testrange describe MODULE:FACTORY`` renders the network + VM
topology of a test factory without provisioning anything.  Use it
as a fast sanity check before a long run:

.. code-block:: bash

    testrange describe examples/two_networks_three_vms.py:gen_tests
