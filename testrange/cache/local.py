"""LocalCache — content-addressed file store under ``$XDG_CACHE_HOME/testrange/isos/``.

Layout:
    <root>/isos/<sha256>.bin   (opaque content)
    <root>/isos/<sha256>.json  (sidecar metadata)

All writes use ``.partial`` + ``os.replace`` so a torn write never leaves
a plausible-but-corrupt file at the canonical path.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testrange._log import get_logger
from testrange.exceptions import CacheError, CacheMissError

_log = get_logger(__name__)


@dataclass(frozen=True)
class CacheEntryInfo:
    """Metadata for one cache entry, deserialized from its ``<sha>.json``."""

    sha256: str
    size: int
    names: tuple[str, ...]
    origin: str | None
    added_at: str
    description: str | None
    path: Path  # The .bin path on the host

    @property
    def short_sha(self) -> str:
        return self.sha256[:16]


def default_root() -> Path:
    """Resolve the default cache root from ``$XDG_CACHE_HOME`` / ``~/.cache``."""
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "testrange"


class LocalCache:
    """File-backed content-addressed cache.

    Methods are not thread-safe but use atomic-rename writes so a SIGKILL
    during a write leaves the canonical path either fully-old or fully-new.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_root()).resolve()
        self.isos = self.root / "isos"
        self.isos.mkdir(parents=True, exist_ok=True)

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

        if bin_path.exists() and sidecar.exists():
            info = self._read_sidecar(sidecar)
            if origin != info.origin and tmp != bin_path:
                _log.info("entry already in cache: %s (origin differs from existing)", sha[:16])
            if name and name not in info.names:
                info = self._append_alias(sidecar, info, name, description=description)
            if src.startswith(("http://", "https://")) and tmp.parent != self.isos:
                tmp.unlink(missing_ok=True)
            return info

        # First write the .bin atomically
        if tmp != bin_path:
            _atomic_copy(tmp, bin_path)
            if src.startswith(("http://", "https://")):
                tmp.unlink(missing_ok=True)

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
        """Stream ``url`` into a ``.partial`` file under ``isos/``. Returns (tmp_path, url)."""
        tmp = self.isos / ".download.partial"
        _log.info("fetching %s", url)
        # urlopen verifies TLS via the system CA store by default in Python 3.6+.
        with urllib.request.urlopen(url) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
        return tmp, url

    def resolve(self, identifier: str) -> CacheEntryInfo:
        """Resolve a sha or pretty-name to a :class:`CacheEntryInfo`.

        Raises :class:`CacheMissError` if not found.
        """
        from testrange.cache.entry import CacheEntry

        if CacheEntry(identifier).looks_like_sha:
            # If a short prefix (16 chars) is given, accept any sha that starts with it.
            for info in self.iter_entries():
                if info.sha256.startswith(identifier):
                    return info
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

    def add_name(
        self,
        identifier: str,
        new_name: str,
    ) -> CacheEntryInfo:
        """Add a new pretty-name alias to an existing entry."""
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
        tmp = path.with_suffix(path.suffix + ".partial")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

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
    """Copy ``src`` to ``dst`` via ``<dst>.partial`` + ``os.replace``."""
    tmp = dst.with_suffix(dst.suffix + ".partial")
    with src.open("rb") as r, tmp.open("wb") as w:
        shutil.copyfileobj(r, w)
    os.replace(tmp, dst)


def _now_utc_iso() -> str:
    """RFC-3339 UTC timestamp, e.g. ``2026-05-11T00:30:00Z``."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["CacheEntryInfo", "LocalCache", "default_root"]
