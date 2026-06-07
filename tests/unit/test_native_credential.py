"""CORE-60: per-call guest credential resolution + threading through the bind.

``native_guest_credential`` sources the guest OS login a credential-requiring
native channel (VMware Tools / Hyper-V) authenticates with; QGA backends ignore
it. The orchestrator threads the resolved credential into the three
``native_guest_*`` accessors at communicator-bind time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, NativeCommunicator
from testrange.credentials import PosixCred
from testrange.credentials.base import Credential
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.guest_io import GuestExec
from testrange.networks import Network, Switch
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.run_phase import bind_communicators, native_guest_credential
from testrange.state.store import StateStore, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor


def _vm(builder: CloudInitBuilder) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(name="g", devices=[CPU(1), Memory(256), OSDrive("pool1", 8)]),
        builder=builder,
        communicator=NativeCommunicator(),
    )


def _img(**kw: Any) -> CloudInitBuilder:
    return CloudInitBuilder(base=CacheEntry("debian-13"), **kw)


class TestNativeGuestCredential:
    def test_no_credentials_resolves_none(self) -> None:
        assert native_guest_credential(_vm(_img())) is None

    def test_sole_credential_is_picked(self) -> None:
        cred = PosixCred("u", password="p")
        assert native_guest_credential(_vm(_img(credentials=[cred]))) is cred

    def test_admin_wins_over_others(self) -> None:
        admin = PosixCred("ops", password="x", admin=True)
        builder = _img(credentials=[PosixCred("root", password="r"), admin])
        assert native_guest_credential(_vm(builder)) is admin

    def test_ambiguous_non_admin_set_resolves_none(self) -> None:
        builder = _img(credentials=[PosixCred("a", password="a"), PosixCred("b", password="b")])
        assert native_guest_credential(_vm(builder)) is None


class _RecordingDriver(MockDriver):
    """Captures the credential threaded into the native exec accessor."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.seen_credential: Credential | None = None

    def native_guest_execute(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestExec:
        self.seen_credential = credential

        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute


def _ctx(plan: Plan, driver: MockDriver, tmp_path: Path) -> RunContext:
    store = StateStore(run_dir_for("r1", root=tmp_path / "state"))
    store.initialize(run_id="r1", plan_name="p", driver_class="MockDriver", driver_uri="")
    switches = plan.hypervisor.networks
    return RunContext(
        plan=plan,
        resolved=ResolvedBackend(driver=driver, driver_uri=""),
        store=store,
        cache=CacheManager(local=LocalCache(root=tmp_path / "cache")),
        run_id="r1",
        plan_name="p",
        build_timeout_s=60.0,
        lease_timeout_s=60.0,
        addressing={n.name: NetworkAddressing.from_switch(s) for s in switches for n in s.networks},
    )


def test_bind_threads_credential_to_accessor(tmp_path: Path) -> None:
    cred = PosixCred("svc", password="pw", admin=True)
    plan = Plan(
        "p",
        MockHypervisor(
            networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24")],
            pools=[StoragePool("pool1", 64)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="g",
                        devices=[
                            CPU(1),
                            Memory(256),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=StaticAddr("10.0.1.10")),
                        ],
                    ),
                    builder=_img(credentials=[cred]),
                    communicator=NativeCommunicator(),
                )
            ],
        ),
    )
    driver = _RecordingDriver(pool_root=tmp_path / "pools")
    bind_communicators(_ctx(plan, driver, tmp_path))
    assert driver.seen_credential is cred
