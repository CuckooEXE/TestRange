"""Orchestrator runtime.

Drives the lifecycle: preflight -> install -> run -> test -> cleanup. The
Orchestrator brokers between Plan-time data and the driver/cache, respecting
the stovepipe rule — nothing in `testrange.builders`,
`testrange.communicators`, or `testrange.credentials` reaches into the
others. The Orchestrator pulls what each consumer needs from the VMRecipe
and hands it over.
"""

from __future__ import annotations

import signal
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

from testrange._log import get_logger
from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.manager import CacheManager
from testrange.communicators.qga import QGACommunicator
from testrange.communicators.ssh import SSHCommunicator
from testrange.credentials.posix import PosixCred
from testrange.drivers import driver_for
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.exceptions import (
    BuildNotReadyError,
    CacheError,
    CacheMissError,
    InstallTimeoutError,
    OrchestratorError,
    PreflightError,
)
from testrange.networks.base import Network, NetworkAddressing, Switch
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

# Transient install network. Subnet must not collide with any user-declared
# network in the Plan; the driver's preflight validates that (the orchestrator
# passes this constant in as a kwarg so the driver doesn't reach upward).
# A single hardcoded subnet is fine for the single-run-per-host case; a
# hashed-from-run-id derivation would be needed for concurrent runs sharing a
# libvirtd.
INSTALL_NETWORK = Network("install", "10.97.99.0/24", dhcp=True, dns=False)


@dataclass(frozen=True)
class TestResult:
    """Outcome of one test function."""

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


@dataclass(frozen=True)
class OrchestratorHandle:
    """Test-code-facing handle.

    Exposes the run id, the live hypervisor driver, and the per-VM bound
    handles. Test code can reach the driver via ``orch.driver`` for
    backend-level operations not surfaced through a VM's communicator
    (e.g., snapshot, power-state queries).

    ``leak`` is a bound method on the parent :class:`Orchestrator`; call
    it to skip teardown on ``__exit__`` (useful for live debugging and
    for the ``testrange repl`` subcommand).
    """

    run_id: str
    driver: HypervisorDriver
    vms: Mapping[str, VMHandle]
    leak: Callable[[], None]


class Orchestrator:
    """Lifecycle context manager.

    ``with Orchestrator(plan) as orch:`` brings the range up
    (preflight -> install -> run) and tears it down on `__exit__`. Every
    exception path goes through cleanup unless ``leak()`` has been called.
    """

    def __init__(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager | None = None,
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
        self._store = StateStore(run_dir_for(self.run_id))
        self._handle: OrchestratorHandle | None = None
        self._leak = False
        self._plan_name = plan.name or "plan"
        self._pool_backends: dict[str, str] = {}  # plan_name -> backend_name
        self._network_backends: dict[str, str] = {}  # plan_name -> backend_name
        self._post_install_paths: dict[str, Path] = {}  # vm_name -> cached disk path
        self._uploaded_bases: set[tuple[str, str]] = set()  # (pool_backend, vol_name)
        # Builder-facing addressing map. The orchestrator brokers per the
        # stovepipe rule: builders never see a hypervisor type, they get the
        # one piece of info they need — per-network CIDR/prefix/gateway/dhcp.
        self._addressing: Mapping[str, NetworkAddressing] = {
            n.name: NetworkAddressing.from_network(n) for n in self._all_user_networks()
        }

    def _all_user_networks(self) -> Sequence[Network]:
        all_networks = getattr(self.plan.hypervisor, "all_networks", None)
        if all_networks is None:
            return ()
        return tuple(all_networks)

    def _build_driver(self) -> HypervisorDriver:
        return driver_for(self.plan.hypervisor)

    def __enter__(self) -> OrchestratorHandle:
        self._install_signal_handlers()
        self.driver.connect()
        try:
            report = self.driver.preflight(
                self.plan,
                cache_manager=self.cache,
                install_network=INSTALL_NETWORK,
            )
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
                self._wait_builder_ready()
            except Exception:
                _log.exception("bring-up failed; tearing down")
                self._teardown()
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
        install_net = INSTALL_NETWORK
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

    def _ensure_base_in_pool(self, pool_backend: str, source_path: Path) -> VolumeRef:
        """Upload a host-side base image into the pool, idempotent per run.

        Returns the in-pool path. The volume name is derived from the cache
        file's stem (a content sha), so multiple VMs sharing a base share
        the in-pool upload too.
        """
        vol_name = f"tr_base_{source_path.stem}{self.driver.volume_suffix('base_image')}"
        target_ref = self.driver.compose_volume_ref(pool_backend, vol_name)
        key = (pool_backend, vol_name)
        if key in self._uploaded_bases:
            return self.driver.upload_to_pool(target_ref, source_path)
        self._store.record_intent(
            kind="base_image",
            backend_name=vol_name,
            plan_name=None,
            pool_backend=pool_backend,
        )
        self.driver.upload_to_pool(target_ref, source_path)
        self._store.confirm(vol_name, pool_backend=pool_backend)
        self._uploaded_bases.add(key)
        return target_ref

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
        macs = tuple(
            self.driver.compose_mac(self._plan_name, vm.name, i) for i in range(len(vm.spec.nics))
        )
        config_hash = builder.config_hash(
            vm.spec,
            vm,
            addressing=self._addressing,
            base_sha=base_info.sha256,
            macs=macs,
        )
        post_install_name = f"_post_install_{config_hash}"

        # Cache hit? Manager checks local then HTTP (if configured); a hit
        # on the HTTP tier triggers a fetch into local before returning.
        try:
            cached = self.cache.resolve(post_install_name)
            assert cached.path is not None  # fetch=True guarantees this
            self._post_install_paths[vm.name] = cached.path
            _log.info("vm %s: cache hit on %s", vm.name, config_hash)
            return
        except CacheMissError:
            _log.info("vm %s: cache miss on %s; building install VM", vm.name, config_hash)
        except CacheError as e:
            # HTTP tier reachable but reported a non-404 error (e.g. 5xx).
            # Treat as a miss for resilience — local is source of truth —
            # but log loud enough to be noticed in CI.
            _log.warning(
                "vm %s: cache lookup error on %s (%s); building install VM",
                vm.name,
                config_hash,
                e,
            )

        pool_backend = self._pool_backends[vm.spec.os_drive.pool]
        install_vm_backend = self.driver.compose_resource_name(self.run_id, "install_vm", vm.name)
        install_disk_name = f"{install_vm_backend}{self.driver.volume_suffix('install_disk')}"
        install_seed_name = f"{install_vm_backend}-seed{self.driver.volume_suffix('install_seed')}"
        install_disk_ref = self.driver.compose_volume_ref(pool_backend, install_disk_name)
        install_seed_ref = self.driver.compose_volume_ref(pool_backend, install_seed_name)

        # Create install overlay
        self._store.record_intent(
            kind="install_disk",
            backend_name=install_disk_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        assert base_info.path is not None  # cache.resolve(fetch=True) materializes locally
        base_ref = self._ensure_base_in_pool(pool_backend, base_info.path)
        self.driver.create_disk_from_base(install_disk_ref, base_ref)
        self._store.confirm(install_disk_name, pool_backend=pool_backend)

        # Render + write seed
        seed_bytes = builder.render_seed(vm.spec, vm, addressing=self._addressing, macs=macs)
        self._store.record_intent(
            kind="install_seed",
            backend_name=install_seed_name,
            plan_name=vm.name,
            pool_backend=pool_backend,
        )
        self.driver.write_to_pool(install_seed_ref, seed_bytes)
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
            os_disk_ref=install_disk_ref,
            seed_iso_ref=install_seed_ref,
            network_refs=install_network_refs,
        )
        self._store.confirm(install_vm_backend)
        self.driver.start_vm(install_vm_backend)

        # Poll for shutoff (the install runcmd ends with `poweroff`).
        self._wait_for_shutoff(install_vm_backend, vm.name)

        # Snapshot the post-install disk into the cache. The pool volume is
        # not necessarily readable by the orchestrator process — drivers may
        # run the hypervisor under their own service account or on a remote
        # host — so we stream it back via the driver, into a local temp
        # file, then ingest from there.
        with tempfile.NamedTemporaryFile(
            prefix=f"tr_post_install_{vm.name}_",
            suffix=self.driver.volume_suffix("install_disk"),
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self.driver.download_from_pool(install_disk_ref, tmp_path)
            info = self.cache.add(tmp_path, name=post_install_name)
        finally:
            tmp_path.unlink(missing_ok=True)
        assert info.path is not None  # manager.add returns local-flavored info
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
        self.driver.delete_volume(install_seed_ref)
        self._store.forget(install_seed_name)
        self.driver.delete_volume(install_disk_ref)
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
            run_disk_name = f"{vm.name}{self.driver.volume_suffix('run_disk')}"
            run_disk_ref = self.driver.compose_volume_ref(pool_backend, run_disk_name)
            self._store.record_intent(
                kind="run_disk",
                backend_name=run_disk_name,
                plan_name=vm.name,
                pool_backend=pool_backend,
            )
            base_ref = self._ensure_base_in_pool(pool_backend, self._post_install_paths[vm.name])
            self.driver.create_disk_from_base(run_disk_ref, base_ref)
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
                os_disk_ref=run_disk_ref,
                seed_iso_ref=None,
                network_refs=self._network_backends,
            )
            self._store.confirm(vm_backend)
            self.driver.start_vm(vm_backend)

    def _build_handle(self) -> OrchestratorHandle:
        vms_map: dict[str, VMHandle] = {
            vm.name: VMHandle(
                name=vm.name,
                backend_name=self.driver.compose_resource_name(self.run_id, "vm", vm.name),
                communicator=vm.communicator,
            )
            for vm in self.plan.hypervisor.vms
        }
        return OrchestratorHandle(
            run_id=self.run_id,
            driver=self.driver,
            vms=vms_map,
            leak=self.leak,
        )

    def _bind_communicators(self) -> None:
        """Bind each VM's communicator at run-phase bring-up.

        Each Communicator declares its own ``bind`` signature; the orchestrator
        dispatches by communicator type and hands each one the inputs it needs.
        Transport-specific state (IPs, callables) lives on the bound
        communicator, not on VMHandle. The ``isinstance`` ladder is the
        sanctioned trust boundary between the user's Plan and dispatch.
        """
        assert self._handle is not None
        for vm in self.plan.hypervisor.vms:
            comm = vm.communicator
            if isinstance(comm, SSHCommunicator):
                ip = self._discover_ip(vm)
                cred = self._lookup_credential(vm)
                comm.bind(host=ip, credential=cred)
                _log.info("vm %s: bound SSHCommunicator at %s", vm.name, ip)
            elif isinstance(comm, QGACommunicator):
                backend = self.driver.compose_resource_name(self.run_id, "vm", vm.name)
                comm.bind(
                    execute=self.driver.native_guest_execute(backend),
                    read_file=self.driver.native_guest_read_file(backend),
                    write_file=self.driver.native_guest_write_file(backend),
                )
                _log.info("vm %s: bound QGACommunicator via %s", vm.name, backend)
            else:
                _log.debug(
                    "vm %s: communicator %s not bindable; skipping",
                    vm.name,
                    type(comm).__name__,
                )

    def _wait_builder_ready(self) -> None:
        """Drive each builder's readiness check via the bound communicator.

        The builder runs its own readiness command through the injected
        ``execute`` callable (``vm.communicator.execute`` — whatever the
        communicator is) and raises :class:`BuildNotReadyError` itself.
        Builders never see a Communicator type; the orchestrator only
        brokers the callable and tags failures with the VM name.
        """
        for vm in self.plan.hypervisor.vms:
            try:
                vm.builder.wait_ready(vm.spec, vm, vm.communicator.execute)
            except BuildNotReadyError as e:
                raise BuildNotReadyError(f"vm {vm.name!r}: {e}") from e

    def _discover_ip(self, vm: VMRecipe) -> str:
        """Resolve the IPv4 address of the VM's **first** declared NIC.

        Static (``nic.ipv4 is not None``): return the declared address
        directly — the staged run-phase netplan applies it on the first
        run-phase boot.

        DHCP: poll the driver for the lease keyed on the stable MAC derived
        from ``(plan_name, vm_name, nic_idx=0)`` until ``lease_timeout_s``
        elapses. Raises :class:`OrchestratorError` on timeout.
        """
        if not vm.spec.nics:
            raise OrchestratorError(f"vm {vm.name!r}: no NICs; cannot resolve an address")
        first_nic = vm.spec.nics[0]
        if first_nic.ipv4 is not None:
            return first_nic.ipv4
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
            raise OrchestratorError(f"vm {vm.name!r}: only CloudInitBuilder is supported in v0")
        if not isinstance(vm.communicator, SSHCommunicator):
            raise OrchestratorError(f"vm {vm.name!r}: communicator is not SSHCommunicator")
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

    def _teardown(self) -> None:
        """LIFO teardown using state.json as the source of truth."""
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
            _log.info("teardown [%d/%d] destroy %s %s", idx, total, r.kind, r.backend_name)
            try:
                self.driver.destroy(r.kind, r.backend_name, **dict(r.metadata))
                self._store.forget(r.backend_name)
                ok += 1
            except Exception as e:
                failed += 1
                _log.warning(
                    "teardown [%d/%d] %s %s failed: %s",
                    idx,
                    total,
                    r.kind,
                    r.backend_name,
                    e,
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
                len(remaining),
                self.run_id,
            )


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
        results.append(TestResult(name=name, passed=True, duration=time.monotonic() - start))


__all__ = [
    "Orchestrator",
    "OrchestratorHandle",
    "TestResult",
    "run_tests",
]
