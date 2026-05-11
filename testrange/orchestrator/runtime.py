"""Orchestrator runtime.

Phase 4 wires the full lifecycle:
  preflight -> install -> run -> (test, Phase 5) -> cleanup

The Orchestrator brokers between Plan-time data and the driver/cache,
respecting the stovepipe rule: nothing in `testrange.builders`,
`testrange.communicators`, or `testrange.credentials` reaches into the
others — the Orchestrator pulls what each consumer needs from the
VMRecipe and hands it over.
"""

from __future__ import annotations

import signal
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.manager import CacheManager
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.posix import PosixCred
from testrange.drivers.base import HypervisorDriver
from testrange.drivers.libvirt import LibvirtDriver, LibvirtHypervisor
from testrange.exceptions import (
    DriverError,
    InstallTimeoutError,
    OrchestratorError,
    PreflightError,
)
from testrange.networks.base import Network, Switch
from testrange.plan import Plan
from testrange.state.schema import (
    PHASE_CLEANUP,
    PHASE_DONE,
    PHASE_INSTALL,
    PHASE_LEAKED,
    PHASE_RUN,
)
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms.handle import VMHandle
from testrange.vms.recipe import VMRecipe

_log = get_logger(__name__)

# Per-VM install timeout. Cloud-init + apt install can be slow.
_DEFAULT_INSTALL_TIMEOUT_S = 600.0

# Per-VM DHCP-lease timeout after the run VM starts.
_DEFAULT_LEASE_TIMEOUT_S = 120.0

# Transient install network — see PLAN.md note. Hardcoded subnet for v0;
# we'll move to a hashed-from-run-id derivation when conflict ever shows up.
_INSTALL_CIDR = "10.97.99.0/24"


@dataclass(frozen=True)
class TestResult:
    """Outcome of one test function. Phase 5 fills the test runtime."""

    name: str
    passed: bool
    error: str | None = None
    duration: float = 0.0

    def report_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        line = f"[{status}] {self.name} ({self.duration:.2f}s)"
        if self.error:
            line += f"\n      {self.error}"
        return line


@dataclass
class OrchestratorHandle:
    """Test-code-facing handle. Phase 5 wires per-VM IPs + communicators."""

    run_id: str
    vms: Mapping[str, VMHandle] = field(default_factory=dict)


class Orchestrator:
    """Phase-sequencing context manager.

    ``with Orchestrator(plan) as orch:`` brings the range up
    (preflight -> install -> run) and tears it down on `__exit__`.
    Per PLAN.md, every exception path goes through cleanup unless
    ``leak()`` has been called.
    """

    def __init__(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager | None = None,
        state_root: Path | None = None,
        run_id: str | None = None,
        install_timeout_s: float = _DEFAULT_INSTALL_TIMEOUT_S,
        lease_timeout_s: float = _DEFAULT_LEASE_TIMEOUT_S,
    ) -> None:
        self.plan = plan
        self.cache = cache_manager or CacheManager()
        self.run_id = run_id or new_run_id()
        self.install_timeout_s = install_timeout_s
        self.lease_timeout_s = lease_timeout_s
        self.driver: HypervisorDriver = self._build_driver()
        self._store = StateStore(run_dir_for(self.run_id, root=state_root))
        self._handle: OrchestratorHandle | None = None
        self._leak = False
        self._plan_name = plan.name or "plan"
        self._pool_backends: dict[str, str] = {}      # plan_name -> backend_name
        self._network_backends: dict[str, str] = {}   # plan_name -> backend_name
        self._post_install_paths: dict[str, Path] = {}  # vm_name -> cached disk path
        self._uploaded_bases: set[tuple[str, str]] = set()  # (pool_backend, vol_name)

    # ---- driver inference ---------------------------------------------

    def _build_driver(self) -> HypervisorDriver:
        hyp = self.plan.hypervisor
        if isinstance(hyp, LibvirtHypervisor):
            return LibvirtDriver(uri=hyp.connection)
        raise DriverError(f"unsupported hypervisor type: {type(hyp).__name__}")

    # ---- context manager ----------------------------------------------

    def __enter__(self) -> OrchestratorHandle:
        self._install_signal_handlers()
        self.driver.connect()
        try:
            report = self.driver.preflight(self.plan, cache_manager=self.cache)
            if not report:
                raise PreflightError(report.render())
            self._store.initialize(
                run_id=self.run_id,
                plan_name=self._plan_name,
                driver_class=self.driver.DRIVER_NAME,
                driver_uri=getattr(self.plan.hypervisor, "connection", ""),
            )
            try:
                self._install_phase()
                self._run_phase()
                self._handle = self._build_handle()
                self._bind_communicators()
            except Exception:
                _log.exception("bring-up failed; tearing down")
                self._teardown(force=True)
                raise
            return self._handle
        except Exception:
            self.driver.disconnect()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_val, exc_tb
        try:
            if self._leak:
                _log.warning("leak: skipping teardown; run state retained")
                self._store.set_phase(PHASE_LEAKED)
                self._store.release()
            else:
                if exc_type is not None:
                    _log.info("tearing down after %s", exc_type.__name__)
                self._teardown()
        finally:
            self._restore_signal_handlers()
            self.driver.disconnect()

    def leak(self) -> None:
        """Skip teardown on ``__exit__``. Use for live debugging."""
        self._leak = True

    # ---- signal handlers ----------------------------------------------

    def _install_signal_handlers(self) -> None:
        self._prior_signal_handlers: dict[int, Any] = {}

        def _handler(signum: int, _frame: FrameType | None) -> None:
            _log.warning("received signal %d; raising KeyboardInterrupt for cleanup", signum)
            raise KeyboardInterrupt(f"signal {signum}")

        sigs: tuple[int, ...] = (signal.SIGTERM,)
        if sys.platform != "win32":
            sigs += (signal.SIGHUP,)
        for sig in sigs:
            try:
                self._prior_signal_handlers[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError) as e:
                _log.debug("could not install handler for signal %d: %s", sig, e)

    def _restore_signal_handlers(self) -> None:
        for sig, prior in getattr(self, "_prior_signal_handlers", {}).items():
            try:
                signal.signal(sig, prior)
            except (ValueError, OSError):
                pass

    # ---- install phase ------------------------------------------------

    def _install_phase(self) -> None:
        self._store.set_phase(PHASE_INSTALL)
        hyp = self.plan.hypervisor

        # 1. Create user pools first (install overlays live there).
        for pool in hyp.pools:
            backend = self.driver.compose_resource_name(self.run_id, "pool", pool.name)
            self._store.record_intent(kind="pool", backend_name=backend, plan_name=pool.name)
            self.driver.create_pool(pool, backend)
            self._store.confirm(backend)
            self._pool_backends[pool.name] = backend

        # 2. Transient install network (internet=True for apt et al.).
        install_net_backend = self.driver.compose_resource_name(
            self.run_id, "install_network", "install"
        )
        install_net = Network("install", _INSTALL_CIDR, dhcp=True, dns=False)
        install_switch = Switch("install", install_net, internet=True)
        self._store.record_intent(
            kind="install_network",
            backend_name=install_net_backend,
            plan_name="install",
        )
        self.driver.create_network(install_net, install_switch, install_net_backend)
        self._store.confirm(install_net_backend)

        # 3. Per VM: cache hit -> skip; cache miss -> build install VM.
        for vm in hyp.vms:
            self._install_one_vm(vm, install_net_backend)

        # 4. Tear down install network (run phase uses user networks).
        self.driver.destroy_network(install_net_backend)
        self._store.forget(install_net_backend)

    def _ensure_base_in_pool(self, pool_backend: str, source_path: Path) -> Path:
        """Upload a host-side base image into the pool, idempotent per run.

        Returns the in-pool path. Cache files are named ``<sha>.bin``; the
        ``.bin`` stem gives content-addressed dedup across VMs in the same
        plan that share a base.
        """
        vol_name = f"tr_base_{source_path.stem}.qcow2"
        key = (pool_backend, vol_name)
        if key in self._uploaded_bases:
            return self.driver.upload_to_pool(pool_backend, vol_name, source_path)
        self._store.record_intent(
            kind="base_image",
            backend_name=vol_name,
            plan_name=None,
            pool_backend=pool_backend,
        )
        in_pool_path = self.driver.upload_to_pool(pool_backend, vol_name, source_path)
        self._store.confirm(vol_name, pool_backend=pool_backend)
        self._uploaded_bases.add(key)
        return in_pool_path

    def _install_one_vm(self, vm: VMRecipe, install_net_backend: str) -> None:
        if not vm.spec.nics:
            raise OrchestratorError(
                f"vm {vm.name!r} declares no NICs; cloud-init install needs at "
                "least one NIC for internet access during install"
            )

        builder = vm.builder
        if not isinstance(builder, CloudInitBuilder):
            raise OrchestratorError(
                f"vm {vm.name!r}: only CloudInitBuilder is supported in v0, "
                f"got {type(builder).__name__}"
            )

        base_info = self.cache.resolve(builder.base)
        config_hash = builder.config_hash(vm.spec, vm, base_sha=base_info.sha256)
        post_install_name = f"_post_install_{config_hash}"

        # Cache hit?
        try:
            cached = self.cache.local.resolve(post_install_name)
            self._post_install_paths[vm.name] = cached.path
            _log.info("vm %s: cache hit on %s", vm.name, config_hash)
            return
        except Exception:
            _log.info("vm %s: cache miss on %s; building install VM", vm.name, config_hash)

        pool_backend = self._pool_backends[vm.spec.os_drive.pool]
        install_vm_backend = self.driver.compose_resource_name(
            self.run_id, "install_vm", vm.name
        )
        install_disk_name = f"{install_vm_backend}.qcow2"
        install_seed_name = f"{install_vm_backend}-seed.iso"

        # Create install overlay
        self._store.record_intent(
            kind="install_disk",
            backend_name=install_disk_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        base_in_pool = self._ensure_base_in_pool(pool_backend, base_info.path)
        install_disk_path = self.driver.create_overlay_disk(
            pool_backend, install_disk_name, base_in_pool
        )
        self._store.confirm(install_disk_name, pool_backend=pool_backend)

        # Render + write seed
        seed_bytes = builder.render_seed(vm.spec, vm)
        self._store.record_intent(
            kind="install_seed",
            backend_name=install_seed_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        seed_path = self.driver.write_to_pool(pool_backend, install_seed_name, seed_bytes)
        self._store.confirm(install_seed_name, pool_backend=pool_backend)

        # Define + start install VM with ALL NICs on the install network
        install_network_refs = {nic.network: install_net_backend for nic in vm.spec.nics}
        self._store.record_intent(
            kind="install_vm",
            backend_name=install_vm_backend,
            plan_name=vm.name,
        )
        self.driver.create_vm(
            install_vm_backend,
            vm.spec,
            self._plan_name,
            os_disk_path=install_disk_path,
            seed_iso_path=seed_path,
            network_refs=install_network_refs,
        )
        self._store.confirm(install_vm_backend)
        self.driver.start_vm(install_vm_backend)

        # Poll for shutoff (the install runcmd ends with `poweroff`).
        self._wait_for_shutoff(install_vm_backend, vm.name)

        # Snapshot the post-install disk into the cache. The on-disk file is
        # owned by the hypervisor's service account (libvirt-qemu in system
        # mode), so stream it back to a user-readable temp file first, then
        # ingest from there.
        with tempfile.NamedTemporaryFile(
            prefix=f"tr_post_install_{vm.name}_",
            suffix=".qcow2",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self.driver.download_from_pool(pool_backend, install_disk_name, tmp_path)
            info = self.cache.local.add(tmp_path, name=post_install_name)
        finally:
            tmp_path.unlink(missing_ok=True)
        self._post_install_paths[vm.name] = info.path
        _log.info(
            "vm %s: cached post-install disk as %s (%s)",
            vm.name,
            config_hash,
            info.short_sha,
        )

        # Tear down install resources (transient; remove from state.json).
        self.driver.destroy_vm(install_vm_backend)
        self._store.forget(install_vm_backend)
        self.driver.delete_volume(pool_backend, install_seed_name)
        self._store.forget(install_seed_name)
        self.driver.delete_volume(pool_backend, install_disk_name)
        self._store.forget(install_disk_name)

    def _wait_for_shutoff(self, backend_name: str, vm_name: str) -> None:
        deadline = time.monotonic() + self.install_timeout_s
        last_state = "?"
        while time.monotonic() < deadline:
            state = self.driver.get_vm_power_state(backend_name)
            if state != last_state:
                _log.info("vm %s state: %s", vm_name, state)
                last_state = state
            if state == "shutoff":
                return
            time.sleep(2.0)
        raise InstallTimeoutError(
            f"vm {vm_name!r} did not power off within {self.install_timeout_s:.0f}s"
        )

    # ---- run phase ----------------------------------------------------

    def _run_phase(self) -> None:
        self._store.set_phase(PHASE_RUN)
        hyp = self.plan.hypervisor

        for switch in hyp.networks:
            for net in switch.networks:
                backend = self.driver.compose_resource_name(self.run_id, "network", net.name)
                self._store.record_intent(
                    kind="network",
                    backend_name=backend,
                    plan_name=net.name,
                )
                self.driver.create_network(net, switch, backend)
                self._store.confirm(backend)
                self._network_backends[net.name] = backend

        for vm in hyp.vms:
            pool_backend = self._pool_backends[vm.spec.os_drive.pool]
            run_disk_name = f"{vm.name}.qcow2"
            self._store.record_intent(
                kind="run_disk",
                backend_name=run_disk_name,
                plan_name=vm.name,
                pool_backend=pool_backend,
            )
            base_in_pool = self._ensure_base_in_pool(
                pool_backend, self._post_install_paths[vm.name]
            )
            run_disk_path = self.driver.create_overlay_disk(
                pool_backend,
                run_disk_name,
                base_in_pool,
            )
            self._store.confirm(run_disk_name, pool_backend=pool_backend)

            vm_backend = self.driver.compose_resource_name(self.run_id, "vm", vm.name)
            self._store.record_intent(
                kind="vm",
                backend_name=vm_backend,
                plan_name=vm.name,
            )
            self.driver.create_vm(
                vm_backend,
                vm.spec,
                self._plan_name,
                os_disk_path=run_disk_path,
                seed_iso_path=None,
                network_refs=self._network_backends,
            )
            self._store.confirm(vm_backend)
            self.driver.start_vm(vm_backend)

    # ---- handle for tests --------------------------------------------

    def _build_handle(self) -> OrchestratorHandle:
        vms_map: dict[str, VMHandle] = {}
        for vm in self.plan.hypervisor.vms:
            vms_map[vm.name] = VMHandle(
                name=vm.name,
                ip="",
                communicator=vm.communicator,
            )
        return OrchestratorHandle(run_id=self.run_id, vms=vms_map)

    # ---- communicator bind --------------------------------------------

    def _bind_communicators(self) -> None:
        """Discover each VM's IP and bind its communicator.

        Each Communicator's bind signature is its own (PLAN.md decision
        5); the orchestrator dispatches by type and hands each the
        inputs it needs.
        """
        assert self._handle is not None
        for vm in self.plan.hypervisor.vms:
            if not isinstance(vm.communicator, SSHCommunicator):
                _log.debug(
                    "vm %s: no SSHCommunicator (%s); skipping bind",
                    vm.name,
                    type(vm.communicator).__name__,
                )
                continue
            ip = self._discover_ip(vm)
            cred = self._lookup_credential(vm)
            vm.communicator.bind(host=ip, credential=cred)
            self._handle.vms[vm.name].ip = ip
            _log.info("vm %s: bound SSHCommunicator at %s", vm.name, ip)

    def _discover_ip(self, vm: VMRecipe) -> str:
        if not vm.spec.nics:
            raise OrchestratorError(
                f"vm {vm.name!r}: no NICs; cannot bind a network communicator"
            )
        first_nic = vm.spec.nics[0]
        net_backend = self._network_backends[first_nic.network]
        mac = self.driver.compose_mac(self._plan_name, vm.name, 0)
        deadline = time.monotonic() + self.lease_timeout_s
        while time.monotonic() < deadline:
            ip = self.driver.get_lease_ip(net_backend, mac)
            if ip:
                return ip
            time.sleep(2.0)
        raise OrchestratorError(
            f"vm {vm.name!r} did not acquire a DHCP lease on "
            f"{first_nic.network!r} within {self.lease_timeout_s:.0f}s"
        )

    def _lookup_credential(self, vm: VMRecipe) -> PosixCred:
        builder = vm.builder
        if not isinstance(builder, CloudInitBuilder):
            raise OrchestratorError(
                f"vm {vm.name!r}: only CloudInitBuilder is supported in v0"
            )
        if not isinstance(vm.communicator, SSHCommunicator):
            raise OrchestratorError(
                f"vm {vm.name!r}: communicator is not SSHCommunicator"
            )
        cred = builder.find_credential(vm.communicator.username)
        if cred is None:
            usernames = [c.username for c in builder.credentials]
            raise OrchestratorError(
                f"vm {vm.name!r}: SSHCommunicator({vm.communicator.username!r}) "
                f"has no matching credential in builder.credentials; "
                f"declared: {usernames}"
            )
        if not isinstance(cred, PosixCred):
            raise OrchestratorError(
                f"vm {vm.name!r}: credential for {vm.communicator.username!r} "
                f"is not a PosixCred (got {type(cred).__name__})"
            )
        return cred

    # ---- teardown ------------------------------------------------------

    def _teardown(self, *, force: bool = False) -> None:
        """LIFO teardown using state.json as the source of truth."""
        del force
        try:
            self._store.set_phase(PHASE_CLEANUP)
        except Exception as e:
            _log.warning("could not set cleanup phase: %s", e)
            return
        try:
            state = self._store.read()
        except Exception as e:
            _log.warning("could not read state for teardown: %s", e)
            return
        resources = list(reversed(state.resources))
        total = len(resources)
        if total == 0:
            _log.info("teardown: nothing to do (state has no resources)")
        else:
            _log.info("teardown: %d resource(s) to destroy (LIFO)", total)
        ok = 0
        failed = 0
        for idx, r in enumerate(resources, start=1):
            _log.info(
                "teardown [%d/%d] destroy %s %s", idx, total, r.kind, r.backend_name
            )
            try:
                self.driver.destroy(r.kind, r.backend_name, **dict(r.metadata))
                self._store.forget(r.backend_name)
                ok += 1
            except Exception as e:
                failed += 1
                _log.warning(
                    "teardown [%d/%d] %s %s failed: %s",
                    idx, total, r.kind, r.backend_name, e,
                )
        if total > 0:
            _log.info("teardown summary: %d ok, %d failed", ok, failed)
        try:
            remaining = self._store.read().resources
        except Exception:
            remaining = ()
        if not remaining:
            self._store.set_phase(PHASE_DONE)
            self._store.release()
            self._store.remove()
        else:
            _log.warning(
                "teardown: %d resource(s) still recorded in state; run id=%s",
                len(remaining), self.run_id,
            )


# ---- run_tests entry point ---------------------------------------------


def run_tests(
    tests: list[Callable[[OrchestratorHandle], None]],
    plan: Plan,
    *,
    cache_manager: CacheManager | None = None,
    fail_fast: bool = False,
    leak_on_failure: bool = False,
) -> list[TestResult]:
    """Bring the range up, execute the tests, tear it down.

    Tests run sequentially. Continue-on-failure is the default;
    ``fail_fast=True`` stops on the first failure. With
    ``leak_on_failure=True``, if any test fails the orchestrator skips
    teardown and the user can SSH in to debug; tear down later with
    ``testrange cleanup <run_id>``.
    """
    results: list[TestResult] = []
    o = Orchestrator(plan, cache_manager=cache_manager)
    with o as orch:
        _execute_tests(orch, tests, results, fail_fast=fail_fast)
        if leak_on_failure and any(not r.passed for r in results):
            _log.warning("--leak-on-failure: skipping teardown; run_id=%s", o.run_id)
            o.leak()
    return results


def _execute_tests(
    orch: OrchestratorHandle,
    tests: list[Callable[[OrchestratorHandle], None]],
    results: list[TestResult],
    *,
    fail_fast: bool,
) -> None:
    """Run tests sequentially, capture failures, append to ``results``."""
    for t in tests:
        name = getattr(t, "__name__", repr(t))
        start = time.monotonic()
        try:
            t(orch)
        except Exception as e:
            tb = traceback.format_exc()
            results.append(
                TestResult(
                    name=name,
                    passed=False,
                    error=tb if tb.strip() else str(e),
                    duration=time.monotonic() - start,
                )
            )
            if fail_fast:
                _log.warning("--fail-fast: stopping on %s", name)
                return
            continue
        results.append(
            TestResult(name=name, passed=True, duration=time.monotonic() - start)
        )


__all__ = [
    "Orchestrator",
    "OrchestratorHandle",
    "TestResult",
    "run_tests",
]
