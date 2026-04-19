Debugging a test plan with ``testrange repl``
==============================================

When a test fails because of a VM misconfiguration — a missing package,
a broken post-install command, a service that never started — it's often
faster to **inspect the running VMs interactively** than to add ``print``
statements and re-run.

``testrange repl`` provisions a test plan exactly the way ``testrange run``
would, then drops you into a Python REPL with the same names a test
function receives:

* ``orch`` — the started :class:`~testrange.backends.libvirt.Orchestrator`.
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
or hit Ctrl-D. Pass ``--keep`` to skip teardown and print the libvirt
domain/network names plus the run scratch dir, so you can keep poking
with ``virsh``, ``ssh``, etc.:

.. code-block:: console

   $ testrange repl ./my_tests.py:gen_tests --keep
   ...
   >>> exit()

   Run kept alive. To clean up manually:
     Domains:  tr-web-abcdef12
     Networks: tr-net-abcd
     Run dir:  /tmp/testrange-run-abcdef12
   Suggested:
     sudo virsh destroy tr-web-abcdef12 && sudo virsh undefine tr-web-abcdef12
     sudo virsh net-destroy tr-net-abcd && sudo virsh net-undefine tr-net-abcd
     rm -rf /tmp/testrange-run-abcdef12

Use ``--keep`` carefully — it leaks libvirt state if you forget to
clean up.

Watching an install-phase VM
----------------------------

Most of the time the orchestrator's install phase is a black box —
the logs say "waiting for builder to finish and power off (timeout
1800s)" and 15–30 minutes later the cached disk shows up.  When a
Windows install misbehaves (answer file rejected, Setup stuck at a
prompt) you need to *see the screen*.  Two complementary tools:

**Opt-in VNC** (``TESTRANGE_VNC=1``).  The default libvirt domain XML
is headless (``<graphics>`` omitted, QEMU runs ``-display none``).
Setting ``TESTRANGE_VNC=1`` in the environment of the ``testrange``
process tells
:func:`~testrange.backends.libvirt.VM._base_domain_xml` to attach a
``<graphics type='vnc' listen='127.0.0.1' autoport='yes'/>`` plus a
QXL video device.  The listener is pinned to ``127.0.0.1`` so nothing
is exposed beyond the host.  Find the port with
``virsh -c qemu:///system domdisplay <domain>``.

From a remote machine, tunnel the port over your existing SSH
connection:

.. code-block:: bash

   # On the host running testrange:
   DOM=$(virsh -c qemu:///system list --name | grep tr-build-winbox)
   virsh -c qemu:///system domdisplay "$DOM"   # prints vnc://127.0.0.1:<port>

   # From your local machine, in a new terminal:
   ssh -L 5900:127.0.0.1:5900 user@host
   open vnc://127.0.0.1:5900                   # macOS, any VNC client otherwise

**Terminal-only screenshots.**  If you're SSH-only and would rather
not set up a VNC client, libvirt can dump the framebuffer to a PPM
file directly — no ``TESTRANGE_VNC`` needed — and
``caca-utils``' ``img2txt`` renders it as colour ASCII:

.. code-block:: bash

   sudo apt-get install -y caca-utils
   DOM=tr-build-winbox-<id>
   while sleep 3; do
       virsh -c qemu:///system screenshot "$DOM" /tmp/vm.ppm >/dev/null 2>&1 \
           && clear && img2txt -W "$(tput cols)" /tmp/vm.ppm
   done

Good enough to distinguish "partitioning" from "copying files" from
"OOBE".  Terminals with sixel support (kitty, WezTerm, xterm-sixel)
can use ``img2sixel`` from ``libsixel-bin`` for a much sharper
render.

When the install reboots into the run phase the domain name changes
from ``tr-build-<vm>-<id>`` to ``tr-<vm>-<id>``; update ``DOM``.

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
