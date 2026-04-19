Changelog
=========

Significant changes to TestRange, newest first.  Versions follow
`Semantic Versioning <https://semver.org/>`_ once the API stabilises;
during the ``0.1.x`` series anything may change.

Unreleased
----------

Windows install path
~~~~~~~~~~~~~~~~~~~~

Four interlocking fixes to make the out-of-the-box
``WindowsUnattendedBuilder`` flow actually reach a cached Windows
image on a standard multi-edition consumer ISO.

**Fixed: UEFI boot-order bug caused installs to hang indefinitely.**
The install-phase domain listed CD-ROMs as ``[seed, *extras]``, which
put the autounattend seed ISO first.  libvirt expands
``<boot dev='cdrom'/>`` by assigning ``bootindex=1`` to the *first*
CD-ROM in the device list, so UEFI tried to boot a non-bootable ISO,
fell through to an empty disk, and idled forever — the
``winbox-install.qcow2`` stayed at ~193 KB across multi-hour runs.
When ``boot_cdrom=True`` the bootable ``extra_cdroms[0]`` (the Windows
ISO) is now placed first; the seed ISO is merely attached so Setup
scans its volume for ``autounattend.xml``.  Regression:
``test_bootable_cdrom_is_first``.

**Fixed: ``<ProductKey>`` in the wrong schema location.**
The autounattend generator placed ``<ProductKey>`` as a direct child
of ``Microsoft-Windows-Setup``; Microsoft's unattend schema requires
it inside ``<UserData>``.  Setup silently ignored it and reported
*"can't read product key from the answer file"*.  Moved the element
into the correct parent.  Regression:
``test_product_key_nested_inside_userdata``.

**Changed: default ``product_key`` now ships the Windows 10/11 Pro generic install key.**
Multi-edition consumer ISOs (``Win10_*_English_x64.iso`` shape)
refuse to install unattended without *either* a ``ProductKey`` or
explicit edition metadata.  The new default
(``VK7JG-NPHTM-C97JM-9MPGT-3V66T``, publicly documented by Microsoft)
tells Setup to pick Pro and continue.  Does not activate — fine for
test-range use.  Pass ``WindowsUnattendedBuilder(product_key=None)``
to restore the old behaviour for Enterprise-eval / single-edition
ISOs.  Regression: ``test_default_product_key_emitted``.

**Added: orchestrator spams spacebars past the "Press any key" prompt.**
Windows install ISOs under UEFI show a five-second *Press any key to
boot from CD or DVD...* prompt that a headless VM has no way to
satisfy — OVMF exhausts boot options and drops to the EFI shell.
:meth:`~testrange.vms.builders.base.Builder.needs_boot_keypress` is a
new method on the builder ABC (default ``False``);
:class:`~testrange.vms.builders.WindowsUnattendedBuilder` returns
``True``.  When set,
:meth:`~testrange.backends.libvirt.VM._run_install_phase` spawns a
daemon thread that calls ``domain.sendKey(KEY_SPACE)`` once per
second for 30 seconds, then exits.  Thread is joined in the
``finally`` block.  Regression: ``TestBootKeypressSpam`` in
``tests/test_vm_libvirt.py``.

Install-phase resilience
~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: interrupted installs leaked libvirt domains.**
Three compounding bugs meant a ``KeyboardInterrupt`` (or any
exception during a 30-minute Windows install wait) left an orphaned
``tr-build-<vm>-<id>`` domain live under ``qemu:///system`` with no
Python process to tidy it.  All three are fixed:

1. :meth:`~testrange.backends.libvirt.VM._run_install_phase` used a
   local ``domain`` variable and reached the destroy/undefine code
   only on the normal-completion path.  Wrapping the wait loop in
   ``try/finally`` (with a new ``_destroy_and_undefine`` helper)
   guarantees cleanup on every exit — shutoff, timeout, cache-write
   error, ``KeyboardInterrupt``, anything.
2. Even if teardown *had* run, it couldn't see the install domain:
   ``vm.shutdown()`` operates on ``self._domain``, which only
   ``start_run()`` populated.  The install-phase domain is now
   stashed on ``self._install_domain`` as a safety net, and
   ``shutdown()`` cleans both.
3. :meth:`~testrange.backends.libvirt.Orchestrator.__enter__` caught
   only ``Exception``; ``KeyboardInterrupt`` and ``SystemExit``
   derive from ``BaseException`` and bypassed teardown.  Widened the
   handler.

Regressions live in ``tests/test_vm_libvirt.py::TestShutdown``,
``TestInstallPhaseCleanup``, and
``tests/test_teardown_resilience.py::test_keyboardinterrupt_during_enter_triggers_teardown``.

Debugging
~~~~~~~~~

**Added: ``TESTRANGE_VNC=1`` environment-variable toggle.**
When set, :func:`~testrange.backends.libvirt.VM._base_domain_xml`
attaches a VNC graphics device listening on ``127.0.0.1`` with an
auto-assigned port, plus a QXL video device.  Off by default so CI
and headless runs stay silent.  Find the port with
``virsh -c qemu:///system domdisplay <domain>``, tunnel over SSH,
connect with any VNC client.  See :doc:`usage/debugging`.

**Added: "Watching an install-phase VM" section in the debugging guide.**
Covers both the new VNC toggle and the terminal-only
``virsh screenshot`` + ``img2txt`` / ``img2sixel`` workflow for
SSH-only setups.

v0.1.0
------

Initial release.
