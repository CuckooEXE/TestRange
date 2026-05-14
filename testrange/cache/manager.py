"""CacheManager — brokers between the local cache and an optional HTTP tier.

Local is the source of truth. HTTP is a best-effort mirror: failures log a
WARNING and don't abort the local operation.

Read path
    resolve(ref) tries local first; on miss, tries http (if configured);
    on http hit, fetches into local + writes the sidecar last so an
    interrupted fetch never leaves a half-committed entry.

Write path
    add / delete / add_name / forget_name run on local first, then mirror
    to http best-effort.

Listing
    list_entries is local-only — the HTTP server has no listing protocol.
"""

from __future__ import annotations

from pathlib import Path

from testrange._log import get_logger
from testrange.cache.entry import CacheEntry
from testrange.cache.http import HttpCache
from testrange.cache.local import CacheEntryInfo, LocalCache
from testrange.exceptions import CacheError, CacheMissError

_log = get_logger(__name__)


class CacheManager:
    """Broker between :class:`LocalCache` (always) and :class:`HttpCache` (optional)."""

    def __init__(
        self,
        local: LocalCache | None = None,
        http: HttpCache | None = None,
    ) -> None:
        self.local = local or LocalCache()
        self.http = http

    @property
    def root(self) -> Path:
        return self.local.root

    # ---- resolution ------------------------------------------------------

    def resolve(self, ref: str | CacheEntry, *, fetch: bool = True) -> CacheEntryInfo:
        """Resolve a CacheEntry / identifier across both tiers.

        Local is checked first. On miss, the HTTP tier (if configured) is
        queried. With ``fetch=True`` (default), an HTTP hit triggers a
        download into local and the local-flavored info is returned; with
        ``fetch=False`` the HTTP info is returned directly (``path=None``)
        — this is what passive callers (``testrange describe``, preflight
        existence checks) should use.
        """
        identifier = ref.identifier if isinstance(ref, CacheEntry) else ref
        try:
            return self.local.resolve(identifier)
        except CacheMissError:
            if self.http is None:
                raise

        info = self.http.resolve(identifier)
        if not fetch:
            return info
        return self._fetch_and_materialize(info)

    def resolve_path(self, ref: str | CacheEntry) -> Path:
        """Convenience: ``resolve(ref, fetch=True).path``."""
        info = self.resolve(ref, fetch=True)
        assert info.path is not None, "resolve(fetch=True) must produce a local path"
        return info.path

    def _fetch_and_materialize(self, info: CacheEntryInfo) -> CacheEntryInfo:
        """Stream HTTP-tier ``info`` into the local cache and return the local entry."""
        assert self.http is not None
        bin_path = self.local.isos / f"{info.sha256}.bin"
        sidecar = self.local.isos / f"{info.sha256}.json"
        # bin first, sidecar LAST — a crash mid-fetch leaves an orphan
        # ``<sha>.bin`` that resolve() ignores (it scans sidecars).
        self.http.fetch(info.sha256, bin_path)
        local_info = CacheEntryInfo(
            sha256=info.sha256,
            size=info.size,
            names=info.names,
            origin=info.origin,
            added_at=info.added_at,
            description=info.description,
            path=bin_path,
        )
        self.local._write_sidecar(sidecar, local_info)
        _log.info("fetched %s from http cache (%d bytes)", info.sha256[:16], info.size)
        return local_info

    # ---- mutation --------------------------------------------------------

    def add(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> CacheEntryInfo:
        """Add to local + mirror to http (best-effort)."""
        info = self.local.add(source, name=name, description=description)
        if self.http is not None:
            assert info.path is not None
            try:
                self.http.push(info, info.path)
            except Exception as e:
                _log.warning("http cache: push %s failed: %s", info.short_sha, e)
        return info

    def delete(self, identifier: str) -> CacheEntryInfo:
        """Delete from local + mirror to http (best-effort)."""
        info = self.local.delete(identifier)
        if self.http is not None:
            try:
                self.http.delete(info)
            except Exception as e:
                _log.warning("http cache: delete %s failed: %s", info.short_sha, e)
        return info

    def add_name(self, identifier: str, new_name: str) -> CacheEntryInfo:
        """Alias on local + mirror to http (best-effort)."""
        info = self.local.add_name(identifier, new_name)
        if self.http is not None:
            try:
                self.http.add_name(info.sha256, new_name)
            except Exception as e:
                _log.warning(
                    "http cache: add_name %s→%s failed: %s",
                    info.short_sha,
                    new_name,
                    e,
                )
        return info

    def forget_name(self, name: str) -> CacheEntryInfo:
        """Drop alias from local + mirror to http (best-effort)."""
        info = self.local.forget_name(name)
        if self.http is not None:
            try:
                self.http.forget_name(name)
            except Exception as e:
                _log.warning("http cache: forget_name %s failed: %s", name, e)
        return info

    # ---- manual reconciliation ------------------------------------------

    def push(self, identifier: str) -> CacheEntryInfo:
        """Copy a local entry to the HTTP tier. Raises if no HTTP configured."""
        if self.http is None:
            raise CacheError("cache push: no HTTP cache configured")
        info = self.local.resolve(identifier)
        assert info.path is not None
        self.http.push(info, info.path)
        return info

    def pull(self, identifier: str) -> CacheEntryInfo:
        """Fetch from the HTTP tier into local. Raises if no HTTP configured."""
        if self.http is None:
            raise CacheError("cache pull: no HTTP cache configured")
        info = self.http.resolve(identifier)
        return self._fetch_and_materialize(info)
