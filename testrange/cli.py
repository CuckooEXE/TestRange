"""testrange CLI entry point.

Phase 0 ships:
  - ``testrange --version``
  - ``testrange describe <plan.py>`` — passive pretty-print
  - all other subcommands stubbed; print "not implemented yet — Phase N"

Phase 1 implements ``cache``; Phase 2 implements ``cleanup``; Phase 4 implements ``run``.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

from testrange import __version__
from testrange._log import configure as configure_logging
from testrange.cache.entry import CacheEntry
from testrange.networks.base import Network, Switch
from testrange.plan import Plan
from testrange.vms.recipe import VMRecipe


def _load_plan_module(path: str) -> tuple[Plan, list[Any]]:
    """Import a user plan module by file path and pull PLAN + TESTS off it."""
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


def _describe(args: argparse.Namespace) -> int:
    plan, tests = _load_plan_module(args.plan)
    hyp = plan.hypervisor
    hyp_kind = type(hyp).__name__
    print(f"Plan ({hyp_kind})")
    connection = getattr(hyp, "connection", None)
    if connection:
        print(f"  connection: {connection}")
    print()

    switches = getattr(hyp, "networks", ())
    if switches:
        print("Switches:")
        for sw in switches:
            assert isinstance(sw, Switch)
            attrs = []
            if sw.mgmt:
                attrs.append("mgmt")
            if sw.internet:
                attrs.append("internet")
            attr_str = f" [{', '.join(attrs)}]" if attrs else ""
            print(f"  {sw.name}{attr_str}")
            for n in sw.networks:
                assert isinstance(n, Network)
                flags = []
                if n.dhcp:
                    flags.append("dhcp")
                if n.dns:
                    flags.append("dns")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                print(f"    - {n.name}: {n.cidr}{flag_str}")
        print()

    pools = getattr(hyp, "pools", ())
    if pools:
        print("Storage pools:")
        for pool in pools:
            print(f"  {pool.name}: {pool.size_gb} GB")
        print()

    cache_refs: list[CacheEntry] = []
    vms = getattr(hyp, "vms", ())
    if vms:
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
                drv = getattr(nic, "driver", None)
                if drv:
                    extra.append(f"driver={drv}")
                extra_str = f" ({', '.join(extra)})" if extra else ""
                print(f"    nic:    {nic.network}{extra_str}")
            builder = vm.builder
            base = getattr(builder, "base", None)
            if isinstance(base, CacheEntry):
                cache_refs.append(base)
                print(f"    base:   {base!r}  ⚠ cache resolution lands in Phase 1")
            creds = getattr(builder, "credentials", ())
            if creds:
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
        print(f"CacheEntry references: {len(cache_refs)} (resolution wired in Phase 1)")
    return 0


def _not_implemented(phase: int) -> argparse.Action:
    def _handler(args: argparse.Namespace) -> int:
        print(
            f"testrange {args.subcommand}: not implemented yet — lands in Phase {phase}.",
            file=sys.stderr,
        )
        return 2
    return _handler  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testrange")
    parser.add_argument("--version", action="version", version=f"testrange {__version__}")
    parser.add_argument(
        "--verbose", action="store_true", help="enable DEBUG-level logging"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARN", "WARNING", "ERROR"),
        help="set log level (default INFO)",
    )
    parser.add_argument(
        "--cache",
        metavar="URL",
        default=None,
        help="HTTP cache URL (lands in a later phase)",
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_describe = sub.add_parser("describe", help="passively pretty-print a plan")
    p_describe.add_argument("plan", help="path to the plan file (.py)")
    p_describe.set_defaults(func=_describe)

    for name, phase in [
        ("cache", 1),
        ("run", 4),
        ("cleanup", 2),
    ]:
        p = sub.add_parser(name, help=f"(stub — Phase {phase})")
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(func=_not_implemented(phase), subcommand=name)

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
