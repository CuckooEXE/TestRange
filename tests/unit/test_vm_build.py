"""VM-node materialize against the MockDriver (ADR-0010 §2/§3/§4, ADR-0030).

The materialize walk warms the cache and leaves the backend empty:

* every writable disk (OS + each data disk) lands in the cache as its own entry;
* the build VM boots with all those disks attached;
* nothing — build VM, disks, switch, sidecar, *or the build pool* — survives;
* a second build over a warm cache creates **no** backend resources at all.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.base import Device
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.drivers.base import BUILD_NIC_NIC_IDX, VolumeRef
from testrange.exceptions import BuildFailedError, DriverError, OrchestratorError
from testrange.handles import NetworkHandle, PoolHandle
from testrange.networks import Network, NetworkAddressing, Sidecar, Switch
from testrange.networks.base import BuildNic
from testrange.networks.sidecar import SIDECAR_DNSMASQ_CONF
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.executor import materialize_graph, realize_graph
from testrange.orchestrator.vm_build import (
    VMBuildProbe,
    _decode_b64_tolerant,
    _fallback_log,
    build_nic_for,
)
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor, OriginlessBuilder


def _plan(*, data_disks: int = 1) -> Plan:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(
        Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True))
    )
    devices: list[Device] = [CPU(1), Memory(512), OSDrive(hyp.pools["pool1"], 8)]
    devices += [HardDrive(hyp.pools["pool1"], 16) for _ in range(data_disks)]
    devices.append(NetworkIface(hyp.networks["netA"], addr=DHCPAddr()))
    hyp.add_vm(
        VMRecipe(
            spec=VMSpec(name="web", devices=devices),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"),
                credentials=[PosixCred("u", password="p")],
            ),
            communicator=SSHCommunicator("u"),
        )
    )
    return Plan("hello", hyp)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default SSHCommunicator.execute to a success no-op.

    A VM node's realize gates on communicator + builder readiness, both of
    which exec through the communicator — stub it so the realize-driving tests
    complete without real SSH.
    """

    def fake_execute(
        self: SSHCommunicator,
        argv: Any,
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        del self, argv, timeout, cwd
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)


def _ctx(
    plan: Plan,
    driver: MockDriver,
    cache: CacheManager,
    *,
    sidecar_ready_timeout_s: float = 120.0,
) -> GraphContext:
    run_id = new_run_id()
    store = StateStore(run_dir_for(run_id))
    store.initialize(run_id=run_id, plan_name=plan.name, driver_class="MockDriver", driver_uri="")
    addressing: Mapping[str, NetworkAddressing] = {
        n.name: NetworkAddressing.from_switch(s)
        for s in plan.hypervisor.declared_switches
        for n in s.networks
    }
    return GraphContext(
        plan=plan,
        resolved=ResolvedBackend(
            driver=driver,
            driver_uri="",
        ),
        store=store,
        cache=cache,
        run_id=run_id,
        plan_name=plan.name,
        build_timeout_s=5.0,
        lease_timeout_s=5.0,
        addressing=addressing,
        sidecar_ready_timeout_s=sidecar_ready_timeout_s,
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


class TestMaterialize:
    def test_captures_every_writable_disk(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        # N+1 artifacts: one OS disk + one data disk.
        names = _built_names(cache)
        assert len(names) == 2
        assert any(n.endswith("__os") for n in names)
        assert any(n.endswith("__data0") for n in names)

    def test_build_vm_booted_with_all_disks(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=2)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

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
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        # Build VM + sidecar destroyed; build pool destroyed; switch torn down.
        assert driver._vms == {}
        assert driver._pools == set()
        assert driver._switches == {}
        assert any(c[0] == "destroy_pool" for c in driver.calls)

    def test_flaky_post_capture_delete_does_not_abort_build(
        self, env: tuple[CacheManager, MockDriver], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # H4 (ORCH-13): a single failing post-capture delete must not abort the
        # build or skip teardown_build_infra. The disk is already captured; the
        # failed resource stays recorded so cleanup/teardown can retry it.
        cache, driver = env
        plan = _plan(data_disks=1)
        ctx = _ctx(plan, driver, cache)

        def _flaky(vol_ref: VolumeRef) -> None:
            raise DriverError("simulated flaky delete")

        # teardown_build_infra uses destroy_vm/destroy_network/destroy_pool, never
        # delete_volume, so failing every delete isolates the post-capture path.
        monkeypatch.setattr(driver, "delete_volume", _flaky)

        materialize_graph(ctx, plan.graph)  # must NOT raise despite the flaky deletes

        # The disks were still captured into the cache…
        assert _built_names(cache)
        # …teardown_build_infra still ran (build pool + switch reclaimed)…
        assert driver._pools == set()
        assert driver._switches == {}
        # …and the OS disk, whose delete failed, is left recorded for cleanup
        # to retry (record-before-create ledger, not silently forgotten).
        assert any(r.kind == "build_disk" for r in ctx.store.read().resources)

    def test_capture_temp_lands_on_cache_filesystem(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # CORE-4: the captured disk is downloaded to a temp file before it is
        # ingested. That temp must sit on the cache's filesystem — not the
        # system tempdir (often a small tmpfs /tmp), where a multi-GiB OS disk
        # ENOSPCs. Assert every download target is under the cache root.
        cache, driver = env
        plan = _plan(data_disks=1)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        downloads = [Path(c[1][1]) for c in driver.calls if c[0] == "download_from_pool"]
        assert downloads, "expected at least one disk capture"
        for dest in downloads:
            assert cache.local.root in dest.parents, f"capture temp escaped cache fs: {dest}"

    def test_second_build_is_full_cache_hit(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        # Second build over the warm cache: no backend resources at all.
        driver.calls = []
        ctx2 = _ctx(plan, driver, cache)
        materialize_graph(ctx2, plan.graph)
        creating = {"create_pool", "create_switch", "create_network", "create_vm"}
        assert not any(c[0] in creating for c in driver.calls)
        # ...and the realize walk still gets its disk set populated from cache.
        assert set(ctx2.built_disk_paths["web"]) == {"os", "data0"}

    def test_drifted_sidecar_invalidates_build_cache(
        self, env: tuple[CacheManager, MockDriver], tmp_path: Path
    ) -> None:
        # CI-1: the sidecar serves DHCP/DNS/NAT during every build, so a
        # drifted sidecar image must move config_hash and force a rebuild —
        # not silently reuse the disks built against the old sidecar.
        cache, driver = env
        plan = _plan(data_disks=1)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)
        first_names = set(_built_names(cache))
        assert first_names

        # Rebuild the sidecar (same pretty-name, different content sha).
        drifted = tmp_path / "drifted-sidecar.qcow2"
        drifted.write_bytes(b"DRIFTED-SIDECAR" * 100)
        cache.local.forget_name("testrange-sidecar")
        cache.local.add(drifted, name="testrange-sidecar")

        driver.calls = []
        ctx2 = _ctx(plan, driver, cache)
        materialize_graph(ctx2, plan.graph)

        # The drift is a cache miss: the build VM is stood up again...
        assert any(c[0] == "create_vm" and "build_vm" in c[1][0] for c in driver.calls)
        # ...and the rebuilt disks land under a *new* config_hash.
        assert set(_built_names(cache)) - first_names

    def test_no_nics_builds(self, env: tuple[CacheManager, MockDriver]) -> None:
        # ORCH-5: the orchestrator no longer rejects a VM with no NICs — whether
        # a build needs network access is the builder's concern, not the generic
        # materialize walk's. A no-NIC VM builds and captures its OS disk.
        cache, driver = env
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True))
        )
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name="web", devices=[CPU(1), Memory(512), OSDrive(hyp.pools["pool1"], 8)]
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
                ),
                communicator=SSHCommunicator("u"),
            )
        )
        plan = Plan("hello", hyp)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)
        create = [c for c in driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]]
        assert len(create) == 1
        assert any(n.endswith("__os") for n in _built_names(cache))

    def test_no_origin_at_all_is_rejected(self, env: tuple[CacheManager, MockDriver]) -> None:
        # BUILD-1: the probe reads OS-disk origin via the Builder ABC seam
        # (not isinstance). A builder that provides NEITHER an image base
        # (os_disk_base) NOR a boot medium (boot_media) has no way to populate an
        # OS disk and fails loud at probe. (The installer-origin happy path —
        # os_disk_base None + boot_media set — is covered separately.)
        cache, driver = env
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True))
        )
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name="web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive(hyp.pools["pool1"], 8),
                        NetworkIface(hyp.networks["netA"]),
                    ],
                ),
                builder=OriginlessBuilder(),
                communicator=SSHCommunicator("u"),
            )
        )
        plan = Plan("hello", hyp)
        with pytest.raises(OrchestratorError, match="neither an OS-disk base image"):
            materialize_graph(_ctx(plan, driver, cache), plan.graph)

    def test_installer_origin_materializes_blank_disk_and_boots_media(
        self, env: tuple[CacheManager, MockDriver], tmp_path: Path
    ) -> None:
        # BUILD-1 happy path: a builder with os_disk_base() None but a boot_media()
        # builds via the materialize seam — the orchestrator creates a BLANK OS
        # disk (never upload_to_pool's a base onto it), stages the install medium,
        # and create_vm gets boot_media_ref + the VM's uefi firmware.
        from testrange.builders.base import Builder
        from testrange.credentials.base import Credential

        iso = tmp_path / "pve.iso"
        iso.write_bytes(b"FAKE-PVE-INSTALLER-ISO" * 100)
        cache, driver = env
        cache.local.add(iso, name="pve-iso")

        class _PVEBuilder(Builder):
            @property
            def credentials(self) -> tuple[Credential, ...]:
                return ()

            def os_disk_base(self) -> None:
                return None

            def boot_media(self) -> CacheEntry:
                return CacheEntry("pve-iso")

            def config_hash(  # type: ignore[no-untyped-def]
                self,
                spec,
                recipe,
                *,
                addressing,
                base_sha="",
                sidecar_sha="",
                macs=(),
                build_nic,
                native_agent=None,
            ):
                # Fold base_sha (the orchestrator passes the boot-media sha here)
                # so a different installer ISO keys a different build.
                return ("pve" + base_sha)[:16].ljust(16, "0")

            def render_seed(  # type: ignore[no-untyped-def]
                self, spec, recipe, *, addressing, macs=(), build_nic, native_agent=None
            ):
                return b"answer.toml-seed"

        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True))
        )
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name="pve",
                    firmware="uefi",
                    devices=[CPU(2), Memory(2048), OSDrive(hyp.pools["pool1"], 16)],
                ),
                builder=_PVEBuilder(),
                communicator=SSHCommunicator("root"),
            )
        )
        plan = Plan("hello", hyp)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        # create_vm saw the bootable medium + UEFI firmware (the sidecar VM is
        # also recorded; select the PVE build VM by name).
        created = next(v for v in driver.created_vms.values() if v.vm_name == "pve")
        assert created.firmware == "uefi"
        assert created.boot_media is not None
        # The sidecar stays BIOS image-origin — installer firmware/media are
        # scoped to the VM that declared them.
        sidecar = next(v for v in driver.created_vms.values() if v.vm_name != "pve")
        assert sidecar.firmware == "bios"
        assert sidecar.boot_media is None

        # Behavioral, not string-sniffing: the recorded OS-disk ref was created
        # BLANK and never base-uploaded, and the recorded boot-media ref was
        # uploaded to the pool.
        blank_targets = [c[1][0] for c in driver.calls if c[0] == "create_blank_volume"]
        upload_targets = [c[1][0] for c in driver.calls if c[0] == "upload_to_pool"]
        assert created.os_disk in blank_targets
        assert created.os_disk not in upload_targets
        assert created.boot_media in upload_targets

        # And the build produced a cached OS disk (full lifecycle reached capture).
        assert _built_names(cache)

    def test_installer_origin_with_no_seed_still_builds(
        self, env: tuple[CacheManager, MockDriver], tmp_path: Path
    ) -> None:
        # ESXi single-CDROM shape (BUILD-8): an installer-origin builder whose
        # render_seed() returns None — the ks.cfg rides the boot media, so no
        # seed volume is staged. The build still runs: blank OS disk + boot
        # media, create_vm with seed_iso_ref=None, full lifecycle to capture.
        from testrange.builders.base import Builder
        from testrange.credentials.base import Credential

        iso = tmp_path / "esxi.iso"
        iso.write_bytes(b"FAKE-ESXI-ISO" * 100)
        cache, driver = env
        cache.local.add(iso, name="esxi-iso")

        class _ESXiShapedBuilder(Builder):
            @property
            def credentials(self) -> tuple[Credential, ...]:
                return ()

            def os_disk_base(self) -> None:
                return None

            def boot_media(self) -> CacheEntry:
                return CacheEntry("esxi-iso")

            def config_hash(  # type: ignore[no-untyped-def]
                self,
                spec,
                recipe,
                *,
                addressing,
                base_sha="",
                sidecar_sha="",
                macs=(),
                build_nic,
                native_agent=None,
            ):
                return ("esxi" + base_sha)[:16].ljust(16, "0")

            def render_seed(  # type: ignore[no-untyped-def]
                self, spec, recipe, *, addressing, macs=(), build_nic, native_agent=None
            ):
                return None

        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 64))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True))
        )
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name="esxi",
                    firmware="bios",
                    devices=[CPU(2), Memory(4096), OSDrive(hyp.pools["pool1"], 40)],
                ),
                builder=_ESXiShapedBuilder(),
                communicator=SSHCommunicator("root"),
            )
        )
        plan = Plan("hello", hyp)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)

        created = next(v for v in driver.created_vms.values() if v.vm_name == "esxi")
        assert created.boot_media is not None  # booted the installer media
        # No seed volume was written (render_seed -> None).
        assert not any(c[0] == "write_to_pool" and "seed" in c[1][0] for c in driver.calls)
        # The OS disk was materialized blank, and the build reached capture.
        assert "create_blank_volume" in [name for name, _, _ in driver.calls]
        assert _built_names(cache)


@pytest.fixture
def propagating_logs() -> Iterator[None]:
    """Let testrange records reach caplog's root handler.

    A CLI test that ran earlier in the session may have called
    ``configure_logging``, which sets the ``testrange`` logger to
    ``propagate=False`` (it owns its own Rich handler). caplog captures at the
    ROOT logger, so these console-mirror assertions need propagation restored
    for their duration — the test must not depend on suite ordering.
    """
    root = logging.getLogger("testrange")
    prior = root.propagate
    root.propagate = True
    try:
        yield
    finally:
        root.propagate = prior


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
        plan = _plan(data_disks=0)
        materialize_graph(_ctx(plan, driver, cache), plan.graph)
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
        plan = _plan(data_disks=1)
        with pytest.raises(BuildFailedError) as ei:
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        err = ei.value
        assert err.rc == 100
        assert err.cmd == "apt-get update"
        assert err.log == log
        assert log.decode() in str(err)  # the captured log is in the message

    def test_failed_build_caches_nothing(self, env: tuple[CacheManager, MockDriver]) -> None:
        # The corrupt-cache guard: a failed build must not leave a `_built_`
        # artifact behind for the realize walk to pick up.
        cache, driver = env
        driver.build_result_stream = [b'TESTRANGE-RESULT: fail rc=1 cmd="false"\n']
        plan = _plan(data_disks=1)
        with pytest.raises(BuildFailedError):
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        assert _built_names(cache) == []

    def test_power_off_without_token_is_failure(self, env: tuple[CacheManager, MockDriver]) -> None:
        # Guest powered off (serial stream EOFs) without ever emitting `ok` —
        # a mid-provision crash. Must be a loud failure, not a silent success.
        cache, driver = env
        driver.build_result_stream = [b"[ booting ] cloud-init crashed\n"]
        plan = _plan(data_disks=0)
        with pytest.raises(BuildFailedError, match="without reporting a build result"):
            materialize_graph(_ctx(plan, driver, cache), plan.graph)

    def test_console_output_streams_to_log(
        self,
        env: tuple[CacheManager, MockDriver],
        caplog: pytest.LogCaptureFixture,
        propagating_logs: None,
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
        plan = _plan(data_disks=0)
        with (
            caplog.at_level(logging.DEBUG, logger="testrange.orchestrator.vm_build.console"),
            pytest.raises(BuildFailedError),
        ):
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        streamed = [r.getMessage() for r in caplog.records if r.name.endswith(".console")]
        assert any("Setting up nginx" in m for m in streamed)  # build chatter shown
        assert not any("TESTRANGE-RESULT" in m for m in streamed)  # framing hidden
        assert not any(log.decode() in m for m in streamed)  # base64 block hidden

    def test_console_output_is_scrubbed_of_control_bytes(
        self,
        env: tuple[CacheManager, MockDriver],
        caplog: pytest.LogCaptureFixture,
        propagating_logs: None,
    ) -> None:
        # Raw guest terminal escapes (colour, clear-screen, embedded \r) must be
        # stripped before mirroring so they can't hijack the operator's terminal
        # (CORE-6).
        cache, driver = env
        driver.build_result_stream = [
            b"\x1b[2J\x1b[1;32m[  OK  ]\x1b[0m Started thing.\r\n",
            b'TESTRANGE-RESULT: fail rc=1 cmd="false"\n',
        ]
        plan = _plan(data_disks=0)
        with (
            caplog.at_level(logging.DEBUG, logger="testrange.orchestrator.vm_build.console"),
            pytest.raises(BuildFailedError),
        ):
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        streamed = [r.getMessage() for r in caplog.records if r.name.endswith(".console")]
        assert any("[  OK  ] Started thing." in m for m in streamed)
        assert not any("\x1b" in m or "\r" in m for m in streamed)

    def test_fail_log_is_scrubbed_in_error_message(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # The decoded failure log surfaced in BuildFailedError is scrubbed too —
        # a guest log full of colour codes shouldn't garble the operator's
        # terminal when the error is printed (CORE-6).
        cache, driver = env
        log = b"\x1b[31mE: package broken\x1b[0m\r\n"
        driver.build_result_stream = [
            b'TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"\n'
            b"TESTRANGE-LOG-BEGIN\n" + base64.b64encode(log) + b"\nTESTRANGE-LOG-END\n"
        ]
        plan = _plan(data_disks=0)
        with pytest.raises(BuildFailedError) as ei:
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        rendered = str(ei.value)
        assert "E: package broken" in rendered
        assert "\x1b" not in rendered and "\r" not in rendered
        assert ei.value.log == log  # raw bytes preserved on the attribute

    def test_fail_log_with_console_noise_is_decoded_not_dumped(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # BUILD-23: the failure log rides the guest's *shared* serial, so kernel
        # chatter can interleave into the base64 block. A strict decode raises and
        # the old fallback dumped the raw base64 to the console. The decode must
        # tolerate the noise and surface readable log text, never the blob.
        cache, driver = env
        log = b"E: Unable to fetch http://deb.debian.org/ Connection timed out\n"
        b64 = base64.b64encode(log)
        mid = len(b64) // 2
        noisy = b64[:mid] + b"\n[   12.34] random: crng init done\n" + b64[mid:]
        driver.build_result_stream = [
            b'TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"\n'
            b"TESTRANGE-LOG-BEGIN\n" + noisy + b"\nTESTRANGE-LOG-END\n"
        ]
        plan = _plan(data_disks=0)
        with pytest.raises(BuildFailedError) as ei:
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        rendered = str(ei.value)
        assert "Unable to fetch" in rendered  # decoded text (the clean prefix) surfaced
        assert b64[:mid].decode() not in rendered  # the raw base64 is NOT dumped

    def test_power_off_after_partial_log_frame_decodes(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        # BUILD-23: the guest emitted a LOG-BEGIN block then died (poweroff -f)
        # before the closing LOG-END and without a parseable RESULT line. The
        # no-result fallback must decode the partial block, not dump raw base64.
        cache, driver = env
        log = b"cloud-init bombed: KeyError 'runcmd'\n"
        driver.build_result_stream = [
            b"[ booting ]\nTESTRANGE-LOG-BEGIN\n" + base64.b64encode(log) + b"\n"
        ]
        plan = _plan(data_disks=0)
        with pytest.raises(BuildFailedError, match="without reporting a build result") as ei:
            materialize_graph(_ctx(plan, driver, cache), plan.graph)
        assert "cloud-init bombed" in str(ei.value)


class TestSerialLogDecode:
    """BUILD-23: the serial log block decodes to readable text, never a blob."""

    def test_clean_block_is_exact(self) -> None:
        log = b"the quick brown fox\n"  # len not a multiple of 3 -> base64 padding
        assert _decode_b64_tolerant(base64.b64encode(log)) == log

    def test_crlf_wrapping_is_stripped(self) -> None:
        # coreutils `base64` line-wraps at 76 cols, and the serial adds CR.
        log = b"x" * 200
        wrapped = b"\r\n".join(base64.b64encode(log)[i : i + 76] for i in range(0, 272, 76))
        assert _decode_b64_tolerant(wrapped) == log

    def test_truncated_block_recovers_a_prefix(self) -> None:
        # Distinct bytes + a non-quantum (rem==2) cut: startswith only holds if the
        # surviving prefix is byte-aligned, so this catches an alignment regression
        # that an all-identical payload would hide.
        log = bytes(range(60)) * 2
        truncated = base64.b64encode(log)[:62]  # poweroff -f cut it mid-quantum
        decoded = _decode_b64_tolerant(truncated)
        assert decoded and log.startswith(decoded)  # clean aligned prefix; no raise, no blob

    def test_lone_trailing_char_is_dropped(self) -> None:
        # A compacted length of 1 mod 4 cannot form a base64 quantum; the lone
        # trailing char must be DROPPED (re-padding it would make b64decode raise).
        log = bytes(range(45))  # 45 % 3 == 0 -> 60 base64 chars, no padding
        decoded = _decode_b64_tolerant(base64.b64encode(log) + b"Q")  # +1 char => rem==1
        assert decoded == log  # the stray char is dropped, the clean block recovered

    def test_empty_is_empty(self) -> None:
        assert _decode_b64_tolerant(b"") == b""
        assert _decode_b64_tolerant(b"\r\n  \r\n") == b""

    def test_fallback_decodes_unclosed_frame(self) -> None:
        log = b"provisioning died here\n"
        buf = b"boot chatter\nTESTRANGE-LOG-BEGIN\n" + base64.b64encode(log) + b"\n"
        assert _fallback_log(buf) == log

    def test_fallback_without_frame_returns_bounded_raw_tail(self) -> None:
        buf = b"Z" * 9000  # no framing at all
        tail = _fallback_log(buf)
        assert tail == buf[-4096:] and len(tail) == 4096


class TestBuildToRunDataDisk:
    """Data-disk content survives build -> cache -> run (ADR-0010 §4)."""

    def test_data_disk_content_round_trips(self, env: tuple[CacheManager, MockDriver]) -> None:
        cache, driver = env
        plan = _plan(data_disks=1)
        ctx = _ctx(plan, driver, cache)

        materialize_graph(ctx, plan.graph)
        # Build captured the data disk into the cache; remember those bytes.
        cached_data0 = ctx.built_disk_paths["web"]["data0"].read_bytes()
        assert b"16G" in cached_data0  # the sized blank the build VM booted with

        # No backend resources survive the build — the realize walk rebuilds
        # from cache.
        realize_graph(ctx, plan.graph)

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
    """ADR-0010 §8: a VM node realizes only after its network node's sidecar is
    serving — the readiness gate runs inside the network node's realize."""

    def test_waits_for_sidecar_before_first_user_vm(
        self, env: tuple[CacheManager, MockDriver]
    ) -> None:
        cache, driver = env
        plan = _plan(data_disks=0)
        ctx = _ctx(plan, driver, cache)
        materialize_graph(ctx, plan.graph)
        driver.calls = []
        realize_graph(ctx, plan.graph)

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
        # Tiny readiness timeout so the loop gives up fast.
        ctx = _ctx(plan, driver, cache, sidecar_ready_timeout_s=0.01)
        materialize_graph(ctx, plan.graph)
        driver.guest_agent_unreachable = True
        with pytest.raises(OrchestratorError, match="not ready"):
            realize_graph(ctx, plan.graph)
        # The user VM never started — the gate blocked first.
        assert not any(c[0] == "create_vm" and c[1][0].startswith("tr_vm_") for c in driver.calls)


class TestBuildNicMacSelection:
    """ESXI-18: an installer-origin build NIC must wear the MAC of the VM's
    first DECLARED NIC — an installed OS (ESXi vmk0) pins its identity to the
    install-time MAC and keeps it across the build->run VM recreation, and the
    run phase polls the lease file for the declared idx-0 MAC. Image-origin
    builds keep the reserved disjoint slot."""

    class _InstallerBuilder(OriginlessBuilder):
        def boot_media(self) -> CacheEntry:
            return CacheEntry("installer-iso")

    def _ctx(self) -> Any:
        class _Drv:
            def compose_mac(self, plan: str, vm: str, idx: int) -> str:
                return f"02:{idx & 0xFF:02x}:00:00:00:01"

        return SimpleNamespace(driver=_Drv(), plan_name="p")

    def _switch(self) -> Switch:
        return Switch("build", Network("bn"), cidr="10.97.0.0/24", sidecar=Sidecar(dhcp=True))

    def _vm(self, builder: Any, nics: int) -> VMRecipe:
        devices: list[Device] = [CPU(1), Memory(512), OSDrive(PoolHandle("pool1"), 8)]
        devices += [
            NetworkIface(NetworkHandle("bn", switch="build"), addr=DHCPAddr()) for _ in range(nics)
        ]
        return VMRecipe(
            spec=VMSpec(name="vm", devices=devices),
            builder=builder,
            communicator=SSHCommunicator("u"),
        )

    def test_installer_origin_wears_the_declared_idx0_mac(self) -> None:
        ctx = self._ctx()
        nic = build_nic_for(ctx, self._switch(), self._vm(self._InstallerBuilder(), nics=2), 0)
        assert nic.mac == ctx.driver.compose_mac("p", "vm", 0)

    def test_image_origin_keeps_the_reserved_slot(self) -> None:
        ctx = self._ctx()
        builder = CloudInitBuilder(base=CacheEntry("debian-13"))
        nic = build_nic_for(ctx, self._switch(), self._vm(builder, nics=2), 0)
        assert nic.mac == ctx.driver.compose_mac("p", "vm", BUILD_NIC_NIC_IDX)

    def test_installer_origin_without_nics_keeps_the_reserved_slot(self) -> None:
        # No declared NIC means nothing will ever poll a lease for idx 0 —
        # there is no identity to inherit.
        ctx = self._ctx()
        nic = build_nic_for(ctx, self._switch(), self._vm(self._InstallerBuilder(), nics=0), 0)
        assert nic.mac == ctx.driver.compose_mac("p", "vm", BUILD_NIC_NIC_IDX)


class TestVMBuildProbeOriginInvariant:
    """VMBuildProbe must carry exactly one OS-disk origin (ORCH-21): base_path
    XOR boot_media_path, consistent with the resolved installer_origin flag.
    'Both' would silently drop the boot medium and 'neither' would yield a
    blank, unbootable disk — the dataclass backstops a future edit that
    violates this."""

    def _make(
        self,
        *,
        installer_origin: bool,
        base_path: Path | None,
        boot_media_path: Path | None,
        paths_resolved: bool = True,
    ) -> VMBuildProbe:
        sw = Switch("sw", Network("n"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        vm = VMRecipe(
            spec=VMSpec(name="vm", devices=[CPU(1), Memory(512), OSDrive(PoolHandle("pool1"), 8)]),
            builder=OriginlessBuilder(),
            communicator=SSHCommunicator("u"),
        )
        return VMBuildProbe(
            vm=vm,
            builder=OriginlessBuilder(),
            config_hash="0" * 16,
            macs=(),
            build_nic=BuildNic(
                mac="02:00:00:aa:bb:cc",
                network="n",
                addr=StaticAddr("10.0.0.3"),
                addressing=NetworkAddressing.from_switch(sw),
            ),
            native_agent=None,
            installer_origin=installer_origin,
            base_path=base_path,
            boot_media_path=boot_media_path,
            roles=("os",),
            cached_paths=None,
            paths_resolved=paths_resolved,
        )

    def test_image_origin_ok(self) -> None:
        self._make(installer_origin=False, base_path=Path("/base.qcow2"), boot_media_path=None)

    def test_installer_origin_ok(self) -> None:
        self._make(installer_origin=True, base_path=None, boot_media_path=Path("/inst.iso"))

    def test_both_origins_rejected(self) -> None:
        with pytest.raises(OrchestratorError, match="exactly one OS-disk origin"):
            self._make(
                installer_origin=False,
                base_path=Path("/base.qcow2"),
                boot_media_path=Path("/inst.iso"),
            )

    def test_installer_origin_without_medium_rejected(self) -> None:
        with pytest.raises(OrchestratorError, match="no boot medium"):
            self._make(installer_origin=True, base_path=None, boot_media_path=None)

    def test_image_origin_without_base_rejected(self) -> None:
        with pytest.raises(OrchestratorError, match="no base path"):
            self._make(installer_origin=False, base_path=None, boot_media_path=None)

    def test_metadata_only_probe_skips_path_presence(self) -> None:
        # paths_resolved=False is the read-only inspection path (probe_fetch
        # off): origin shas are resolved but nothing is materialized, so there
        # are no local paths to validate.
        self._make(
            installer_origin=True, base_path=None, boot_media_path=None, paths_resolved=False
        )
