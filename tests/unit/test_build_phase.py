"""Build phase against the MockDriver (ADR-0010 §2/§3/§4).

The build phase warms the cache and leaves the backend empty:

* every writable disk (OS + each data disk) lands in the cache as its own entry;
* the build VM boots with all those disks attached;
* nothing — build VM, disks, switch, sidecar, *or the build pool* — survives;
* a second build over a warm cache creates **no** backend resources at all.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.mock import MockDriver, MockHypervisor
from testrange.exceptions import BuildFailedError, OrchestratorError
from testrange.networks import Network, NetworkAddressing, Sidecar, Switch
from testrange.networks.sidecar import SIDECAR_DNSMASQ_CONF
from testrange.orchestrator.build_phase import build_phase
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.run_phase import run_phase
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms import VMRecipe, VMSpec


def _plan(*, data_disks: int = 1) -> Plan:
    devices: list[object] = [CPU(1), Memory(512), OSDrive("pool1", 8)]
    for _ in range(data_disks):
        devices.append(HardDrive("pool1", 16))
    devices.append(NetworkIface("netA", addr=DHCPAddr()))
    return Plan(
        MockHypervisor(
            networks=[
                Switch(
                    "sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True)
                )
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(name="web", devices=devices),  # type: ignore[arg-type]
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
        name="hello",
    )


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _ctx(plan: Plan, driver: MockDriver, cache: CacheManager) -> RunContext:
    run_id = new_run_id()
    store = StateStore(run_dir_for(run_id))
    store.initialize(run_id=run_id, plan_name=plan.name, driver_class="MockDriver", driver_uri="")
    switches = plan.hypervisor.networks
    addressing: Mapping[str, NetworkAddressing] = {
        n.name: NetworkAddressing.from_switch(s) for s in switches for n in s.networks
    }
    return RunContext(
        plan=plan,
        driver=driver,
        store=store,
        cache=cache,
        run_id=run_id,
        plan_name=plan.name,
        build_timeout_s=5.0,
        lease_timeout_s=5.0,
        addressing=addressing,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[CacheManager, MockDriver]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    base = tmp_path / "fake-base.qcow2"
    base.write_bytes(b"FAKE-BASE-DISK" * 100)
    cache.add(base, name="debian-13")
    sidecar = tmp_path / "fake-sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR" * 100)
    cache.add(sidecar, name="testrange-sidecar")
    driver = MockDriver(pool_root=tmp_path / "pools")
    driver.connect()
    return CacheManager(local=cache), driver


def _built_names(cache: CacheManager) -> list[str]:
    return sorted(
        name
        for info in cache.local.list_entries()
        for name in info.names
        if name.startswith("_built_")
    )


class TestBuildPhase:
    def test_captures_every_writable_disk(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        build_phase(_ctx(plan, driver, cache))

        # N+1 artifacts: one OS disk + one data disk.
        names = _built_names(cache)
        assert len(names) == 2
        assert any(n.endswith("__os") for n in names)
        assert any(n.endswith("__data0") for n in names)

    def test_build_vm_booted_with_all_disks(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=2)
        build_phase(_ctx(plan, driver, cache))

        # The build VM's create_vm carried two data-disk refs (the 4th arg).
        build_creates = [c for c in driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]]
        assert len(build_creates) == 1
        data_refs = build_creates[0][1][2]
        assert len(data_refs) == 2
        # Each data disk was provisioned blank + sized; the OS disk was resized.
        assert sum(1 for c in driver.calls if c[0] == "create_blank_volume") == 2
        assert any(c[0] == "resize_volume" for c in driver.calls)

    def test_backend_is_empty_after_build(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        build_phase(_ctx(plan, driver, cache))

        # Build VM + sidecar destroyed; build pool destroyed; switch torn down.
        assert driver._vms == {}
        assert driver._pools == set()
        assert driver._switches == {}
        assert any(c[0] == "destroy_pool" for c in driver.calls)

    def test_capture_temp_lands_on_cache_filesystem(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # CORE-4: the captured disk is downloaded to a temp file before it is
        # ingested. That temp must sit on the cache's filesystem — not the
        # system tempdir (often a small tmpfs /tmp), where a multi-GiB OS disk
        # ENOSPCs. Assert every download target is under the cache root.
        cache, driver = env
        plan = _plan(data_disks=1)
        build_phase(_ctx(plan, driver, cache))

        downloads = [Path(c[1][1]) for c in driver.calls if c[0] == "download_from_pool"]
        assert downloads, "expected at least one disk capture"
        for dest in downloads:
            assert cache.local.root in dest.parents, f"capture temp escaped cache fs: {dest}"

    def test_second_build_is_full_cache_hit(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        build_phase(_ctx(plan, driver, cache))

        # Second build over the warm cache: no backend resources at all.
        driver.calls = []
        ctx2 = _ctx(plan, driver, cache)
        build_phase(ctx2)
        creating = {"create_pool", "create_switch", "create_network", "create_vm"}
        assert not any(c[0] in creating for c in driver.calls)
        # ...and the run phase still gets its disk set populated from cache.
        assert set(ctx2.built_disk_paths["web"]) == {"os", "data0"}

    def test_drifted_sidecar_invalidates_build_cache(
        self, env: tuple[CacheManager, MockDriver], tmp_path: Path
    ) -> None:
        # CI-1: the sidecar serves DHCP/DNS/NAT during every build, so a
        # drifted sidecar image must move config_hash and force a rebuild —
        # not silently reuse the disks built against the old sidecar.
        cache, driver = env
        plan = _plan(data_disks=1)
        build_phase(_ctx(plan, driver, cache))
        first_names = set(_built_names(cache))
        assert first_names

        # Rebuild the sidecar (same pretty-name, different content sha).
        drifted = tmp_path / "drifted-sidecar.qcow2"
        drifted.write_bytes(b"DRIFTED-SIDECAR" * 100)
        cache.local.forget_name("testrange-sidecar")
        cache.local.add(drifted, name="testrange-sidecar")

        driver.calls = []
        ctx2 = _ctx(plan, driver, cache)
        build_phase(ctx2)

        # The drift is a cache miss: the build VM is stood up again...
        assert any(c[0] == "create_vm" and "build_vm" in c[1][0] for c in driver.calls)
        # ...and the rebuilt disks land under a *new* config_hash.
        assert set(_built_names(cache)) - first_names


class TestBuildResultSignaling:
    """ADR §21: success keys on the serial ``ok`` token, not power-off.

    These paths were untestable before the build-result sink — a mock VM that
    powered off always looked like success. Now the orchestrator reads the
    guest's explicit result and a failure raises before any disk is cached.
    """

    def test_success_reads_sink_then_gates_capture_on_shutoff(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # Success is keyed on the serial sink (not power-off-as-success), but
        # capture is still gated on the VM reaching shutoff so a live backend
        # doesn't read a disk out from under a still-running qemu (ORCH-7).
        cache, driver = env
        build_phase(_ctx(_plan(data_disks=0), driver, cache))
        kinds = [c[0] for c in driver.calls]
        assert "read_build_result_sink" in kinds  # the success signal
        assert driver.power_state_calls > 0  # capture gated on a shutoff poll
        # The result is read before the disk is captured.
        assert kinds.index("read_build_result_sink") < kinds.index("download_from_pool")

    def test_fail_record_raises_with_command_and_log(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        cache, driver = env
        log = b"E: Unable to fetch some archives\n"
        driver.build_result_stream = [
            b'TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"\n'
            b"TESTRANGE-LOG-BEGIN\n" + base64.b64encode(log) + b"\nTESTRANGE-LOG-END\n"
        ]
        with pytest.raises(BuildFailedError) as ei:
            build_phase(_ctx(_plan(data_disks=1), driver, cache))
        err = ei.value
        assert err.rc == 100
        assert err.cmd == "apt-get update"
        assert err.log == log
        assert log.decode() in str(err)  # the captured log is in the message

    def test_failed_build_caches_nothing(self, env: tuple[CacheManager, MockDriver]) -> None:
        # The corrupt-cache guard: a failed build must not leave a `_built_`
        # artifact behind for the run phase to pick up.
        cache, driver = env
        driver.build_result_stream = [b'TESTRANGE-RESULT: fail rc=1 cmd="false"\n']
        with pytest.raises(BuildFailedError):
            build_phase(_ctx(_plan(data_disks=1), driver, cache))
        assert _built_names(cache) == []

    def test_power_off_without_token_is_failure(self, env: tuple[CacheManager, MockDriver]) -> None:
        # Guest powered off (serial stream EOFs) without ever emitting `ok` —
        # a mid-provision crash. Must be a loud failure, not a silent success.
        cache, driver = env
        driver.build_result_stream = [b"[ booting ] cloud-init crashed\n"]
        with pytest.raises(BuildFailedError, match="without reporting a build result"):
            build_phase(_ctx(_plan(data_disks=0), driver, cache))

    def test_console_output_streams_to_log(
        self, env: tuple[CacheManager, MockDriver], caplog: pytest.LogCaptureFixture
    ) -> None:
        # Build chatter is mirrored to the console logger live; the protocol's
        # own framing (the RESULT line, the base64 log block) is not.
        cache, driver = env
        log = base64.b64encode(b"secret log bytes")
        driver.build_result_stream = [
            b"Cloud-init v. 24.1 running 'modules:final'\n",
            b"Setting up nginx (1.24.0-1)\n",
            b'TESTRANGE-RESULT: fail rc=1 cmd="false"\n'
            b"TESTRANGE-LOG-BEGIN\n" + log + b"\nTESTRANGE-LOG-END\n",
        ]
        with caplog.at_level(logging.DEBUG, logger="testrange.orchestrator.build_phase.console"):
            with pytest.raises(BuildFailedError):
                build_phase(_ctx(_plan(data_disks=0), driver, cache))
        streamed = [r.getMessage() for r in caplog.records if r.name.endswith(".console")]
        assert any("Setting up nginx" in m for m in streamed)  # build chatter shown
        assert not any("TESTRANGE-RESULT" in m for m in streamed)  # framing hidden
        assert not any(log.decode() in m for m in streamed)  # base64 block hidden


class TestBuildToRunDataDisk:
    """Data-disk content survives build -> cache -> run (ADR-0010 §4)."""

    def test_data_disk_content_round_trips(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        ctx = _ctx(plan, driver, cache)

        build_phase(ctx)
        # Build captured the data disk into the cache; remember those bytes.
        cached_data0 = ctx.built_disk_paths["web"]["data0"].read_bytes()
        assert b"16G" in cached_data0  # the sized blank the build VM booted with

        # No backend resources survive the build — the run phase rebuilds from cache.
        run_phase(ctx)

        # Find the run VM's data disk on the backend and confirm it carries the
        # exact bytes captured at build (host -> pool upload, no clone).
        run_uploads = [
            c
            for c in driver.calls
            if c[0] == "upload_to_pool" and "tr_vm_" in c[1][0] and c[1][0].endswith("-data0.qcow2")
        ]
        assert len(run_uploads) == 1
        run_disk_path = Path(run_uploads[0][1][0])
        assert run_disk_path.read_bytes() == cached_data0


class TestSidecarReadinessGate:
    """ADR-0010 §8: block on sidecar readiness before the first user VM."""

    def test_waits_for_sidecar_before_first_user_vm(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        cache, driver = env
        plan = _plan(data_disks=0)
        ctx = _ctx(plan, driver, cache)
        build_phase(ctx)
        driver.calls = []
        run_phase(ctx)

        # The readiness probe (reading the delivered dnsmasq.conf) must come
        # before the first user VM's create_vm.
        names = [(i, c) for i, c in enumerate(driver.calls)]
        first_readiness = next(
            i
            for i, c in names
            if c[0] == "native_guest_read_file" and c[1][1] == SIDECAR_DNSMASQ_CONF
        )
        first_user_vm = next(
            i for i, c in names if c[0] == "create_vm" and c[1][0].startswith("tr_vm_")
        )
        assert first_readiness < first_user_vm

    def test_unreachable_agent_fails_loud(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=0)
        ctx = _ctx(plan, driver, cache)
        # Tiny readiness timeout so the loop gives up fast.
        ctx = RunContext(
            plan=ctx.plan,
            driver=ctx.driver,
            store=ctx.store,
            cache=ctx.cache,
            run_id=ctx.run_id,
            plan_name=ctx.plan_name,
            build_timeout_s=ctx.build_timeout_s,
            lease_timeout_s=ctx.lease_timeout_s,
            addressing=ctx.addressing,
            sidecar_ready_timeout_s=0.01,
        )
        build_phase(ctx)
        driver.guest_agent_unreachable = True
        with pytest.raises(OrchestratorError, match="not ready"):
            run_phase(ctx)
        # The user VM never started — the gate blocked first.
        assert not any(c[0] == "create_vm" and c[1][0].startswith("tr_vm_") for c in driver.calls)
