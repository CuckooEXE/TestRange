"""Connection plumbing for the ESXi driver (ESXI-1).

The driver is **pyVmomi-only** for the control plane (vSwitch/portgroup, VM
lifecycle, snapshots, guest-ops, disk inflate). One sanctioned non-pyVmomi
transport lives here: the datastore **``/folder`` HTTPS endpoint** for volume
byte I/O in both directions (``upload_to_pool`` PUT, ``download_from_pool`` GET)
— ESXi has no SOAP byte-egress for datastore files, so the bytes ride an
authenticated ``requests`` channel against the host's file service.

``EsxiConn`` is the connection config (round-trips through the teardown URI);
``EsxiClient`` wraps a live ``ServiceInstance`` plus the resolved standalone-host
managed objects (host / compute-resource / resource-pool / datacenter /
datastore) and the task waiter. The driver holds exactly one ``EsxiClient``; the
concern modules (``_net``/``_storage``/``_vm``/``_guest``/``_serial``) take it as
their first argument.

Standalone host only (ADR-0025): ``apiType == 'HostAgent'``. There is exactly
one HostSystem (``ha-host``), one ComputeResource, one root resource pool
(``ha-root-pool``), and one datacenter (``ha-datacenter``); vCenter / DVS are out
of scope.
"""

from __future__ import annotations

import ssl
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange._progress import ProgressReporter
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

_log = get_logger(__name__)

# A VM clone/import/snapshot task is usually quick, but a disk inflate
# (CopyVirtualDisk) on a multi-GB image runs minutes. Size the default
# generously; callers pass a tighter value where they want one.
_TASK_TIMEOUT_S = 1200.0
_TASK_POLL_S = 1.0

# HTTP timeouts for the /folder byte channel: a generous read for multi-GB disk
# transfers, a short connect.
_HTTP_CONNECT_TIMEOUT_S = 30.0
_HTTP_READ_TIMEOUT_S = 1800.0
# Streaming chunk for the /folder byte channel. 4 MiB keeps the per-iteration
# HTTP/syscall overhead negligible on a multi-GB vmdk transfer without buffering
# the whole disk in memory (mirrors the libvirt storage pump).
_CHUNK = 4 * 1024 * 1024


def _import_pyvmomi() -> tuple[Any, Any]:
    """Lazy import of pyVmomi. Raises :class:`DriverError` with an install hint.

    The driver module imports with pyvmomi absent (the SDK is optional and the
    package must register without it); the import is forced here, at ``connect``.
    Returns ``(pyVim.connect, pyVmomi.vim)``.
    """
    try:
        from pyVim import connect as vim_connect
        from pyVmomi import vim
    except ImportError as e:
        raise DriverError("pyvmomi is not installed; install with `pip install -e .[esxi]`") from e
    return vim_connect, vim


def _import_requests() -> Any:
    """Lazy import of requests — the datastore ``/folder`` byte transport."""
    try:
        import requests
        import urllib3
    except ImportError as e:
        raise DriverError("requests is not installed; install with `pip install -e .[esxi]`") from e
    # ESXi ships a self-signed cert and we connect with verify off; silence the
    # per-request warning (the lab posture is documented).
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return requests


@dataclass(frozen=True)
class EsxiConn:
    """Everything needed to reach a standalone ESXi host over SOAP + /folder.

    ``datastore`` is the backing store volume byte I/O targets (analogous to
    Proxmox ``backing_storage``); it rides the teardown URI so ``compose_volume_ref``
    purity survives a ``from_uri`` cleanup driver.
    """

    host: str
    user: str = "root"
    password: str = ""
    datastore: str = "datastore1"
    verify_ssl: bool = False
    port: int = 443

    def to_uri(self) -> str:
        """Round-trip to the URI persisted in state.json (cleanup entry point).

        Credentials are embedded (lab posture, per the approved plan); a
        production backend would carry only non-secret routing here.
        """
        userinfo = urllib.parse.quote(self.user, safe="")
        if self.password:
            userinfo += ":" + urllib.parse.quote(self.password, safe="")
        query = urllib.parse.urlencode(
            {"datastore": self.datastore, "verify": "1" if self.verify_ssl else "0"}
        )
        return f"esxi://{userinfo}@{self.host}:{self.port}/?{query}"

    @classmethod
    def from_uri(cls, uri: str) -> EsxiConn:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "esxi":
            raise DriverError(f"expected an esxi:// connection URI, got {uri!r}")
        if not parsed.hostname:
            raise DriverError(f"connection URI is missing a host: {uri!r}")
        q = urllib.parse.parse_qs(parsed.query)
        return cls(
            host=parsed.hostname,
            user=urllib.parse.unquote(parsed.username or "root"),
            password=urllib.parse.unquote(parsed.password or ""),
            datastore=q.get("datastore", ["datastore1"])[0],
            verify_ssl=q.get("verify", ["0"])[0] == "1",
            port=parsed.port or 443,
        )


class EsxiClient:
    """A live ESXi connection: pyVmomi ``ServiceInstance`` + resolved MoRefs.

    The driver builds one of these in ``connect()``; unit tests inject a
    duck-typed fake (exposing ``content``, ``host``, ``datastore``,
    ``resource_pool``, ``wait_for_task``, ``folder_put``/``folder_get`` …) so no
    real network is touched and pyvmomi need not be installed for the unit run.
    """

    def __init__(self, conn: EsxiConn) -> None:
        self._conn = conn
        self._si: Any | None = None
        self._content: Any | None = None
        self._vim: Any | None = None
        # Resolved once at connect() from the standalone host's inventory.
        self._host: Any | None = None
        self._compute: Any | None = None
        self._resource_pool: Any | None = None
        self._datacenter: Any | None = None
        self._datastore: Any | None = None
        # Serializes each discrete guest-ops SOAP call across the I/O phases'
        # worker threads (ADR-0023): pyVmomi's stub is not guaranteed thread-safe
        # under a session-ticket refresh. Acquired in _guest.py around the
        # StartProgram/ListProcesses/InitiateFileTransfer/DeleteFile calls (the
        # siblings honor the same contract); held per-op and released before the
        # poll sleep and the byte transfers, so concurrent guests still overlap.
        # Host network-system reconfigure (AddVirtualSwitch/AddPortGroup) is a
        # separate concern guarded by the driver's _state_lock, not this lock.
        self.call_lock = threading.RLock()

    @property
    def conn(self) -> EsxiConn:
        return self._conn

    @property
    def content(self) -> Any:
        if self._content is None:
            raise DriverError("EsxiClient used before connect()")
        return self._content

    @property
    def vim(self) -> Any:
        if self._vim is None:
            raise DriverError("EsxiClient used before connect()")
        return self._vim

    @property
    def host(self) -> Any:
        if self._host is None:
            raise DriverError("EsxiClient.host read before connect() resolved it")
        return self._host

    @property
    def compute_resource(self) -> Any:
        if self._compute is None:
            raise DriverError("EsxiClient.compute_resource read before connect()")
        return self._compute

    @property
    def resource_pool(self) -> Any:
        if self._resource_pool is None:
            raise DriverError("EsxiClient.resource_pool read before connect()")
        return self._resource_pool

    @property
    def datacenter(self) -> Any:
        if self._datacenter is None:
            raise DriverError("EsxiClient.datacenter read before connect()")
        return self._datacenter

    @property
    def datacenter_name(self) -> str:
        return str(self.datacenter.name)

    @property
    def datastore(self) -> Any:
        if self._datastore is None:
            raise DriverError("EsxiClient.datastore read before connect()")
        return self._datastore

    @property
    def datastore_name(self) -> str:
        return self._conn.datastore

    @property
    def network_system(self) -> Any:
        """The host's ``HostNetworkSystem`` — vSwitch/portgroup reconfigure."""
        return self.host.configManager.networkSystem

    def connect(self) -> None:
        vim_connect, vim = _import_pyvmomi()
        self._vim = vim
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._conn.verify_ssl:
            ctx = ssl.create_default_context()
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            self._si = vim_connect.SmartConnect(
                host=self._conn.host,
                user=self._conn.user,
                pwd=self._conn.password,
                port=self._conn.port,
                sslContext=ctx,
            )
        except Exception as e:
            raise DriverError(
                f"ESXi connect to {self._conn.host}:{self._conn.port} as "
                f"{self._conn.user!r} failed: {e}"
            ) from e
        try:
            self._content = self._si.RetrieveContent()
            self._resolve_inventory()
        except Exception:
            # SmartConnect already opened an authenticated server-side session. If
            # RetrieveContent / inventory resolution then fails (vCenter target,
            # host count, or a datastore not yet mounted during a nested-ESXi
            # boot), close it before propagating — otherwise wait_esxi_ready's
            # retry loop leaks one ESXi session per poll until the host's session
            # limit is hit (ESXI-33).
            self.close()
            raise
        _log.info(
            "connected to ESXi %s (%s) datastore %s",
            self._conn.host,
            self._content.about.fullName,
            self._conn.datastore,
        )

    def _resolve_inventory(self) -> None:
        """Resolve the standalone host's singleton managed objects.

        Fails loud if the endpoint is vCenter (multiple hosts) — the driver is
        standalone-only (ADR-0025); a vCenter target needs the future DVS-aware
        driver, not a silent mis-bind.
        """
        content = self.content
        about = content.about
        if about.apiType != "HostAgent":
            raise DriverError(
                f"ESXi driver targets a STANDALONE host (apiType 'HostAgent'); "
                f"{self._conn.host!r} reports apiType {about.apiType!r} "
                "(vCenter is out of scope — use a DVS-aware driver)"
            )
        self._datacenter = content.rootFolder.childEntity[0]
        hosts = list(self._view(self.vim.HostSystem))
        if len(hosts) != 1:
            raise DriverError(
                f"standalone ESXi expected exactly one host; found {len(hosts)} on "
                f"{self._conn.host!r}"
            )
        self._host = hosts[0]
        self._compute = self._host.parent
        self._resource_pool = self._compute.resourcePool
        self._datastore = self._find_datastore(self._conn.datastore)

    def _find_datastore(self, name: str) -> Any:
        for ds in self.host.datastore:
            if ds.name == name:
                return ds
        have = sorted(ds.name for ds in self.host.datastore)
        raise DriverError(
            f"datastore {name!r} not found on host {self._conn.host!r} (have: {have})"
        )

    def _view(self, kind: Any) -> Iterator[Any]:
        """Iterate every managed object of ``kind`` under the root folder."""
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [kind], True)
        try:
            yield from list(view.view)
        finally:
            view.Destroy()

    def close(self) -> None:
        if self._si is not None:
            vim_connect, _vim = _import_pyvmomi()
            try:
                vim_connect.Disconnect(self._si)
            except Exception as e:  # pragma: no cover - best-effort teardown
                _log.warning("ESXi disconnect failed: %s", e)
        self._si = None
        self._content = None

    def find_vm(self, name: str) -> Any | None:
        """The VirtualMachine whose inventory ``name`` is ``name``, or ``None``.

        ESXi addresses a VM by an opaque MoRef; the orchestrator only knows the
        deterministic backend name the driver stamped into ``config.name``
        (ADR-0008 §6), so resolution scans the host's VM list for it.
        """
        for vm in self.host.vm:
            if vm.name == name:
                return vm
        return None

    def require_vm(self, name: str) -> Any:
        vm = self.find_vm(name)
        if vm is None:
            raise DriverError(
                f"no ESXi VM named {name!r} on host {self._conn.host!r} "
                "(stamped-name resolution found none)"
            )
        return vm

    def wait_for_task(self, task: Any, *, timeout: float = _TASK_TIMEOUT_S) -> Any:
        """Block until a pyVmomi Task completes; return its result or raise.

        Polls ``task.info.state`` (``queued``/``running``/``success``/``error``)
        rather than the property-collector wait helper so the same poll cadence
        works against the unit fakes. Raises :class:`DriverError` on task error
        or timeout.
        """
        import time

        vim = self.vim
        deadline = time.monotonic() + timeout
        while True:
            info = task.info
            state = info.state
            if state == vim.TaskInfo.State.success:
                return info.result
            if state == vim.TaskInfo.State.error:
                fault = getattr(info, "error", None)
                msg = getattr(fault, "msg", None) or str(fault)
                # ESXi's top-level fault msg is often generic ("Module
                # 'DevicePowerOn' power on failed"); the specific reason (e.g. an
                # IDE slave with no master) lives in faultMessage — surface it.
                details = [
                    m.message
                    for m in (getattr(fault, "faultMessage", None) or [])
                    if getattr(m, "message", None) and m.message != msg
                ]
                if details:
                    msg = f"{msg} ({'; '.join(details)})"
                raise DriverError(f"ESXi task failed: {msg}")
            if time.monotonic() > deadline:
                raise DriverError(f"ESXi task did not finish within {timeout:.0f}s (state={state})")
            time.sleep(_TASK_POLL_S)

    def _folder_url(self, ds_path: str) -> str:
        """HTTPS URL of a datastore file under the ``/folder`` file service.

        ``ds_path`` is the datastore-relative path (``folder/name.vmdk``), not a
        ``[datastore] …`` bracket reference.
        """
        query = urllib.parse.urlencode(
            {"dcPath": self.datacenter_name, "dsName": self._conn.datastore}
        )
        path = urllib.parse.quote(ds_path.lstrip("/"))
        return f"https://{self._conn.host}:{self._conn.port}/folder/{path}?{query}"

    def _auth(self) -> Any:
        requests = _import_requests()
        return requests.auth.HTTPBasicAuth(self._conn.user, self._conn.password)

    def folder_put(self, source_path: Path, ds_path: str) -> None:
        """Upload a local file to a datastore path over the /folder PUT endpoint.

        ``requests`` streams the open file handle directly (preserving
        Content-Length so ESXi doesn't buffer the whole image); a chunked
        generator would force chunked-transfer-encoding, so per-chunk progress
        isn't available here — report span only.
        """
        requests = _import_requests()
        url = self._folder_url(ds_path)
        total = source_path.stat().st_size
        reporter = ProgressReporter(total, f"upload {Path(ds_path).name}", log=_log)
        try:
            with source_path.open("rb") as fh:
                resp = requests.put(
                    url,
                    data=fh,
                    auth=self._auth(),
                    verify=self._conn.verify_ssl,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_READ_TIMEOUT_S),
                )
            resp.raise_for_status()
        except Exception as e:
            raise DriverError(
                f"datastore upload of {Path(ds_path).name} "
                f"({total / (1024 * 1024):.0f} MiB) to {self._conn.host} failed: {e}"
            ) from e
        finally:
            reporter.update(total)
            reporter.finish()

    def folder_get(self, ds_path: str, dest_path: Path) -> None:
        """Download a datastore file to a local path over the /folder GET endpoint."""
        requests = _import_requests()
        url = self._folder_url(ds_path)
        try:
            with requests.get(
                url,
                auth=self._auth(),
                verify=self._conn.verify_ssl,
                stream=True,
                timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_READ_TIMEOUT_S),
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                # `with` so a mid-stream write/iter failure still releases the rich
                # Live (cursor, live region) instead of corrupting the terminal on
                # the way out to the except below (CORE-94).
                with ProgressReporter(total, f"download {dest_path.name}", log=_log) as reporter:
                    done = 0
                    with dest_path.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=_CHUNK):
                            fh.write(chunk)
                            done += len(chunk)
                            reporter.update(done)
        except Exception as e:
            raise DriverError(
                f"datastore download of {Path(ds_path).name} from {self._conn.host} failed: {e}"
            ) from e

    def _fix_transfer_host(self, url: str) -> str:
        """Rewrite a guest-ops transfer URL's host to the connection host.

        ``Initiate*FileTransfer*`` returns a URL whose host is the ESXi host's
        own advertised name — often the wildcard ``*`` ("the host you reached me
        on") or an internal name the orchestrator can't resolve. Pin it to the
        host we actually connected to; the one-time ticket in the query is what
        authorizes the transfer.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if host in (None, "*") or host != self._conn.host:
            netloc = f"{self._conn.host}:{parsed.port or self._conn.port}"
            parsed = parsed._replace(netloc=netloc)
        return urllib.parse.urlunparse(parsed)

    def guest_file_get(self, url: str) -> bytes:
        """GET a guest file over a one-time transfer URL (no auth; ticket in URL)."""
        requests = _import_requests()
        fixed = self._fix_transfer_host(url)
        resp = requests.get(
            fixed,
            verify=self._conn.verify_ssl,
            timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_READ_TIMEOUT_S),
        )
        resp.raise_for_status()
        return bytes(resp.content)

    def guest_file_put(self, url: str, data: bytes) -> None:
        """PUT bytes to a guest file over a one-time transfer URL."""
        requests = _import_requests()
        fixed = self._fix_transfer_host(url)
        resp = requests.put(
            fixed,
            data=data,
            verify=self._conn.verify_ssl,
            timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_READ_TIMEOUT_S),
        )
        resp.raise_for_status()

    def folder_exists(self, ds_path: str) -> bool:
        """Whether a datastore file exists, via a /folder HEAD (200 vs 404).

        Read-only (works regardless of the SOAP write license), so it is the
        idempotency probe for ``upload_to_pool`` / ``write_to_pool``: a present
        file is established positively, not by swallowing a later write's error.
        """
        requests = _import_requests()
        resp = requests.head(
            self._folder_url(ds_path),
            auth=self._auth(),
            verify=self._conn.verify_ssl,
            timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_CONNECT_TIMEOUT_S),
        )
        if resp.status_code == 200:
            return True
        if resp.status_code in (404, 400):
            return False
        resp.raise_for_status()
        return False

    def folder_read_from(self, ds_path: str, offset: int) -> bytes:
        """Read new bytes of a datastore file from ``offset`` (incremental tail).

        A Range GET against the /folder file service: ``206`` returns just the new
        tail; a ``200`` (server ignored Range) is sliced; ``416`` (range past EOF)
        and ``404`` (file not yet created — the build VM hasn't opened its serial
        port) both mean "nothing new", returned as ``b""``. Backs the serial
        build-result sink's incremental polling (ESXI-8).
        """
        requests = _import_requests()
        resp = requests.get(
            self._folder_url(ds_path),
            auth=self._auth(),
            verify=self._conn.verify_ssl,
            headers={"Range": f"bytes={offset}-"},
            timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_CONNECT_TIMEOUT_S),
        )
        if resp.status_code == 206:
            return bytes(resp.content)
        if resp.status_code == 200:
            return bytes(resp.content[offset:])
        if resp.status_code in (404, 416):
            return b""
        resp.raise_for_status()
        return b""

    def folder_delete(self, ds_path: str) -> bool:
        """Delete a datastore file over /folder DELETE. Returns whether it existed."""
        requests = _import_requests()
        url = self._folder_url(ds_path)
        resp = requests.delete(
            url,
            auth=self._auth(),
            verify=self._conn.verify_ssl,
            timeout=(_HTTP_CONNECT_TIMEOUT_S, _HTTP_READ_TIMEOUT_S),
        )
        if resp.status_code in (404, 400):
            return False
        resp.raise_for_status()
        return True
