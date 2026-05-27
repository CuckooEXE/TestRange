"""CloudInitBuilder — cloud-init seed renderer for Linux guests.

Phase 0: data-only skeleton — fields, validation, no rendering.
Phase 3: real ``render_seed`` via pycdlib + a deterministic ``config_hash``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.builders.base import Builder
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.packages.base import Package

if TYPE_CHECKING:  # pragma: no cover
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


class CloudInitBuilder(Builder):
    """Cloud-init seed builder.

    Credentials, packages, and post-install commands all live on the
    Builder. The Communicator does not see them directly — at bind time
    the orchestrator looks up ``builder.credentials`` by username.
    """

    def __init__(
        self,
        *,
        base: CacheEntry,
        credentials: Sequence[Credential] = (),
        packages: Sequence[Package] = (),
        post_install_commands: Sequence[str] = (),
    ) -> None:
        if not isinstance(base, CacheEntry):
            raise TypeError(
                f"CloudInitBuilder.base must be a CacheEntry, got {type(base).__name__}"
            )
        creds = tuple(credentials)
        pkgs = tuple(packages)
        cmds = tuple(post_install_commands)
        for c in creds:
            if not isinstance(c, Credential):
                raise TypeError(
                    f"CloudInitBuilder.credentials must contain Credential, got {type(c).__name__}"
                )
        for p in pkgs:
            if not isinstance(p, Package):
                raise TypeError(
                    f"CloudInitBuilder.packages must contain Package, got {type(p).__name__}"
                )
        for cmd in cmds:
            if not isinstance(cmd, str) or not cmd:
                raise ValueError(
                    "CloudInitBuilder.post_install_commands entries must be non-empty strings"
                )
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"CloudInitBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        self.base = base
        self._credentials = creds
        self.packages = pkgs
        self.post_install_commands = cmds

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self._credentials

    def find_credential(self, username: str) -> Credential | None:
        """Look up a credential by username. Returns None if not found."""
        for c in self._credentials:
            if c.username == username:
                return c
        return None

    def config_hash(self, spec: VMSpec, recipe: VMRecipe) -> str:
        raise NotImplementedError("CloudInitBuilder.config_hash lands in Phase 3")

    def render_seed(self, spec: VMSpec, recipe: VMRecipe) -> bytes:
        raise NotImplementedError("CloudInitBuilder.render_seed lands in Phase 3")
