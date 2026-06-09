"""testrange CLI entry point."""

from __future__ import annotations

import argparse
import code
import functools
import importlib.util
import inspect
import sys
from collections.abc import Callable, Iterable
from enum import IntEnum
from pathlib import Path
from typing import Any

from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from testrange import __version__
from testrange._console import err_console, out_console
from testrange._dashboard import run_dashboard
from testrange._log import configure as configure_logging
from testrange.cache.entry import CacheEntry
from testrange.cache.http import HttpCache
from testrange.cache.local import CacheEntryInfo
from testrange.cache.manager import CacheManager
from testrange.connect import BackendProfile, load_profile
from testrange.devices.network import DHCPAddr, StaticAddr
from testrange.drivers import scheme_for_hypervisor
from testrange.exceptions import (
    BuildFailedError,
    BuildRequiredError,
    CacheError,
    CacheMissError,
    DriverError,
    OrchestratorError,
    PreflightError,
    ProfileError,
    StateError,
    StateLockedError,
    TestRangeError,
)
from testrange.networks.base import Network, Switch
from testrange.orchestrator._parallel import DEFAULT_MAX_WORKERS
from testrange.orchestrator.backend import resolve_backend
from testrange.orchestrator.dashboard_state import DashboardState
from testrange.orchestrator.runner import build_range, run_tests
from testrange.plan import Plan
from testrange.state.cleanup import (
    cleanup_all,
    cleanup_run,
    format_cleanup_results,
    format_run_list,
    list_runs,
)
from testrange.vms.recipe import VMRecipe

# A subcommand handler: takes the parsed args, returns a process exit code.
Handler = Callable[[argparse.Namespace], int]


class Exit(IntEnum):
    """Process exit codes for the CLI (see ``cli-tool-design`` conventions)."""

    OK = 0
    FAILURE = 1  # a phase ran but failed: build/orchestrator error, test failures
    USAGE = 2  # bad invocation: missing/invalid plan, preflight reject, cache miss
    CLEANUP_ERRORS = 3  # cleanup ran but some resources would not tear down
    INTERRUPTED = 130  # SIGINT (Ctrl-C) during a phase


def _out(msg: str = "") -> None:
    """Print user-facing data to the shared stdout Console (ADR-0029).

    ``markup``/``highlight`` are off so dynamic content (CIDRs, ``repr`` strings,
    paths) renders literally and a stray ``[..]`` is never parsed as rich markup.
    ``soft_wrap`` keeps each message on one logical line (like ``print``) instead
    of hard-wrapping at the console width — so a status line or a test traceback
    stays greppable and substring-stable regardless of terminal width.
    """
    out_console().print(msg, markup=False, highlight=False, soft_wrap=True)


def _err(msg: str) -> None:
    """Print a diagnostic/error to the shared stderr Console (ADR-0029).

    ``soft_wrap`` keeps the message un-wrapped (see :func:`_out`) so an error
    line is never split mid-phrase by the console width.
    """
    err_console().print(msg, markup=False, highlight=False, soft_wrap=True)


def _build_manager(args: argparse.Namespace) -> CacheManager:
    # Resolve the HTTP cache base URL from --cache. If unset, manager.http
    # stays None and the cache is local-only. The flag is the only knob —
    # no env var — so a `testrange` invocation is fully self-describing.
    url = getattr(args, "cache", None)
    http = HttpCache(url) if url else None
    return CacheManager(http=http)


_PROBE = object()


def _accepts_one_arg(fn: Any) -> bool:
    """True if ``fn`` can be called with exactly one positional argument."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # un-introspectable (builtin / C callable) — don't reject
    try:
        sig.bind(_PROBE)
    except TypeError:
        return False
    return True


def _validate_tests(raw: object, path: str) -> list[Any]:
    """Confirm ``TESTS`` is a list of one-arg callables (each receives the orch handle).

    The runner calls every test as ``t(orch)`` (see ``run_tests``), so a
    non-list ``TESTS`` or an entry that isn't callable-with-one-arg is a plan
    bug we surface up front rather than at execution time.
    """
    if not isinstance(raw, list):
        _err(f"error: {path}: TESTS must be a list of test functions, got {type(raw).__name__}")
        sys.exit(Exit.USAGE)
    for t in raw:
        if not callable(t):
            _err(f"error: {path}: TESTS entry {t!r} is not callable")
            sys.exit(Exit.USAGE)
        if not _accepts_one_arg(t):
            name = getattr(t, "__name__", repr(t))
            _err(
                f"error: {path}: test {name!r} must take exactly one argument "
                "(the orchestrator handle)"
            )
            sys.exit(Exit.USAGE)
    return raw


def _load_plan_module(path: str) -> tuple[Plan, list[Any]]:
    p = Path(path).resolve()
    if not p.exists():
        _err(f"error: plan file not found: {path}")
        sys.exit(Exit.USAGE)
    spec = importlib.util.spec_from_file_location(f"_userplan_{p.stem}", p)
    if spec is None or spec.loader is None:
        _err(f"error: cannot load plan module: {path}")
        sys.exit(Exit.USAGE)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except TestRangeError as e:
        # The plan built a topology testrange rejected (e.g. Hypervisor/Switch
        # validation). A plan-authoring error, not a crash — exit USAGE.
        _err(f"error: invalid plan {path}: {e}")
        sys.exit(Exit.USAGE)
    except Exception as e:
        # A plan is arbitrary user .py executed at import; anything it raises
        # (incl. the bare ValueError topology validation throws) must surface as
        # a usage error naming the plan, not a raw testrange traceback. repr
        # keeps the type+message debuggable.
        _err(f"error: failed to load plan {path}: {e!r}")
        sys.exit(Exit.USAGE)
    plan = getattr(module, "PLAN", None)
    if not isinstance(plan, Plan):
        _err(f"error: {path} does not define a top-level PLAN: Plan(...)")
        sys.exit(Exit.USAGE)
    return plan, _validate_tests(getattr(module, "TESTS", []), path)


_DEFAULT_PROFILE_FILE = "connect.toml"


def _parse_profile_spec(spec: str) -> tuple[Path, str]:
    """Split a ``--profile`` value ``[<file>:]<name>`` into ``(path, name)``.

    The default file is ``connect.toml`` (ADR-0016): ``foo`` → ``(connect.toml,
    foo)``; ``other.toml:foo`` → ``(other.toml, foo)``. The split is on the last
    ``:`` so a name itself is colon-free.
    """
    if ":" in spec:
        filename, _, name = spec.rpartition(":")
        filename = filename or _DEFAULT_PROFILE_FILE
    else:
        filename, name = _DEFAULT_PROFILE_FILE, spec
    return Path(filename), name


def _load_profile_arg(args: argparse.Namespace) -> BackendProfile | None:
    """Load the ``--profile [<file>:]<name>`` profile if given, else ``None``.

    A missing/malformed profile is a usage error (exit 2): the loader's
    :class:`ProfileError` is printed and the process exits here, before any
    backend work begins.
    """
    spec = getattr(args, "profile", None)
    if not spec:
        return None
    path, name = _parse_profile_spec(spec)
    try:
        return load_profile(path, name)
    except ProfileError as e:
        _err(f"error: {e}")
        sys.exit(Exit.USAGE)


def _build(args: argparse.Namespace) -> int:
    plan, _tests = _load_plan_module(args.plan)
    profile = _load_profile_arg(args)
    mgr = _build_manager(args)
    dashboard = DashboardState()
    try:
        with run_dashboard(
            dashboard, enabled=not args.no_dashboard, console=err_console(), verbose=args.verbose
        ):
            run_id = build_range(
                plan,
                cache_manager=mgr,
                profile=profile,
                jobs=args.jobs,
                build_timeout_s=args.build_timeout,
                dashboard=dashboard,
            )
    except DriverError as e:
        # Binding/pin mismatch or a backend-agnostic plan with no --profile.
        _err(f"error: {e}")
        return Exit.USAGE
    except PreflightError as e:
        _err(f"preflight failed:\n{e}")
        return Exit.USAGE
    except CacheMissError as e:
        _err(f"cache miss: {e}")
        return Exit.USAGE
    except CacheError as e:
        _err(f"cache error: {e}")
        return Exit.FAILURE
    except BuildFailedError as e:
        _err(f"build failed: {e}")
        return Exit.FAILURE
    except OrchestratorError as e:
        _err(f"build failed: {e}")
        return Exit.FAILURE
    except KeyboardInterrupt:
        _err("interrupted; teardown attempted")
        return Exit.INTERRUPTED
    _out(f"build complete; cache warmed (run_id={run_id})")
    return Exit.OK


def _run(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    profile = _load_profile_arg(args)
    mgr = _build_manager(args)
    dashboard = DashboardState()
    try:
        with run_dashboard(
            dashboard, enabled=not args.no_dashboard, console=err_console(), verbose=args.verbose
        ):
            results = run_tests(
                tests,
                plan,
                cache_manager=mgr,
                fail_fast=args.fail_fast,
                leak_on_failure=args.leak_on_failure,
                require_cache=args.require_cache,
                profile=profile,
                verbose=args.verbose,
                jobs=args.jobs,
                build_timeout_s=args.build_timeout,
                lease_timeout_s=args.lease_timeout,
                dashboard=dashboard,
            )
    except DriverError as e:
        _err(f"error: {e}")
        return Exit.USAGE
    except BuildRequiredError as e:
        _err(f"cache miss: {e}")
        return Exit.USAGE
    except PreflightError as e:
        _err(f"preflight failed:\n{e}")
        return Exit.USAGE
    except CacheMissError as e:
        _err(f"cache miss: {e}")
        return Exit.USAGE
    except CacheError as e:
        _err(f"cache error: {e}")
        return Exit.FAILURE
    except BuildFailedError as e:
        _err(f"build failed: {e}")
        return Exit.FAILURE
    except OrchestratorError as e:
        _err(f"orchestrator failed: {e}")
        return Exit.FAILURE
    except KeyboardInterrupt:
        _err("interrupted; teardown attempted")
        return Exit.INTERRUPTED
    for r in results:
        _out(r.report_line())
    return Exit.OK if all(r.passed for r in results) else Exit.FAILURE


def _repl(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    profile = _load_profile_arg(args)
    mgr = _build_manager(args)
    _print_describe(plan, tests, mgr, profile)
    _out()

    from testrange.orchestrator.runtime import Orchestrator

    try:
        o = Orchestrator(plan, cache_manager=mgr, profile=profile)
    except DriverError as e:
        _err(f"error: {e}")
        return Exit.USAGE
    try:
        with o as orch:
            banner = (
                f"testrange repl — run_id={orch.run_id}\n"
                f"  vms: {sorted(orch.vms)}\n"
                f"  orch.leak() — skip teardown on exit\n"
                f"  Ctrl-D / exit() — exit the REPL"
            )
            code.interact(
                banner=banner,
                local={"orch": orch, "plan": plan, "tests": tests},
                exitmsg="",
            )
    except PreflightError as e:
        _err(f"preflight failed:\n{e}")
        return Exit.USAGE
    except OrchestratorError as e:
        _err(f"orchestrator failed: {e}")
        return Exit.FAILURE
    except KeyboardInterrupt:
        _err("interrupted; teardown attempted")
        return Exit.INTERRUPTED
    return Exit.OK


def _cleanup(args: argparse.Namespace) -> int:
    try:
        if args.list:
            # Read-only: enumerate runs and their status, tear down nothing.
            _out(format_run_list(list_runs()))
            return Exit.OK
        if args.all:
            results = list(cleanup_all(dry_run=args.dry_run))
            if not results:
                _out("(no runs)")
                return Exit.OK
            _out(format_cleanup_results(results))
            return Exit.CLEANUP_ERRORS if any(r.errors for r in results) else Exit.OK
        if not args.run_id:
            _err("error: cleanup requires <run-id> or --all")
            return Exit.USAGE
        r = cleanup_run(args.run_id, dry_run=args.dry_run)
        _out(format_cleanup_results([r]))
        return Exit.CLEANUP_ERRORS if r.errors else Exit.OK
    except StateLockedError as e:
        _err(f"error: {e}")
        return Exit.FAILURE
    except StateError as e:
        _err(f"error: {e}")
        return Exit.USAGE
    except KeyboardInterrupt:
        _err("interrupted")
        return Exit.INTERRUPTED
    except TestRangeError as e:
        # cleanup is the recovery path; a driver error mid-teardown (e.g. a
        # failed connect()) must not surface as a raw traceback.
        _err(f"error: {e}")
        return Exit.CLEANUP_ERRORS


def _describe(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    profile = _load_profile_arg(args)
    mgr = _build_manager(args)
    # A profile that fails to resolve is reported (on stderr) by _print_describe;
    # exit non-zero so `testrange describe … && testrange run …` stops on a
    # broken binding instead of proceeding (H13).
    binding_ok = _print_describe(plan, tests, mgr, profile)
    return Exit.OK if binding_ok else Exit.USAGE


def _add_binding(tree: Tree, plan: Plan, profile: BackendProfile | None) -> bool:
    """Add the resolved backend binding to ``tree``; return whether it is usable.

    Returns ``False`` only when a profile was given but the binding failed to
    resolve — that error goes to **stderr** and the caller exits non-zero, so a
    ``describe && run`` chain stops instead of proceeding on a broken binding
    (H13). A missing profile (UNBOUND) is informational, not an error.
    """
    hyp = plan.hypervisor
    if profile is None:
        scheme = scheme_for_hypervisor(hyp)
        if scheme is not None:
            tree.add(
                Text(f"backend: UNBOUND (pinned to {scheme!r}; pass --profile <{scheme}-profile>)")
            )
        else:
            tree.add(Text("backend: UNBOUND (pass --profile <name> to run)"))
        return True
    try:
        resolved = resolve_backend(plan, profile)
    except DriverError as e:
        # Surface the failed binding on stderr (not in the stdout tree) so the
        # caller can exit non-zero and a `describe && run` chain stops.
        _err(f"backend: ERROR — {e}")
        return False

    node = tree.add(Text("backend"))
    node.add(Text(f"driver: {profile.scheme} ({resolved.driver.DRIVER_NAME})"))
    for label, value in profile.describe_fields():
        node.add(Text(f"{label}: {value}"))
    if profile.uplinks:
        rendered = ", ".join(f"{k} -> {v}" for k, v in sorted(profile.uplinks.items()))
        node.add(Text(f"uplinks: {rendered}"))
    else:
        node.add(Text("uplinks: none"))
    bs = getattr(hyp, "build_switch", None)
    if bs is None:
        node.add(Text("build egress: none (isolated build network)"))
    else:
        node.add(Text(f"build egress: switch {bs.name!r} (uplink={bs.uplink or 'none'})"))
    return True


def _print_describe(
    plan: Plan, tests: list[Any], mgr: CacheManager, profile: BackendProfile | None = None
) -> bool:
    """Pretty-print a Plan + its tests; return whether the binding is usable.

    Shared by `describe` and `repl`.
    """
    hyp = plan.hypervisor
    tree = Tree(Text(f"Plan ({type(hyp).__name__})"))
    binding_ok = _add_binding(tree, plan, profile)

    if switches := getattr(hyp, "networks", ()):
        sw_node = tree.add(Text("Switches"))
        for sw in switches:
            assert isinstance(sw, Switch)
            attrs = []
            if sw.mgmt:
                attrs.append("mgmt")
            if sw.uplink:
                attrs.append(f"uplink={sw.uplink}")
            if sw.sidecar is not None:
                services = [
                    name
                    for name, on in [
                        ("dhcp", sw.sidecar.dhcp),
                        ("dns", sw.sidecar.dns),
                        ("nat", sw.sidecar.nat),
                    ]
                    if on
                ]
                attrs.append(f"sidecar={'+'.join(services)}")
            attr_str = f" [{', '.join(attrs)}]" if attrs else ""
            switch = sw_node.add(Text(f"{sw.name}: {sw.cidr}{attr_str}"))
            for n in sw.networks:
                assert isinstance(n, Network)
                switch.add(Text(n.name))

    if pools := getattr(hyp, "pools", ()):
        pool_node = tree.add(Text("Storage pools"))
        for pool in pools:
            pool_node.add(Text(f"{pool.name}: {pool.size_gb} GB"))

    cache_refs: list[CacheEntry] = []
    if vms := getattr(hyp, "vms", ()):
        vm_node = tree.add(Text("VMs"))
        for vm in vms:
            assert isinstance(vm, VMRecipe)
            node = vm_node.add(Text(vm.name))
            node.add(Text(f"cpu:    {vm.spec.cpu.count} vCPU"))
            node.add(Text(f"memory: {vm.spec.memory.size_mb} MB"))
            node.add(
                Text(
                    f"os:     {type(vm.spec.os_drive).__name__}({vm.spec.os_drive.pool!r}, "
                    f"{vm.spec.os_drive.size_gb} GB)"
                )
            )
            for d in vm.spec.data_drives:
                node.add(Text(f"disk:   {d.pool!r}, {d.size_gb} GB"))
            for nic in vm.spec.nics:
                extra = []
                if isinstance(nic.addr, StaticAddr):
                    extra.append(f"static={nic.addr.addr}")
                elif isinstance(nic.addr, DHCPAddr):
                    extra.append("dhcp")
                else:
                    extra.append("no addr")
                if drv := getattr(nic, "driver", None):
                    extra.append(f"driver={drv}")
                extra_str = f" ({', '.join(extra)})" if extra else ""
                node.add(Text(f"nic:    {nic.network}{extra_str}"))
            builder = vm.builder
            # OS-disk origin via the Builder ABC seams (not a concrete attr):
            # image-based builders return os_disk_base(), installer-based ones
            # return boot_media() (the install ISO). Both are CacheEntry refs the
            # user must have cached, so surface either.
            for label, entry in (
                ("base", builder.os_disk_base()),
                ("media", builder.boot_media()),
            ):
                if entry is None:
                    continue
                cache_refs.append(entry)
                try:
                    # Passive describe — don't pull a multi-GB base over HTTP
                    # just to print one line. fetch=False is the rule for
                    # any non-install-phase resolve.
                    info = mgr.resolve(entry, fetch=False)
                    node.add(
                        Text(
                            f"{label}:   {entry!r}  -> {info.short_sha} ({_format_size(info.size)})"
                        )
                    )
                except CacheMissError:
                    node.add(Text(f"{label}:   {entry!r}  (!) not in cache"))
            if creds := getattr(builder, "credentials", ()):
                names = ", ".join(c.username + ("(admin)" if c.admin else "") for c in creds)
                node.add(Text(f"creds:  {names}"))
            comm = vm.communicator
            username = getattr(comm, "username", None)
            arg = repr(username) if username is not None else ""
            node.add(Text(f"comm:   {type(comm).__name__}({arg})"))

    if tests:
        test_node = tree.add(Text(f"Tests: {len(tests)}"))
        for t in tests:
            test_node.add(Text(getattr(t, "__name__", repr(t))))

    if cache_refs:
        unique = {e.identifier for e in cache_refs}
        tree.add(Text(f"CacheEntry references: {len(unique)} unique"))

    out_console().print(tree)
    return binding_ok


def _cache_errors(fn: Handler) -> Handler:
    """Map the cache exception family onto exit codes, uniformly for every cache op."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except CacheMissError as e:
            _err(f"error: {e}")
            return Exit.USAGE
        except CacheError as e:
            _err(f"error: {e}")
            return Exit.FAILURE
        except TestRangeError as e:  # pragma: no cover (broad safety net)
            _err(f"error: {e}")
            return Exit.FAILURE

    return wrapper


@_cache_errors
def _cache_add(args: argparse.Namespace) -> int:
    # Goes through the broker so the HTTP tier gets a mirror.
    info = _build_manager(args).add(args.source, name=args.name, description=args.description)
    _out(info.sha256)
    return Exit.OK


@_cache_errors
def _cache_list(args: argparse.Namespace) -> int:
    # Local-only; the HTTP tier has no listing protocol.
    _print_list(_build_manager(args).local.list_entries())
    return Exit.OK


@_cache_errors
def _cache_del(args: argparse.Namespace) -> int:
    info = _build_manager(args).delete(args.identifier)
    _out(f"deleted {info.short_sha}")
    return Exit.OK


@_cache_errors
def _cache_rename(args: argparse.Namespace) -> int:
    info = _build_manager(args).add_name(args.identifier, args.new_name)
    _out(f"{info.short_sha}: names now {list(info.names)}")
    return Exit.OK


@_cache_errors
def _cache_forget_name(args: argparse.Namespace) -> int:
    info = _build_manager(args).forget_name(args.name)
    _out(f"{info.short_sha}: names now {list(info.names)}")
    return Exit.OK


@_cache_errors
def _cache_push(args: argparse.Namespace) -> int:
    info = _build_manager(args).push(args.identifier)
    _out(f"pushed {info.short_sha} -> http cache")
    return Exit.OK


@_cache_errors
def _cache_purge(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    entries = mgr.local.list_entries()
    if not entries:
        _out("cache is empty; nothing to purge")
        return Exit.OK
    total = _format_size(sum(e.size for e in entries))
    if args.dry_run or not args.yes:
        _out(f"would purge {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} ({total}):")
        _print_list(entries)
        if not args.dry_run:
            _out("\nre-run with --yes to delete them")
        return Exit.OK
    removed = mgr.purge()
    _out(f"purged {len(removed)} entr{'y' if len(removed) == 1 else 'ies'} ({total})")
    return Exit.OK


@_cache_errors
def _cache_pull(args: argparse.Namespace) -> int:
    info = _build_manager(args).pull(args.identifier)
    _out(f"pulled {info.short_sha} <- http cache")
    return Exit.OK


def _print_list(entries: Iterable[CacheEntryInfo]) -> None:
    rows = list(entries)
    if not rows:
        _out("(empty)")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("SHA", no_wrap=True)
    table.add_column("SIZE", justify="right")
    table.add_column("NAMES / ORIGIN")
    for info in rows:
        names = ", ".join(info.names) if info.names else "-"
        # The origin (if any) rides under the names on a second wrapped line,
        # keeping each entry to a single table row.
        cell = names if not info.origin else f"{names}\norigin: {info.origin}"
        table.add_row(Text(info.short_sha), Text(_format_size(info.size)), Text(cell))
    out_console().print(table)


def _format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testrange")
    parser.add_argument("--version", action="version", version=f"testrange {__version__}")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="set log level (default INFO)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the build serial console / test output (live dashboard, or plain logs off a TTY)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="disable the live run/build dashboard; emit plain log lines instead",
    )
    parser.add_argument(
        "--cache",
        default=None,
        metavar="URL",
        help=(
            "shared HTTP cache base URL (e.g. https://cache.local:8443). "
            "TLS is never verified — the server is expected to sit behind "
            "a private network gate."
        ),
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_describe = sub.add_parser("describe", help="passively pretty-print a plan")
    p_describe.add_argument("plan", help="path to the plan file (.py)")
    _add_connect_arg(p_describe)
    p_describe.set_defaults(func=_describe)

    p_cache = sub.add_parser("cache", help="manage the local cache")
    cache_sub = p_cache.add_subparsers(dest="cache_subcommand", required=True)

    p_add = cache_sub.add_parser("add", help="add a path or URL to the cache")
    p_add.add_argument("source", help="local path or http(s):// URL")
    p_add.add_argument("--name", default=None, help="optional pretty-name alias")
    p_add.add_argument("--description", default=None, help="optional description")
    p_add.set_defaults(func=_cache_add)

    p_list = cache_sub.add_parser("list", help="list cached entries")
    p_list.set_defaults(func=_cache_list)

    p_del = cache_sub.add_parser("del", help="delete an entry by sha or name")
    p_del.add_argument("identifier", help="content sha (or prefix >= 16 hex) or pretty-name")
    p_del.set_defaults(func=_cache_del)

    p_purge = cache_sub.add_parser(
        "purge",
        help="delete ALL local cache entries (requires --yes)",
        description=(
            "Delete every entry from the local cache. Without --yes this only "
            "lists what would go (a no-op). Local-only: the shared HTTP cache "
            "(--cache) is never touched; remove a shared entry with `cache del`."
        ),
    )
    p_purge.add_argument("--yes", action="store_true", help="confirm: actually delete every entry")
    p_purge.add_argument(
        "--dry-run", action="store_true", help="list what would be deleted, then stop"
    )
    p_purge.set_defaults(func=_cache_purge)

    p_rename = cache_sub.add_parser("rename", help="add a pretty-name alias to an entry")
    p_rename.add_argument("identifier", help="content sha or existing name")
    p_rename.add_argument("new_name", help="new alias to attach")
    p_rename.set_defaults(func=_cache_rename)

    p_forget = cache_sub.add_parser("forget-name", help="remove a single alias")
    p_forget.add_argument("name", help="alias to remove")
    p_forget.set_defaults(func=_cache_forget_name)

    p_push = cache_sub.add_parser(
        "push", help="copy a local entry to the HTTP cache (requires --cache)"
    )
    p_push.add_argument("identifier", help="content sha or pretty-name")
    p_push.set_defaults(func=_cache_push)

    p_pull = cache_sub.add_parser(
        "pull", help="fetch an entry from the HTTP cache into local (requires --cache)"
    )
    p_pull.add_argument("identifier", help="content sha or pretty-name")
    p_pull.set_defaults(func=_cache_pull)

    p_cleanup = sub.add_parser("cleanup", help="tear down resources from a previous run")
    p_cleanup.add_argument("run_id", nargs="?", default=None, help="run id to clean up")
    p_cleanup.add_argument("--all", action="store_true", help="clean up every run dir")
    p_cleanup.add_argument(
        "--list",
        action="store_true",
        help="list existing runs and their status; tear down nothing",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be destroyed without touching the backend",
    )
    p_cleanup.set_defaults(func=_cleanup)

    p_build = sub.add_parser(
        "build",
        help="warm the cache (build every VM); run no tests",
        description=(
            "Provision every VM to completion and capture its disks into the "
            "cache (local, plus the HTTP tier when --cache is set), then tear "
            "down all build infra. Runs no tests and creates no run VMs. A "
            "subsequent `testrange run` is a pure warm-cache bring-up."
        ),
    )
    p_build.add_argument("plan", help="path to the plan file (.py)")
    p_build.add_argument(
        "--build-timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="max seconds a build VM may take to report its result over serial (default 600)",
    )
    _add_jobs_arg(p_build)
    _add_connect_arg(p_build)
    p_build.set_defaults(func=_build)

    p_run = sub.add_parser("run", help="bring up the range, run tests, tear down")
    p_run.add_argument("plan", help="path to the plan file (.py)")
    p_run.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop on the first test failure",
    )
    p_run.add_argument(
        "--leak-on-failure",
        action="store_true",
        help="if any test fails, skip teardown so you can SSH in to debug",
    )
    p_run.add_argument(
        "--require-cache",
        action="store_true",
        help="fail fast if any artifact is missing instead of auto-building it first",
    )
    p_run.add_argument(
        "--build-timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="max seconds a build VM may take to report its result over serial (default 600)",
    )
    p_run.add_argument(
        "--lease-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="max seconds a run VM may take to acquire its DHCP lease (default 120)",
    )
    _add_jobs_arg(p_run)
    _add_connect_arg(p_run)
    p_run.set_defaults(func=_run)

    p_repl = sub.add_parser(
        "repl",
        help="bring the range up and drop into a Python REPL (no tests)",
        description=(
            "Bring the range up, print the `describe` output, then drop "
            "into a stdlib Python REPL with `orch`, `plan`, and `tests` "
            "pre-bound. Call orch.leak() to skip teardown on exit; "
            "Ctrl-D / exit() leaves and tears the range down."
        ),
    )
    p_repl.add_argument("plan", help="path to the plan file (.py)")
    _add_connect_arg(p_repl)
    p_repl.set_defaults(func=_repl)

    return parser


def _jobs_arg(value: str) -> int:
    """Parse ``--jobs N``: a non-negative int. ``0`` and ``1`` both mean serial.

    Negatives are rejected loud at the boundary rather than silently coerced to
    serial (``resolve_workers`` would clamp them) — a negative ``--jobs`` is a
    user error, not a request for serial. ``0`` is accepted and means serial,
    mirroring the familiar ``make -j``-style "no extra workers" spelling.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--jobs expects an integer, got {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"--jobs must be >= 0 (0 or 1 = serial), got {n}")
    return n


def _add_jobs_arg(parser: argparse.ArgumentParser) -> None:
    """Attach ``--jobs N`` to a plan-taking verb (run/build).

    Caps the I/O phases' bounded thread pool (ADR-0023): per-VM bring-up
    uploads, build-disk downloads, and readiness waits. Omit for the default
    cap; ``--jobs 0`` or ``--jobs 1`` forces the phases serial (handy for
    debugging).
    """
    parser.add_argument(
        "--jobs",
        type=_jobs_arg,
        default=None,
        metavar="N",
        help=f"max concurrent workers for the I/O phases "
        f"(default: {DEFAULT_MAX_WORKERS}; 0 or 1 = serial)",
    )


def _add_connect_arg(parser: argparse.ArgumentParser) -> None:
    """Attach ``--profile [FILE:]NAME`` to a plan-taking verb (run/build/repl/describe).

    Binds a plan to a backend via a named profile in a local TOML file (ADR-0016).
    The default file is ``connect.toml`` (``--profile myProxmox``); a different
    file is ``--profile other.toml:myProxmox``. Every runnable plan needs it
    (CORE-19). There is no environment fallback — the flag is the only knob, so
    an invocation is fully self-describing.
    """
    parser.add_argument(
        "--profile",
        metavar="[FILE:]NAME",
        default=None,
        help="connection profile binding the plan to a backend ([<file>:]<name>; "
        "default file connect.toml)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level)
    rc = args.func(args)
    return int(rc or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
