Installation
============

System Prerequisites
--------------------

TestRange ships with multiple hypervisor backends under
:mod:`testrange.backends`.  The default drives KVM/QEMU through
libvirt; the Proxmox backend
(:class:`~testrange.backends.proxmox.ProxmoxOrchestrator`) drives a
remote PVE cluster over the REST API.  The packages below get the
libvirt backend working.  See :doc:`/api/backends` for the full
list, and the *ProxMox VE installs* and *Windows VMs* sub-sections
below for the optional extras each alternative needs.

On Debian / Ubuntu:

.. code-block:: bash

   sudo apt-get install -y \
       libvirt-daemon-system \
       qemu-kvm \
       qemu-utils \
       libvirt-dev \
       bridge-utils

   # Allow your user to manage VMs without sudo
   sudo adduser $USER libvirt
   sudo adduser $USER kvm
   # Log out and back in for group changes to take effect

On Fedora / RHEL / Rocky:

.. code-block:: bash

   sudo dnf install -y \
       libvirt \
       qemu-kvm \
       qemu-img \
       libvirt-devel \
       virt-install

   sudo systemctl enable --now libvirtd
   sudo usermod -aG libvirt $USER

ProxMox VE installs (optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To provision ProxMox VE VMs (boot a vanilla PVE installer ISO and
land in a cached post-install image, the way
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator` and
``examples/nested_proxmox_public_private.py`` do), TestRange needs
``xorriso`` on ``$PATH``.  ``xorriso`` (libisoburn's CLI) drives
the modification of the vanilla PVE ISO so the unattended-install
mode marker (``/auto-installer-mode.toml``) gets injected without
disturbing the ISO's hybrid El Torito + GPT/MBR + HFS+ + EFI
System Partition layout — which PVE's UEFI GRUB depends on for
locating its own ``grub.cfg``.

On Debian / Ubuntu:

.. code-block:: bash

   sudo apt-get install -y xorriso

On Fedora / RHEL / Rocky:

.. code-block:: bash

   sudo dnf install -y xorriso

On macOS:

.. code-block:: bash

   brew install xorriso

When the binary is missing, the prepare-iso step fails fast with a
:class:`~testrange.vms.builders._proxmox_prepare.ProxmoxPrepareError`
explaining what to install — no broken ISO ever lands in the
cache.  Migrating away from the binary is on the roadmap (see
``TODO.md``); for now it's required to use the Proxmox backend
against PVE 9.x ISOs.

Windows VMs (optional)
~~~~~~~~~~~~~~~~~~~~~~~

To provision Windows VMs you will also need:

- An original Windows installation ISO.  Microsoft does not publish
  stable download URLs, so TestRange cannot fetch one for you — point
  the VM's ``iso=`` at a file you downloaded.
- ``ovmf`` (UEFI firmware): ``sudo apt-get install ovmf`` on Debian /
  Ubuntu, ``sudo dnf install edk2-ovmf`` on Fedora / RHEL.  TestRange
  references ``/usr/share/OVMF/OVMF_CODE_4M.fd`` +
  ``OVMF_VARS_4M.fd`` directly.
- ``pywinrm`` for the WinRM communicator:
  ``pip install "testrange[winrm]"``.

You do **not** need to download ``virtio-win.iso`` yourself — TestRange
fetches it lazily on first Windows build and caches it under
``<cache_root>/images/virtio-win.iso``.  Network access is needed only
that first time.

See :doc:`windows` for the full Windows-vs-Linux rundown and the
install-phase walkthrough.


Python Package
--------------

.. code-block:: bash

   pip install testrange

For documentation building:

.. code-block:: bash

   pip install "testrange[docs]"
   cd docs && make html


Verifying the Installation
--------------------------

.. code-block:: python

   import testrange
   print(testrange.__version__)

   import libvirt
   conn = libvirt.open("qemu:///system")
   print("libvirt connection OK:", conn.getHostname())
   conn.close()


Nested virtualization (optional)
--------------------------------

TestRange supports :class:`~testrange.Hypervisor` VMs — VMs that run
their own inner orchestrator and inner VMs.  For that to work, the
physical host's KVM module needs **nested virtualization** enabled so
the L1 hypervisor VM can expose VMX/SVM to its L2 guests.

Check whether nested is already on:

.. code-block:: bash

   cat /sys/module/kvm_intel/parameters/nested   # Intel
   cat /sys/module/kvm_amd/parameters/nested     # AMD

If the output is ``Y`` or ``1`` you're done.  Otherwise, enable it
persistently via a modprobe option:

.. code-block:: bash

   # Intel CPUs
   echo 'options kvm_intel nested=1' | sudo tee /etc/modprobe.d/kvm-nested.conf
   sudo modprobe -r kvm_intel && sudo modprobe kvm_intel

   # AMD CPUs
   echo 'options kvm_amd nested=1' | sudo tee /etc/modprobe.d/kvm-nested.conf
   sudo modprobe -r kvm_amd && sudo modprobe kvm_amd

``modprobe -r`` fails if any VM is running — shut those down first, or
just reboot, which applies the option cleanly.

TestRange does **not** check this at runtime.  Without the flag,
inner VMs will fail to boot with opaque KVM errors from the L1 guest;
that's by design — the check belongs at install time, not on every
orchestrator start.

The outer VM (:class:`~testrange.Hypervisor`) additionally needs
to be able to ``ssh`` in via a key-based login from the host, because
nested libvirt drives the inner layer over ``qemu+ssh://``.  Put a
:class:`~testrange.Credential` with ``ssh_key=`` on the hypervisor's
user list, and make sure the matching private key is known to your
``ssh-agent`` / ``~/.ssh/``.

.. note::

   L2 guests run ~2–4× slower than L1 guests.  Nested is intended for
   *testing the orchestration flow*, not for production-grade
   performance.


Host dnsmasq coexistence
------------------------

If you're already running ``dnsmasq`` on the host (common with
``NetworkManager`` or a manually-configured split-DNS setup), the
default wildcard bind on ``0.0.0.0:53`` will conflict with libvirt's
per-bridge dnsmasq when TestRange starts a network with ``dns=True``
(the common case).  Symptom::

    dnsmasq: failed to create listening socket for 10.42.1.1:
    Address already in use

Fix: teach the host dnsmasq to bind only to specific interfaces and
ignore libvirt's bridges.  Drop this in
``/etc/dnsmasq.d/90-libvirt-compat.conf``:

.. code-block:: ini

   bind-dynamic
   except-interface=virbr*
   except-interface=vbr*
   except-interface=vnet*

Then ``sudo systemctl restart dnsmasq``.  Confirm with
``ss -tlnp 'sport = :53'`` — you should see specific interface IPs
bound rather than ``0.0.0.0:53``.


Cache location and permissions
------------------------------

By default, TestRange caches downloaded base images and compressed
VM snapshots under ``/var/tmp/testrange/<user>/``.  TestRange manages
its own permissions — cache directories are created ``0755`` and
staged image files are ``0644``, which is everything the
``qemu:///system`` daemon needs to read backing disks and CD-ROMs.

**If you override the cache location**, set ``TESTRANGE_CACHE_DIR``
to a path whose every directory component is world-executable
(``chmod +x``).  Anywhere under ``/var/tmp``, ``/srv``, or ``/opt``
works out of the box; paths under a user's home directory typically
do not.  See :doc:`caching` for the cache layout.
