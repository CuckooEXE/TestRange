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
from collections.abc import Mapping
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
    ) -> str:
        """16-char hex hash that uniquely identifies the post-install disk.

        Pure: must not depend on run_id, clocks, or any non-deterministic
        input. Same spec+recipe+addressing+base_sha -> same hash, every
        time. This is the cache key for the post-install disk.
        """

    @abstractmethod
    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
    ) -> bytes:
        """Render the install payload (e.g., a cloud-init seed ISO) as bytes."""
