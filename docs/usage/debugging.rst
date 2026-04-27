Debugging a test plan with ``testrange repl``
==============================================

When a test fails because of a VM misconfiguration — a missing package,
a broken post-install command, a service that never started — it's often
faster to **inspect the running VMs interactively** than to add ``print``
statements and re-run.

``testrange repl`` provisions a test plan exactly the way ``testrange run``
would, then drops you into a Python REPL with the same names a test
function receives:

* ``orch`` — the started :class:`~testrange.Orchestrator`.
* ``vms`` — ``list[VM]``, in the order they were declared.
* One binding per VM, named after the VM (e.g. ``web``, ``db``), so you
  can type ``web.exec([...])`` instead of ``orch.vms["web"].exec([...])``.
  Names that would shadow Python builtins (``list``, ``dict``, ``id``…)
  are skipped — use ``orch.vms["list"]`` in that case.

Usage
-----

.. code-block:: bash

   testrange repl PATH:FACTORY [--test NAME] [--keep] [--log-level LEVEL]

``PATH:FACTORY`` accepts the same form as ``testrange run`` and
``testrange describe``. If the factory returns more than one test,
either pass ``--test NAME`` or pick one from the interactive prompt.

The REPL prefers `IPython <https://ipython.readthedocs.io>`_ if it's
installed (``pip install testrange[repl]``) and falls back to the
standard library's ``code.InteractiveConsole`` so the feature works on
any installation.

Worked example: the missing ``nginx``
-------------------------------------

Suppose ``my_tests.py`` defines a test that asserts nginx is running:

.. code-block:: python

   def smoke(orch):
       web = orch.vms["web"]
       web.exec(["systemctl", "is-active", "nginx"]).check()

The test fails because we forgot ``Apt("nginx")`` in ``pkgs=``. To
investigate without editing the file, drop into the REPL:

.. code-block:: console

   $ testrange repl ./my_tests.py:gen_tests --test smoke
   ...orchestrator brings the VMs up (cache-warm: a few seconds)...
   TestRange REPL — test 'smoke'
     orch          Orchestrator
     vms           list[VM] (1)
     web           VM

   Try:  web.exec(['uname', '-r']).stdout_text
   Ctrl-D or exit() to quit.
   >>> web.exec(["systemctl", "status", "nginx"])
   ExecResult(exit_code=4, stdout=b'',
              stderr=b'Unit nginx.service could not be found.\n')
   >>> web.exec(["dpkg", "-l", "nginx"]).exit_code
   1
   >>> exit()

That's the diagnosis: the package was never installed. Fix ``pkgs=`` and
re-run the test.

Keeping VMs alive after the REPL exits
--------------------------------------

By default the orchestrator's normal teardown runs when you ``exit()``
or hit Ctrl-D. Pass ``--keep`` to skip teardown and print the
backend-resource names plus the run scratch dir, so you can keep
poking with the backend's CLI, ``ssh``, etc.  The exact suggested
cleanup commands are produced by the backend (each backend's
:meth:`~testrange.orchestrator_base.AbstractOrchestrator.keep_alive_hints`
returns the right verbs for its own toolchain) — example output:

.. code-block:: console

   $ testrange repl ./my_tests.py:gen_tests --keep
   ...
   >>> exit()

   Run kept alive. To clean up manually:
     VMs:      tr-web-abcdef12
     Networks: tr-net-abcd
     Run dir:  /tmp/testrange-run-abcdef12
   Suggested:
     <backend-specific cleanup commands>
     rm -rf /tmp/testrange-run-abcdef12

Use ``--keep`` carefully — it leaks backend state if you forget to
clean up.

Cleaning up after a SIGKILL'd run
---------------------------------

When a TestRange process exits cleanly — normal completion, an
exception inside a test, even ``Ctrl+C`` — the orchestrator's
``__exit__`` runs and tears down every VM, network, and scratch
file it created.  When the process exits *un*\ cleanly (``kill -9``,
OOM killer, host reboot, anything that bypasses Python cleanup),
none of that runs and resources stay live.

``testrange cleanup`` reconstructs every resource the test factory
+ run id would have produced and best-effort destroys each.  Backend
resource names are deterministic functions of the spec and the run
id — so given both, TestRange can compute exactly what to delete.

.. code-block:: bash

    testrange cleanup ./my_tests.py:gen_tests \
        deadbeef-1111-2222-3333-444455556666

Find the run id in the original run's log output (the orchestrator
prints ``run id <uuid>`` on entry) or in the leftover scratch
directory at ``<cache_root>/runs/<uuid>/``.  Idempotent —
already-deleted resources are silently skipped, so it's safe to run
repeatedly.

Per-backend semantics:

* **libvirt.**  Destroys + undefines ``tr-<vm[:10]>-<runid[:8]>``,
  ``tr-build-<vm[:10]>-<runid[:8]>``, ``tr-<net[:6]>-<runid[:4]>``
  for each spec'd VM/network, plus the ephemeral install network
  ``tr-instal-<runid[:4]>``, plus the per-run scratch dir.
* **Proxmox.**  Destroys per-run clone VMIDs named
  ``tr-<vm[:10]>-<runid[:8]>`` plus the SDN vnets named
  ``<net[:4]><runid[:4]>`` for the run, plus the per-run install
  vnet named ``inst<runid[:4]>``.  PVE templates
  (``tr-template-<config_hash[:12]>``) are the install-once-clone-
  many cache and are *never* touched by per-run cleanup — use
  ``testrange proxmox-prune-templates`` to evict them
  (see :ref:`proxmox-template-cache`).  When a Proxmox-flavoured
  Hypervisor is involved (libvirt outer + inner
  ``ProxmoxOrchestrator``), the inner orchestrator's PVE-side
  state lives on the Hypervisor VM — destroying the outer VM
  removes everything; ``testrange cleanup`` against the outer
  factory currently doesn't walk into the inner orchestrator's
  PVE node, so a half-killed nested run may leave inner VMIDs +
  vnets behind on PVE that need ``qm destroy`` / ``pvesh delete
  /cluster/sdn/vnets/...`` by hand.
* **Other backends** (Hyper-V) raise
  :class:`NotImplementedError` until they wire their own
  :meth:`~testrange.orchestrator_base.AbstractOrchestrator.cleanup`.

When in doubt, ``testrange cleanup`` is always the right first
step before falling back to manual ``virsh destroy`` / ``qm
destroy`` / equivalent.

.. _proxmox-template-cache:

Proxmox template cache
~~~~~~~~~~~~~~~~~~~~~~

The Proxmox backend caches the result of each VM install as a PVE
template (``qm template``).  Subsequent runs for the same spec
(same ``cache_key``) skip the install entirely and ``qm clone`` the
template instead.  Templates persist across runs and across
``testrange cleanup`` invocations on purpose — they're the
expensive thing the cache exists to keep.

To inspect and evict the template cache:

.. code-block:: bash

    # Show every TestRange-managed template on the node.
    testrange proxmox-list-templates \
        --orchestrator proxmox://root:pw@pve.example.com/pve01

    # Delete every TestRange-managed template (full cache wipe).
    # The next run for any spec will re-install from scratch.
    testrange proxmox-prune-templates \
        --orchestrator proxmox://root:pw@pve.example.com/pve01 \
        --yes

    # Or evict a specific template by display name.  --name is
    # repeatable.
    testrange proxmox-prune-templates \
        --orchestrator proxmox://root:pw@pve.example.com/pve01 \
        --name tr-template-deadbeefcafe \
        --yes

Both commands open their own PVE connection and do **not** require
``__enter__`` — they're safe to run while a separate testrange run
is in progress (the prune command refuses to touch active per-run
clones since those don't carry the ``template`` flag).

Crash-recovery sweep
~~~~~~~~~~~~~~~~~~~~

If a previous testrange process was killed mid-install — after
``qm create`` but before ``qm template`` — the half-promoted VM
gets left behind with the target template's display name but no
``template`` flag.  On the next install attempt for the same spec
the orchestrator detects this case via
:func:`~testrange.backends.proxmox.vm._delete_orphan_templates` and
sweeps the orphan automatically before retrying ``qm create``.  No
operator action required, but the sweep is logged at WARNING so
you can spot post-incident.

Watching an install-phase VM
----------------------------

Most of the time the orchestrator's install phase is a black box —
the logs say "waiting for builder to finish and power off (timeout
1800s)" and 15–30 minutes later the cached disk shows up.  When a
Windows install misbehaves (answer file rejected, Setup stuck at a
prompt) you need to *see the screen*.  Two complementary tools.

.. note::

   The shell snippets below use the CLI tools that ship with one
   particular backend.  Other backends provide equivalent verbs
   through their own CLI; check that backend's docs for the matching
   commands.

**Opt-in VNC** (``TESTRANGE_VNC=1``).  By default the backend
defines a headless install-phase domain (no graphics device, no
display).  Setting ``TESTRANGE_VNC=1`` in the environment of the
``testrange`` process tells the backend to attach a VNC graphics
device pinned to ``127.0.0.1`` plus a QXL video device, so nothing
is exposed beyond the host.  Find the port via the backend's
domain-display command.

From a remote machine, tunnel the port over your existing SSH
connection:

.. code-block:: bash

   # On the host running testrange:
   DOM=$(virsh -c qemu:///system list --name | grep tr-build-winbox)
   virsh -c qemu:///system domdisplay "$DOM"   # prints vnc://127.0.0.1:<port>

   # From your local machine, in a new terminal:
   ssh -L 5900:127.0.0.1:5900 user@host
   open vnc://127.0.0.1:5900                   # macOS, any VNC client otherwise

**Terminal-only screenshots (sixel).**  If you're SSH-only and your
local terminal supports sixel (WezTerm, iTerm2, Kitty, Windows
Terminal 1.22+, ghostty, foot, mlterm, xterm launched with
``-ti vt340``), a ``while`` loop + ``virsh screenshot`` +
``img2sixel`` gives you a live low-res view of the framebuffer with
no VNC client, SSH tunnel, or ``TESTRANGE_VNC`` needed.  The
domain *does* need a video device — ``TESTRANGE_VNC=1`` adds a QXL
one; otherwise the install-phase domain is headless and
``screenshot`` errors with "no graphics device".

Requires ``TESTRANGE_VNC=1`` on the ``testrange`` run so the
install/run domains get the QXL device, plus ``libsixel-bin`` on
the host for ``img2sixel``:

.. code-block:: bash

   sudo apt-get install -y libsixel-bin

   # Find the running proxmox (or any) domain.  The builder-name
   # prefix is ``tr-build-<vm>-<id>`` during install and
   # ``tr-<vm>-<id>`` once the run phase has started — update the
   # pattern if you're watching a different VM.
   DOM=$(sudo virsh list --name | grep '^tr-proxmox-' | head -1)

   while sleep 3; do
       virsh -c qemu:///system screenshot "$DOM" /tmp/vm.ppm >/dev/null 2>&1 \
           && clear && img2sixel /tmp/vm.ppm
   done

.. note::

   Use ``virsh screenshot`` (which pulls the framebuffer via
   libvirt's privileged stream API) rather than
   ``virsh qemu-monitor-command --hmp '$DOM' 'screendump /tmp/s.ppm'``.
   The monitor command tells QEMU to open the file itself, and
   AppArmor's libvirt profile blocks QEMU from writing to arbitrary
   paths — you'll get a ``failed to open file ... Permission denied``
   that can't be fixed by chowning the file or pointing it at
   ``/var/lib/libvirt/qemu/``.  ``virsh screenshot`` sidesteps the
   profile entirely because libvirt writes the file.

**Terminal-only screenshots (ASCII fallback).**  If your terminal
doesn't do sixel, ``caca-utils``' ``img2txt`` renders colour ASCII
instead — good enough to tell "partitioning" from "copying files"
from "OOBE":

.. code-block:: bash

   sudo apt-get install -y caca-utils
   DOM=$(sudo virsh list --name | grep '^tr-build-' | head -1)
   while sleep 3; do
       virsh -c qemu:///system screenshot "$DOM" /tmp/vm.ppm >/dev/null 2>&1 \
           && clear && img2txt -W "$(tput cols)" /tmp/vm.ppm
   done

When the install reboots into the run phase the domain name changes
from ``tr-build-<vm>-<id>`` to ``tr-<vm>-<id>``; update the ``grep``
pattern.

Tips
----

* Iteration is fast. The first ``testrange repl`` for a given VM
  configuration pays the install cost; subsequent invocations hit the
  cache (see :doc:`caching`) and start in seconds.
* The REPL is full Python, so loops and helpers compose naturally::

      >>> for vm in vms:
      ...     print(vm.name, vm.exec(["uptime"]).stdout_text.strip())

* :meth:`~testrange.vms.base.AbstractVM.upload`,
  :meth:`~testrange.vms.base.AbstractVM.download`,
  :meth:`~testrange.vms.base.AbstractVM.read_text`, and
  :meth:`~testrange.vms.base.AbstractVM.write_text` are all available
  for poking at files inside the VM.
