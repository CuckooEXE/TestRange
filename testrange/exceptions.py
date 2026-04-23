"""Custom exceptions for the TestRange package.

All TestRange exceptions inherit from :class:`TestRangeError`, allowing
callers to catch the entire family with a single ``except TestRangeError``.
"""

from __future__ import annotations


class TestRangeError(Exception):
    """Base exception for all TestRange errors."""


class VMBuildError(TestRangeError):
    """Raised when a VM fails during the build/install phase.

    This typically means cloud-init did not complete successfully, a required
    package could not be installed, or the VM never reached a powered-off state
    within the build timeout.
    """


class VMTimeoutError(TestRangeError):
    """Raised when an operation on a VM exceeds its configured timeout.

    Common causes: guest agent not responding after boot, command hanging
    inside the VM, or the install phase taking longer than the build timeout.
    """


class VMNotRunningError(TestRangeError):
    """Raised when a runtime method is called on a VM that is not started.

    Call :meth:`~testrange.orchestrator_base.AbstractOrchestrator.__enter__`
    (or use the orchestrator as a context manager) before accessing VM methods.
    """


class CommunicationError(TestRangeError):
    """Base class for any error from a VM communication backend.

    Concrete transports raise more specific subclasses: :class:`GuestAgentError`,
    :class:`SSHError`, :class:`WinRMError`.
    """


class GuestAgentError(CommunicationError):
    """Raised when a guest-agent communicator reports an error response.

    Concrete backends map their native guest-agent transports onto this
    exception — the ``message`` attribute carries the raw error string
    the backend observed.
    """


class SSHError(CommunicationError):
    """Raised when an SSH communicator operation fails.

    Wraps paramiko transport/authentication errors and SFTP failures.
    """


class WinRMError(CommunicationError):
    """Raised when a WinRM communicator operation fails.

    Wraps pywinrm protocol errors and non-zero PowerShell exit statuses
    from control-plane operations (file read/write).
    """


class NetworkError(TestRangeError):
    """Raised when a virtual network cannot be created, started, or destroyed."""


class CacheError(TestRangeError):
    """Raised when a cache operation fails.

    This covers disk I/O errors, image-manipulation subprocess
    failures, and concurrent-access locking timeouts.
    """


class ImageNotFoundError(TestRangeError):
    """Raised when a VM image cannot be resolved.

    The ``iso=`` parameter on a VM spec must be either an absolute
    local path or an ``https://`` URL.
    """


class CloudInitError(TestRangeError):
    """Raised when cloud-init configuration generation fails.

    For example, if a :class:`~testrange.packages.Homebrew` package is
    requested but no non-root user credential is defined.
    """


class OrchestratorError(TestRangeError):
    """Raised when the orchestrator encounters an unrecoverable error.

    This is the catch-all for backend connection failures, unexpected
    domain states, and teardown errors.
    """
