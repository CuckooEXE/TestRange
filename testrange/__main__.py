"""Entry point for ``python -m testrange``.

Mirrors the ``testrange`` console script installed by ``pyproject.toml``
so the package can be run as a module without the script being on
``$PATH``.
"""

from __future__ import annotations

from testrange._cli import main

if __name__ == "__main__":
    main()
