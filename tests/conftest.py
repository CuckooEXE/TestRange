"""Shared pytest fixtures + test-only backend registration.

The in-memory ``mock`` backend is **test-only** (BACKEND-1.E): it lives in
``tests/mock_driver.py`` rather than ``testrange/drivers/`` (libvirt is the
reference implementation). Importing it here, once per session, runs its
module-scope ``register()`` / ``register_profile()`` so that unit tests and
``--profile mock`` resolve the ``mock`` scheme and the ``MockDriver`` name —
exactly as the production drivers' side-effect imports do for libvirt/proxmox.
"""

from __future__ import annotations

from tests import mock_driver as _mock_driver  # noqa: F401  (side-effect: registers mock)
