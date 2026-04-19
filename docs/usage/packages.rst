Packages
========

TestRange has first-class support for five package managers — two
native Linux ones, one native Windows one, plus pip and Homebrew for
cross-distro tooling.  All of them show up as ordinary list entries
under ``pkgs=[...]``; the cloud-init builder splits them internally
between the fast native path and the shell ``runcmd`` fallback.

Apt (Debian / Ubuntu)
---------------------

.. code-block:: python

    pkgs=[
        Apt("nginx"),
        Apt("postgresql-15"),
        Apt("htop"),
    ]

These go directly into cloud-init's ``packages:`` list.  Cloud-init
handles ``apt-get update``, dependency resolution, and retry on
transient mirror flakiness.  Version pinning is available via the
usual apt syntax — ``Apt("postgresql-15=15.5-*")``.

Dnf (Fedora / RHEL / Rocky / AlmaLinux / CentOS Stream)
-------------------------------------------------------

.. code-block:: python

    pkgs=[
        Dnf("nginx"),
        Dnf("postgresql-server"),
    ]

Behaves identically to :class:`~testrange.packages.Apt` from the
user's perspective — the difference is just which cloud-init manager
key gets used.

Pip (any distro with Python)
----------------------------

.. code-block:: python

    pkgs=[
        Pip("requests"),
        Pip("sqlalchemy", version="2.0.30"),
    ]

Emitted as ``runcmd`` entries.  The install uses the system Python
by default; if you need a specific interpreter or virtualenv, pass
your own shell via ``post_install_cmds``.

Homebrew (macOS or Linuxbrew)
-----------------------------

.. code-block:: python

    pkgs=[
        Homebrew("node"),
        Homebrew("ripgrep"),
    ]

Homebrew bootstraps itself on first use and then installs the listed
formulae.  **Requires at least one non-root user** because Homebrew
refuses to install as root; the cloud-init builder raises
:class:`~testrange.exceptions.CloudInitError` at build time if your
``users=[...]`` has only a root credential.

Winget (Windows)
----------------

.. code-block:: python

    pkgs=[
        Winget("Git.Git"),
        Winget("Microsoft.VisualStudioCode"),
    ]

The only package kind honoured on the Windows install path.  The
autounattend builder appends ``winget install`` commands to
``FirstLogonCommands`` with ``--accept-source-agreements`` and
``--accept-package-agreements`` so installs are non-interactive.  On a
Linux VM ``Winget`` entries are silently dropped; conversely, Apt /
Dnf / Pip / Homebrew entries are silently dropped on Windows.  See
:doc:`windows` for the full install-phase walkthrough.

Mixing package managers
-----------------------

You can combine any of these freely:

.. code-block:: python

    pkgs=[
        Apt("python3"),
        Apt("build-essential"),
        Pip("cryptography"),
        Pip("numpy"),
    ]

Order of install is native-first (so ``python3`` is present before
pip runs), then runcmd entries in the order listed.

Extending
---------

Custom package managers (nix, zypper, pacman, etc.) are straightforward
to add by subclassing
:class:`~testrange.packages.AbstractPackage`.  See
:doc:`extending`.
