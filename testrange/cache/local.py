"""LocalCache — content-addressed file store under ``$XDG_CACHE_HOME/testrange/isos/``.

Layout:
    <root>/isos/<sha256>.bin   (opaque content)
    <root>/isos/<sha256>.json  (sidecar metadata)

All writes use ``.partial`` + ``os.replace`` so a crash mid-write never
leaves a plausible-but-corrupt file at the canonical path. This is crash
safety for the single owning process (TestRange is single-instance — see
ADR-0018), not a guard against concurrent writers.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testrange._log import get_logger
from testrange.cache._names import validate_name
from testrange.exceptions import CacheError, CacheMissError

_log = get_logger(__name__)


@dataclass(frozen=True)
class CacheEntryInfo:
    """Metadata for one cache entry, deserialized from its ``<sha>.json``.

    ``path`` is the local ``.bin`` path on the host when the entry is
    materialized locally; ``None`` for entries resolved against the HTTP
    tier without ``fetch``.
    """

    sha256: str
    size: int
    names: tuple[str, ...]
    origin: str | None
    added_at: str
    description: str | None
    path: Path | None

    @property
    def short_sha(self) -> str:
        return self.sha256[:16]


def default_root() -> Path:
    """Resolve the default cache root from ``$XDG_CACHE_HOME`` / ``~/.cache``."""
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "testrange"


class LocalCache:
    """File-backed content-addressed cache.

    Single-instance by contract (ADR-0018): methods are not thread- or
    process-safe. They use atomic-rename writes purely for crash safety, so a
    SIGKILL during a write leaves the canonical path either fully-old or
    fully-new — not to serialize concurrent writers (there are none).
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_root()).resolve()
        self.isos = self.root / "isos"
        self.isos.mkdir(parents=True, exist_ok=True)
        # Serializes the sidecar read-modify-write inside the mutating methods so
        # concurrent in-process adds of the *same* content (parallel build-disk
        # captures, ADR-0020) merge their name aliases instead of clobbering, and
        # never race a fixed-name temp. The slow byte copy/hash stays outside it,
        # so distinct-content adds still parallelize fully. Not a cross-process
        # guard (ADR-0018) — single-instance still holds.
        self._write_lock = threading.Lock()

    @property
    def staging(self) -> Path:
        """Scratch dir on the cache filesystem for in-flight downloads/captures.

        Callers that stream large content (a captured build disk) need a temp
        file on the *same* filesystem as ``isos/`` — the system tempdir is
        often a small tmpfs ``/tmp`` that ENOSPCs on a multi-GiB disk (CORE-4),
        and a same-filesystem temp also keeps the subsequent ingest a cheap
        intra-fs copy. Created on first access.
        """
        d = self.root / "staging"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> CacheEntryInfo:
        """Add a local file or URL to the cache.

        Returns the resulting :class:`CacheEntryInfo`. If an entry with
        the same content sha already exists, the new name (if any) is
        added to its alias list and the existing entry is returned.
        """
        if name is not None:
            validate_name(name)
        src = str(source)
        if src.startswith(("http://", "https://")):
            tmp, origin = self._download_url(src)
        else:
            p = Path(src).expanduser().resolve()
            if not p.is_file():
                raise CacheError(f"add: not a file: {p}")
            tmp = p
            origin = str(p)

        sha = _sha256_of(tmp)
        bin_path = self.isos / f"{sha}.bin"
        sidecar = self.isos / f"{sha}.json"

        # Materialize the content-addressed ``.bin`` first, *unlocked*: the copy
        # is the slow part and is idempotent (same sha → same bytes), so parallel
        # adds of distinct content overlap fully and two adds of the same new sha
        # just both land identical bytes via unique temps.
        if not bin_path.exists() and tmp != bin_path:
            _atomic_copy(tmp, bin_path)
        if src.startswith(("http://", "https://")):
            tmp.unlink(missing_ok=True)  # the download tmp is scratch once .bin lands

        # Serialize only the sidecar read-modify-write: re-read under the lock so
        # a concurrent same-sha add's alias is merged rather than clobbered.
        with self._write_lock:
            if sidecar.exists():
                info = self._read_sidecar(sidecar)
                if origin != info.origin and tmp != bin_path:
                    _log.info("entry already in cache: %s (origin differs)", sha[:16])
                if name and name not in info.names:
                    info = self._append_alias(sidecar, info, name, description=description)
                return info
            names: tuple[str, ...] = (name,) if name else ()
            info = CacheEntryInfo(
                sha256=sha,
                size=bin_path.stat().st_size,
                names=names,
                origin=origin,
                added_at=_now_utc_iso(),
                description=description,
                path=bin_path,
            )
            self._write_sidecar(sidecar, info)
        _log.info("added cache entry %s (%d bytes)", sha[:16], info.size)
        return info

    def _download_url(self, url: str) -> tuple[Path, str]:
        """Stream ``url`` into a unique temp file under ``isos/``. Returns (tmp_path, url).

        The temp name comes from :func:`tempfile.mkstemp`, not a fixed
        ``.download.partial`` — two concurrent fetches (a parallel build-disk
        capture, the I/O phases on a thread pool) must not interleave into one
        file and promote a disk whose bytes don't hash to its name (CACHE-4).
        """
        _log.info("fetching %s", url)
        fd, tmp_name = tempfile.mkstemp(dir=self.isos, suffix=".download.partial")
        tmp = Path(tmp_name)
        # ``mkstemp`` already owns ``fd``; wrap it first so it's always closed,
        # then stream the body in. On any failure the partial is removed rather
        # than left as cache litter.
        # S310: the scheme is pre-validated by add() (http/https only) before we
        # ever reach here, so file:/custom schemes cannot slip through.
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url) as resp:  # noqa: S310
                shutil.copyfileobj(resp, out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp, url

    def resolve(self, identifier: str) -> CacheEntryInfo:
        """Resolve a sha or pretty-name to a :class:`CacheEntryInfo`.

        Raises :class:`CacheMissError` if not found.
        """
        from testrange.cache.entry import CacheEntry

        if CacheEntry(identifier).looks_like_sha:
            # A short prefix (e.g. 16 chars) accepts any sha that starts with it,
            # but an ambiguous prefix matching more than one entry must fail loud
            # rather than silently return the first sha-sorted match.
            matches = [info for info in self.iter_entries() if info.sha256.startswith(identifier)]
            if len(matches) > 1:
                shas = ", ".join(m.sha256[:16] for m in matches)
                raise CacheError(
                    f"sha-prefix {identifier!r} is ambiguous in local cache "
                    f"(matches {shas}); use a longer prefix"
                )
            if matches:
                return matches[0]
            raise CacheMissError(f"no entry with sha-prefix {identifier!r} in local cache")

        for info in self.iter_entries():
            if identifier in info.names:
                return info
        raise CacheMissError(
            f"no entry with name {identifier!r} in local cache; "
            f"add via `testrange cache add <path-or-url> --name {identifier}`"
        )

    def iter_entries(self) -> Iterator[CacheEntryInfo]:
        """Yield every entry in deterministic (sha-sorted) order."""
        for p in sorted(self.isos.glob("*.json")):
            try:
                yield self._read_sidecar(p)
            except (json.JSONDecodeError, OSError, KeyError) as e:
                _log.warning("skipping bad sidecar %s: %s", p.name, e)

    def list_entries(self) -> list[CacheEntryInfo]:
        return list(self.iter_entries())

    def delete(self, identifier: str) -> CacheEntryInfo:
        """Remove the entry's .bin and .json. Returns the removed info."""
        info = self.resolve(identifier)
        sidecar = self.isos / f"{info.sha256}.json"
        bin_path = self.isos / f"{info.sha256}.bin"
        bin_path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        _log.info("deleted cache entry %s", info.short_sha)
        return info

    def purge(self) -> list[CacheEntryInfo]:
        """Delete every entry (``.bin`` + ``.json``). Returns the removed infos.

        Local-only: there is no shared-tier coordination here (see
        :meth:`CacheManager.purge`). A snapshot of the entries is taken before
        deleting so iteration is not invalidated mid-walk.
        """
        removed = self.list_entries()
        for info in removed:
            (self.isos / f"{info.sha256}.bin").unlink(missing_ok=True)
            (self.isos / f"{info.sha256}.json").unlink(missing_ok=True)
        if removed:
            _log.info("purged %d cache entr%s", len(removed), "y" if len(removed) == 1 else "ies")
        return removed

    def add_name(
        self,
        identifier: str,
        new_name: str,
    ) -> CacheEntryInfo:
        """Add a new pretty-name alias to an existing entry."""
        validate_name(new_name)
        info = self.resolve(identifier)
        if new_name in info.names:
            return info
        clash = self._find_by_name(new_name)
        if clash is not None and clash.sha256 != info.sha256:
            raise CacheError(
                f"name {new_name!r} already belongs to a different entry "
                f"({clash.short_sha}); use `cache forget-name {new_name}` first"
            )
        sidecar = self.isos / f"{info.sha256}.json"
        return self._append_alias(sidecar, info, new_name)

    def forget_name(self, name: str) -> CacheEntryInfo:
        """Remove a pretty-name alias from whichever entry has it."""
        info = self._find_by_name(name)
        if info is None:
            raise CacheMissError(f"no entry has name {name!r}")
        new_names = tuple(n for n in info.names if n != name)
        new_info = CacheEntryInfo(
            sha256=info.sha256,
            size=info.size,
            names=new_names,
            origin=info.origin,
            added_at=info.added_at,
            description=info.description,
            path=info.path,
        )
        sidecar = self.isos / f"{info.sha256}.json"
        self._write_sidecar(sidecar, new_info)
        return new_info

    def _find_by_name(self, name: str) -> CacheEntryInfo | None:
        for info in self.iter_entries():
            if name in info.names:
                return info
        return None

    def _read_sidecar(self, path: Path) -> CacheEntryInfo:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        sha = data["sha256"]
        return CacheEntryInfo(
            sha256=sha,
            size=int(data.get("size", 0)),
            names=tuple(data.get("names") or ()),
            origin=data.get("origin"),
            added_at=data.get("added_at", ""),
            description=data.get("description"),
            path=self.isos / f"{sha}.bin",
        )

    def _write_sidecar(self, path: Path, info: CacheEntryInfo) -> None:
        body = {
            "sha256": info.sha256,
            "size": info.size,
            "names": list(info.names),
            "origin": info.origin,
            "added_at": info.added_at,
            "description": info.description,
        }
        text = json.dumps(body, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, text)

    def _append_alias(
        self,
        sidecar: Path,
        info: CacheEntryInfo,
        new_name: str,
        *,
        description: str | None = None,
    ) -> CacheEntryInfo:
        new_info = CacheEntryInfo(
            sha256=info.sha256,
            size=info.size,
            names=(*info.names, new_name),
            origin=info.origin,
            added_at=info.added_at,
            description=description if description is not None else info.description,
            path=info.path,
        )
        self._write_sidecar(sidecar, new_info)
        return new_info


def _sha256_of(path: Path) -> str:
    """Stream-hash a file with SHA-256. Returns lowercase hex digest."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` via a unique temp + ``os.replace``.

    The temp name comes from :func:`tempfile.mkstemp` (not a fixed
    ``<dst>.partial``) so two concurrent copies of the same content sha don't
    collide on one staging path (ADR-0020).
    """
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, suffix=".partial")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as w, src.open("rb") as r:
            shutil.copyfileobj(r, w)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a unique temp + ``os.replace``.

    Unique temp (not ``<path>.partial``) so concurrent writers of the same
    content-addressed sidecar don't race a fixed staging name (ADR-0020).
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _now_utc_iso() -> str:
    """RFC-3339 UTC timestamp, e.g. ``2026-05-11T00:30:00Z``."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["CacheEntryInfo", "LocalCache", "default_root"]
