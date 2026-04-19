"""Proxmox VE QEMU guest-agent communicator (SCAFFOLDING).

.. warning::

   Not yet implemented.  Instantiating and calling any method raises
   :class:`NotImplementedError` with a TODO list pointing at the
   Proxmox REST endpoints that would satisfy the :class:`AbstractCommunicator`
   contract.

Design notes
------------

Proxmox exposes the QEMU guest agent over HTTPS at::

    /api2/json/nodes/{node}/qemu/{vmid}/agent

The endpoints of interest for an :class:`AbstractCommunicator` impl:

- ``POST /agent/ping`` — used to wait for readiness.
- ``POST /agent/exec`` + ``GET /agent/exec-status/{pid}`` —
  command execution with output capture (polls for completion).
- ``POST /agent/file-read`` / ``POST /agent/file-write`` — file I/O
  (base64-wrapped chunks, 48 KiB Proxmox default limit).
- ``GET /agent/get-osinfo`` / ``GET /agent/network-get-interfaces``
  — what libvirt surfaces via ``guest-*`` JSON-RPC calls.

Unlike the libvirt :class:`~testrange.backends.libvirt.GuestAgentCommunicator`
— which holds a ``libvirt.virDomain`` and pokes ``libvirt_qemu.qemuAgentCommand``
directly — this communicator will carry a ``proxmoxer`` client handle
plus ``(node, vmid)`` and translate each :class:`AbstractCommunicator`
method into the right REST call.
"""

from __future__ import annotations

from testrange.communication.base import AbstractCommunicator, ExecResult


class ProxmoxGuestAgentCommunicator(AbstractCommunicator):
    """Proxmox QEMU guest-agent communicator (SCAFFOLDING).

    :param client: A ``proxmoxer.ProxmoxAPI`` (or equivalent) client
        authenticated against the Proxmox REST API.
    :param node: Proxmox node name hosting the VM.
    :param vmid: Proxmox numeric VMID.
    """

    def __init__(
        self,
        client: object,
        node: str,
        vmid: int,
    ) -> None:
        self._client = client
        self._node = node
        self._vmid = vmid

    def wait_ready(self, timeout: int = 300) -> None:
        # TODO: POST /nodes/{node}/qemu/{vmid}/agent/ping in a loop
        # until 200 OK (or ``timeout`` elapses).  Raise VMTimeoutError
        # on deadline.
        raise NotImplementedError(
            "ProxmoxGuestAgentCommunicator.wait_ready is not yet implemented."
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        # TODO: POST /agent/exec with the argv; poll
        # /agent/exec-status/{pid} until ``exited`` is true (or
        # ``timeout`` elapses); decode ``out-data`` + ``err-data``
        # base64 payloads into the ExecResult.
        raise NotImplementedError(
            "ProxmoxGuestAgentCommunicator.exec is not yet implemented."
        )

    def get_file(self, path: str) -> bytes:
        # TODO: POST /agent/file-read {path}; Proxmox returns
        # base64-encoded content inline (guest agent caps at ~48 KiB
        # per read; loop with offset+count for larger files).
        raise NotImplementedError(
            "ProxmoxGuestAgentCommunicator.get_file is not yet implemented."
        )

    def put_file(self, path: str, data: bytes) -> None:
        # TODO: POST /agent/file-write {path, content (base64)};
        # chunk to stay under MaxEnvelopeSizekb, same as the libvirt
        # path does for WinRM.
        raise NotImplementedError(
            "ProxmoxGuestAgentCommunicator.put_file is not yet implemented."
        )

    def hostname(self) -> str:
        # TODO: GET /agent/get-host-name or exec(["hostname"]).
        raise NotImplementedError(
            "ProxmoxGuestAgentCommunicator.hostname is not yet implemented."
        )
