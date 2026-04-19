Packages
========

Packages are declared on the VM spec (``pkgs=[...]``) and installed
during the one-time install phase.  They're baked into the cached
disk image, so subsequent runs of a VM with the same package list
reuse the cache and skip installation entirely.

Native vs. runcmd
-----------------

The cloud-init builder splits the package list into two buckets:

1. **Native packages** (apt, dnf) go into cloud-init's ``packages:``
   key.  Cloud-init handles repo refresh, dependency resolution, and
   retries on transient failures.  This is the fast path.

2. **Non-native packages** (pip, brew, winget) are emitted as
   ``runcmd`` entries — shell commands that run after native packages
   have installed.  This lets a Homebrew install (which itself needs
   ``curl`` and a user account) layer on top of a freshly installed
   distro.

You can mix types freely in a single ``pkgs=[...]`` list; the split
is automatic.

.. note::

    :class:`~testrange.packages.Homebrew` requires at least one
    non-root :class:`~testrange.credentials.Credential` in the VM's
    ``users`` list — Homebrew refuses to install as root.  The
    cloud-init builder validates this at build time and raises
    :class:`~testrange.exceptions.CloudInitError` with an actionable
    message if no suitable user exists.

Reference
---------

.. autoclass:: testrange.packages.Apt
   :members:
   :show-inheritance:

.. autoclass:: testrange.packages.Dnf
   :members:
   :show-inheritance:

.. autoclass:: testrange.packages.Homebrew
   :members:
   :show-inheritance:

.. autoclass:: testrange.packages.Pip
   :members:
   :show-inheritance:

.. autoclass:: testrange.packages.Winget
   :members:
   :show-inheritance:

.. autoclass:: testrange.packages.AbstractPackage
   :members:
   :show-inheritance:
