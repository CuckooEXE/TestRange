Example gallery
===============

The ``examples/`` directory in the repository contains 16 runnable
test-spec files.  Each one is a self-contained ``gen_tests()`` factory
you can launch with ``testrange run``; the tables below group them by
what they demonstrate.  Source each file in your editor alongside
this page — docstrings and inline comments stay authoritative, this
gallery is a curated index.

Every example runs with::

    testrange run examples/<name>.py:gen_tests

Add ``--log-level DEBUG`` for verbose orchestrator output, or switch
``run`` → ``describe`` to preview the topology without booting any
VMs.

Starting out
------------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - File
     - What it shows
   * - ``hello_world.py``
     - The smallest possible spec — one Debian VM, one ``uname -r``
       assertion, one NAT network.  Run this first to confirm your
       libvirt / OVMF setup works.
   * - ``exec_features.py``
     - A tour of every surface on :meth:`VM.exec`: exit codes,
       captured stdout / stderr, ``env=``, ``timeout=``, and the
       ``.check()`` helper that raises on non-zero exit.
   * - ``file_io.py``
     - The four ergonomic file helpers on
       :class:`~testrange.vms.base.AbstractVM` —
       :meth:`~testrange.vms.base.AbstractVM.read_text` /
       :meth:`~testrange.vms.base.AbstractVM.write_text`,
       :meth:`~testrange.vms.base.AbstractVM.upload`,
       :meth:`~testrange.vms.base.AbstractVM.download` — and the raw
       ``get_file`` / ``put_file`` primitives underneath.

Provisioning variations
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - File
     - What it shows
   * - ``local_image.py``
     - ``iso=`` accepts an absolute local path, not just an
       ``https://`` URL.  Useful for pre-hardened golden images and
       Packer output.  TestRange layers a copy-on-write overlay
       rather than mutating the source file.
   * - ``bring_your_own_image.py``
     - Skip the install phase entirely with
       ``builder=NoOpBuilder()``.  The user-supplied qcow2 is
       content-hash staged into the cache, ``pkgs=`` and
       ``post_install_cmds=`` are silently ignored, and ``users=`` is
       *informational* — the accounts must already exist in the
       image.
   * - ``multi_distro.py``
     - One test provisioning Debian 12, Ubuntu 24.04, and Rocky 9 in
       parallel from upstream cloud-image URLs.  The base-image cache
       keys per-URL so the first run downloads / installs each
       distro once; subsequent runs are near-instant.
   * - ``package_mix.py``
     - Native packages via cloud-init's ``packages:`` (``Apt``,
       ``Dnf``) alongside non-native ones via ``runcmd`` (``Pip``,
       ``Homebrew``).  The cache hash folds in the full package list
       so each permutation gets its own cache entry.
   * - ``multi_disk.py``
     - Multiple ``HardDrive`` entries on a single VM.  The first is
       always the OS disk; subsequent ones are ephemeral blank data
       volumes stored in the per-run scratch dir.  Shows both the
       ergonomic numeric form (``HardDrive(20)``) and the string form
       (``HardDrive("5GB")``).
   * - ``service_config.py``
     - A common integration-test shape: install packages once (goes
       into cache), override the service config at test time via
       ``upload`` / ``write_text``.  Config changes don't bust the
       cache hash because they happen after provisioning.
   * - ``nested_proxmox_public_private.py``
     - ProxMox VE as an L1 guest with a sibling Debian sidecar.
       Auto-selected :class:`~testrange.vms.builders.ProxmoxAnswerBuilder`
       does the unattended PVE install through the upstream
       ``answer.toml`` mechanism (UEFI, kebab-case schema,
       ``reboot-mode = "power-off"``); sidecar smoke-tests the
       ProxMox API at ``https://10.0.0.10:8006/api2/json/version``.
       Inner-orchestrator plumbing is staged behind
       ``TODO(proxmox-nest):`` markers, ready to enable once the
       ProxMox backend's ``root_on_vm()`` lands.

Networking
----------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - File
     - What it shows
   * - ``isolated_network.py``
     - Positive proof that ``internet=False`` actually cuts off
       outbound traffic — libvirt installs no NAT forwarding rules.
       The guest agent still works because it's virtio-serial, not
       TCP.  Other tests can rely on this invariant for
       fail-closed assertions.
   * - ``cross_network_dns.py``
     - Network name used as TLD for DNS: one jump host dual-homed on
       ``Engineering`` + ``Ops`` resolves ``auth.Engineering`` and
       ``logs.Ops`` to distinct peers.  TestRange does not register
       bare names — every cross-VM lookup is explicit about which
       network it belongs to.
   * - ``static_ip_lab.py``
     - ``VirtualNetwork(dhcp=False, ...)`` disables libvirt's DHCP
       server.  Every NIC on that network must come with an explicit
       ``ip=`` on its ``vNIC`` — the orchestrator
       enforces this at provisioning time.
   * - ``two_networks_three_vms.py``
     - The flagship networking example.  Two networks (NAT +
       isolated), three VMs (one public-facing nginx, one dual-homed
       client, one private-only nginx), assertions that walk every
       interesting reachability path: outbound internet, public peer
       by FQDN, private peer by static IP.

Communication backends
----------------------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - File
     - What it shows
   * - ``ssh_communicator.py``
     - Swap the default QEMU guest-agent communicator for an SSH
       one.  Exercises both auth paths (ephemeral ed25519 key, and
       the credential's plaintext password) and asserts both return
       the same hostname as the guest-agent channel.  See
       :doc:`communication`.
   * - ``winrm_communicator.py``
     - End-to-end Windows flow: ``TESTRANGE_WIN_ISO`` points at a
       Win10/11 install ISO, TestRange runs Setup under OVMF with an
       autounattend seed, caches the disk, boots the run-phase
       domain, and talks over WinRM on TCP 5985.  First run takes
       15–30 minutes (Setup is not fast); subsequent runs are
       seconds.  See :doc:`windows`.

Concurrency
-----------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - File
     - What it shows
   * - ``concurrency.py``
     - Passing ``-j N`` to ``testrange run`` dispatches up to ``N``
       tests to a thread pool.  Install-phase subnets are
       auto-serialised under a cross-process file lock, so concurrent
       install phases never collide; user-declared
       :class:`~testrange.backends.libvirt.VirtualNetwork` subnets are
       *not* auto-rewritten and must be non-overlapping.  Results
       come back in input order regardless of completion order.

Running the whole set
---------------------

Every example is a standalone ``testrange run`` target, so the
simplest smoke test is a ``for`` loop.  Example excludes the Windows
one (it requires ``TESTRANGE_WIN_ISO``):

.. code-block:: bash

   for ex in examples/*.py; do
       case "$(basename "$ex")" in
           winrm_communicator.py) continue ;;
       esac
       echo "=== $ex ==="
       testrange run "$ex:gen_tests" || exit 1
   done

First pass through the loop populates the base-image and post-install
caches; subsequent passes are dominated by boot + handshake time.
