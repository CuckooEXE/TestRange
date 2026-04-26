Changelog
=========

Significant changes to TestRange, newest first.  Versions follow
`Semantic Versioning <https://semver.org/>`_ once the API stabilises;
during the ``0.1.x`` series anything may change.

Unreleased
----------

ProxMox VE install path
~~~~~~~~~~~~~~~~~~~~~~~

**Added: ``ProxmoxAnswerBuilder``** for unattended ProxMox VE
installs.  Auto-selected for ``iso=`` strings matching
``proxmox-ve[-_]*.iso``; emits an ``answer.toml`` to a
``PROXMOX-AIS``-labeled seed ISO and prepares the main installer ISO
in pure Python (no ``proxmox-auto-install-assistant`` host
dependency, no ``xorriso``).  Working PVE 9.x out of the box;
declare ``VirtualNetworkRef(..., ip="...")`` for the run-phase
network and the builder synthesises a ``from-answer`` static config
that survives the install-to-run network swap.  Example:
``examples/nested_proxmox_public_private.py``.

The path lives on top of six PVE-specific behaviours, all
regression-tested.  Five are just correct handling of how PVE 9.x
ships rather than workarounds: activation via
``/cdrom/auto-installer-mode.toml`` at the ISO root (PVE 9.x;
earlier releases looked inside the initrd); kebab-case
``answer.toml`` field names that don't match the underscored
mode-file fields; ``reboot-mode = "power-off"`` to turn the
installer's reboot into the SHUTOFF the cache pipeline expects;
the ``from-dhcp``-vs-``from-answer`` distinction (the former
freezes the install-phase lease as static, the latter takes the
answer's static config verbatim); and interface-name-based NIC
filtering (the install-phase MAC differs from the run-phase MAC,
but interface name is stable across the swap).  The one true
workaround is OVMF-only firmware to sidestep a SeaBIOS + q35 +
SATA-CD GRUB triple-fault during PVE's first boot.

PVE installs also exercise the per-VM UEFI NVRAM sidecar described
under *Cache layout* below, but that is a libvirt-backend mechanism
needed by any UEFI install (Windows was the first guest to surface
it); it lives in :mod:`testrange.backends.libvirt.vm` and the cache
layer, not in the ProxMox builder.

Cache layout
~~~~~~~~~~~~

**Added: per-VM UEFI NVRAM sidecar at ``<vms_dir>/<hash>.nvram.fd``.**
Install-phase NVRAM (where the installer writes EFI ``BootOrder``
entries) is now snapshotted into the cache alongside the qcow2,
because libvirt's ``VIR_DOMAIN_UNDEFINE_NVRAM`` deletes the per-run
NVRAM at teardown.  Run-phase domains seed their NVRAM from the
cached sidecar rather than the empty global ``OVMF_VARS`` template,
so any UEFI install whose distro doesn't write the
``/EFI/BOOT/BOOTX64.EFI`` removable-path fallback (PVE included)
still boots cleanly.  New helpers:
:meth:`~testrange.cache.CacheManager.vm_nvram_ref`,
:meth:`~testrange.cache.CacheManager.store_vm_nvram`,
:meth:`~testrange.cache.CacheManager.get_vm_nvram`.  Backwards-
compatible: existing entries without sidecars stay valid for BIOS
installs (cloud-init), and a missing sidecar on a UEFI install
falls through to the template just as before.

**Added: prepared-ISO cache for ProxMox installer media** at
``<images_dir>/proxmox-prepared-<sha>.iso``, populated by
:meth:`~testrange.cache.CacheManager.get_proxmox_prepared_iso` on
first use.  Keyed by the SHA-256 of the vanilla ISO so the
expensive (~1 s) prep step happens once per upstream version,
amortised across every VM that builds against it.

DAC ownership of UEFI NVRAM
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Fixed: NVRAM file ``Permission denied`` after install completes.**
When libvirt creates the per-domain NVRAM by copying the
``<nvram template="...">`` source on first ``domain.create()``,
the DAC security driver records no original-owner xattr — the
file stays ``libvirt-qemu:0600`` after the domain stops, and the
NVRAM-snapshot read fails with EACCES on any non-libvirt-qemu user.
:func:`~testrange.backends.libvirt.vm._preseed_nvram` pre-creates
the NVRAM as the invoking user with mode ``0644`` *before*
``defineXML``; DAC's ``remember_owner`` xattr then has an original
owner to restore on shutdown, and the snapshot reader can open
the file.  Behaviour is identical at install time (the seeded
bytes are the OVMF_VARS template, exactly what libvirt would have
copied).

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
