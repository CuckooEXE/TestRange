"""Unit tests for :mod:`testrange.packages`."""

from __future__ import annotations

import pytest

from testrange.packages import (
    AbstractPackage,
    Apt,
    Dnf,
    Homebrew,
    Pip,
    Winget,
)


class TestAbstractPackage:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            AbstractPackage("x")  # type: ignore[abstract]

    def test_base_repr_uses_subclass_name(self) -> None:
        assert repr(Apt("nginx")) == "Apt('nginx')"
        assert repr(Dnf("nginx")) == "Dnf('nginx')"


class TestApt:
    def test_package_manager(self) -> None:
        assert Apt("nginx").package_manager == "apt"

    def test_native_name_returns_self(self) -> None:
        assert Apt("docker.io").native_package_name() == "docker.io"

    def test_install_commands_empty(self) -> None:
        assert Apt("nginx").install_commands() == []


class TestDnf:
    def test_package_manager(self) -> None:
        assert Dnf("nginx").package_manager == "dnf"

    def test_native_name_returns_self(self) -> None:
        assert Dnf("podman").native_package_name() == "podman"

    def test_install_commands_empty(self) -> None:
        assert Dnf("nginx").install_commands() == []


class TestHomebrew:
    def test_package_manager(self) -> None:
        assert Homebrew("gh").package_manager == "brew"

    def test_native_name_none(self) -> None:
        assert Homebrew("gh").native_package_name() is None

    def test_install_commands_template_contains_brew_user_placeholder(self) -> None:
        cmds = Homebrew("gh").install_commands()
        assert len(cmds) == 1
        assert "{brew_user}" in cmds[0]
        assert "brew install gh" in cmds[0]

    def test_install_homebrew_command_has_user_placeholder(self) -> None:
        cmd = Homebrew.install_homebrew_command()
        assert "{user}" in cmd
        assert "raw.githubusercontent.com/Homebrew/install" in cmd


class TestPip:
    def test_package_manager(self) -> None:
        assert Pip("requests").package_manager == "pip"

    def test_native_name_none(self) -> None:
        assert Pip("requests").native_package_name() is None

    def test_system_install_command(self) -> None:
        cmds = Pip("requests").install_commands()
        assert cmds == ["pip3 install requests"]

    def test_user_install_command(self) -> None:
        cmds = Pip("requests", user_install=True).install_commands()
        assert cmds == ["pip3 install --user requests"]

    def test_user_install_flag_stored(self) -> None:
        assert Pip("x").user_install is False
        assert Pip("x", user_install=True).user_install is True

    def test_repr_default(self) -> None:
        assert repr(Pip("requests")) == "Pip('requests')"

    def test_repr_user_install(self) -> None:
        assert repr(Pip("requests", user_install=True)) == "Pip('requests', user_install=True)"


class TestWinget:
    def test_package_manager(self) -> None:
        assert Winget("Git.Git").package_manager == "winget"

    def test_native_name_none(self) -> None:
        assert Winget("Git.Git").native_package_name() is None

    def test_install_command_includes_silent_flags(self) -> None:
        cmds = Winget("Git.Git").install_commands()
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "winget install --id Git.Git" in cmd
        assert "--silent" in cmd
        assert "--accept-package-agreements" in cmd
        assert "--accept-source-agreements" in cmd


class TestCrossImplementationInvariants:
    """Properties that must hold for every package implementation."""

    @pytest.fixture(
        params=[
            Apt("nginx"),
            Dnf("nginx"),
            Homebrew("gh"),
            Pip("requests"),
            Winget("Git.Git"),
        ],
        ids=lambda p: type(p).__name__,
    )
    def pkg(self, request: pytest.FixtureRequest) -> AbstractPackage:
        return request.param

    def test_has_name(self, pkg: AbstractPackage) -> None:
        assert pkg.name

    def test_package_manager_is_nonempty_string(self, pkg: AbstractPackage) -> None:
        assert isinstance(pkg.package_manager, str)
        assert pkg.package_manager

    def test_install_commands_is_list_of_strings(self, pkg: AbstractPackage) -> None:
        cmds = pkg.install_commands()
        assert isinstance(cmds, list)
        assert all(isinstance(c, str) for c in cmds)

    def test_native_or_install_but_not_both(self, pkg: AbstractPackage) -> None:
        """A package either uses the native manifest (returns non-None) or
        emits install_commands — not both."""
        native = pkg.native_package_name()
        cmds = pkg.install_commands()
        if native is not None:
            assert cmds == []
        else:
            assert cmds
