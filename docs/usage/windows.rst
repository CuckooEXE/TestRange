Windows VMs
===========

TestRange boots Linux and Windows guests through the same
:class:`~testrange.Orchestrator` and the same
:class:`~testrange.VM` spec, but the machinery underneath is
different.  This page covers how the Windows build path differs
from the Linux flow and what you have to get right to make the
one-command ``testrange run`` work end-to-end on a Windows guest.

.. note::

   Many implementation details below describe how a hypervisor
   backend that builds Windows from an installer ISO has to wire
   the install-phase domain (UEFI firmware, AHCI primary disk,
   e1000e NIC, virtio-win driver CD-ROM, …).  Specific class
   references resolve to whichever backend you're running; check
   that backend's source under :mod:`testrange.backends` for the
   exact wiring it ships.

.. _linux-vs-windows:

Linux vs Windows at a glance
----------------------------

.. list-table::
   :header-rows: 1
   :widths: 28 36 36

   * -
     - Linux
     - Windows
   * - Install media
     - A cloud ``qcow2`` / ``img`` (already installed; TestRange
       customises it)
     - An installer ``.iso`` (TestRange boots it and lets Setup
       install from scratch)
   * - Source of base disk
     - Overlay on cached cloud image
     - Blank qcow2 of the requested size
   * - Provisioning mechanism
     - cloud-init NoCloud seed ISO (``user-data``, ``meta-data``,
       ``network-config``)
     - Windows Setup ``autounattend.xml`` on a seed ISO
   * - Default communicator
     - ``guest-agent`` (virtio-serial, no TCP)
     - ``winrm`` (TCP 5985 on a static IP)
   * - Firmware
     - SeaBIOS (legacy)
     - OVMF (UEFI; required for Windows 10+ GPT installs)
   * - Primary disk bus
     - ``virtio`` (``vda``)
     - ``sata`` (``sda``) — Windows Setup has native AHCI drivers
       but not virtio-blk
   * - NIC model
     - ``virtio``
     - ``e1000e`` — Windows Setup has native e1000e drivers but not
       virtio-net
   * - Extra install-time CD-ROMs
     - None
     - Windows install ISO + ``virtio-win.iso`` (drivers +
       ``qemu-ga`` MSI)
   * - Install-completion signal
     - cloud-init ``power_state: poweroff``
     - Last FirstLogonCommand: ``shutdown /s /t 0``
   * - Cache key
     - ``vm_config_hash(iso, users, packages, cmds, disk_size)``
     - Same hash, distinct content — keyed on the ISO path or URL
       just like Linux

How the Windows install phase works
-----------------------------------

When you construct a VM with an ``iso=`` that looks like a Windows
installer (matched by
:func:`~testrange.vms.images.is_windows_image` — ``.iso`` + Windows-ish
filename), :meth:`~testrange.vms.base.AbstractVM.build` routes to the
Windows install path instead of the cloud-init one.  The path is:

1. **Resolve + stage the installer ISO.**
   :meth:`~testrange.cache.CacheManager.stage_local_iso` content-hashes
   the ISO and copies it into ``<cache_root>/images/iso-<sha>.iso``.
   Subsequent runs reuse the staged copy.

2. **Create a blank qcow2** of the primary disk's size.  The Linux
   path overlays a pre-installed cloud image; Windows Setup installs
   from scratch, so we hand it an empty disk.

3. **Generate an autounattend ISO.**
   :class:`~testrange.vms.builders.WindowsUnattendedBuilder` composes the
   answer file (disk partitioning, user accounts, locale, first-logon
   commands); :func:`~testrange.vms.builders.build_autounattend_iso_bytes`
   produces an ISO 9660 seed volume that Windows Setup auto-detects.

4. **Fetch / reuse the virtio-win ISO.**
   :meth:`~testrange.cache.CacheManager.get_virtio_win_iso` downloads
   the signed ``virtio-win.iso`` from
   ``fedorapeople.org/groups/virt/virtio-win`` on first use (~800 MiB)
   and caches it permanently at
   ``<cache_root>/images/virtio-win.iso``.  The ISO carries
   NetKVM/viostor/vioscsi/pvpanic drivers and the ``qemu-guest-agent``
   MSI that first-logon commands install.

5. **Define a UEFI install-phase domain.**
   The backend points the firmware loader at the OVMF code blob and
   refers to a per-run NVRAM copy in the run scratch dir so OVMF
   variable writes don't leak between runs.  Four SATA devices are
   attached: the blank disk at ``sda``, the **Windows install ISO
   at** ``sdb`` **(the bootable CD-ROM)**, then ``virtio-win.iso``
   at ``sdc``, and the autounattend seed at ``sdd``.  The bootable
   CD-ROM is listed first so the backend assigns it ``bootindex=1``;
   the unattend seed is merely attached so Setup scans its volume
   for ``autounattend.xml``.  NIC model is ``e1000e``.

6. **Start the domain, spam spacebars past the 'Press any key' prompt.**
   Windows install ISOs show a ~5-second *Press any key to boot from
   CD or DVD...* prompt under UEFI.  A headless VM has nothing to
   press it with, so the orchestrator spawns a short-lived daemon
   thread that calls :meth:`virDomain.sendKey` with ``KEY_SPACE``
   (Linux keycode 57) once a second for 30 seconds.  Builders opt in
   by returning ``True`` from
   :meth:`~testrange.vms.builders.base.Builder.needs_boot_keypress`;
   :class:`~testrange.vms.builders.WindowsUnattendedBuilder` does,
   :class:`CloudInitBuilder` and :class:`NoOpBuilder` do not.

7. **Wait for power-off.**  Setup partitions, installs, reboots
   into the installed system, drops into the first-logon script
   (which installs virtio drivers + ``qemu-guest-agent`` MSI, enables
   WinRM, runs Winget packages, runs your ``post_install_cmds``, then
   calls ``shutdown /s /t 0``).  The orchestrator polls for
   ``VIR_DOMAIN_SHUTOFF`` exactly like the Linux path.

   The install-phase domain is wrapped in a ``try/finally`` so the
   backend always destroys and undefines it on exit — including on
   ``KeyboardInterrupt``, timeout, or cache-write failure.
   :meth:`~testrange.orchestrator_base.AbstractOrchestrator.__enter__`
   catches ``BaseException`` (not just ``Exception``) so Ctrl+C
   during the 30-minute install runs teardown before the interrupt
   propagates, and the install domain is also stashed on the VM
   instance so :meth:`~testrange.vms.base.AbstractVM.shutdown` can
   clean it up as a safety net.  Net result: no orphaned
   ``tr-build-winbox-*`` resources left behind on the host.

8. **Compress + cache.**  The installed disk is written to
   ``<cache_root>/vms/<config_hash>/<primary-disk-filename>`` with a
   matching ``manifest.json``.  Subsequent runs overlay this cached
   disk and boot directly into Windows.

How the Windows run phase works
-------------------------------

Once a cached disk exists,
:meth:`~testrange.vms.base.AbstractVM.start_run` creates a copy-on-
write overlay on it and boots a UEFI domain (no autounattend, no
install ISO, no virtio-win ISO attached).  The NVRAM is per-run
scratch so Windows can update UEFI vars without leaking between
tests.  The orchestrator constructs a
:class:`~testrange.communication.winrm.WinRMCommunicator` pointed
at the first static IP on the VM's
:class:`~testrange.devices.vNIC` and waits for port 5985 to
answer.  From there your test function uses the normal
:meth:`~testrange.vms.base.AbstractVM.exec` /
:meth:`~testrange.vms.base.AbstractVM.get_file` /
:meth:`~testrange.vms.base.AbstractVM.put_file` helpers; WinRM is
the transport instead of the host-mediated guest agent.

Writing a Windows VM spec
-------------------------

.. code-block:: python

    VM(
        name="winbox",
        iso="/srv/iso/Win10_21H1_English_x64.iso",
        users=[
            # The root credential sets the built-in Administrator
            # password (convention in WindowsUnattendedBuilder).
            Credential("root", "TR-Admin!2026"),
            Credential("deploy", "TR-Deploy!2026", sudo=True),
        ],
        pkgs=[Winget("Git.Git")],
        post_install_cmds=[
            "New-LocalGroup -Name 'CI' -ErrorAction SilentlyContinue",
        ],
        devices=[
            vCPU(2),
            Memory(4),
            HardDrive(40),
            vNIC("WinNet", ip="10.60.0.10"),
        ],
        communicator="winrm",  # defaulted for Windows ISOs; shown here
    )

Things to notice:

- The ``root`` credential becomes the **built-in Administrator** (the
  :class:`~testrange.vms.builders.WindowsUnattendedBuilder` uses its
  password in ``AdministratorPassword``).  Additional credentials
  become local accounts; ``sudo=True`` adds them to the
  ``Administrators`` group.
- :class:`~testrange.packages.Winget` is the only package manager
  wired into the autounattend; Apt/Dnf/Pip/Homebrew are ignored on
  Windows.
- ``post_install_cmds`` are **PowerShell commands**, not Bash.
- A :class:`~testrange.devices.vNIC` with a **static IP**
  is required whenever the WinRM communicator is in use (v1 does not
  support DHCP-lease discovery; see :doc:`communication`).

The ``winrm`` communicator is selected automatically when ``iso=``
points at a Windows ISO.  For a prebuilt Windows image pass
``builder=NoOpBuilder(windows=True)`` — the ``windows=`` flag
propagates UEFI / SATA / e1000e / WinRM defaults to the run phase.

Prerequisites
-------------

**Host packages.**

.. code-block:: bash

    # Debian / Ubuntu
    sudo apt-get install -y ovmf

    # Fedora / RHEL
    sudo dnf install -y edk2-ovmf

TestRange references ``/usr/share/OVMF/OVMF_CODE_4M.fd`` and
``OVMF_VARS_4M.fd`` directly.  Distros that package OVMF elsewhere
(e.g. ``/usr/share/edk2/``) need a symlink or an issue tracker for
configurable paths.

**Python extras.**

.. code-block:: bash

    pip install "testrange[winrm]"

This pulls in ``pywinrm``, which
:class:`~testrange.communication.winrm.WinRMCommunicator` depends on.

**Network egress on first run.**  The ``virtio-win.iso`` download is
lazy and happens the first time any Windows VM builds.  About 800 MiB.
Subsequent runs are entirely offline.

**An ISO on disk.**  Microsoft does not publish stable download URLs,
so you supply the file.  Any Windows 10 / 11 install ISO works (Pro,
Enterprise, Education).  Multi-edition consumer ISOs (the standard
``Win10_*_English_x64.iso`` shape) work out of the box — the default
``product_key`` on
:class:`~testrange.vms.builders.WindowsUnattendedBuilder` is the
publicly documented Windows 10/11 Pro generic install key, which
tells Setup to pick the Pro image and proceed unattended.  The
resulting VM runs unactivated, which is fine for test-range use.

Tuning the unattend
-------------------

:class:`~testrange.vms.builders.WindowsUnattendedBuilder` accepts
``product_key=``, ``ui_language=``, and ``timezone=`` keyword
arguments.  They aren't exposed on
:class:`~testrange.VM` yet — if you need them, subclass
the VM or use the builder directly and set the XML into a custom
autounattend ISO before orchestrating.  That's a v1 gap we'll close
when there's demand.

**``product_key=``** controls Windows Setup's edition selection on
multi-edition install ISOs.  Three knob positions:

- **Default** (``"VK7JG-NPHTM-C97JM-9MPGT-3V66T"``, the Windows 10/11
  Pro generic install key) — Setup picks Pro and runs unattended.
  Unactivated.  Fine for CI / test ranges.
- **Retail key** — the VM activates during first-logon if it has
  internet egress on the install network.
- **``None``** — omit the ``<ProductKey>`` element entirely.  Valid
  for Enterprise-evaluation ISOs and single-edition media that don't
  need a key to pick an image.  On multi-edition ISOs this will fail
  Setup because Setup can't tell which edition to install.

The generated FirstLogonCommands always end with a
``shutdown /s /t 0``.  If your ``post_install_cmds`` restart or
shutdown the guest themselves, the trailing shutdown becomes a no-op
and the orchestrator's poweroff detection fires as soon as the VM
actually stops.

Troubleshooting
---------------

``Permission denied`` on the Windows ISO
  TestRange auto-stages the ISO into the cache; if that fails, make
  sure your user has write access to the cache root (defaults to
  ``/var/tmp/testrange/<user>``) — see :doc:`installation`.

``Failed to start Windows install domain`` with OVMF path errors
  Your distro ships OVMF at a non-standard path.  Verify with
  ``ls /usr/share/OVMF/OVMF_CODE_4M.fd``.  Symlink as needed or open
  an issue.

Install hangs at ``Press any key to boot from CD or DVD``
  Should not happen during the install phase: the orchestrator spams
  spacebars for 30 seconds after the install domain boots (see
  :meth:`~testrange.vms.builders.base.Builder.needs_boot_keypress`).
  If you see this symptom on a run-phase boot the install phase did
  not complete cleanly last time — check the cached manifest under
  ``<cache_root>/vms/<hash>/manifest.json`` and delete the matching
  cached primary disk if necessary.

Install drops to a UEFI shell (``Shell>``)
  OVMF tried every boot target and found nothing bootable.  Almost
  always means the Windows ISO isn't the first CD-ROM in the device
  list — a regression in this area would break the
  ``<boot dev='cdrom'/>`` → ``bootindex=1`` assignment (see
  the backend's base domain XML helper and the
  ``test_bootable_cdrom_is_first`` regression test).

``Can't read product key from the answer file``
  The ``<ProductKey>`` element is in the wrong location in the
  autounattend XML — Microsoft's unattend schema puts it inside
  ``<UserData>`` under ``Microsoft-Windows-Setup``; anywhere else
  Setup silently ignores it.  Also look at the ``product_key=`` value
  — ``None`` on a multi-edition ISO (Home/Pro/Education) makes Setup
  prompt for edition selection and fail.

``WinRM at http://10.x.x.x:5985/wsman not ready after 300s``
  Either (a) Windows finished Setup but FirstLogonCommands didn't
  enable WinRM, (b) the static IP on the
  :class:`~testrange.devices.vNIC` doesn't match what the
  guest's DHCP client was handed (the backend's bridge-local DHCP
  reservation needs the MAC to match — ``register_vm`` computes a
  deterministic MAC), or (c) the Windows Firewall is still blocking
  5985 because the network profile is ``Public`` and the
  ``-SkipNetworkProfileCheck`` flag on ``Enable-PSRemoting`` didn't
  take.  Connect with the backend's console viewer on the host to
  inspect.

End-to-end example
------------------

``examples/winrm_communicator.py`` reads ``TESTRANGE_WIN_ISO`` from
the environment and runs a full install → cache → WinRM round-trip::

    TESTRANGE_WIN_ISO=/srv/iso/Win10_21H1_English_x64.iso \\
        testrange run examples/winrm_communicator.py:gen_tests

First run: 15-30 minutes (Windows Setup is not fast).  Subsequent
runs: about as long as a Linux VM boot + handshake.
