"""Thin client for the bundled ``cache/`` HTTP artifact store.

The store is a stock nginx + ``ngx_http_dav_module`` instance: ``GET``
reads a blob, ``PUT`` writes one, ``DELETE`` removes one.  See
``cache/`` at the repo root and :doc:`/usage/http_cache` for the
server-side definition.

Used as an optional second-tier cache by :class:`~testrange.cache.CacheManager`:
the local on-disk cache is always consulted first; the remote is a
fill source on miss and a publish target on store.  All methods are
best-effort — connection errors, timeouts, and unexpected status codes
are logged and surface as ``False`` / ``None`` so a flaky cache cannot
break a test run, only slow it back down to the cold-install path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from testrange._logging import get_logger

if TYPE_CHECKING:
    import requests

_log = get_logger(__name__)

# Connect/read timeouts.  PUT bodies can be multi-GiB disk images,
# so the read timeout is per-chunk, not for the whole transfer.
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 60

# Stream uploads/downloads in this chunk size.  Larger blocks reduce
# Python overhead on the multi-GiB transfers; smaller blocks update
# the read-timeout window more often if a peer goes silent.
_CHUNK_BYTES = 1 << 20  # 1 MiB


class HttpCache:
    """Client for a remote HTTP-based artifact store.

    :param base_url: Root URL of the cache server, e.g.
        ``"https://cache.testrange"``.  Trailing slash optional.
    :param verify: TLS verification mode.  ``True`` (default) requires
        a trusted cert chain; ``False`` accepts self-signed (intended
        for the bundled docker setup); a string is treated as a path to
        a CA bundle.
    """

    def __init__(self, base_url: str, *, verify: bool | str = True) -> None:
        import requests  # noqa: PLC0415  (lazy: only imported when remote cache used)

        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self._session: requests.Session = requests.Session()
        self._session.verify = verify

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def exists(self, path: str) -> bool:
        """Return ``True`` if *path* is present on the remote.

        Uses ``GET`` with a one-byte ``Range`` header rather than
        ``HEAD`` because ``ngx_http_dav_module`` doesn't always
        advertise content-length on HEAD for files just-PUT.

        Network errors or unexpected status codes return ``False`` —
        callers treat that as a miss and continue with the cold path.
        """
        import requests  # noqa: PLC0415

        try:
            resp = self._session.get(
                self._url(path),
                headers={"Range": "bytes=0-0"},
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                stream=True,
            )
            resp.close()
        except requests.RequestException as exc:
            _log.warning("http-cache exists(%s): %s", path, exc)
            return False
        return resp.status_code in (200, 206)

    def get(self, path: str, dest: Path) -> bool:
        """Download *path* to *dest*.  Returns ``True`` on success.

        Streams the body to a ``.partial`` sibling and renames atomically
        on completion so a torn download leaves no plausible-looking
        artifact at the final name.
        """
        import requests  # noqa: PLC0415

        url = self._url(path)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        try:
            with self._session.get(
                url,
                stream=True,
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            ) as resp:
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                tmp.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=_CHUNK_BYTES):
                        fh.write(chunk)
        except requests.RequestException as exc:
            _log.warning("http-cache get(%s): %s", path, exc)
            tmp.unlink(missing_ok=True)
            return False
        tmp.rename(dest)
        return True

    def put(self, path: str, src: Path) -> bool:
        """Upload *src* to *path*.  Returns ``True`` on success.

        Streams the file body so we don't load multi-GiB artifacts
        into memory.  Failures log a warning but don't raise — the
        local cache write that motivated this PUT already succeeded.
        """
        import requests  # noqa: PLC0415

        url = self._url(path)
        try:
            with open(src, "rb") as fh:
                resp = self._session.put(
                    url,
                    data=_chunk_reader(fh),
                    timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                )
            resp.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            _log.warning("http-cache put(%s): %s", path, exc)
            return False
        return True

    def delete(self, path: str) -> bool:
        """Remove *path* from the remote.  Returns ``True`` on success."""
        import requests  # noqa: PLC0415

        try:
            resp = self._session.delete(
                self._url(path),
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            )
        except requests.RequestException as exc:
            _log.warning("http-cache delete(%s): %s", path, exc)
            return False
        return resp.status_code in (200, 204, 404)


def _chunk_reader(fh):  # type: ignore[no-untyped-def]
    """Yield fixed-size chunks from *fh* for streamed uploads."""
    while True:
        block = fh.read(_CHUNK_BYTES)
        if not block:
            return
        yield block
