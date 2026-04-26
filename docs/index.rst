TestRange
=========

**TestRange** is a pip-installable Python package for orchestrating
hypervisor-backed virtual machine environments for integration
testing.  The default backend drives KVM/QEMU through libvirt;
additional backends are peer implementations of the same abstract
surface (see :doc:`api/backends`).

It handles:

- Spinning up isolated or internet-connected virtual networks
- Provisioning Linux (and Windows) VMs from cloud images using
  cloud-init — just hand ``iso=`` an ``https://`` URL to an
  upstream cloud image
- Caching installed VM snapshots so subsequent runs start in seconds
  (the first run does the slow install; every run after that is a
  thin copy-on-write overlay on the cached primary disk)
- Talking to running VMs via a backend-native side channel — the
  default backend uses the QEMU guest agent over ``virtio-serial``
  so no network port is exposed to the host, and fully isolated
  networks can still be inspected

.. code-block:: bash

   pip install testrange


At a glance
-----------

.. code-block:: bash

   # Preview what a test factory will provision — no VMs booted
   testrange describe my_tests.py:tests

   # Provision + run — INFO-level progress on stderr
   testrange run my_tests.py:tests

   # Inspect the disk-image cache
   testrange cache-list


Test code stays declarative::

    def my_test(orchestrator):
        web = orchestrator.vms["web"]
        web.exec(["systemctl", "is-active", "nginx"]).check()
        web.upload("./nginx.conf", "/etc/nginx/sites-enabled/test.conf")
        assert "<h1>ok</h1>" in web.read_text("/var/www/html/index.html")


.. toctree::
   :maxdepth: 2
   :caption: User Guide

   usage/index

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/index

.. toctree::
   :maxdepth: 1
   :caption: Project

   changelog


Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
