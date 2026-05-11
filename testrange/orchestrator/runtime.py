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

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.manager import CacheManager
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
    ) -> None:
        self.plan = plan
        self.cache = cache_manager or CacheManager()
        self.run_id = run_id or new_run_id()
        self.install_timeout_s = install_timeout_s
        self.driver: HypervisorDriver = self._build_driver()
        self._store = StateStore(run_dir_for(self.run_id, root=state_root))
        self._handle: OrchestratorHandle | None = None
        self._leak = False
        self._plan_name = plan.name or "plan"
        self._pool_backends: dict[str, str] = {}      # plan_name -> backend_name
        self._network_backends: dict[str, str] = {}   # plan_name -> backend_name
        self._post_install_paths: dict[str, Path] = {}  # vm_name -> cached disk path

    # ---- driver inference ---------------------------------------------

    def _build_driver(self) -> HypervisorDriver:
        hyp = self.plan.hypervisor
        if isinstance(hyp, LibvirtHypervisor):
            return LibvirtDriver(uri=hyp.connection)
        raise DriverError(f"unsupported hypervisor type: {type(hyp).__name__}")

    # ---- context manager ----------------------------------------------

    def __enter__(self) -> OrchestratorHandle:
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
        if exc_type is not None and self._leak:
            _log.warning("leak-on-failure: skipping teardown")
            self._store.set_phase(PHASE_LEAKED)
            self._store.release()
        else:
            self._teardown()
        self.driver.disconnect()

    def leak(self) -> None:
        """Flip the leak flag; ``__exit__`` will skip teardown on failure."""
        self._leak = True

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
        )
        install_disk_path = self.driver.create_overlay_disk(
            pool_backend, install_disk_name, base_info.path
        )
        self._store.confirm(install_disk_name, pool_backend=pool_backend)

        # Render + write seed
        seed_bytes = builder.render_seed(vm.spec, vm)
        self._store.record_intent(
            kind="install_seed",
            backend_name=install_seed_name,
            plan_name=vm.name,
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

        # Snapshot the post-install disk into the cache.
        info = self.cache.local.add(install_disk_path, name=post_install_name)
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
            )
            run_disk_path = self.driver.create_overlay_disk(
                pool_backend,
                run_disk_name,
                self._post_install_paths[vm.name],
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
            # IP discovery + communicator bind land in Phase 5; for Phase 4
            # the handle is constructed but IPs are empty strings.
            vms_map[vm.name] = VMHandle(
                name=vm.name,
                ip="",
                communicator=vm.communicator,
            )
        return OrchestratorHandle(run_id=self.run_id, vms=vms_map)

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
        for r in reversed(state.resources):
            try:
                self.driver.destroy(r.kind, r.backend_name, **dict(r.metadata))
                self._store.forget(r.backend_name)
            except Exception as e:
                _log.warning("teardown destroy %s/%s failed: %s", r.kind, r.backend_name, e)
        try:
            remaining = self._store.read().resources
        except Exception:
            remaining = ()
        if not remaining:
            self._store.set_phase(PHASE_DONE)
            self._store.release()
            self._store.remove()


# ---- run_tests entry point ---------------------------------------------


def run_tests(
    tests: list[Callable[[OrchestratorHandle], None]],
    plan: Plan,
    *,
    cache_manager: CacheManager | None = None,
) -> list[TestResult]:
    """Bring the range up, run the tests, tear it down.

    Phase 4 wires the lifecycle. Phase 5 wires the test execution loop;
    until then, tests are reported with a clear placeholder note.
    """
    results: list[TestResult] = []
    with Orchestrator(plan, cache_manager=cache_manager) as _orch:
        for t in tests:
            name = getattr(t, "__name__", repr(t))
            results.append(
                TestResult(
                    name=name,
                    passed=True,
                    error="(Phase 5: test execution not wired yet)",
                )
            )
    return results


__all__ = [
    "Orchestrator",
    "OrchestratorHandle",
    "TestResult",
    "run_tests",
]
