"""testrange CLI entry point."""

from __future__ import annotations

import argparse
import code
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from testrange import __version__
from testrange._log import configure as configure_logging
from testrange.cache.entry import CacheEntry
from testrange.cache.http import HttpCache
from testrange.cache.local import CacheEntryInfo
from testrange.cache.manager import CacheManager
from testrange.exceptions import (
    CacheError,
    CacheMissError,
    OrchestratorError,
    PreflightError,
    StateError,
    StateLockedError,
    TestRangeError,
)
from testrange.networks.base import Network, Switch
from testrange.orchestrator.runtime import run_tests
from testrange.plan import Plan
from testrange.state.cleanup import cleanup_all, cleanup_run, format_cleanup_results
from testrange.vms.recipe import VMRecipe


def _build_manager(args: argparse.Namespace) -> CacheManager:
    # Resolve the HTTP cache base URL from --cache. If unset, manager.http
    # stays None and behavior matches v0.0.1. The flag is the only knob —
    # no env var — so a `testrange` invocation is fully self-describing.
    url = getattr(args, "cache", None)
    http = HttpCache(url) if url else None
    return CacheManager(http=http)


def _load_plan_module(path: str) -> tuple[Plan, list[Any]]:
    p = Path(path).resolve()
    if not p.exists():
        print(f"error: plan file not found: {path}", file=sys.stderr)
        sys.exit(2)
    spec = importlib.util.spec_from_file_location(f"_userplan_{p.stem}", p)
    if spec is None or spec.loader is None:
        print(f"error: cannot load plan module: {path}", file=sys.stderr)
        sys.exit(2)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = getattr(module, "PLAN", None)
    if not isinstance(plan, Plan):
        print(f"error: {path} does not define a top-level PLAN: Plan(...)", file=sys.stderr)
        sys.exit(2)
    tests = getattr(module, "TESTS", [])
    return plan, list(tests)


def _run(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    mgr = _build_manager(args)
    try:
        results = run_tests(
            tests,
            plan,
            cache_manager=mgr,
            fail_fast=args.fail_fast,
            leak_on_failure=args.leak_on_failure,
        )
    except PreflightError as e:
        print(f"preflight failed:\n{e}", file=sys.stderr)
        return 2
    except OrchestratorError as e:
        print(f"orchestrator failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; teardown attempted", file=sys.stderr)
        return 130
    for r in results:
        print(r.report_line())
    return 0 if all(r.passed for r in results) else 1


def _repl(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    mgr = _build_manager(args)
    _print_describe(plan, tests, mgr)
    print()

    from testrange.orchestrator.runtime import Orchestrator

    o = Orchestrator(plan, cache_manager=mgr)
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
        print(f"preflight failed:\n{e}", file=sys.stderr)
        return 2
    except OrchestratorError as e:
        print(f"orchestrator failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; teardown attempted", file=sys.stderr)
        return 130
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    try:
        if args.all:
            results = list(cleanup_all(dry_run=args.dry_run))
            if not results:
                print("(no runs)")
                return 0
            print(format_cleanup_results(results))
            return 3 if any(r.errors for r in results) else 0
        if not args.run_id:
            print("error: cleanup requires <run-id> or --all", file=sys.stderr)
            return 2
        r = cleanup_run(args.run_id, dry_run=args.dry_run)
        print(format_cleanup_results([r]))
        return 3 if r.errors else 0
    except StateLockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _describe(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    mgr = _build_manager(args)
    _print_describe(plan, tests, mgr)
    return 0


def _print_describe(plan: Plan, tests: list[Any], mgr: CacheManager) -> None:
    """Pretty-print a Plan + its tests. Shared by `describe` and `repl`."""
    hyp = plan.hypervisor
    print(f"Plan ({type(hyp).__name__})")
    if connection := getattr(hyp, "connection", None):
        print(f"  connection: {connection}")
    print()

    if switches := getattr(hyp, "networks", ()):
        print("Switches:")
        for sw in switches:
            assert isinstance(sw, Switch)
            attrs = [n for n, on in [("mgmt", sw.mgmt), ("internet", sw.internet)] if on]
            attr_str = f" [{', '.join(attrs)}]" if attrs else ""
            print(f"  {sw.name}{attr_str}")
            for n in sw.networks:
                assert isinstance(n, Network)
                flags = [f for f, on in [("dhcp", n.dhcp), ("dns", n.dns)] if on]
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                print(f"    - {n.name}: {n.cidr}{flag_str}")
        print()

    if pools := getattr(hyp, "pools", ()):
        print("Storage pools:")
        for pool in pools:
            print(f"  {pool.name}: {pool.size_gb} GB")
        print()

    cache_refs: list[CacheEntry] = []
    if vms := getattr(hyp, "vms", ()):
        print("VMs:")
        for vm in vms:
            assert isinstance(vm, VMRecipe)
            print(f"  {vm.name}")
            print(f"    cpu:    {vm.spec.cpu.count} vCPU")
            print(f"    memory: {vm.spec.memory.size_mb} MB")
            print(
                f"    os:     {type(vm.spec.os_drive).__name__}({vm.spec.os_drive.pool!r}, "
                f"{vm.spec.os_drive.size_gb} GB)"
            )
            for d in vm.spec.data_drives:
                print(f"    disk:   {d.pool!r}, {d.size_gb} GB")
            for nic in vm.spec.nics:
                extra = []
                if drv := getattr(nic, "driver", None):
                    extra.append(f"driver={drv}")
                extra_str = f" ({', '.join(extra)})" if extra else ""
                print(f"    nic:    {nic.network}{extra_str}")
            builder = vm.builder
            if isinstance(base := getattr(builder, "base", None), CacheEntry):
                cache_refs.append(base)
                try:
                    # Passive describe — don't pull a multi-GB base over HTTP
                    # just to print one line. fetch=False is the rule for
                    # any non-install-phase resolve.
                    info = mgr.resolve(base, fetch=False)
                    print(f"    base:   {base!r}  -> {info.short_sha} ({_format_size(info.size)})")
                except CacheMissError:
                    print(f"    base:   {base!r}  ⚠ not in cache")
            if creds := getattr(builder, "credentials", ()):
                names = ", ".join(c.username + ("(admin)" if c.admin else "") for c in creds)
                print(f"    creds:  {names}")
            comm = vm.communicator
            print(f"    comm:   {type(comm).__name__}({getattr(comm, 'username', '?')!r})")
        print()

    if tests:
        print(f"Tests: {len(tests)}")
        for t in tests:
            print(f"  - {getattr(t, '__name__', repr(t))}")
        print()

    if cache_refs:
        unique = {e.identifier for e in cache_refs}
        print(f"CacheEntry references: {len(unique)} unique")


def _cache(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    sub = args.cache_subcommand
    try:
        if sub == "add":
            # Goes through the broker so HTTP gets a mirror.
            info = mgr.add(
                args.source,
                name=args.name,
                description=args.description,
            )
            print(info.sha256)
            return 0
        if sub == "list":
            # Local-only; HTTP has no listing protocol.
            _print_list(mgr.local.list_entries())
            return 0
        if sub == "del":
            info = mgr.delete(args.identifier)
            print(f"deleted {info.short_sha}")
            return 0
        if sub == "rename":
            info = mgr.add_name(args.identifier, args.new_name)
            print(f"{info.short_sha}: names now {list(info.names)}")
            return 0
        if sub == "forget-name":
            info = mgr.forget_name(args.name)
            print(f"{info.short_sha}: names now {list(info.names)}")
            return 0
        if sub == "push":
            info = mgr.push(args.identifier)
            print(f"pushed {info.short_sha} → http cache")
            return 0
        if sub == "pull":
            info = mgr.pull(args.identifier)
            print(f"pulled {info.short_sha} ← http cache")
            return 0
    except CacheMissError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except CacheError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except TestRangeError as e:  # pragma: no cover (broad safety net)
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"error: unknown cache subcommand {sub!r}", file=sys.stderr)
    return 2


def _print_list(entries: Iterable[CacheEntryInfo]) -> None:
    rows = list(entries)
    if not rows:
        print("(empty)")
        return
    width_sha = 18
    width_size = 12
    print(f"{'SHA':<{width_sha}}  {'SIZE':>{width_size}}  NAMES / ORIGIN")
    for info in rows:
        names = ", ".join(info.names) if info.names else "-"
        print(f"{info.short_sha:<{width_sha}}  {_format_size(info.size):>{width_size}}  {names}")
        if info.origin:
            print(f"{'':<{width_sha}}  {'':>{width_size}}  origin: {info.origin}")


def _format_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n //= 1024
    return f"{n} TiB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testrange")
    parser.add_argument("--version", action="version", version=f"testrange {__version__}")
    parser.add_argument("--verbose", action="store_true", help="enable DEBUG-level logging")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARN", "WARNING", "ERROR"),
        help="set log level (default INFO)",
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
    p_describe.set_defaults(func=_describe)

    p_cache = sub.add_parser("cache", help="manage the local cache")
    cache_sub = p_cache.add_subparsers(dest="cache_subcommand", required=True)

    p_add = cache_sub.add_parser("add", help="add a path or URL to the cache")
    p_add.add_argument("source", help="local path or http(s):// URL")
    p_add.add_argument("--name", default=None, help="optional pretty-name alias")
    p_add.add_argument("--description", default=None, help="optional description")

    cache_sub.add_parser("list", help="list cached entries")

    p_del = cache_sub.add_parser("del", help="delete an entry by sha or name")
    p_del.add_argument("identifier", help="content sha (or prefix >= 16 hex) or pretty-name")

    p_rename = cache_sub.add_parser("rename", help="add a pretty-name alias to an entry")
    p_rename.add_argument("identifier", help="content sha or existing name")
    p_rename.add_argument("new_name", help="new alias to attach")

    p_forget = cache_sub.add_parser("forget-name", help="remove a single alias")
    p_forget.add_argument("name", help="alias to remove")

    p_push = cache_sub.add_parser(
        "push", help="copy a local entry to the HTTP cache (requires --cache)"
    )
    p_push.add_argument("identifier", help="content sha or pretty-name")

    p_pull = cache_sub.add_parser(
        "pull", help="fetch an entry from the HTTP cache into local (requires --cache)"
    )
    p_pull.add_argument("identifier", help="content sha or pretty-name")

    p_cache.set_defaults(func=_cache)

    p_cleanup = sub.add_parser("cleanup", help="tear down resources from a previous run")
    p_cleanup.add_argument("run_id", nargs="?", default=None, help="run id to clean up")
    p_cleanup.add_argument("--all", action="store_true", help="clean up every run dir")
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be destroyed without touching the backend",
    )
    p_cleanup.set_defaults(func=_cleanup)

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
    p_repl.set_defaults(func=_repl)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = "DEBUG" if args.verbose else args.log_level
    configure_logging(level=level)
    rc = args.func(args)
    return int(rc or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
