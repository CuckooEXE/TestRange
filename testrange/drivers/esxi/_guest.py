"""VMware Tools guest-ops transport for the ESXi backend (ESXI-5).

The native in-guest channel: ``GuestOperationsManager`` over the SOAP control
plane. Unlike QGA (libvirt/Proxmox), VMware Tools authenticates against the
**guest OS** on every call — so each ``make_*`` binds the per-call ``credential``
(CORE-60, ADR-0008) into a ``NamePasswordAuthentication``. A missing credential
is a hard error, not a guess.

Two VMware-Tools realities shape this:

- **exec captures no output.** ``StartProgramInGuest`` returns only a pid;
  ``ListProcessesInGuest`` later yields its exit code — there is no stdout/stderr
  capture. So ``execute`` runs the command under ``/bin/sh -c`` with output
  redirected to guest temp files, polls for exit, then reads those files back
  over the guest file-transfer channel and deletes them. A guest shell + Tools
  is therefore required (the cloud image installs ``open-vm-tools``).
- **file I/O is an HTTPS side-channel.** ``Initiate*FileTransfer*`` returns a
  one-time URL the orchestrator GETs/PUTs directly (the ticket in the URL
  authorizes it); :meth:`EsxiClient.guest_file_get`/``put`` pin its host.
"""

from __future__ import annotations

import secrets
import shlex
import time
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.communicators.base import ExecResult
from testrange.exceptions import GuestAgentError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.credentials.base import Credential
    from testrange.drivers.esxi._client import EsxiClient
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile

_log = get_logger(__name__)
_POLL_INTERVAL_S = 0.5


def _auth(client: EsxiClient, credential: Credential | None, backend_name: str) -> Any:
    """Build a ``NamePasswordAuthentication`` from the per-call guest credential."""
    if credential is None:
        raise GuestAgentError(
            f"ESXi VMware Tools guest-ops on {backend_name!r} require a guest credential "
            "(username+password); none was supplied (CORE-60). Bake one into the builder."
        )
    password = getattr(credential, "password", None)
    if not password:
        raise GuestAgentError(
            f"ESXi guest-ops need a password credential for {credential.username!r} on "
            f"{backend_name!r}; key-only credentials are not usable over VMware Tools"
        )
    return client.vim.vm.guest.NamePasswordAuthentication(
        username=credential.username, password=password, interactiveSession=False
    )


def _managers(client: EsxiClient) -> tuple[Any, Any]:
    gom = client.content.guestOperationsManager
    return gom.processManager, gom.fileManager


def make_execute(client: EsxiClient, backend_name: str, credential: Credential | None) -> GuestExec:
    vm = client.require_vm(backend_name)
    auth = _auth(client, credential, backend_name)
    pm, fm = _managers(client)
    vim = client.vim

    def _execute(
        argv: Sequence[str], *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        start = time.monotonic()
        token = secrets.token_hex(8)
        # In-GUEST temp paths (not orchestrator-host files — S108 is about host
        # temp races, which don't apply across the guest-ops boundary).
        out, err, rc = (f"/tmp/tr-exec-{token}.{ext}" for ext in ("out", "err", "rc"))  # noqa: S108
        cmd = " ".join(shlex.quote(a) for a in argv)
        wrapper = f"{cmd} >{out} 2>{err}; echo $? >{rc}"
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/sh",
            arguments=f"-c {shlex.quote(wrapper)}",
            workingDirectory=cwd or "",
        )
        try:
            # Serialize each discrete guest-ops SOAP call on the client's call_lock
            # (ADR-0023): the I/O phases drive ONE shared pyVmomi stub concurrently,
            # and its session/ticket-refresh state is not thread-safe. Held per-op
            # and released before the poll sleep (and before the byte transfers in
            # _read) so concurrent guests still overlap — mirrors proxmox/libvirt
            # _guest.py, which honor the same contract (ESXI-32).
            with client.call_lock:
                pid = pm.StartProgramInGuest(vm=vm, auth=auth, spec=spec)
        except Exception as e:
            raise GuestAgentError(f"VMware Tools exec failed on {backend_name!r}: {e}") from e
        while True:
            try:
                # Like every per-call site here, translate raw pyVmomi faults to
                # GuestAgentError: NativeCommunicator's reconnect loop retries
                # only GuestAgentError (communicators/native.py), so a raw fault
                # would escape execute() instead of being retried (ESXI-35).
                with client.call_lock:
                    procs = pm.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid])
            except Exception as e:
                raise GuestAgentError(
                    f"VMware Tools exec poll failed on {backend_name!r}: {e}"
                ) from e
            info = procs[0] if procs else None
            if info is not None and info.exitCode is not None:
                break
            if time.monotonic() - start > timeout:
                raise GuestAgentError(
                    f"VMware Tools exec of {list(argv)!r} timed out after {timeout}s "
                    f"on {backend_name!r}"
                )
            time.sleep(_POLL_INTERVAL_S)
        stdout = _read(client, fm, vm, auth, out, backend_name)
        stderr = _read(client, fm, vm, auth, err, backend_name)
        rc_raw = _read(client, fm, vm, auth, rc, backend_name).strip()
        for path in (out, err, rc):
            _delete(client, fm, vm, auth, path)
        return ExecResult(
            exit_code=int(rc_raw) if rc_raw.isdigit() else int(info.exitCode),
            stdout=stdout,
            stderr=stderr,
            duration=time.monotonic() - start,
        )

    return _execute


def _read(client: EsxiClient, fm: Any, vm: Any, auth: Any, path: str, backend_name: str) -> bytes:
    try:
        # Lock the SOAP call; release before the (slow) HTTP byte transfer
        # (ESXI-32). Both legs translate to GuestAgentError — the only exception
        # NativeCommunicator's reconnect loop retries (communicators/native.py)
        # (ESXI-35).
        with client.call_lock:
            info = fm.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=path)
        return client.guest_file_get(info.url)
    except Exception as e:
        raise GuestAgentError(
            f"VMware Tools exec-output read of {path!r} failed on {backend_name!r}: {e}"
        ) from e


def _delete(client: EsxiClient, fm: Any, vm: Any, auth: Any, path: str) -> None:
    try:
        with client.call_lock:
            fm.DeleteFileInGuest(vm=vm, auth=auth, filePath=path)
    except Exception as e:  # best-effort temp cleanup; a leaked /tmp file is harmless
        _log.debug("guest temp cleanup of %s failed: %s", path, e)


def make_read_file(
    client: EsxiClient, backend_name: str, credential: Credential | None
) -> GuestReadFile:
    vm = client.require_vm(backend_name)
    auth = _auth(client, credential, backend_name)
    _pm, fm = _managers(client)

    def _read_file(path: str) -> bytes:
        try:
            with client.call_lock:
                info = fm.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=path)
            return client.guest_file_get(info.url)
        except Exception as e:
            raise GuestAgentError(
                f"VMware Tools file-read {path!r} failed on {backend_name!r}: {e}"
            ) from e

    return _read_file


def make_write_file(
    client: EsxiClient, backend_name: str, credential: Credential | None
) -> GuestWriteFile:
    vm = client.require_vm(backend_name)
    auth = _auth(client, credential, backend_name)
    _pm, fm = _managers(client)
    vim = client.vim

    def _write_file(path: str, data: bytes) -> None:
        try:
            with client.call_lock:
                url = fm.InitiateFileTransferToGuest(
                    vm=vm,
                    auth=auth,
                    guestFilePath=path,
                    fileAttributes=vim.vm.guest.FileManager.FileAttributes(),
                    fileSize=len(data),
                    overwrite=True,
                )
            client.guest_file_put(url, data)
        except Exception as e:
            raise GuestAgentError(
                f"VMware Tools file-write {path!r} failed on {backend_name!r}: {e}"
            ) from e

    return _write_file
