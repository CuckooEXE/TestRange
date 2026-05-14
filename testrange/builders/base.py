"""Builder ABC.

The Builder is the only component that drives the install lifecycle — no
Communicator is involved during install. The Builder produces a
self-terminating install payload (e.g., cloud-init seed that ends with
``poweroff``), and the orchestrator polls driver-level power state.

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

        Pure: must not depend on run_id, clocks, or any non-deterministic
        input. Same spec+recipe+addressing+base_sha+macs -> same hash,
        every time. This is the cache key for the post-install disk.

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

    def wait_ready_argv(
        self, spec: VMSpec, recipe: VMRecipe
    ) -> tuple[str, ...] | None:
        """Argv whose exit-zero signals the brought-up VM is ready for tests.

        ``None`` (the default) means no check needed — for builders that
        produce a fully-baked disk with no post-boot finalization.
        Concretes override when their build leaves work to finish at
        run-phase boot (cloud-init's stage machine, Ignition's
        finalize, etc.). The orchestrator executes the returned argv
        via the bound Communicator after ``_bind_communicators`` and
        before yielding the ``OrchestratorHandle`` to test code; a
        non-zero exit raises :class:`BuildNotReadyError`.
        """
        del spec, recipe
        return None
