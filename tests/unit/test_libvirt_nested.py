"""Inner-binding helpers for nested virt (BACKEND-10): URI, profile, readiness."""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence

import pytest

from testrange.communicators.base import ExecResult
from testrange.drivers.libvirt import LibvirtDriver
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.drivers.libvirt._nested import (
    inner_libvirt_profile,
    inner_ssh_uri,
    wait_libvirtd_ready,
)
from testrange.exceptions import OrchestratorError


class TestInnerSshUri:
    def test_carries_user_host_keyfile_and_flags(self) -> None:
        uri = inner_ssh_uri("10.50.0.42", "admin", keyfile="/tmp/k")
        parsed = urllib.parse.urlparse(uri)
        assert parsed.scheme == "qemu+ssh"
        assert parsed.username == "admin"
        assert parsed.hostname == "10.50.0.42"
        assert parsed.path == "/system"
        q = urllib.parse.parse_qs(parsed.query)
        assert q["keyfile"] == ["/tmp/k"]
        assert q["no_verify"] == ["1"]
        assert q["sshauth"] == ["privkey"]

    def test_absolute_keyfile_survives_encoding(self) -> None:
        uri = inner_ssh_uri("h", "u", keyfile="/var/tmp/tr key.pem")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
        assert q["keyfile"] == ["/var/tmp/tr key.pem"]


class TestInnerProfile:
    def test_builds_a_libvirt_driver_over_ssh(self) -> None:
        profile = inner_libvirt_profile(
            "10.50.0.42", "admin", keyfile="/tmp/k", uplinks={"egress": "br-egress"}
        )
        assert profile.scheme == "libvirt"
        assert profile.uplinks == {"egress": "br-egress"}
        driver = profile.build_driver()
        assert isinstance(driver, LibvirtDriver)
        # The teardown URI round-trips the qemu+ssh connect URI.
        back = LibvirtConn.from_uri(driver.uri)
        assert back.libvirt_uri.startswith("qemu+ssh://admin@10.50.0.42/system")


class _FakeExec:
    """A GuestExec stub: fails the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(
        self, argv: Sequence[str], *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ExecResult(
                exit_code=1, stdout=b"", stderr=b"error: failed to connect", duration=0.0
            )
        return ExecResult(exit_code=0, stdout=b" Id   Name   State\n", stderr=b"", duration=0.0)


class TestWaitLibvirtdReady:
    def test_returns_once_virsh_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("testrange.drivers.libvirt._nested.time.sleep", lambda _s: None)
        ex = _FakeExec(fail_times=2)
        wait_libvirtd_ready(ex, timeout=60.0, poll=0.0)
        assert ex.calls == 3  # two failures then a success

    def test_times_out_when_libvirtd_never_comes_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("testrange.drivers.libvirt._nested.time.sleep", lambda _s: None)
        ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        monkeypatch.setattr("testrange.drivers.libvirt._nested.time.monotonic", lambda: next(ticks))
        with pytest.raises(OrchestratorError, match="libvirtd not ready"):
            wait_libvirtd_ready(_FakeExec(fail_times=99), timeout=2.0, poll=0.0)
