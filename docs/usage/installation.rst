Installation
============

System Prerequisites
--------------------

TestRange ships with multiple hypervisor backends under
:mod:`testrange.backends`; the default — and currently the only fully
implemented one — drives KVM/QEMU through libvirt.  The packages
below get that backend working.  See :doc:`/api/backends` for the
full list and for what each alternative backend needs.

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
