"""HttpCache — second-tier shared cache speaking GET/PUT/DELETE over HTTPS.

A dumb path-served cache (see ``cache-server/``). The path schema mirrors
the local layout:

    GET / PUT / DELETE  /isos/<sha>.bin    — content
    GET / PUT / DELETE  /isos/<sha>.json   — sidecar metadata
    GET / PUT / DELETE  /names/<name>      — text file whose body is the
                                             sha this name aliases

TLS is never verified. This cache is meant for a private LAN behind some
other gate (VPN, mTLS-terminating reverse proxy, ...); the server itself
runs no auth and no rate-limit, and self-signed certs are expected.
``urllib3.InsecureRequestWarning`` is silenced on import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testrange._log import get_logger
from testrange.cache._names import validate_name
from testrange.cache.local import CacheEntryInfo
from testrange.exceptions import CacheError, CacheMissError

_log = get_logger(__name__)


def _import_requests() -> Any:
    """Lazy import. Raises CacheError with an install hint if requests is missing."""
    try:
        import requests
        import urllib3
    except ImportError as e:
        raise CacheError(
            "requests is not installed; install with `pip install -e .[http]`"
        ) from e
    # The cache server uses self-signed certs by design; suppress the per-
    # request warning that requests emits when verify=False. The fact that
    # the cache is unverified is documented; no need to spam the log.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return requests


# Default timeout for HTTP calls. Connect+read in one knob since requests
# bundles them under a single (connect, read) tuple — we set the read side
# generously because qcow2 transfers can be slow.
_DEFAULT_TIMEOUT = (10.0, 600.0)


class HttpCache:
    """Dumb HTTP/HTTPS cache tier.

    The server is expected to be a plain WebDAV-style file store (see
    ``cache-server/``). All requests use ``verify=False`` — this is
    intentional and not a configurable knob.
    """

    def __init__(self, base_url: str) -> None:
        if not isinstance(base_url, str) or not base_url:
            raise CacheError("HttpCache.base_url must be a non-empty string")
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _get(self, path: str, *, stream: bool = False) -> Any:
        requests = _import_requests()
        return requests.get(self._url(path), verify=False, stream=stream, timeout=_DEFAULT_TIMEOUT)

    def _put(self, path: str, data: Any, *, headers: dict[str, str] | None = None) -> Any:
        requests = _import_requests()
        return requests.put(
            self._url(path),
            data=data,
            headers=headers or {},
            verify=False,
            timeout=_DEFAULT_TIMEOUT,
        )

    def _delete(self, path: str) -> Any:
        requests = _import_requests()
        return requests.delete(self._url(path), verify=False, timeout=_DEFAULT_TIMEOUT)

    # ---- public surface --------------------------------------------------

    def resolve(self, identifier: str) -> CacheEntryInfo:
        """Look up by full sha or by name. Raises ``CacheMissError`` on 404.

        Returned :class:`CacheEntryInfo` carries ``path=None`` — HTTP-tier
        entries don't have a local path until they're fetched.
        """
        from testrange.cache.entry import CacheEntry

        if CacheEntry(identifier).looks_like_sha and len(identifier) == 64:
            sha = identifier
        else:
            # Try as a name first (it's the common runtime case).
            sha = self._resolve_name(identifier)
        return self._read_sidecar(sha)

    def _resolve_name(self, name: str) -> str:
        resp = self._get(f"/names/{name}")
        if resp.status_code == 404:
            raise CacheMissError(f"no entry with name {name!r} in http cache")
        if not resp.ok:
            raise CacheError(
                f"http cache: GET /names/{name} → {resp.status_code}"
            )
        sha: str = resp.text.strip()
        if not sha:
            raise CacheError(f"http cache: name {name!r} resolved to empty sha")
        return sha

    def _read_sidecar(self, sha: str) -> CacheEntryInfo:
        resp = self._get(f"/isos/{sha}.json")
        if resp.status_code == 404:
            raise CacheMissError(f"no sidecar for {sha[:16]} in http cache")
        if not resp.ok:
            raise CacheError(
                f"http cache: GET /isos/{sha}.json → {resp.status_code}"
            )
        data = json.loads(resp.text)
        return CacheEntryInfo(
            sha256=data["sha256"],
            size=int(data.get("size", 0)),
            names=tuple(data.get("names") or ()),
            origin=data.get("origin"),
            added_at=data.get("added_at", ""),
            description=data.get("description"),
            path=None,  # http-tier entries have no local path until fetched
        )

    def fetch(self, sha: str, dest_path: Path) -> None:
        """Stream ``/isos/<sha>.bin`` into ``dest_path``."""
        resp = self._get(f"/isos/{sha}.bin", stream=True)
        if resp.status_code == 404:
            raise CacheMissError(f"no entry with sha {sha[:16]} in http cache")
        if not resp.ok:
            raise CacheError(f"http cache: GET /isos/{sha}.bin → {resp.status_code}")
        _log.info("fetching %s ← http cache → %s", sha[:16], dest_path)
        with dest_path.open("wb") as out:
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    out.write(chunk)

    def push(self, info: CacheEntryInfo, bin_path: Path) -> None:
        """Upload bin + sidecar + name aliases. Write order:

        1. ``/isos/<sha>.bin`` (content)
        2. ``/isos/<sha>.json`` (sidecar — entry becomes visible)
        3. ``/names/<n>`` for every name (aliases)

        A crash between (1) and (2) leaves an orphan bin that the
        sidecar-LAST rule renders invisible; a crash between (2) and
        (3) leaves the entry resolvable by sha but not by the new name.
        Re-running ``push`` is idempotent.
        """
        size = bin_path.stat().st_size
        _log.info("pushing %s (%d bytes) → http cache", info.sha256[:16], size)
        with bin_path.open("rb") as f:
            resp = self._put(
                f"/isos/{info.sha256}.bin",
                data=f,
                headers={"Content-Length": str(size)},
            )
        self._raise_for_put(resp, f"/isos/{info.sha256}.bin")

        sidecar_body = self._sidecar_body(info)
        resp = self._put(f"/isos/{info.sha256}.json", data=sidecar_body.encode("utf-8"))
        self._raise_for_put(resp, f"/isos/{info.sha256}.json")

        for name in info.names:
            self._put_name(name, info.sha256)

    def delete(self, info: CacheEntryInfo) -> None:
        """Remove all server-side artifacts. Inverse order of push:

        1. ``/names/<n>`` for every name (drop aliases first so a stale
           pointer never references a missing sidecar)
        2. ``/isos/<sha>.json`` (entry becomes invisible)
        3. ``/isos/<sha>.bin`` (reclaim space)
        """
        _log.info("deleting %s ← http cache", info.sha256[:16])
        for name in info.names:
            self._delete_name(name)
        self._delete_quiet(f"/isos/{info.sha256}.json")
        self._delete_quiet(f"/isos/{info.sha256}.bin")

    def add_name(self, sha: str, name: str) -> None:
        """Attach a name alias on the server. Caller must keep the sidecar in sync."""
        validate_name(name)
        # Refresh the sidecar with the new name in the list FIRST, then add
        # the pointer file. Sidecar-first keeps the resolve-by-name path
        # consistent with the entry's own ``names`` array.
        try:
            info = self._read_sidecar(sha)
        except CacheMissError as e:
            raise CacheError(
                f"http cache: cannot add_name {name!r} — sha {sha[:16]} not present"
            ) from e
        if name not in info.names:
            new_info = CacheEntryInfo(
                sha256=info.sha256,
                size=info.size,
                names=(*info.names, name),
                origin=info.origin,
                added_at=info.added_at,
                description=info.description,
                path=None,
            )
            resp = self._put(
                f"/isos/{sha}.json", data=self._sidecar_body(new_info).encode("utf-8")
            )
            self._raise_for_put(resp, f"/isos/{sha}.json")
        self._put_name(name, sha)

    def forget_name(self, name: str) -> None:
        """Drop a name alias on the server. Caller updates the sidecar."""
        try:
            sha = self._resolve_name(name)
        except CacheMissError:
            _log.info("http cache: name %r already absent", name)
            return
        # Drop the pointer first so resolve-by-name fails immediately, then
        # rewrite the sidecar without the alias.
        self._delete_name(name)
        try:
            info = self._read_sidecar(sha)
        except CacheMissError:
            return
        new_names = tuple(n for n in info.names if n != name)
        new_info = CacheEntryInfo(
            sha256=info.sha256,
            size=info.size,
            names=new_names,
            origin=info.origin,
            added_at=info.added_at,
            description=info.description,
            path=None,
        )
        resp = self._put(
            f"/isos/{sha}.json", data=self._sidecar_body(new_info).encode("utf-8")
        )
        self._raise_for_put(resp, f"/isos/{sha}.json")

    # ---- helpers ----------------------------------------------------------

    def _put_name(self, name: str, sha: str) -> None:
        validate_name(name)
        resp = self._put(f"/names/{name}", data=sha.encode("ascii"))
        self._raise_for_put(resp, f"/names/{name}")

    def _delete_name(self, name: str) -> None:
        self._delete_quiet(f"/names/{name}")

    def _delete_quiet(self, path: str) -> None:
        resp = self._delete(path)
        if resp.status_code in (200, 204, 404):
            return
        raise CacheError(f"http cache: DELETE {path} → {resp.status_code}")

    def _raise_for_put(self, resp: Any, path: str) -> None:
        # nginx DAV PUT returns 201 Created or 204 No Content on success.
        if resp.status_code in (200, 201, 204):
            return
        raise CacheError(f"http cache: PUT {path} → {resp.status_code}")

    def _sidecar_body(self, info: CacheEntryInfo) -> str:
        body = {
            "sha256": info.sha256,
            "size": info.size,
            "names": list(info.names),
            "origin": info.origin,
            "added_at": info.added_at,
            "description": info.description,
        }
        return json.dumps(body, indent=2, sort_keys=True) + "\n"


__all__ = ["HttpCache"]
