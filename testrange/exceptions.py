"""testrange's error hierarchy.

All testrange-raised exceptions inherit from ``TestRangeError``.
Specific concerns get their own subclass so callers can catch narrowly.
"""

from __future__ import annotations

from testrange._ansi import scrub_terminal_control


class TestRangeError(Exception):
    """Base class for every error raised by testrange."""


class PlanError(TestRangeError):
    """A user-supplied Plan is structurally invalid (bad references, duplicate names, etc.)."""


class GraphError(PlanError):
    """The finalized :class:`~testrange.graph.build_graph.BuildGraph` is structurally invalid.

    A :class:`PlanError` subclass: a malformed graph is a malformed plan, so the
    existing invalid-plan exit-code path (and preflight) catches it without a new
    branch. The concrete subclasses below name the specific defect.
    """


class DuplicateNodeError(GraphError):
    """Two nodes share a name within one graph — node names must be unique."""


class DanglingDependencyError(GraphError):
    """An edge references a node name that is not present in the graph."""


class SelfDependencyError(GraphError):
    """An edge names the same node as both dependent and dependency (a self-loop)."""


class GraphCycleError(GraphError):
    """The dependency edges form a cycle, so no topological order exists."""


class PreflightError(TestRangeError):
    """Preflight surfaced one or more error-level findings."""


class CacheError(TestRangeError):
    """Cache layer failure."""


class CacheMissError(CacheError):
    """A CacheEntry could not be resolved against any tier."""


class DriverError(TestRangeError):
    """Hypervisor driver failure."""


class ProfileError(TestRangeError):
    """A ``--profile`` connection profile is missing, unreadable, or malformed."""


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


class GatewayError(TestRangeError):
    """A guest-reachability gateway was misconfigured or could not be established.

    Raised for non-retryable conditions (missing credentials, no usable
    transport). Transient connect/channel failures surface as the underlying
    transport's exception so a caller's retry loop can act on them.
    """


class BuilderError(TestRangeError):
    """Builder-side failure (render, seed authoring, etc.)."""


class BuildNotReadyError(BuilderError):
    """A brought-up VM never reached the builder-declared ready state."""


class OrchestratorError(TestRangeError):
    """Orchestrator-level failure (phase sequencing, lifecycle)."""


class BuildFailedError(BuilderError):
    """A build VM reported (or implied) a provisioning failure.

    Raised by the orchestrator when the build-result sink yields a ``fail``
    record, or when the build VM powers off without emitting the positive
    ``ok`` token (a guest that crashed mid-provision). Carries the failing
    command + its exit code and the decoded build log so the user sees *what*
    failed and *why*, instead of a silently-cached corrupt disk.

    Distinct from :class:`BuildTimeoutError`, which is the watchdog for a true
    wedge (a guest that never emits a record *and* never powers off).
    """

    def __init__(
        self,
        vm: str,
        *,
        rc: int | None = None,
        cmd: str | None = None,
        log: bytes = b"",
        detail: str | None = None,
    ) -> None:
        self.vm = vm
        self.rc = rc
        self.cmd = cmd
        self.log = log
        parts = [f"vm {vm!r}: build failed"]
        if detail is not None:
            parts.append(detail)
        elif cmd is not None:
            parts.append(f"command {cmd!r} exited {rc}")
        elif rc is not None:
            parts.append(f"exit code {rc}")
        message = "; ".join(parts)
        if log:
            decoded = scrub_terminal_control(log.decode("utf-8", "replace"))
            message += "\n--- build log ---\n" + decoded
        super().__init__(message)


class BuildTimeoutError(OrchestratorError):
    """Build VM did not power off within the configured timeout."""


class BuildRequiredError(OrchestratorError):
    """``run --require-cache`` found one or more artifacts missing from the cache."""
