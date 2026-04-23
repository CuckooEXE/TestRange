"""Cross-run coordination primitives.

Most of TestRange is naturally concurrency-safe: each orchestrator
opens its own backend connection, installs a uniquely-named set of
objects (``tr-*-<runid>``), and cleans them up on exit.  A few places,
however, need shared state:

- **Install subnet selection.**  Backends that draw install-phase NAT
  subnets from a shared pool must serialise the pick/claim step —
  otherwise two runs probing simultaneously will both see the same
  slot as free, and one run's dnsmasq will later fail to bind the
  bridge IP.  :func:`install_subnet_lock` serialises the sequence
  across runs in the same process *and* across processes (the lock
  file lives in ``/var/tmp/testrange-locks/``).

The lock is held for the shortest possible span — only long enough to
claim a subnet and bring the network up on the backend — so concurrent
test runs don't queue on each other for install time.
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock

_LOCK_ROOT = Path("/var/tmp/testrange-locks")


def _ensure_lock_root() -> Path:
    """Return the shared lock directory, creating it if needed.

    :returns: Path to the lock root.
    """
    _LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        _LOCK_ROOT.chmod(0o755)
    except PermissionError:
        # Another user created the dir; we can still take locks here
        # because ``/var/tmp`` is sticky-world-writable.
        pass
    return _LOCK_ROOT


def install_subnet_lock(timeout: float = 300.0) -> FileLock:
    """Return a :class:`~filelock.FileLock` protecting the install subnet pool.

    Acquire around the pick-a-subnet / define-network / start-network
    sequence in any backend that shares its install-subnet pool across
    concurrent runs, so two tests don't race to claim the same CIDR.

    :param timeout: Seconds to wait for the lock before giving up.
    :returns: A :class:`FileLock` ready to be used as a context manager.
    """
    return FileLock(
        str(_ensure_lock_root() / "install-subnet.lock"),
        timeout=timeout,
    )


def vm_build_lock(config_hash: str, timeout: float = 3600.0) -> FileLock:
    """Serialise install-phase builds that share a config hash.

    Two concurrent tests whose VMs have identical iso/users/packages/
    post-install-commands/disk-size produce the same
    :func:`~testrange.cache.vm_config_hash` and therefore target the
    same cached qcow2 file.  Without coordination they'd both run the
    full install phase in parallel and then race on the write side of
    the snapshot-compress step.

    Holding this lock across the cache-check / install / cache-store
    sequence means the first arrival does the install and the second
    waits, then hits the cache for free.

    :param config_hash: The VM's config hash.
    :param timeout: Seconds to wait for the lock before giving up;
        defaults to an hour (install phases can take a while).
    :returns: A :class:`FileLock` ready to be used as a context manager.
    """
    return FileLock(
        str(_ensure_lock_root() / f"build-{config_hash}.lock"),
        timeout=timeout,
    )
