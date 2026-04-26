"""Live PVE integration tests.

Skipped unless ``TESTRANGE_PROXMOX_HOST`` (and ``TESTRANGE_PROXMOX_PASSWORD``,
or alternatively the token-auth env vars) are set in the environment.
These exercise the orchestrator + SDN-network paths against a real
PVE node; ideal for catching API / endpoint drift between PVE
versions.

Required environment
--------------------

- ``TESTRANGE_PROXMOX_HOST`` — hostname or IP of a reachable PVE node.
- ``TESTRANGE_PROXMOX_USER`` — defaults to ``root@pam``.
- ``TESTRANGE_PROXMOX_PASSWORD`` — set for ticket auth, **or**
- ``TESTRANGE_PROXMOX_TOKEN_NAME`` + ``TESTRANGE_PROXMOX_TOKEN_VALUE``
  for API-token auth.
- ``TESTRANGE_PROXMOX_NODE`` — optional; defaults to the only node.
- ``TESTRANGE_PROXMOX_STORAGE`` — optional; defaults to the first
  ``images``-capable pool.

Tests are deliberately conservative: they create resources with
unmistakably-test-prefixed names (``trlive*``), tear them down at
the end of each test even on failure, and never touch resources they
didn't create.  Re-running on a leaked PVE is safe.
"""

from __future__ import annotations

import os
import uuid

import pytest


def _env_or_skip() -> dict[str, object]:
    """Read PVE connection params from env, or skip the test."""
    host = os.environ.get("TESTRANGE_PROXMOX_HOST")
    if not host:
        pytest.skip(
            "TESTRANGE_PROXMOX_HOST not set — skipping live PVE tests",
        )

    kwargs: dict[str, object] = {
        "host": host,
        "user": os.environ.get("TESTRANGE_PROXMOX_USER") or "root@pam",
    }
    password = os.environ.get("TESTRANGE_PROXMOX_PASSWORD")
    token_name = os.environ.get("TESTRANGE_PROXMOX_TOKEN_NAME")
    token_value = os.environ.get("TESTRANGE_PROXMOX_TOKEN_VALUE")
    if password:
        kwargs["password"] = password
    elif token_name and token_value:
        kwargs["token_name"] = token_name
        kwargs["token_value"] = token_value
    else:
        pytest.skip(
            "neither TESTRANGE_PROXMOX_PASSWORD nor "
            "TESTRANGE_PROXMOX_TOKEN_NAME+_VALUE set",
        )

    if (node := os.environ.get("TESTRANGE_PROXMOX_NODE")):
        kwargs["node"] = node
    if (storage := os.environ.get("TESTRANGE_PROXMOX_STORAGE")):
        kwargs["storage"] = storage

    return kwargs


@pytest.fixture
def orch():
    """Yield an entered :class:`ProxmoxOrchestrator` against the live
    PVE.  ``__exit__`` runs after the test."""
    from testrange.backends.proxmox import ProxmoxOrchestrator

    kwargs = _env_or_skip()
    instance = ProxmoxOrchestrator(**kwargs)
    with instance as entered:
        yield entered


def _build_orch(**overrides):
    """Build (don't enter) a fresh :class:`ProxmoxOrchestrator` with
    overrides on top of the env-derived auth kwargs."""
    from testrange.backends.proxmox import ProxmoxOrchestrator

    kwargs = _env_or_skip()
    kwargs.update(overrides)
    return ProxmoxOrchestrator(**kwargs)


@pytest.fixture
def short_run_id() -> str:
    """A 4-char run ID that survives PVE's 8-char SDN cap when
    combined with a 4-char vnet prefix."""
    return uuid.uuid4().hex[:4]


class TestOrchestratorAuth:
    def test_enters_and_exits(self, orch) -> None:
        """If we got an entered orchestrator, ``__enter__`` worked
        and we resolved a node + storage + zone."""
        assert orch._client is not None
        assert orch._node is not None
        assert orch._storage is not None
        assert orch._zone == "tr"

    def test_zone_is_simple_type(self, orch) -> None:
        """The bootstrap zone is a Simple zone — that's the SDN
        flavour TestRange uses."""
        zones = orch._client.cluster.sdn.zones.get()
        ours = next((z for z in zones if z.get("zone") == orch._zone), None)
        assert ours is not None
        assert ours.get("type") == "simple"

    def test_storage_accepts_images(self, orch) -> None:
        """The auto-resolved storage must accept ``images`` content
        — otherwise the VM-build slice has nowhere to put disks."""
        stores = orch._client.nodes(orch._node).storage.get()
        ours = next(
            (s for s in stores if s["storage"] == orch._storage), None,
        )
        assert ours is not None
        assert "images" in ours.get("content", "")


class TestNetworkLifecycle:
    def test_start_then_stop(self, orch, short_run_id: str) -> None:
        """Create a vnet + subnet, confirm they appear, tear down,
        confirm they're gone."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork

        net = ProxmoxVirtualNetwork(
            name="trlive", subnet="10.250.0.0/24", internet=False,
        )
        net.bind_run(short_run_id)
        backend = net.backend_name()
        assert len(backend) <= 8

        try:
            net.start(orch)
            vnets = orch._client.cluster.sdn.vnets.get()
            assert any(v["vnet"] == backend for v in vnets), (
                f"vnet {backend!r} not in {vnets!r}"
            )
            subnets = orch._client.cluster.sdn.vnets(backend).subnets.get()
            assert subnets, f"vnet {backend!r} has no subnets"
            assert subnets[0]["subnet"].startswith(orch._zone)
        finally:
            net.stop(orch)

        vnets = orch._client.cluster.sdn.vnets.get()
        assert not any(v["vnet"] == backend for v in vnets), (
            f"vnet {backend!r} still present after stop()"
        )

    def test_internet_subnet_has_snat(
        self, orch, short_run_id: str,
    ) -> None:
        """``internet=True`` round-trips to the SDN subnet's ``snat=1``."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork

        net = ProxmoxVirtualNetwork(
            name="trnat", subnet="10.251.0.0/24", internet=True,
        )
        net.bind_run(short_run_id)
        try:
            net.start(orch)
            backend = net.backend_name()
            subnets = orch._client.cluster.sdn.vnets(backend).subnets.get()
            assert subnets
            # PVE returns ``snat`` as ``1``/``0`` (int) or absent.
            assert int(subnets[0].get("snat", 0)) == 1
            assert subnets[0].get("gateway") == net.gateway_ip
        finally:
            net.stop(orch)

    def test_stop_is_idempotent(self, orch, short_run_id: str) -> None:
        """``stop()`` can be called twice without raising — important
        for the orchestrator's defensive teardown path."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork

        net = ProxmoxVirtualNetwork("trdup", "10.252.0.0/24")
        net.bind_run(short_run_id)
        net.start(orch)
        net.stop(orch)
        net.stop(orch)  # must not raise

    def test_collision_rollback_preserves_other_network(
        self, orch, short_run_id: str,
    ) -> None:
        """Regression: when ``start()`` fails because PVE refuses to
        create the vnet (e.g. another run has it), the rollback path
        must not list-and-delete the other run's vnet by name.  The
        creation-state flags on :class:`ProxmoxVirtualNetwork`
        guarantee rollback only undoes work the failed call did."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork
        from testrange.exceptions import NetworkError

        first = ProxmoxVirtualNetwork("trcoll", "10.253.0.0/24")
        first.bind_run(short_run_id)
        first.start(orch)
        backend = first.backend_name()

        try:
            colliding = ProxmoxVirtualNetwork("trcoll", "10.253.0.0/24")
            colliding.bind_run(short_run_id)
            with pytest.raises(NetworkError):
                colliding.start(orch)

            # The other vnet must still be there.
            vnets = {v["vnet"] for v in orch._client.cluster.sdn.vnets.get()}
            assert backend in vnets, (
                f"colliding rollback destroyed the other run's vnet "
                f"{backend!r}; surviving vnets: {vnets!r}"
            )
            # And the colliding instance must not falsely think it
            # owns anything.
            assert colliding._vnet_created is False
            assert colliding._subnet_created is False
        finally:
            first.stop(orch)


class TestNetworkLifecycleViaOrchestrator:
    """The orchestrator's ``__enter__`` / ``__exit__`` should drive the
    SDN-network lifecycle end-to-end — bind_run + start on entry,
    stop on exit, with rollback if any single network's start fails."""

    def test_with_block_starts_and_stops_networks(self) -> None:
        from testrange.backends.proxmox import ProxmoxVirtualNetwork

        nets = [
            ProxmoxVirtualNetwork("trA", "10.245.1.0/24", internet=False),
            ProxmoxVirtualNetwork("trB", "10.245.2.0/24", internet=True),
        ]
        orch = _build_orch(networks=nets)
        with orch:
            active = {v["vnet"] for v in orch._client.cluster.sdn.vnets.get()}
            for net in nets:
                assert net.backend_name() in active

        # After __exit__, all configured networks should be torn down.
        with _build_orch() as probe:
            after = {v["vnet"] for v in probe._client.cluster.sdn.vnets.get()}
            for net in nets:
                assert net.backend_name() not in after, (
                    f"vnet {net.backend_name()!r} leaked after __exit__"
                )

    def test_failed_start_rolls_back_earlier_networks(self) -> None:
        """If the second network fails, the first must be torn down."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork
        from testrange.exceptions import NetworkError

        good = ProxmoxVirtualNetwork("trrg", "10.245.3.0/24")
        bad = ProxmoxVirtualNetwork("trrb", "10.245.4.0/24")
        # Sabotage the second by clearing its parsed network — start()
        # will trip on gateway_ip and raise.
        bad._network = None  # type: ignore[assignment]

        # AttributeError surfaces from ``gateway_ip`` resolution, then
        # gets wrapped into ``NetworkError`` by the network's start().
        with (
            pytest.raises((NetworkError, AttributeError)),
            _build_orch(networks=[good, bad]),
        ):
            pass  # pragma: no cover — never reached

        # ``good`` must not survive the rollback.
        with _build_orch() as probe:
            active = {v["vnet"] for v in probe._client.cluster.sdn.vnets.get()}
            assert good.backend_name() not in active, (
                f"vnet {good.backend_name()!r} leaked after rollback"
            )

    def test_leak_preserves_networks_and_emits_hints(self) -> None:
        """``leak()`` keeps the networks alive past ``__exit__`` and
        :meth:`keep_alive_hints` reports the ``pvesh`` cleanup
        commands a human would run."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork

        net = ProxmoxVirtualNetwork("trlk", "10.245.5.0/24")
        orch = _build_orch(networks=[net])
        with orch:
            orch.leak()
            hints = orch.keep_alive_hints()
        backend = net.backend_name()
        assert any(backend in h for h in hints)
        assert any("/cluster/sdn" in h for h in hints[-1:])  # reload

        # Verify the vnet survived, then clean up by hand.
        with _build_orch() as probe:
            active = {v["vnet"] for v in probe._client.cluster.sdn.vnets.get()}
            assert backend in active
            net._client = probe._client
            net.stop(probe)


class TestVMLifecycle:
    """End-to-end provisioning: orchestrator brings up a Debian-12
    cloud-init VM on a fresh SDN vnet, exec'es a command via SSH,
    then tears the whole thing down.

    Slow (~5-10 min on first run; ~2-3 min once the qcow2 is on PVE
    storage).  Marked ``slow`` so the default live run can opt out.
    """

    DEBIAN12_URL = (
        "https://cloud.debian.org/images/cloud/bookworm/latest/"
        "debian-12-genericcloud-amd64.qcow2"
    )

    @pytest.mark.slow
    def test_debian12_install_and_ssh(self, short_run_id: str) -> None:
        from testrange import (
            Credential,
            Memory,
            vNIC,
            vCPU,
        )
        from testrange.backends.proxmox import (
            ProxmoxVirtualNetwork,
            ProxmoxVM,
        )

        net = ProxmoxVirtualNetwork(
            "trvmL", "10.244.0.0/24", internet=True,
        )
        # Non-root user goes first so SSHCommunicator selects it.
        # Debian cloud images block root password login by default
        # (sshd's ``PermitRootLogin prohibit-password`` plus
        # cloud-init's ``disable_root: true``), so a root-only
        # credential list would fail SSH auth.
        vm = ProxmoxVM(
            name="trvmsmoke",
            iso=self.DEBIAN12_URL,
            users=[
                Credential("debian", "testrange", sudo=True),
                Credential("root", "testrange"),
            ],
            devices=[
                vCPU(2),
                Memory(1.0),
                vNIC("trvmL", ip="10.244.0.10"),
            ],
            communicator="ssh",
        )

        orch = _build_orch(networks=[net], vms=[vm])
        with orch:
            assert vm._vmid is not None
            result = vm.exec(["uname", "-s"])
            assert result.exit_code == 0
            stdout = result.stdout or b""
            if isinstance(stdout, bytes):
                stdout = stdout.decode()
            assert "Linux" in stdout

            # Hostname comes from cloud-init's hostname module.
            assert vm.hostname() == "trvmsmoke"

        # After teardown, no VMID with our name should remain on the
        # node and no vnets we created should be left.
        with _build_orch() as probe:
            running = probe._client.nodes(probe._node).qemu.get()
            names = [v.get("name") for v in running]
            assert "trvmsmoke" not in names, (
                f"VM 'trvmsmoke' leaked: still in {names!r}"
            )
            vnets = {v["vnet"] for v in probe._client.cluster.sdn.vnets.get()}
            assert net.backend_name() not in vnets, (
                f"vnet leaked: {net.backend_name()!r}"
            )


class TestErrorPaths:
    def test_bad_node_surfaces_clear_error(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        from testrange.exceptions import OrchestratorError

        kwargs = _env_or_skip()
        kwargs["node"] = "definitely-not-a-real-node"
        orch = ProxmoxOrchestrator(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(OrchestratorError, match="not in cluster"):
            orch.__enter__()

    def test_bad_storage_surfaces_clear_error(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        from testrange.exceptions import OrchestratorError

        kwargs = _env_or_skip()
        kwargs["storage"] = "definitely-not-a-real-storage"
        orch = ProxmoxOrchestrator(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(OrchestratorError, match="is not configured"):
            orch.__enter__()

    def test_bad_password_surfaces_clear_error(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        from testrange.exceptions import OrchestratorError

        kwargs = _env_or_skip()
        # Override credentials with bad ones — any password-based
        # config becomes a guaranteed-bad password.
        if "password" in kwargs:
            kwargs["password"] = "definitely-wrong-password"
        else:
            # Token auth: scramble the secret.
            kwargs["token_value"] = "definitely-wrong-token-value"
        orch = ProxmoxOrchestrator(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(OrchestratorError, match="cannot reach"):
            orch.__enter__()
