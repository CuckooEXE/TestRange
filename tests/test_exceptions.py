"""Unit tests for :mod:`testrange.exceptions`."""

from __future__ import annotations

import pytest

from testrange.exceptions import (
    CacheError,
    CloudInitError,
    CommunicationError,
    GuestAgentError,
    ImageNotFoundError,
    NetworkError,
    OrchestratorError,
    SSHError,
    TestRangeError,
    VMBuildError,
    VMNotRunningError,
    VMTimeoutError,
    WinRMError,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "exc_cls",
        [
            CacheError,
            CloudInitError,
            CommunicationError,
            GuestAgentError,
            ImageNotFoundError,
            NetworkError,
            OrchestratorError,
            SSHError,
            VMBuildError,
            VMNotRunningError,
            VMTimeoutError,
            WinRMError,
        ],
    )
    def test_all_inherit_from_testrange_error(
        self, exc_cls: type[Exception]
    ) -> None:
        assert issubclass(exc_cls, TestRangeError)

    @pytest.mark.parametrize(
        "transport_exc",
        [GuestAgentError, SSHError, WinRMError],
    )
    def test_transport_errors_inherit_from_communication_error(
        self, transport_exc: type[Exception]
    ) -> None:
        assert issubclass(transport_exc, CommunicationError)

    def test_testrange_error_inherits_exception(self) -> None:
        assert issubclass(TestRangeError, Exception)

    def test_each_exception_is_its_own_leaf(self) -> None:
        # Regression: these are siblings under TestRangeError, not nested.
        assert not issubclass(ImageNotFoundError, CacheError)
        assert not issubclass(VMBuildError, VMTimeoutError)


class TestRaiseAndCatch:
    def test_raise_and_catch_as_base(self) -> None:
        with pytest.raises(TestRangeError) as excinfo:
            raise VMBuildError("boom")
        assert "boom" in str(excinfo.value)
