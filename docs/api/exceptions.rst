Exceptions
==========

All TestRange-raised errors inherit from a common base so you can
catch broadly for teardown or fine-grained for recovery.  Library
internals never raise bare :class:`Exception`; test code should feel
free to catch :class:`~testrange.exceptions.TestRangeError` around a
test body if it wants to distinguish "our bug" from "the VM lied".

.. automodule:: testrange.exceptions
   :members:
   :show-inheritance:
