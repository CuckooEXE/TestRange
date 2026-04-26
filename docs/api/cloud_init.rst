Seed ISO machinery
==================

Linux VMs are provisioned via cloud-init's NoCloud datasource; Windows
VMs via an ``Autounattend.xml`` answer file.  Both paths share the
same shape: a short-lived ``.iso`` seed image is attached to the VM
on first boot, the guest consumes it, then the VM powers itself off
so the installed disk can be cached.  This page covers the seed-ISO
machinery and design rationale; the class reference for the builders
that generate these seeds lives on :doc:`builders`.  For the broader
Windows install pipeline (OVMF, SATA disk, virtio-win ISO, e1000e
NIC) see :doc:`/usage/windows`.

Linux: two seeds per VM
-----------------------

:class:`~testrange.vms.builders.CloudInitBuilder` generates the
cloud-init seed ISO twice:

1. **Phase 1 (install)** — ``<vm>-install-seed.iso``.
   Configures users, installs packages, runs post-install commands,
   and triggers ``power_state: poweroff``.  This is what produces the
   cached snapshot.

2. **Phase 2 (run)** — ``<vm>-seed.iso``.
   Rotates the ``instance-id`` so cloud-init treats each run as a
   new machine, and rewrites the network-config for the test-phase
   NIC layout.  Packages aren't reinstalled — that already happened
   in phase 1.

Writing the ISO uses :mod:`pycdlib` so we don't need ``genisoimage``
or ``xorriso`` on the host.  The volume label is ``cidata`` (required
by NoCloud) and the files are dual-named: short 9660-compatible
``META_DATA`` plus Joliet ``meta-data`` (cloud-init actually reads the
Joliet path, but 9660 has to be present and valid).

Windows: one seed, install-only
-------------------------------

The Windows install path is single-phase.
:func:`~testrange.vms.builders.build_autounattend_iso_bytes` produces a
``<vm>-unattend.iso`` containing a single file at the root:
``autounattend.xml`` (dual-named: ``AUTOUNATT.XML`` on the 9660 side,
``autounattend.xml`` on Joliet).  Windows Setup scans every attached
media volume for that filename and uses the first match, so just
attaching the ISO as a CD-ROM is enough — no kernel command-line
equivalent of ``ds=nocloud`` needed.

There is no phase-2 seed on the Windows side: the cached installed
disk is booted directly on subsequent runs, and the WinRM communicator
carries everything a test might want to do at runtime.  That means
network-config changes between install and run are not automatic on
Windows (yet); static IPs declared on
:class:`~testrange.devices.VirtualNetworkRef` come from the libvirt
dnsmasq DHCP reservation, not from an in-guest netplan.

Design notes
------------

**Password hashes live in user-data.**  Credentials are written as
``hashed_passwd: $6$...`` SHA-512 crypt blobs, not plaintext.  The
seed ISO is still sensitive — it lives in the per-run scratch dir
under ``/tmp`` and is removed at teardown.

**Isolated-DNS networks don't break installs.**  The install
network always has ``dns=True`` so ``apt``/``dnf`` can resolve
upstream repos.  Per-VM ``VirtualNetwork(dns=False)`` only affects
phase 2.

**Gateways and resolvers are plumbed per-network.**  Only
networks with ``internet=True`` contribute ``gateway4``; only
``dns=True`` contributes a ``nameservers`` entry.  This prevents
isolated networks from stealing the default route and pointing
resolvers at a non-listening IP.

See also
--------

- :doc:`builders` — class reference for
  :class:`~testrange.vms.builders.CloudInitBuilder`,
  :class:`~testrange.vms.builders.WindowsUnattendedBuilder`,
  :class:`~testrange.vms.builders.NoOpBuilder`, and the
  :class:`~testrange.vms.builders.base.Builder` ABC.
- :doc:`/usage/vms` — high-level VM spec.
- :doc:`/usage/windows` — Windows install walkthrough.
