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
    "constructed.  Each backend in :mod:`testrange.backends` "
    "self-describes the URL shapes it accepts — see its module "
    "docstring for the exact form.  Omit to keep the test's own "
    "orchestrator."
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

    TARGET has the form ``MODULE[:FACTORY]`` where MODULE is either a
    dotted module name (``mypkg.tests``) or a path to a Python file
    (``./tests.py``), and FACTORY is a zero-argument callable that
    returns a ``list`` of :class:`~testrange.test.Test` objects.

    FACTORY defaults to ``gen_tests`` when omitted.

    Example::

        testrange run ./my_tests.py                # uses gen_tests
        testrange run ./my_tests.py:other_factory  # explicit override

    Target a different hypervisor::

        testrange run tests \\
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


_DEFAULT_FACTORY = "gen_tests"


def _parse_target(target: str) -> tuple[str, str]:
    """Split a ``MODULE[:FACTORY]`` string into its two parts.

    The factory suffix is optional; if the user writes just ``MODULE``
    (or ``./path/to/tests.py``) the factory defaults to
    :data:`_DEFAULT_FACTORY` — the conventional ``gen_tests`` name
    used across every TestRange example.  Writing an explicit trailing
    colon with nothing after it (``path.py:``) is still rejected so
    typos don't silently succeed.

    :param target: Raw user-supplied target string.
    :returns: ``(module_part, factory_name)`` tuple.
    """
    module_part, sep, factory_name = target.partition(":")
    if not module_part:
        click.echo(
            f"TARGET must be 'module[:factory]' (got {target!r}).",
            err=True,
        )
        sys.exit(2)
    if sep and not factory_name:
        click.echo(
            f"TARGET has empty factory name (got {target!r}); either "
            "drop the trailing ':' to use the default 'gen_tests', or "
            "name a factory.",
            err=True,
        )
        sys.exit(2)
    return module_part, factory_name or _DEFAULT_FACTORY


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

    Accepts the same ``MODULE[:FACTORY]`` form as ``run`` (factory
    defaults to ``gen_tests``) but never provisions anything — it just
    loads the factory, instantiates the orchestrator, and walks the
    declared topology::

        testrange describe examples/two_networks_three_vms.py
    """
    module_part, factory_name = _parse_target(target)
    module = _load_module(module_part)
    tests = _load_tests(module, module_part, factory_name)

    for idx, test in enumerate(tests):
        if idx > 0:
            click.echo()
        _print_test(test)


def _print_test(test: Test) -> None:
    """Pretty-print one :class:`~testrange.test.Test` as a network/VM tree.

    Hypervisor VMs render recursively — their ``Inner networks`` and
    ``Inner VMs`` sections hang off the hypervisor's block, indented
    one level deeper.  No orchestrator is entered; the pretty-printer
    walks specs only.
    """
    orch = test._orchestrator
    networks = orch._networks
    vms = orch._vm_list

    click.secho(f"Test: {test.name}", bold=True)

    # Networks block (always present, never last — VMs follow).
    _print_networks_block(networks, trunk="", is_last=False)
    # VMs block (always last under the Test header).
    _print_vms_block(vms, networks, trunk="", is_last=True)


def _print_networks_block(
    networks: list,
    *,
    trunk: str,
    is_last: bool,
    label: str = "Networks",
) -> None:
    """Print one ``Networks (N)`` section under ``trunk``.

    :param networks: Sequence of
        :class:`~testrange.networks.base.AbstractVirtualNetwork`.
    :param trunk: Tree-drawing prefix inherited from the parent block.
        Every line this function emits starts with ``trunk + …``.
    :param is_last: Whether this block is the final sibling under its
        parent.  Controls whether we draw ``├──`` / ``└──`` at the
        block header, and what the per-network child lines use as
        their own continuation pipe.
    :param label: Block header text — overridden for inner layers
        (``"Inner networks"``).
    """
    head = "└──" if is_last else "├──"
    child_trunk = trunk + ("    " if is_last else "│   ")
    click.echo(f"{trunk}{head} {label} ({len(networks)})")
    for i, net in enumerate(networks):
        last_net = i == len(networks) - 1
        net_head = "└──" if last_net else "├──"
        net_child_trunk = child_trunk + ("    " if last_net else "│   ")
        click.echo(
            f"{child_trunk}{net_head} "
            f"{click.style(net.name, fg='cyan', bold=True)}"
        )
        rows = [
            ("subnet",    f"{net.subnet}  (gateway {net.gateway_ip})"),
            ("dhcp",      "yes" if net.dhcp else "no (all static)"),
            ("internet",  "yes (NAT egress)" if net.internet else "no (isolated)"),
            ("dns",       "yes (dnsmasq)" if net.dns else "no"),
        ]
        for j, (k, v) in enumerate(rows):
            last_row = j == len(rows) - 1
            row_head = "└──" if last_row else "├──"
            click.echo(f"{net_child_trunk}{row_head} {k:<9} {v}")


def _print_vms_block(
    vms: list,
    networks: list,
    *,
    trunk: str,
    is_last: bool,
    label: str = "VMs",
) -> None:
    """Print one ``VMs (N)`` section under ``trunk``.

    Hypervisor VMs recurse into their inner networks + inner VMs via
    :func:`_print_networks_block` / :func:`_print_vms_block` with a
    deeper trunk.
    """
    head = "└──" if is_last else "├──"
    child_trunk = trunk + ("    " if is_last else "│   ")
    click.echo(f"{trunk}{head} {label} ({len(vms)})")
    for i, vm in enumerate(vms):
        last_vm = i == len(vms) - 1
        _print_single_vm(
            vm,
            networks,
            trunk=child_trunk,
            is_last=last_vm,
        )


def _print_single_vm(
    vm,
    networks: list,
    *,
    trunk: str,
    is_last: bool,
) -> None:
    """Render one VM's block — iso, cpu, memory, disks, users, pkgs,
    post-install, nics.  When ``vm`` is an
    :class:`~testrange.vms.hypervisor_base.AbstractHypervisor`,
    appends an ``Inner networks`` + ``Inner VMs`` section.
    """
    from testrange.devices import (
        AbstractHardDrive,
        AbstractVNIC,
        Memory,
        vCPU,
    )
    from testrange.vms.hypervisor_base import AbstractHypervisor

    vm_head = "└──" if is_last else "├──"
    vm_trunk = trunk + ("    " if is_last else "│   ")

    # Header line: mark hypervisors so the topology is obvious at a
    # glance even without scrolling to the inner sections.
    if isinstance(vm, AbstractHypervisor):
        tag = click.style(
            f" [Hypervisor → {vm.orchestrator.__name__}]",
            fg="magenta",
        )
    else:
        tag = ""
    click.echo(
        f"{trunk}{vm_head} "
        f"{click.style(vm.name, fg='green', bold=True)}{tag}"
    )

    vcpu = next((d.count for d in vm.devices if isinstance(d, vCPU)), 2)
    mem = next((d.gib for d in vm.devices if isinstance(d, Memory)), 2.0)
    drives = [d for d in vm.devices if isinstance(d, AbstractHardDrive)]
    nics = [
        d for d in vm.devices if isinstance(d, AbstractVNIC)
    ]

    disk_desc = (
        ", ".join(f"{d.size}{d.display_tag()}" for d in drives)
        if drives else "20GB (default)"
    )
    pkg_desc = (
        ", ".join(repr(p) for p in vm.pkgs) if vm.pkgs else "(none)"
    )
    user_desc = (
        ", ".join(
            f"{c.username}{'/sudo' if c.sudo else ''}" for c in vm.users
        )
        if vm.users else "(none)"
    )
    post_desc = (
        f"{len(vm.post_install_cmds)} command(s)"
        if vm.post_install_cmds else "(none)"
    )

    rows = [
        ("iso",           vm.iso),
        ("cpu",           f"{vcpu} vCPU"),
        ("memory",        f"{mem:g} GiB"),
        ("disk",          disk_desc),
        ("users",         user_desc),
        ("packages",      pkg_desc),
        ("post-install",  post_desc),
    ]

    # All VM rows use ``├──`` — the final row is either ``nics`` (for
    # plain VMs) or a nested inner block (for hypervisors) which owns
    # the ``└──`` terminator of the VM's block.
    for k, v in rows:
        click.echo(f"{vm_trunk}├── {k:<13} {v}")

    is_hv = isinstance(vm, AbstractHypervisor)
    # nics: ``└──`` for plain VMs; ``├──`` when a hypervisor block
    # still has inner sections to emit below.
    if not nics:
        click.echo(
            f"{vm_trunk}{'├──' if is_hv else '└──'} nics          (none)"
        )
    else:
        click.echo(f"{vm_trunk}{'├──' if is_hv else '└──'} nics")
        nic_child_trunk = vm_trunk + ("│   " if is_hv else "    ")
        for j, nic in enumerate(nics):
            last_nic = j == len(nics) - 1
            nic_head = "└──" if last_nic else "├──"
            # Resolve against the scope the nic was declared in:
            # an inner VM's ref matches the hypervisor's own inner
            # networks, not the outer ones — ``networks`` is already
            # the correct scope by the time we recurse.
            net = next((n for n in networks if n.name == nic.ref), None)
            if nic.ip:
                addr = f"static {nic.ip}"
            elif net is not None and net.dhcp:
                addr = "DHCP"
            else:
                addr = "auto-reserved"
            net_tag = click.style(nic.ref, fg="cyan")
            click.echo(f"{nic_child_trunk}{nic_head} {net_tag:<20} ({addr})")

    # Hypervisor recursion: inner networks (not last) + inner VMs (last).
    if is_hv:
        _print_networks_block(
            vm.networks,
            trunk=vm_trunk,
            is_last=False,
            label="Inner networks",
        )
        _print_vms_block(
            vm.vms,
            vm.networks,
            trunk=vm_trunk,
            is_last=True,
            label="Inner VMs",
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

    TARGET has the same ``MODULE[:FACTORY]`` form as ``run`` and
    ``describe`` (factory defaults to ``gen_tests``). The chosen
    :class:`~testrange.test.Test`'s orchestrator is started, then the
    REPL is launched with ``orch``, ``vms``, and one binding per VM
    (named after the VM) already in scope::

        testrange repl ./my_tests.py
        testrange repl examples/hello_world.py --test smoke
        testrange repl examples/two_networks_three_vms.py --keep

    Use ``--orchestrator`` to point at a remote backend; the URL
    format is documented in each backend module under
    :mod:`testrange.backends`.

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


@main.command("cleanup")
@click.argument("target")
@click.argument("run_id")
@_orchestrator_option
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    default="INFO",
    help="Minimum level for TestRange stderr logs.",
)
def cleanup_cmd(
    target: str,
    run_id: str,
    orchestrator_url: str | None,
    log_level: str,
) -> None:
    """Tear down resources from a leaked test run.

    TARGET is the same ``MODULE[:FACTORY]`` shape as ``testrange run``.
    RUN_ID is the UUID4 from the original run (find it in the run's log
    output or in ``<cache_root>/runs/<run_id>/``).

    Reconstructs every resource the test factory + run id would have
    produced and best-effort destroys each.  Idempotent; safe to run
    repeatedly.

    Use after a ``kill -9 <testrange-pid>``, OOM-killer hit, host
    reboot mid-run, or anything else that prevented the orchestrator's
    ``__exit__`` from running.

    Example::

        testrange cleanup ./my_tests.py:gen_tests \\
            deadbeef-1111-2222-3333-444455556666
    """
    configure_root_logger(getattr(logging, log_level.upper()))
    module_part, factory_name = _parse_target(target)
    module = _load_module(module_part)
    tests = _load_tests(module, module_part, factory_name)

    failures = 0
    for test in tests:
        orch = _resolve_orchestrator(test, orchestrator_url)
        try:
            orch.cleanup(run_id)
        except NotImplementedError as exc:
            click.echo(f"  {test.name}: {exc}", err=True)
            failures += 1
        except Exception as exc:  # noqa: BLE001 — surface anything as a CLI error
            click.echo(
                f"  {test.name}: cleanup failed: {exc}", err=True,
            )
            failures += 1
        else:
            click.echo(f"  {test.name}: cleanup ok")

    if failures:
        sys.exit(1)


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


_PROXMOX_URL_HELP = (
    "PVE connection URL.  Same shape ``--orchestrator`` uses elsewhere:\n"
    "\n"
    "\b\n"
    "  proxmox://USER:PASS@HOST[:PORT]/NODE[?storage=NAME]\n"
    "  proxmox://TOKENID:SECRET@HOST/NODE\n"
)


def _build_proxmox_orchestrator(url: str):
    """Construct a :class:`ProxmoxOrchestrator` from *url*.

    Helper for the ``proxmox-list-templates`` / ``-prune-templates``
    commands.  Raises ``click.BadParameter`` on a non-proxmox URL so
    the error surfaces as a clean usage message rather than a stack
    trace."""
    from testrange.backends.proxmox import ProxmoxOrchestrator
    from testrange.backends.proxmox import cli_build_orchestrator as _build

    # cli_build_orchestrator wants an existing orchestrator to copy
    # vms/networks from; for these admin commands neither is needed,
    # so pass a bare ProxmoxOrchestrator(host="placeholder").
    placeholder = ProxmoxOrchestrator(host="placeholder")
    orch = _build(url, placeholder)
    if orch is None:
        raise click.BadParameter(
            f"not a proxmox URL: {url!r}.  Expected "
            f"``proxmox://...``."
        )
    return orch


@main.command("proxmox-list-templates")
@click.option("--orchestrator", "url", required=True, help=_PROXMOX_URL_HELP)
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    default="WARNING",
    help="Minimum level for TestRange stderr logs.",
)
def proxmox_list_templates(url: str, log_level: str) -> None:
    """List TestRange-managed PVE templates on a Proxmox node.

    Each template is the cached install for one VM spec
    (``tr-template-<config_hash[:12]>``); a hit on a future
    ``testrange run`` skips the install entirely and clones from
    the matching template.

    Use ``proxmox-prune-templates`` to evict.
    """
    configure_root_logger(getattr(logging, log_level.upper()))
    orch = _build_proxmox_orchestrator(url)
    templates = orch.list_templates()
    if not templates:
        click.echo("No TestRange templates on this PVE node.")
        return
    click.echo(f"{len(templates)} TestRange template(s):")
    for t in templates:
        click.echo(f"  VMID {t['vmid']}  {t['name']}")


@main.command("proxmox-prune-templates")
@click.option("--orchestrator", "url", required=True, help=_PROXMOX_URL_HELP)
@click.option(
    "--name",
    "names",
    multiple=True,
    help=(
        "Restrict prune to templates with these display names.  "
        "Repeatable.  Omit to prune all ``tr-template-*`` VMIDs."
    ),
)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    default="INFO",
    help="Minimum level for TestRange stderr logs.",
)
def proxmox_prune_templates(
    url: str, names: tuple[str, ...], yes: bool, log_level: str,
) -> None:
    """Delete TestRange-managed PVE templates from a Proxmox node.

    Without ``--name``, deletes EVERY ``tr-template-*`` VMID — the
    full proxmox-side cache wipe.  Use ``--name=...`` (repeatable)
    to evict specific templates only.

    Per-VM run clones are not touched; this command only manages
    the persistent template cache.
    """
    configure_root_logger(getattr(logging, log_level.upper()))
    orch = _build_proxmox_orchestrator(url)
    targets = orch.list_templates()
    if names:
        wanted = set(names)
        targets = [t for t in targets if t["name"] in wanted]

    if not targets:
        click.echo("Nothing to prune.")
        return

    if not yes:
        click.echo(f"Will delete {len(targets)} template(s):")
        for t in targets:
            click.echo(f"  VMID {t['vmid']}  {t['name']}")
        click.confirm("Proceed?", abort=True)

    deleted = orch.prune_templates(names=list(names) if names else None)
    click.echo(f"Pruned {deleted} template(s).")
