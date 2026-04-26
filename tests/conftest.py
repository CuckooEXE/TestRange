"""Shared test fixtures and import stubs.

This conftest installs lightweight stubs for heavy / system-level
dependencies (``libvirt``, ``pycdlib``, ``passlib``) if they are not
installed.  This lets the full test suite run on CI runners that do not
have ``libvirt-python`` or a libvirt daemon available.

When the real packages *are* installed, the stubs are not used and the
tests exercise real behaviour.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Dependency stubs (only installed if the real package is missing)
# ---------------------------------------------------------------------------


def _install_libvirt_stub() -> None:
    try:
        import libvirt  # noqa: F401
        return
    except ImportError:
        pass

    mod = types.ModuleType("libvirt")

    class libvirtError(Exception):
        pass

    mod.libvirtError = libvirtError  # type: ignore[attr-defined]
    mod.virConnect = MagicMock  # type: ignore[attr-defined]
    mod.virDomain = MagicMock  # type: ignore[attr-defined]
    mod.virNetwork = MagicMock  # type: ignore[attr-defined]
    mod.VIR_DOMAIN_SHUTOFF = 5  # type: ignore[attr-defined]

    def _open(uri: str) -> Any:
        return MagicMock(name=f"virConnect({uri})")

    mod.open = _open  # type: ignore[attr-defined]
    sys.modules["libvirt"] = mod


def _install_pycdlib_stub() -> None:
    try:
        import pycdlib  # noqa: F401
        return
    except ImportError:
        pass

    mod = types.ModuleType("pycdlib")
    mod.PyCdlib = MagicMock  # type: ignore[attr-defined]
    sys.modules["pycdlib"] = mod


def _install_passlib_stub() -> None:
    try:
        from passlib.hash import (
            sha512_crypt,  # noqa: F401  # pyright: ignore[reportAttributeAccessIssue]
        )
        return
    except ImportError:
        pass

    passlib = types.ModuleType("passlib")
    passlib_hash = types.ModuleType("passlib.hash")

    class _Hasher:
        @staticmethod
        def using(**_: Any) -> Any:
            return _Hasher()

        @staticmethod
        def hash(plaintext: str) -> str:
            return f"$6$rounds=5000$stub${plaintext}"

    passlib_hash.sha512_crypt = _Hasher()  # type: ignore[attr-defined]
    passlib.hash = passlib_hash  # type: ignore[attr-defined]
    sys.modules["passlib"] = passlib
    sys.modules["passlib.hash"] = passlib_hash


def _install_paramiko_stub() -> None:
    try:
        import paramiko  # noqa: F401
        return
    except ImportError:
        pass

    mod = types.ModuleType("paramiko")

    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class _StubClient:
        def set_missing_host_key_policy(self, *_: Any, **__: Any) -> None: ...
        def connect(self, *_: Any, **__: Any) -> None: ...
        def exec_command(self, *_: Any, **__: Any) -> Any:  # noqa: D401
            return MagicMock(), MagicMock(), MagicMock()
        def open_sftp(self, *_: Any, **__: Any) -> Any:
            return MagicMock()
        def close(self) -> None: ...

    mod.SSHException = SSHException  # type: ignore[attr-defined]
    mod.AuthenticationException = AuthenticationException  # type: ignore[attr-defined]
    mod.SSHClient = _StubClient  # type: ignore[attr-defined]
    mod.AutoAddPolicy = MagicMock  # type: ignore[attr-defined]
    sys.modules["paramiko"] = mod


def _install_winrm_stub() -> None:
    try:
        import winrm  # noqa: F401  # pyright: ignore[reportMissingImports]
        return
    except ImportError:
        pass

    mod = types.ModuleType("winrm")

    class _StubSession:
        def __init__(self, *_: Any, **__: Any) -> None: ...
        def run_cmd(self, *_: Any, **__: Any) -> Any:
            return MagicMock(status_code=0, std_out=b"", std_err=b"")
        def run_ps(self, *_: Any, **__: Any) -> Any:
            return MagicMock(status_code=0, std_out=b"", std_err=b"")

    mod.Session = _StubSession  # type: ignore[attr-defined]
    sys.modules["winrm"] = mod


_install_libvirt_stub()
_install_pycdlib_stub()
_install_passlib_stub()
_install_paramiko_stub()
_install_winrm_stub()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(tmp_path: Path) -> Path:
    """A fresh, writable directory suitable for use as a cache root."""
    root = tmp_path / "testrange-cache"
    root.mkdir()
    return root


@pytest.fixture
def fake_qemu_img(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch ``subprocess.run`` inside :mod:`testrange.cache` to record
    invocations instead of executing ``qemu-img``.

    :returns: A list that receives each recorded argv (mutated in-place as
        each call happens).
    """
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *_, **__: Any) -> Any:
        calls.append(list(argv))
        # Mimic the final output file so downstream ``.exists()`` checks pass.
        if len(argv) >= 2 and argv[0] == "qemu-img" and argv[1] in {"create", "convert"}:
            dest = Path(argv[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake qcow2")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    from testrange.storage.disk import _qemu_img
    monkeypatch.setattr(_qemu_img.subprocess, "run", _fake_run)
    return calls


@pytest.fixture
def sample_credential():
    """A non-root Credential with sudo enabled."""
    from testrange.credentials import Credential
    return Credential(
        username="deploy",
        password="correcthorsebatterystaple",
        ssh_key="ssh-ed25519 AAAAAA deploy@host",
        sudo=True,
    )


@pytest.fixture
def root_credential():
    """A root Credential (sudo flag is ignored for root)."""
    from testrange.credentials import Credential
    return Credential(username="root", password="rootpw", sudo=False)
