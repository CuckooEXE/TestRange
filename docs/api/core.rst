Core: Test, Orchestrator, run_tests
====================================

Three classes make up the control flow of every TestRange run:

``Test``
    Declarative bundle of an
    :class:`~testrange.AbstractOrchestrator` configuration plus the
    Python function that should run against the resulting VMs.
    ``Test`` does not own any VMs itself — it's an inert spec until
    :meth:`~testrange.test.Test.run` is called.

``Orchestrator``
    The lifecycle owner.  Opens its connection to the underlying
    hypervisor, brings up an install-phase network, provisions or
    cache-hits each VM, brings up the test networks, boots every VM
    to a ready state, exposes them via
    :attr:`~testrange.AbstractOrchestrator.vms`, and tears everything
    back down on ``__exit__``.  Always used as a context manager so
    teardown is guaranteed.

``TestResult`` / ``run_tests``
    A dataclass capturing pass/fail/duration/traceback, plus a thin
    driver that iterates a list of tests and prints a human summary.
    Most users call ``run_tests`` from the CLI via ``testrange run``.

Design notes
------------

**Ephemeral by default.**  The orchestrator's run dir (under ``/tmp``)
and the install-phase virtual network are always destroyed at exit.
Only the persistent *cache* (see :doc:`cache`) survives across runs.

**One connection per test.**  The orchestrator opens its own
hypervisor connection in ``__enter__`` and closes it in ``__exit__``.
Concurrent tests need concurrent orchestrators.

**Teardown never raises.**  If anything fails during setup, the
orchestrator's ``_teardown`` path is defensively wrapped so the
underlying bug surfaces instead of being masked by a cleanup error.

Concurrency
-----------

:func:`~testrange.test.run_tests` accepts a ``concurrency=N`` keyword
(``testrange run -j N`` on the CLI) that dispatches tests to a
:class:`~concurrent.futures.ThreadPoolExecutor`.  Each test owns its
own orchestrator and hypervisor connection, so the only shared state
is the install-phase subnet pool (``192.168.240.0/24`` …
``192.168.254.0/24``) — pick + define + start is serialised via a
cross-process file lock in :mod:`testrange._concurrency`.  Test
payload (install, boot, verify) runs in parallel.

Concurrency is **not** auto-safe if two tests declare the same
user-defined :class:`~testrange.AbstractVirtualNetwork` subnet.  Give
each concurrent test a distinct address range, or run those tests
serially with ``concurrency=1``.

Reference
---------

.. autoclass:: testrange.test.Test
   :members:
   :show-inheritance:

.. autoclass:: testrange.test.TestResult
   :members:
   :show-inheritance:

.. autofunction:: testrange.test.run_tests

The abstract base class is documented in :doc:`backends`; the
default libvirt implementation is below.

.. autoclass:: testrange.backends.libvirt.Orchestrator
   :members:
   :show-inheritance:
