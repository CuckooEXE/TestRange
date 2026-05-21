"""Builder ABC.

The Builder drives the install lifecycle end to end: it produces a
self-terminating install payload (e.g., a cloud-init seed that ends with
``poweroff``), and the orchestrator polls driver-level power state until
the VM shuts off.

Builders are hypervisor-agnostic. When a builder needs per-network
addressing facts (CIDR, prefix, gateway, DHCP flag) to render guest config,
the orchestrator brokers: it builds a
``Mapping[network_name, NetworkAddressing]`` from
``hypervisor.all_networks`` and hands it in. The Builder never sees the
hypervisor type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.credentials.base import Credential
    from testrange.guest_io import GuestExec
    from testrange.networks.base import NetworkAddressing
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


class Builder(ABC):
    """Abstract builder. Concretes own the install lifecycle."""

    @property
    @abstractmethod
    def credentials(self) -> tuple[Credential, ...]:
        """Credentials baked into the disk by this builder.

        Returned in declaration order. The orchestrator consults this when
        binding a Communicator that names a credential by username.
        """

    @abstractmethod
    def config_hash(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        base_sha: str = "",
        macs: Sequence[str] = (),
    ) -> str:
        """16-char hex hash that uniquely identifies the post-install disk.

        Pure and deterministic: same ``(spec, recipe, addressing, base_sha,
        macs)`` -> same hash, every time, with no ``run_id``/clock/random
        input. This is the post-install cache key; the rationale and the
        contract for builder authors live in ADR-0007.

        ``macs`` (one per NIC in spec order) lets concretes that bake
        positional NIC config into the install payload key the cache on
        the stable MACs the orchestrator will assign at run-phase.
        """

    @abstractmethod
    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
    ) -> bytes:
        """Render the install payload (e.g., a cloud-init seed ISO) as bytes.

        ``macs`` (one per NIC in spec order) lets concretes bake
        positional NIC config (run-phase netplan match-by-MAC etc.) into
        the payload.
        """

    def wait_ready(self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec) -> None:
        """Block until the brought-up VM is ready for tests.

        Default: no-op — for builders that produce a fully-baked disk
        with no post-boot finalization. Concretes whose build leaves
        work to finish at run-phase boot (cloud-init's stage machine,
        Ignition's finalize, etc.) override: run the readiness command
        via ``execute`` and raise :class:`BuildNotReadyError` if it
        never succeeds. The builder never sees a Communicator type —
        only the injected ``execute`` callable. The orchestrator calls
        this after ``_bind_communicators`` and before yielding the
        ``OrchestratorHandle`` to test code.
        """
        del spec, recipe, execute
