"""Package ABC."""

from __future__ import annotations

import re
from abc import ABC
from dataclasses import dataclass

# A package name flows UNQUOTED into ``apt-get install``/``pip3 install`` in the
# provisioning script (cloudinit.py), so it is a shell-injection boundary. Even
# though the plan author is trusted, we validate it the way the project
# validates other author inputs (VM names, IPs): a conservative charset that
# covers every real apt/pip package name (letters, digits, and ``. _ + -``) and
# excludes shell metacharacters (``;`` ``|`` ``&`` ``$`` space, …). Version pins
# belong in a future ``version=`` field, not shell-unsafe chars in the name.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


@dataclass(frozen=True)
class Package(ABC):
    """Abstract package. Concretes carry the package name and any extras."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__}.name must be a non-empty string")
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(
                f"{type(self).__name__}.name {self.name!r} is not a valid package name "
                "(allowed: letters, digits, and . _ + -). It flows into an apt-get/pip "
                "install command, so shell metacharacters are rejected at this boundary."
            )
