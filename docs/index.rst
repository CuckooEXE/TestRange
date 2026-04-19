TestRange
=========

**TestRange** is a pip-installable Python package for orchestrating
KVM/QEMU virtual machine environments for integration testing.

It handles:

- Spinning up isolated or internet-connected virtual networks
- Provisioning Linux (and Windows) VMs from cloud images using
  cloud-init — just hand ``iso=`` an ``https://`` URL to any
  upstream ``.qcow2`` / ``.img``
- Caching installed VM snapshots so subsequent runs start in seconds
  (the first run does the slow install; every run after that is a
  thin qcow2 overlay)
- Talking to running VMs via the QEMU Guest Agent over
  ``virtio-serial`` — **no network port is exposed to the host**,
  so fully isolated networks can still be inspected

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
