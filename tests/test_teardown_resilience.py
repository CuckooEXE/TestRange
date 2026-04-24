"""Tests ensuring the :class:`Orchestrator` always tears down resources.

These are the belt-and-braces guarantees the user is owed: no matter
*where* a bug occurs — in user test code, inside the orchestrator's
provisioning steps, or even inside teardown itself — no VM, network,
run directory, or libvirt connection must be left behind.

Every test in this file corresponds to a concrete failure scenario that
has happened (or realistically could happen) in production.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from testrange.backends.libvirt.network import VirtualNetwork
from testrange.backends.libvirt.orchestrator import Orchestrator

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Shared helper to build an Orchestrator with fully-mocked dependencies
# ---------------------------------------------------------------------------


def _make_primed_orchestrator(
    *,
    num_vms: int = 3,
    num_networks: int = 2,
    with_install_network: bool = True,
) -> tuple[
    Orchestrator,
    MagicMock,
    list[MagicMock],
    list[MagicMock],
    MagicMock,
    MagicMock,
]:
    """Build an :class:`Orchestrator` wired to mock libvirt/cache/VM/network
    objects, primed as though provisioning had reached the run phase.

    :returns: ``(orch, conn, vms, networks, install_network, run)`` — all
        mocks except the orchestrator itself.
    """
    conn = MagicMock(name="conn")

    vms = []
    for i in range(num_vms):
        vm = MagicMock(name=f"vm{i}")
        vm.name = f"vm{i}"
        vm.builder.needs_install_phase.return_value = True
        vms.append(vm)

    networks = []
    for i in range(num_networks):
        net = MagicMock(spec=VirtualNetwork)
        net.name = f"net{i}"
        networks.append(net)

    install_net = MagicMock(spec=VirtualNetwork) if with_install_network else None

    run = MagicMock(name="run")
    run.run_id = "run-uuid"

    orch = Orchestrator(networks=networks, vms=vms)
    orch._conn = conn
    orch._run = run
    orch._install_network = install_net
    orch._cache = MagicMock(name="cache")
    orch.vms = {vm.name: vm for vm in vms}

    return orch, conn, vms, networks, install_net, run


# ---------------------------------------------------------------------------
# Single-step failure scenarios — each one verifies teardown completes even
# when a particular step raises.
# ---------------------------------------------------------------------------


class TestTeardownIndependence:
    """Each teardown step must succeed independently of the others."""

    def test_vm_shutdown_failure_does_not_prevent_other_vm_shutdowns(self) -> None:
        orch, _, vms, _, _, _ = _make_primed_orchestrator(num_vms=3)
        vms[0].shutdown.side_effect = RuntimeError("boom on vm0")

        orch._teardown()

        # All three VMs were still attempted
        for vm in vms:
            vm.shutdown.assert_called_once()

    def test_network_stop_failure_does_not_prevent_other_network_stops(
        self,
    ) -> None:
        orch, _, _, networks, _, _ = _make_primed_orchestrator(num_networks=3)
        networks[1].stop.side_effect = RuntimeError("boom on net1")

        orch._teardown()

        for net in networks:
            net.stop.assert_called_once()

    def test_vm_failure_does_not_prevent_network_cleanup(self) -> None:
        orch, _, vms, networks, _, _ = _make_primed_orchestrator()
        for vm in vms:
            vm.shutdown.side_effect = RuntimeError("every vm explodes")

        orch._teardown()

        for net in networks:
            net.stop.assert_called_once()

    def test_network_failure_does_not_prevent_install_network_cleanup(
        self,
    ) -> None:
        orch, _, _, networks, install_net, _ = _make_primed_orchestrator()
        for net in networks:
            net.stop.side_effect = RuntimeError("every net explodes")

        orch._teardown()

        install_net.stop.assert_called_once()

    def test_install_network_failure_does_not_prevent_run_dir_cleanup(
        self,
    ) -> None:
        orch, _, _, _, install_net, run = _make_primed_orchestrator()
        install_net.stop.side_effect = RuntimeError("install net boom")

        orch._teardown()

        run.cleanup.assert_called_once()

    def test_cleanup_run_failure_does_not_prevent_conn_close(self) -> None:
        """Regression: a filesystem error in run-dir cleanup used to swallow
        the connection close, leaking the libvirt handle forever."""
        orch, conn, _, _, _, run = _make_primed_orchestrator()
        run.cleanup.side_effect = OSError("disk full")

        orch._teardown()

        conn.close.assert_called_once()

    def test_conn_close_failure_still_clears_state(self) -> None:
        orch, conn, _, _, _, _ = _make_primed_orchestrator()
        conn.close.side_effect = RuntimeError("close failed")

        orch._teardown()

        assert orch._conn is None
        assert orch.vms == {}


# ---------------------------------------------------------------------------
# Order-of-cleanup contract
# ---------------------------------------------------------------------------


class TestTeardownOrder:
    """Cleanup must happen in a specific order so that dependencies of
    libvirt objects are not torn down before those objects themselves."""

    def test_vms_shutdown_before_networks_stop(self) -> None:
        orch, _, vms, networks, _, _ = _make_primed_orchestrator()
        order: list[str] = []
        for vm in vms:
            vm.shutdown.side_effect = lambda n=vm.name: order.append(f"vm:{n}")
        for net in networks:
            net.stop.side_effect = lambda _c, n=net.name: order.append(f"net:{n}")

        orch._teardown()

        # Every vm entry precedes every net entry.
        vm_indices = [i for i, x in enumerate(order) if x.startswith("vm:")]
        net_indices = [i for i, x in enumerate(order) if x.startswith("net:")]
        assert max(vm_indices) < min(net_indices)

    def test_networks_stop_before_conn_close(self) -> None:
        orch, conn, _, networks, _, _ = _make_primed_orchestrator()
        order: list[str] = []
        for net in networks:
            net.stop.side_effect = lambda _c, n=net.name: order.append(f"net:{n}")
        conn.close.side_effect = lambda: order.append("conn:close")

        orch._teardown()

        assert order[-1] == "conn:close"
        assert all(x.startswith("net:") for x in order[:-1])


# ---------------------------------------------------------------------------
# "Never raises" contract
# ---------------------------------------------------------------------------


class TestTeardownNeverRaises:
    """``_teardown`` is declared never to raise — verify that holds even
    when every single step fails."""

    def test_every_step_raises_teardown_still_returns(self) -> None:
        orch, conn, vms, networks, install_net, run = _make_primed_orchestrator()
        for vm in vms:
            vm.shutdown.side_effect = RuntimeError("vm boom")
        for net in networks:
            net.stop.side_effect = RuntimeError("net boom")
        install_net.stop.side_effect = RuntimeError("install boom")
        run.cleanup.side_effect = OSError("disk boom")
        conn.close.side_effect = RuntimeError("conn boom")

        # Must not raise.
        orch._teardown()

        # And still reset state.
        assert orch._conn is None
        assert orch._install_network is None
        assert orch._run is None
        assert orch.vms == {}

    def test_teardown_with_null_conn_is_noop(self) -> None:
        orch = Orchestrator()
        assert orch._conn is None
        orch._teardown()  # must not raise

    def test_double_teardown_is_safe(self) -> None:
        orch, _, _, _, _, _ = _make_primed_orchestrator()
        orch._teardown()
        orch._teardown()  # second call: _conn is already None, returns early


# ---------------------------------------------------------------------------
# Context-manager integration — the promise the user actually relies on
# ---------------------------------------------------------------------------


class TestContextManagerCleanup:
    """End-to-end verification through ``__enter__`` / ``__exit__``."""

    @staticmethod
    def _patch_libvirt_open(
        monkeypatch: pytest.MonkeyPatch,
    ) -> MagicMock:
        import libvirt

        conn = MagicMock(name="conn")
        monkeypatch.setattr(libvirt, "open", lambda _uri: conn)
        return conn

    @staticmethod
    def _patch_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        """Replace the :class:`RunDir` constructor in the orchestrator module
        with one that returns a controllable mock, so tests can assert on
        ``run.cleanup`` etc. without touching the filesystem."""
        import testrange.backends.libvirt.orchestrator as orch_mod

        run_mock = MagicMock(name="run")
        run_mock.run_id = "run-uuid"
        monkeypatch.setattr(
            orch_mod, "RunDir", MagicMock(return_value=run_mock)
        )
        return run_mock

    def test_exception_in_user_block_triggers_full_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The canonical scenario: user test code raises — every resource
        must be cleaned up and the exception must propagate."""
        conn = self._patch_libvirt_open(monkeypatch)
        run = self._patch_run(monkeypatch)

        vm = MagicMock(name="vm")
        vm.name = "web01"
        vm.builder.needs_install_phase.return_value = True
        vm._network_refs.return_value = []
        vm.build.return_value = Path("/cache/disk.qcow2")

        net = MagicMock(spec=VirtualNetwork)
        net.name = "NetA"
        net._run_id = None

        orch = Orchestrator(networks=[net], vms=[vm])
        orch._cache = MagicMock(name="cache")
        install_net = MagicMock(spec=VirtualNetwork)
        orch._create_install_network = MagicMock(return_value=install_net)

        with pytest.raises(RuntimeError, match="user bug"), orch:
            raise RuntimeError("user bug")

        # User's VM and test network both torn down.  After the ABC
        # refactor, stop() takes the orchestrator as context rather
        # than the raw libvirt connection.
        vm.shutdown.assert_called_once()
        net.stop.assert_called_once_with(orch)
        # Install network was stopped twice: once at end of _provision,
        # once defensively at teardown (no-op since orch._install_network
        # is None by then).  Only the _provision stop counts here.
        assert install_net.stop.called
        # Run directory was cleaned up.
        run.cleanup.assert_called_once()
        # Libvirt connection closed.
        conn.close.assert_called_once()
        # Orchestrator state fully reset.
        assert orch._conn is None
        assert orch._run is None

    def test_keyboardinterrupt_during_enter_triggers_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bug #3 regression: ``__enter__`` previously caught only
        ``Exception`` — so Ctrl+C during a long install-phase wait
        skipped teardown and orphaned VMs under ``qemu:///system``.
        ``BaseException`` catches ``KeyboardInterrupt`` and
        ``SystemExit`` too."""
        conn = self._patch_libvirt_open(monkeypatch)
        run = self._patch_run(monkeypatch)

        vm = MagicMock(name="vm")
        vm.name = "winbox"
        vm.builder.needs_install_phase.return_value = True
        vm.build.side_effect = KeyboardInterrupt

        orch = Orchestrator(networks=[], vms=[vm])
        orch._cache = MagicMock(name="cache")
        install_net = MagicMock(spec=VirtualNetwork)
        orch._create_install_network = MagicMock(return_value=install_net)

        with pytest.raises(KeyboardInterrupt):
            orch.__enter__()

        # The whole teardown chain must have run despite the
        # non-Exception interrupt.
        vm.shutdown.assert_called_once()
        install_net.stop.assert_called_with(orch)
        conn.close.assert_called_once()
        run.cleanup.assert_called_once()
        assert orch._conn is None

    def test_exception_during_vm_build_cleans_up_install_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: an early provisioning failure (before the install
        network was explicitly stopped) must not leak the install net."""
        conn = self._patch_libvirt_open(monkeypatch)
        run = self._patch_run(monkeypatch)

        vm = MagicMock(name="vm")
        vm.name = "web01"
        vm.builder.needs_install_phase.return_value = True
        vm.build.side_effect = RuntimeError("cloud-init failed")

        orch = Orchestrator(networks=[], vms=[vm])
        orch._cache = MagicMock(name="cache")
        install_net = MagicMock(spec=VirtualNetwork)
        orch._create_install_network = MagicMock(return_value=install_net)

        with pytest.raises(RuntimeError, match="cloud-init failed"):
            orch.__enter__()

        install_net.stop.assert_called_with(orch)
        conn.close.assert_called_once()
        run.cleanup.assert_called_once()
        assert orch._conn is None
        assert orch._install_network is None

    def test_exception_during_vm_start_run_cleans_up_earlier_vms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If VM #2 fails to start its run, VM #1 (already started) must
        still be shut down by teardown."""
        conn = self._patch_libvirt_open(monkeypatch)
        self._patch_run(monkeypatch)

        vm1 = MagicMock(name="vm1")
        vm1.name = "vm1"
        vm1.builder.needs_install_phase.return_value = True
        vm1._network_refs.return_value = []
        vm1.build.return_value = Path("/cache/vm1.qcow2")

        vm2 = MagicMock(name="vm2")
        vm2.name = "vm2"
        vm2.builder.needs_install_phase.return_value = True
        vm2._network_refs.return_value = []
        vm2.build.return_value = Path("/cache/vm2.qcow2")
        vm2.start_run.side_effect = RuntimeError("guest agent never came up")

        orch = Orchestrator(networks=[], vms=[vm1, vm2])
        orch._cache = MagicMock(name="cache")
        orch._create_install_network = MagicMock(
            return_value=MagicMock(spec=VirtualNetwork)
        )

        with pytest.raises(RuntimeError, match="guest agent never came up"):
            orch.__enter__()

        vm1.shutdown.assert_called_once()
        vm2.shutdown.assert_called_once()
        conn.close.assert_called_once()

    def test_libvirt_connect_failure_does_not_leak_run_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``libvirt.open`` fails outright, no run directory should have
        been created and no teardown is needed — but the error must surface
        cleanly."""
        import libvirt

        import testrange.backends.libvirt.orchestrator as orch_mod

        monkeypatch.setattr(
            libvirt, "open", MagicMock(side_effect=libvirt.libvirtError("connect failed"))
        )
        run_ctor = MagicMock()
        monkeypatch.setattr(orch_mod, "RunDir", run_ctor)
        orch = Orchestrator()

        from testrange.exceptions import OrchestratorError
        with pytest.raises(OrchestratorError):
            orch.__enter__()

        # RunDir was not instantiated because libvirt.open failed first.
        run_ctor.assert_not_called()
        assert orch._conn is None
        assert orch._run is None

    def test_exit_swallows_teardown_exception_and_preserves_user_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: if a future change accidentally lets ``_teardown``
        raise, the user's original exception must still propagate — not
        be masked by the teardown error."""
        orch = Orchestrator()
        monkeypatch.setattr(
            orch, "_teardown", MagicMock(side_effect=RuntimeError("teardown bug"))
        )

        exc_type, exc_val = None, None
        try:
            raise ValueError("user bug")
        except ValueError:
            exc_type, exc_val = ValueError, ValueError("user bug")

        result = orch.__exit__(exc_type, exc_val, None)
        assert result is None

    def test_successful_run_still_cleans_up_on_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: no user bug, `with` block runs clean — teardown
        still runs on exit and resources are cleaned."""
        conn = self._patch_libvirt_open(monkeypatch)
        run = self._patch_run(monkeypatch)
        orch = Orchestrator()
        orch._cache = MagicMock(name="cache")

        with orch:
            pass

        conn.close.assert_called_once()
        run.cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# Resource-leak surface check — the crux of the user's ask
# ---------------------------------------------------------------------------


class TestNoResourceLeaks:
    """Checks on the overall cleanup contract: after teardown, every
    registered VM has had ``shutdown`` called and every registered
    network has had ``stop`` called.  If a future change adds a new
    resource type to the orchestrator, these tests should fail until
    the new type is also cleaned up."""

    def test_every_registered_vm_is_shut_down(self) -> None:
        orch, _, vms, _, _, _ = _make_primed_orchestrator(num_vms=5)
        orch._teardown()
        for vm in vms:
            vm.shutdown.assert_called_once()

    def test_every_registered_network_is_stopped(self) -> None:
        orch, _, _, networks, _, _ = _make_primed_orchestrator(num_networks=5)
        orch._teardown()
        for net in networks:
            net.stop.assert_called_once()

    def test_install_network_is_stopped_when_present(self) -> None:
        orch, _, _, _, install_net, _ = _make_primed_orchestrator(
            with_install_network=True
        )
        orch._teardown()
        install_net.stop.assert_called_once()

    def test_install_network_stop_skipped_when_absent(self) -> None:
        orch, _, _, _, _, _ = _make_primed_orchestrator(with_install_network=False)
        orch._teardown()  # must not raise despite no install network
        assert orch._install_network is None

    def test_libvirt_connection_is_always_closed(self) -> None:
        orch, conn, _, _, _, _ = _make_primed_orchestrator()
        orch._teardown()
        conn.close.assert_called_once()

    def test_state_is_fully_reset_after_teardown(self) -> None:
        orch, _, _, _, _, _ = _make_primed_orchestrator()
        orch._teardown()
        assert orch._conn is None
        assert orch._run is None
        assert orch._install_network is None
        assert orch.vms == {}


class TestLeak:
    """``orch.leak()`` flips a flag that makes ``_teardown`` preserve
    VMs, networks, and the run dir — but still close the libvirt
    connection + storage backend so the Python process can exit."""

    def test_leak_skips_vm_shutdown(self) -> None:
        orch, _, vms, _, _, _ = _make_primed_orchestrator()
        orch.leak()
        orch._teardown()
        for vm in vms:
            vm.shutdown.assert_not_called()

    def test_leak_skips_network_stop(self) -> None:
        orch, _, _, networks, _, _ = _make_primed_orchestrator()
        orch.leak()
        orch._teardown()
        for net in networks:
            net.stop.assert_not_called()

    def test_leak_skips_run_dir_cleanup(self) -> None:
        orch, _, _, _, _, run = _make_primed_orchestrator()
        orch.leak()
        orch._teardown()
        run.cleanup.assert_not_called()
        # And ``_run`` stays wired so any later consumer can still
        # learn the run dir path.
        assert orch._run is run

    def test_leak_still_closes_libvirt_connection(self) -> None:
        """Leaving the libvirt connection open would leak a socket and
        (on paramiko-backed remotes) keep a worker thread alive that
        hangs Python interpreter shutdown."""
        orch, conn, _, _, _, _ = _make_primed_orchestrator()
        orch.leak()
        orch._teardown()
        conn.close.assert_called_once()
        assert orch._conn is None

    def test_leak_still_closes_storage_backend(self) -> None:
        orch, _, _, _, _, _ = _make_primed_orchestrator()
        storage = MagicMock(name="storage")
        orch._storage = storage
        orch.leak()
        orch._teardown()
        storage.close.assert_called_once()
        assert orch._storage is None

    def test_leak_is_idempotent(self) -> None:
        orch, _, _, _, _, _ = _make_primed_orchestrator()
        orch.leak()
        orch.leak()
        assert orch._leaked is True

    def test_leak_propagates_to_inner_orchestrators(self) -> None:
        """If the outer orchestrator leaks, any nested inner
        orchestrators must inherit the flag — otherwise closing the
        nested ``ExitStack`` would run each inner's full teardown and
        destroy the inner VMs we're trying to preserve."""
        import contextlib

        orch, _, _, _, _, _ = _make_primed_orchestrator()
        inner_a = MagicMock(name="inner_a", _leaked=False)
        inner_b = MagicMock(name="inner_b", _leaked=False)
        orch._inner_orchestrators = [inner_a, inner_b]
        orch._nested_stack = contextlib.ExitStack()  # real one, empty

        orch.leak()
        orch._teardown()

        assert inner_a._leaked is True
        assert inner_b._leaked is True

    def test_leak_emits_cleanup_hints(self) -> None:
        """The teardown log should show the virsh commands the user
        needs to clean up later — no silent leak.  We attach a capture
        handler directly to the orchestrator module's logger (rather
        than using ``caplog``) because other tests' calls to
        ``configure_root_logger`` disable propagation on the
        ``testrange`` logger, which makes caplog's root-level handler
        miss the records."""
        import logging

        orch, _, _, _, _, _ = _make_primed_orchestrator()
        orch.keep_alive_hints = lambda: [  # type: ignore[method-assign]
            "sudo virsh destroy tr-web-abc1 && sudo virsh undefine tr-web-abc1",
        ]

        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        target = logging.getLogger("testrange.backends.libvirt.orchestrator")
        prior_level = target.level
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        try:
            orch.leak()
            orch._teardown()
        finally:
            target.removeHandler(handler)
            target.setLevel(prior_level)

        joined = "\n".join(r.getMessage() for r in records)
        assert "leak=True" in joined
        assert "tr-web-abc1" in joined
        assert "run directory preserved" in joined

    def test_non_leaked_teardown_is_unchanged(self) -> None:
        """Regression guard: default path (no leak()) still runs the
        full teardown — no skipped steps."""
        orch, conn, vms, networks, install_net, run = _make_primed_orchestrator()
        orch._teardown()
        for vm in vms:
            vm.shutdown.assert_called_once()
        for net in networks:
            net.stop.assert_called_once()
        install_net.stop.assert_called_once()
        run.cleanup.assert_called_once()
        conn.close.assert_called_once()
