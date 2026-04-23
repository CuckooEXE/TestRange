"""TestRange command-line interface.

Provides the ``testrange`` command for running test files and managing the
image cache.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import shutil
import sys
from pathlib import Path
from types import ModuleType

import click

from testrange._logging import configure_root_logger
from testrange._repl import print_keep_summary, start_repl
from testrange._version import __version__
from testrange.backends import cli_build_orchestrator
from testrange.cache import CacheManager
from testrange.orchestrator_base import AbstractOrchestrator
from testrange.test import Test, run_tests

_ORCHESTRATOR_HELP = (
    "Hypervisor backend URL.  Overrides the backend the test factory "
    "constructed.  Examples:\n\n"
    "\b\n"
    "  --orchestrator qemu:///system\n"
    "  --orchestrator qemu+ssh://alice@vmhost/system\n"
    "  --orchestrator proxmox://TOKENID:SECRET@pve.example.com/pve01?storage=local-lvm\n"
    "  --orchestrator proxmox://root:hunter2@pve.example.com\n"
    "\n"
    "Omit to keep the test's own orchestrator.  Each backend in "
    ":mod:`testrange.backends` self-describes the URL shapes it accepts."
)


def _orchestrator_option(f):
    """Attach the single ``--orchestrator URL`` option to a command."""
    return click.option(
        "--orchestrator",
        "orchestrator_url",
        default=None,
        metavar="URL",
        help=_ORCHESTRATOR_HELP,
    )(f)


def _resolve_orchestrator(
    test: Test,
    orchestrator_url: str | None,
) -> AbstractOrchestrator:
    """Swap the test's orchestrator for the CLI-selected backend.

    When *orchestrator_url* is ``None`` the test's own orchestrator
    (whatever the test author constructed) is returned untouched.
    Otherwise the URL is dispatched through
    :func:`testrange.backends.cli_build_orchestrator` — the CLI itself
    knows nothing about which backends exist or what URL shapes they
    accept.
    """
    if orchestrator_url is None:
        return test._orchestrator

    new = cli_build_orchestrator(orchestrator_url, test._orchestrator)
    if new is None:
        raise click.BadParameter(
            f"no backend claims orchestrator URL {orchestrator_url!r}.  "
            "Check the URL scheme against the backends under "
            "testrange.backends."
        )
    return new


@click.group()
@click.version_option(__version__, prog_name="testrange")
def main() -> None:
    """TestRange — VM-based test environment orchestrator."""


@main.command("run")
@click.argument("target")
@click.option(
    "--verbose/--quiet",
    default=True,
    help="Print per-test status lines.",
)
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    default="INFO",
    help="Minimum level for TestRange stderr logs.",
)
@click.option(
    "-j",
    "--concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help=(
        "Maximum tests to run in parallel.  Each test must declare its "
        "own VirtualNetwork subnets; install-phase subnets are "
        "serialised automatically."
    ),
)
@_orchestrator_option
def run_cmd(
    target: str,
    verbose: bool,
    log_level: str,
    concurrency: int,
    orchestrator_url: str | None,
) -> None:
    """Run tests produced by a factory function.

    TARGET has the form ``MODULE:FACTORY`` where MODULE is either a dotted
    module name (``mypkg.tests``) or a path to a Python file
    (``./tests.py``), and FACTORY is a zero-argument callable that returns
    a ``list`` of :class:`~testrange.test.Test` objects.

    Example::

        testrange run tests:gen_tests
        testrange run ./my_tests.py:gen_tests

    Target a different hypervisor::

        testrange run tests:gen_tests \\
            --orchestrator proxmox://TOKENID:SECRET@pve.example.com/pve01
    """
    configure_root_logger(getattr(logging, log_level.upper()))
    module_part, factory_name = _parse_target(target)
    module = _load_module(module_part)
    tests = _load_tests(module, module_part, factory_name)

    # Let the runner override the test author's backend choice.
    if orchestrator_url is not None:
        for test in tests:
            test._orchestrator = _resolve_orchestrator(test, orchestrator_url)

    results = run_tests(tests, verbose=verbose, concurrency=concurrency)
    failed = sum(1 for r in results if not r.passed)

    if failed:
        if verbose:
            for test, result in zip(tests, results, strict=True):
                if not result.passed and result.traceback_str:
                    click.echo(f"\n--- {test.name} traceback ---")
                    click.echo(result.traceback_str)
        sys.exit(1)


def _parse_target(target: str) -> tuple[str, str]:
    """Split a ``MODULE:FACTORY`` string into its two parts.

    :param target: Raw user-supplied target string.
    :returns: ``(module_part, factory_name)`` tuple.
    """
    module_part, sep, factory_name = target.partition(":")
    if not sep or not module_part or not factory_name:
        click.echo(
            f"TARGET must be in 'module:factory' form (got {target!r}).",
            err=True,
        )
        sys.exit(2)
    return module_part, factory_name


def _load_module(module_part: str) -> ModuleType:
    """Import *module_part* as either a file path or a dotted module name.

    :param module_part: File path (``./x.py`` or ``/abs/x.py``) or dotted
        module name (``mypkg.tests``).
    :returns: The loaded module.
    """
    path = Path(module_part)
    looks_like_path = (
        module_part.endswith(".py") or "/" in module_part or path.is_file()
    )

    if looks_like_path:
        if not path.is_file():
            click.echo(f"File not found: {module_part}", err=True)
            sys.exit(1)
        # Make sibling modules importable from the loaded file.
        sys.path.insert(0, str(path.resolve().parent))
        spec = importlib.util.spec_from_file_location("_testrange_target", path)
        if spec is None or spec.loader is None:
            click.echo(f"Cannot load {path}", err=True)
            sys.exit(1)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # Dotted name — ensure cwd is importable for ad-hoc projects.
    sys.path.insert(0, str(Path.cwd()))
    try:
        return importlib.import_module(module_part)
    except ImportError as exc:
        click.echo(f"Cannot import module {module_part!r}: {exc}", err=True)
        sys.exit(1)


def _load_tests(
    module: ModuleType, module_part: str, factory_name: str
) -> list[Test]:
    """Look up *factory_name* in *module*, call it, and validate the result.

    Shared by the ``run``, ``describe``, and ``repl`` commands so they all
    fail with the same diagnostics.

    :param module: The module loaded via :func:`_load_module`.
    :param module_part: The original module argument (for error messages).
    :param factory_name: The attribute name to look up on *module*.
    :returns: The validated ``list[Test]`` returned by the factory.
    """
    factory = getattr(module, factory_name, None)
    if factory is None:
        click.echo(
            f"Module {module_part!r} has no attribute {factory_name!r}.",
            err=True,
        )
        sys.exit(1)
    if not callable(factory):
        click.echo(f"{module_part}:{factory_name} is not callable.", err=True)
        sys.exit(1)

    tests = factory()
    if not isinstance(tests, list) or not all(isinstance(t, Test) for t in tests):
        click.echo(
            f"{module_part}:{factory_name} must return list[Test]; got "
            f"{type(tests).__name__}.",
            err=True,
        )
        sys.exit(1)
    return tests


def _choose_test(tests: list[Test], name: str | None) -> Test:
    """Pick one :class:`Test` from *tests*.

    :param tests: List returned by the factory.
    :param name: Optional ``--test NAME`` filter; matches :attr:`Test.name`.
    :returns: The selected :class:`Test`.
    """
    if not tests:
        click.echo("Factory returned no tests.", err=True)
        sys.exit(1)

    if name is not None:
        for t in tests:
            if t.name == name:
                return t
        names = ", ".join(repr(t.name) for t in tests)
        click.echo(
            f"No test named {name!r}. Available: {names}.", err=True
        )
        sys.exit(1)

    if len(tests) == 1:
        return tests[0]

    click.echo("Multiple tests returned; pick one:")
    for i, t in enumerate(tests, start=1):
        click.echo(f"  [{i}] {t.name}")
    idx: int = click.prompt(
        "Test number",
        type=click.IntRange(min=1, max=len(tests)),
    )
    return tests[idx - 1]


@main.command("describe")
@click.argument("target")
def describe_cmd(target: str) -> None:
    """Pretty-print the networks and VMs defined by a test factory.

    Accepts the same ``MODULE:FACTORY`` form as ``run`` but never
    provisions anything — it just loads the factory, instantiates the
    orchestrator, and walks the declared topology::

        testrange describe examples/two_networks_three_vms.py:gen_tests
    """
    module_part, factory_name = _parse_target(target)
    module = _load_module(module_part)
    tests = _load_tests(module, module_part, factory_name)

    for idx, test in enumerate(tests):
        if idx > 0:
            click.echo()
        _print_test(test)


def _print_test(test: Test) -> None:
    """Pretty-print one :class:`~testrange.test.Test` as a network/VM tree."""
    from testrange.devices import HardDrive, Memory, VirtualNetworkRef, vCPU

    orch = test._orchestrator
    networks = orch._networks
    vms = orch._vm_list

    click.secho(f"Test: {test.name}", bold=True)

    click.echo(f"├── Networks ({len(networks)})")
    for i, net in enumerate(networks):
        last = i == len(networks) - 1
        trunk = "│   " if not last else "    "
        branch = "│   └──" if not last else "└───────"
        # Spacing to align with the network block below
        click.echo(f"│   {'└──' if last else '├──'} {click.style(net.name, fg='cyan', bold=True)}")
        rows = [
            ("subnet",    f"{net.subnet}  (gateway {net.gateway_ip})"),
            ("dhcp",      "yes" if net.dhcp else "no (all static)"),
            ("internet",  "yes (NAT egress)" if net.internet else "no (isolated)"),
            ("dns",       "yes (dnsmasq)" if net.dns else "no"),
        ]
        for j, (label, value) in enumerate(rows):
            last_row = j == len(rows) - 1
            tree = "└──" if last_row else "├──"
            click.echo(f"│   {trunk}{tree} {label:<9} {value}")

    click.echo(f"└── VMs ({len(vms)})")
    for i, vm in enumerate(vms):
        last = i == len(vms) - 1
        # Trunk carried by every inner line of this VM's block. Non-last
        # VMs get a vertical pipe so the tree keeps visual continuity;
        # the last VM's block is flush.
        vm_trunk = "        " if last else "    │   "
        vm_head = "    └──" if last else "    ├──"
        click.echo(
            f"{vm_head} {click.style(vm.name, fg='green', bold=True)}"
        )

        vcpu = next((d.count for d in vm.devices if isinstance(d, vCPU)), 2)
        mem = next((d.gib for d in vm.devices if isinstance(d, Memory)), 2.0)
        drives = [d for d in vm.devices if isinstance(d, HardDrive)]
        nics = [d for d in vm.devices if isinstance(d, VirtualNetworkRef)]

        if drives:
            disk_desc = ", ".join(
                f"{d.size}{' NVMe' if d.nvme else ''}" for d in drives
            )
        else:
            disk_desc = "20GB (default)"

        pkg_desc = (
            ", ".join(repr(p) for p in vm.pkgs) if vm.pkgs else "(none)"
        )
        user_desc = (
            ", ".join(
                f"{c.username}{'/sudo' if c.sudo else ''}" for c in vm.users
            )
            if vm.users else "(none)"
        )
        post = vm.post_install_cmds
        post_desc = f"{len(post)} command(s)" if post else "(none)"

        rows = [
            ("iso",           vm.iso),
            ("cpu",           f"{vcpu} vCPU"),
            ("memory",        f"{mem:g} GiB"),
            ("disk",          disk_desc),
            ("users",         user_desc),
            ("packages",      pkg_desc),
            ("post-install",  post_desc),
        ]
        for label, value in rows:
            click.echo(f"{vm_trunk}├── {label:<13} {value}")

        # NICs section is the last line of the VM block
        if not nics:
            click.echo(f"{vm_trunk}└── nics          (none)")
        else:
            click.echo(f"{vm_trunk}└── nics")
            for j, nic in enumerate(nics):
                last_nic = j == len(nics) - 1
                tree = "└──" if last_nic else "├──"
                # Resolve network metadata if the ref matches a declared net
                net = next((n for n in networks if n.name == nic.name), None)
                if nic.ip:
                    addr = f"static {nic.ip}"
                elif net is not None and net.dhcp:
                    addr = "DHCP"
                else:
                    addr = "auto-reserved"
                net_tag = click.style(nic.name, fg="cyan")
                click.echo(
                    f"{vm_trunk}    {tree} {net_tag:<20} ({addr})"
                )


@main.command("repl")
@click.argument("target")
@click.option(
    "--test",
    "test_name",
    default=None,
    help=(
        "Pick a specific test by name when the factory returns more than "
        "one. Without it, an interactive prompt asks which to use."
    ),
)
@click.option(
    "--keep",
    is_flag=True,
    help=(
        "Skip teardown on REPL exit; print backend-specific cleanup "
        "hints and the run dir so you can keep poking by hand."
    ),
)
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    default="INFO",
    help="Minimum level for TestRange stderr logs.",
)
@_orchestrator_option
def repl_cmd(
    target: str,
    test_name: str | None,
    keep: bool,
    log_level: str,
    orchestrator_url: str | None,
) -> None:
    """Provision a test plan and drop into a Python REPL.

    TARGET has the same ``MODULE:FACTORY`` form as ``run`` and
    ``describe``. The chosen :class:`~testrange.test.Test`'s orchestrator
    is started, then the REPL is launched with ``orch``, ``vms``, and
    one binding per VM (named after the VM) already in scope::

        testrange repl ./my_tests.py:gen_tests
        testrange repl examples/hello_world.py:gen_tests --test smoke
        testrange repl examples/two_networks_three_vms.py:gen_tests --keep

    Use ``--orchestrator`` to point at a remote backend::

        testrange repl tests:gen_tests \\
            --orchestrator qemu+ssh://alice@vmhost/system

    The REPL prefers IPython if installed, otherwise falls back to the
    standard library's ``code.InteractiveConsole`` (works everywhere).
    """
    configure_root_logger(getattr(logging, log_level.upper()))
    module_part, factory_name = _parse_target(target)
    module = _load_module(module_part)
    tests = _load_tests(module, module_part, factory_name)
    test = _choose_test(tests, test_name)
    orch = _resolve_orchestrator(test, orchestrator_url)
    if orchestrator_url is not None:
        test._orchestrator = orch

    orch.__enter__()
    try:
        start_repl(orch, test.name)
    finally:
        if keep:
            print_keep_summary(orch)
        else:
            orch.__exit__(None, None, None)


@main.command("cache-list")
@click.option("--cache-dir", type=click.Path(path_type=Path), default=None)
def cache_list(cache_dir: Path | None) -> None:
    """List cached VM images."""
    cache = CacheManager(root=cache_dir) if cache_dir else CacheManager()
    click.echo(f"Cache root: {cache.root}")

    click.echo("\nDownloaded base images:")
    for meta in sorted(cache.images_dir.glob("*.meta.json")):
        data = json.loads(meta.read_text())
        size_mb = data.get("size_bytes", 0) // (1024 * 1024)
        click.echo(f"  {data.get('url', '?')}  ({size_mb} MiB)")

    click.echo("\nInstalled VM images:")
    for manifest in sorted(cache.vms_dir.glob("*.json")):
        config_hash = manifest.stem
        data = json.loads(manifest.read_text())
        click.echo(
            f"  [{config_hash[:12]}]  {data.get('name', '?')} "
            f"({data.get('iso', '?')})"
        )


@main.command("cache-clear")
@click.option("--cache-dir", type=click.Path(path_type=Path), default=None)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def cache_clear(cache_dir: Path | None, yes: bool) -> None:
    """Delete all cached VM images.

    Does not affect running test runs.
    """

    cache = CacheManager(root=cache_dir) if cache_dir else CacheManager()
    if not yes:
        click.confirm(
            f"Delete all caches under {cache.root}?", abort=True
        )
    shutil.rmtree(cache.vms_dir, ignore_errors=True)
    cache.vms_dir.mkdir(parents=True, exist_ok=True)
    click.echo("VM image cache cleared.")
