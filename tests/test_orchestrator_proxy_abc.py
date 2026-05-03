"""Tests for ``AbstractOrchestrator.proxy()`` — the ABC contract that
backends override to return their tunnel implementation.

The default raises :class:`NotImplementedError` (matching the
:meth:`AbstractOrchestrator.cleanup` pattern) so backends opt in
explicitly — silently returning ``None`` would mislead callers into
thinking the tunnel was open.

What this exercise pins:

* The default implementation raises a clear, name-bearing error so
  the operator knows *which* backend is missing the method.
* A subclass override is honoured: ``orch.proxy()`` returns the
  override's value.
* The error message points the user at remediation paths (sidecar
  VM or manual SSH tunnel) rather than just naming the gap.
"""

from __future__ import annotations

from typing import Any

import pytest

from testrange.orchestrator_base import AbstractOrchestrator


class _MinimalOrch(AbstractOrchestrator):
    """The smallest concrete subclass we can build for testing the
    default ``proxy()`` behaviour.

    Stubs out the abstract methods (``backend_type``, ``__enter__``,
    ``__exit__``) just enough to instantiate.  Everything else
    inherits from the ABC, so ``proxy()`` lands on the default
    implementation.
    """

    @classmethod
    def backend_type(cls) -> str:
        return "minimal-test"

    def __enter__(self) -> AbstractOrchestrator:
        raise NotImplementedError

    def __exit__(self, *args: Any) -> None:
        raise NotImplementedError


class _OverrideOrch(_MinimalOrch):
    """Subclass that overrides ``proxy()`` so the override path is
    exercised."""

    @classmethod
    def backend_type(cls) -> str:
        return "override-test"

    def proxy(self) -> Any:
        return "marker"


class TestDefaultProxy:
    def test_default_raises_not_implemented(self) -> None:
        """Backends that haven't wired ``proxy()`` raise
        :class:`NotImplementedError` so the failure is loud.  A
        silent ``None`` return would let the caller proceed and
        fail later with a confusing AttributeError."""
        orch = _MinimalOrch()
        with pytest.raises(NotImplementedError):
            orch.proxy()

    def test_default_message_names_the_class(self) -> None:
        """The error message names the concrete subclass so the
        operator immediately sees *which* backend is missing the
        method — important when a Hypervisor instance has multiple
        nested orchestrators of different types."""
        orch = _MinimalOrch()
        with pytest.raises(NotImplementedError, match="_MinimalOrch"):
            orch.proxy()

    def test_default_message_includes_remediation(self) -> None:
        """The message points the user at workarounds (sidecar VM,
        manual SSH tunnel) so they can keep moving even when the
        proxy isn't yet wired on their backend."""
        orch = _MinimalOrch()
        with pytest.raises(NotImplementedError) as excinfo:
            orch.proxy()
        msg = str(excinfo.value)
        assert "sidecar" in msg.lower() or "ssh" in msg.lower(), (
            f"NotImplementedError message should suggest a "
            f"workaround; got: {msg!r}"
        )


class TestProxyOverride:
    def test_subclass_override_is_honoured(self) -> None:
        """A subclass that overrides ``proxy()`` returns its value
        unchanged — no MRO surprises, no decorator-induced wrapping
        from the ABC."""
        orch = _OverrideOrch()
        assert orch.proxy() == "marker"
