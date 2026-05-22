"""testrange's error hierarchy.

All testrange-raised exceptions inherit from ``TestRangeError``.
Specific concerns get their own subclass so callers can catch narrowly.
"""

from __future__ import annotations


class TestRangeError(Exception):
    """Base class for every error raised by testrange."""


class PlanError(TestRangeError):
    """A user-supplied Plan is structurally invalid (bad references, duplicate names, etc.)."""


class PreflightError(TestRangeError):
    """Preflight surfaced one or more error-level findings."""


class CacheError(TestRangeError):
    """Cache layer failure."""


class CacheMissError(CacheError):
    """A CacheEntry could not be resolved against any tier."""


class DriverError(TestRangeError):
    """Hypervisor driver failure."""


class GuestAgentError(DriverError):
    """A hypervisor's native guest agent (QGA / VMware Tools / ...) command failed."""


class StateError(TestRangeError):
    """State-file read/write/parse error."""


class StateLockedError(StateError):
    """The owning process is still alive; refuse to mutate state."""


class CommunicatorError(TestRangeError):
    """Communicator transport failure."""


class CommunicatorAlreadyBoundError(CommunicatorError):
    """A communicator was bound twice; construct a fresh instance per VM."""


class CommunicatorClosedError(CommunicatorError):
    """A communicator was used (or re-bound) after close(); it is one-shot."""


class BuilderError(TestRangeError):
    """Builder-side failure (render, seed authoring, etc.)."""


class BuildNotReadyError(BuilderError):
    """A brought-up VM never reached the builder-declared ready state."""


class OrchestratorError(TestRangeError):
    """Orchestrator-level failure (phase sequencing, lifecycle)."""


class BuildTimeoutError(OrchestratorError):
    """Build VM did not power off within the configured timeout."""


class BuildRequiredError(OrchestratorError):
    """``run --require-cache`` found one or more artifacts missing from the cache."""
