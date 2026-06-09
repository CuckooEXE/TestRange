"""pyVmomi fakes for the ESXi driver unit suite (ESXI-10).

The driver constructs pyVmomi spec/device objects off a ``vim`` namespace and
drives managed-object methods (``AddVirtualSwitch``, ``CreateVM_Task``,
``CreateVirtualDisk_Task``, guest-ops, …). These fakes model just enough of that
surface to exercise the driver's *orchestration* logic — call sequence,
build-vs-run device assembly, idempotency, name resolution, error translation —
without a live host or pyvmomi installed. The real pyVmomi API shapes are proven
by the live ``esxi``-marked suite, not here (the same split as the proxmoxer fakes).

``FakeVim`` is a recursive namespace: an attribute chain (``vim.vm.device.VirtualDisk``)
yields a node; calling it (``VirtualDisk(**kw)``) records a ``SimpleNamespace``
tagged with its dotted type; a leaf accessed but not called (``vim.TaskInfo.State.success``)
is an enum sentinel that compares equal by path — so the driver's
``state == vim.X.Y.Z`` enum checks work when both sides come from the same vim.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _VimNode:
    """A node in the fake ``vim`` namespace: callable (construct) + enum-comparable."""

    __slots__ = ("_path",)

    def __init__(self, path: str = "") -> None:
        self._path = path

    def __getattr__(self, name: str) -> _VimNode:
        return _VimNode(f"{self._path}.{name}" if self._path else name)

    def __call__(self, **kw: Any) -> SimpleNamespace:
        return SimpleNamespace(_vimtype=self._path, **kw)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _VimNode) and other._path == self._path

    def __hash__(self) -> int:
        return hash(self._path)

    def __repr__(self) -> str:
        return f"<vim {self._path}>"


class FakeVim(_VimNode):
    """Root of the fake vim namespace."""

    def __init__(self) -> None:
        super().__init__("")


class _FakeTask:
    def __init__(self, result: Any = None) -> None:
        self.result = result


class _NetworkSystem:
    """Records host-network reconfigure calls and mutates the fake host's lists."""

    def __init__(self, host: _FakeHost) -> None:
        self._host = host

    def AddVirtualSwitch(self, vswitchName: str, spec: Any) -> None:
        pnics = list(getattr(getattr(spec, "bridge", None), "nicDevice", []) or [])
        self._host._vswitches[vswitchName] = pnics

    def RemoveVirtualSwitch(self, vswitchName: str) -> None:
        self._host._vswitches.pop(vswitchName, None)

    def AddPortGroup(self, portgrp: Any) -> None:
        self._host._portgroups[portgrp.name] = portgrp.vswitchName

    def RemovePortGroup(self, pgName: str) -> None:
        self._host._portgroups.pop(pgName, None)

    def AddVirtualNic(self, portgroup: str, nic: Any) -> str:
        device = f"vmk{len(self._host._vnics)}"
        self._host._vnics[device] = (portgroup, nic.ip.ipAddress)
        return device

    def RemoveVirtualNic(self, device: str) -> None:
        self._host._vnics.pop(device, None)


class _FakeHost:
    def __init__(self, vim: FakeVim, pnics: list[str]) -> None:
        self._vim = vim
        self._vswitches: dict[str, list[str]] = {}
        self._portgroups: dict[str, str] = {}  # name -> vswitchName
        self._vnics: dict[str, tuple[str, str]] = {}  # device -> (portgroup, ip)
        self._pnics = pnics
        self.vm: list[_FakeVM] = []
        self.configManager = SimpleNamespace(networkSystem=_NetworkSystem(self))

    @property
    def config(self) -> Any:
        net = SimpleNamespace(
            vswitch=[SimpleNamespace(name=n, pnic=p) for n, p in self._vswitches.items()],
            portgroup=[
                SimpleNamespace(spec=SimpleNamespace(name=n, vswitchName=v))
                for n, v in self._portgroups.items()
            ],
            vnic=[
                SimpleNamespace(
                    device=d, portgroup=pg, spec=SimpleNamespace(ip=SimpleNamespace(ipAddress=ip))
                )
                for d, (pg, ip) in self._vnics.items()
            ],
            pnic=[SimpleNamespace(device=d) for d in self._pnics],
        )
        return SimpleNamespace(network=net)


class _FakeVM:
    def __init__(self, vim: FakeVim, name: str, config_spec: Any) -> None:
        self._vim = vim
        self.name = name
        self.config_spec = config_spec  # the ConfigSpec it was created from
        self._power = vim.VirtualMachine.PowerState.poweredOff
        self.snapshot: Any = None
        self.events: list[str] = []

    @property
    def runtime(self) -> Any:
        return SimpleNamespace(powerState=self._power)

    def PowerOnVM_Task(self) -> _FakeTask:
        self._power = self._vim.VirtualMachine.PowerState.poweredOn
        self.events.append("poweron")
        return _FakeTask()

    def PowerOffVM_Task(self) -> _FakeTask:
        self._power = self._vim.VirtualMachine.PowerState.poweredOff
        self.events.append("poweroff")
        return _FakeTask()

    def ShutdownGuest(self) -> None:
        self._power = self._vim.VirtualMachine.PowerState.poweredOff
        self.events.append("shutdownguest")

    def Destroy_Task(self) -> _FakeTask:
        self.events.append("destroy")
        return _FakeTask()


class _VmFolder:
    def __init__(self, client: FakeEsxiClient) -> None:
        self._client = client

    def CreateVM_Task(self, config: Any, pool: Any, host: Any) -> _FakeTask:
        vm = _FakeVM(self._client.vim, config.name, config)
        self._client.host.vm.append(vm)
        return _FakeTask(result=vm)


def _rel(ref_or_path: str) -> str:
    """Datastore-relative path of a bracket ref (``[ds] p/x.vmdk`` -> ``p/x.vmdk``).

    Disks and /folder files share one in-memory store keyed by the relative path,
    so a disk made by VirtualDiskManager is visible to ``folder_exists`` — exactly
    as a live /folder HEAD would see a VMFS disk CopyVirtualDisk just created.
    """
    if ref_or_path.startswith("["):
        return ref_or_path.split("] ", 1)[1]
    return ref_or_path


class _VirtualDiskManager:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def CreateVirtualDisk_Task(self, name: str, datacenter: Any, spec: Any) -> _FakeTask:
        self._files[_rel(name)] = b"\x00" * 16
        return _FakeTask()

    def CopyVirtualDisk_Task(
        self,
        sourceName: str,
        sourceDatacenter: Any,
        destName: str,
        destDatacenter: Any,
        destSpec: Any,
        force: bool,
    ) -> _FakeTask:
        self._files[_rel(destName)] = self._files.get(_rel(sourceName), b"vmfs")
        return _FakeTask()

    def ExtendVirtualDisk_Task(
        self,
        name: str,
        datacenter: Any,
        newCapacityKb: int,
        eagerZero: bool,
    ) -> _FakeTask:
        self._files[_rel(name)] = b"\x00" * 32
        return _FakeTask()

    def DeleteVirtualDisk_Task(self, name: str, datacenter: Any) -> _FakeTask:
        self._files.pop(_rel(name), None)
        return _FakeTask()


class _FileManager:
    def __init__(self, client: FakeEsxiClient) -> None:
        self._client = client

    def MakeDirectory(self, name: str, datacenter: Any, createParentDirectories: bool) -> None:
        self._client.dirs.add(name)

    def DeleteDatastoreFile_Task(self, name: str, datacenter: Any) -> _FakeTask:
        self._client.dirs.discard(name)
        return _FakeTask()


class FakeEsxiClient:
    """A duck-typed stand-in for :class:`EsxiClient` the driver concern modules drive."""

    def __init__(self, *, pnics: list[str] | None = None, datastore_free_gb: int = 1000) -> None:
        self.vim = FakeVim()
        self.datastore_name = "datastore1"
        self.host = _FakeHost(self.vim, pnics or ["vmnic0", "vmnic1"])
        self.resource_pool = SimpleNamespace(name="Resources")
        self.dirs: set[str] = set()
        self.files: dict[str, bytes] = {}  # datastore-relative path -> bytes
        self.guest_files: dict[str, bytes] = {}
        self._free = datastore_free_gb * (1024**3)
        self.datacenter = SimpleNamespace(name="ha-datacenter", vmFolder=_VmFolder(self))
        self.content = SimpleNamespace(
            virtualDiskManager=_VirtualDiskManager(self.files),
            fileManager=_FileManager(self),
            guestOperationsManager=SimpleNamespace(
                processManager=SimpleNamespace(), fileManager=SimpleNamespace()
            ),
        )

    @property
    def datastore(self) -> Any:
        return SimpleNamespace(
            name=self.datastore_name, summary=SimpleNamespace(freeSpace=self._free)
        )

    @property
    def network_system(self) -> Any:
        return self.host.configManager.networkSystem

    def find_vm(self, name: str) -> Any:
        # Annotated Any (not Any | None) so tests can drive the returned fake VM
        # without a None-guard at every call site; the real EsxiClient.find_vm
        # keeps the precise Any | None.
        return next((vm for vm in self.host.vm if vm.name == name), None)

    def require_vm(self, name: str) -> Any:
        vm = self.find_vm(name)
        if vm is None:
            from testrange.exceptions import DriverError

            raise DriverError(f"no fake VM named {name!r}")
        return vm

    def wait_for_task(self, task: _FakeTask, *, timeout: float = 0.0) -> Any:
        return task.result

    def folder_put(self, source_path: Path, ds_path: str) -> None:
        self.files[ds_path] = Path(source_path).read_bytes()

    def folder_get(self, ds_path: str, dest_path: Path) -> None:
        Path(dest_path).write_bytes(self.files.get(ds_path, b""))

    def folder_exists(self, ds_path: str) -> bool:
        return ds_path in self.files

    def folder_delete(self, ds_path: str) -> bool:
        return self.files.pop(ds_path, None) is not None

    def folder_read_from(self, ds_path: str, offset: int) -> bytes:
        return self.files.get(ds_path, b"")[offset:]

    def guest_file_get(self, url: str) -> bytes:
        return self.guest_files.get(url, b"")

    def guest_file_put(self, url: str, data: bytes) -> None:
        self.guest_files[url] = data
