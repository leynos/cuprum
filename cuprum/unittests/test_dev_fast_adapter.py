"""Validate the Maturin Cargo adapter's process and configuration boundary."""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled adapter argv.
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.docs import repo_root

FRAGMENT = "tools/dev-fast/config.toml"
SAFE_CARGO_ARGUMENTS = st.sampled_from((
    "rustc",
    "--lib",
    "--package",
    "cuprum-streams",
    "--quiet",
))


def _adapter_fragment() -> str:
    """Return the fragment path the adapter resolves from its own location."""
    return str((repo_root() / FRAGMENT).resolve())


def _bridge_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Create a recording Cargo child for the adapter's process contract."""
    argv_path = tmp_path / "argv"
    cargo_path = tmp_path / "cargo"
    child = tmp_path / "cargo-child"
    child.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$DEV_FAST_ARGV"\n'
        'printf \'%s\\n\' "$CARGO" > "$DEV_FAST_CARGO_ENV"\n'
        "exit 47\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    # DEV_FAST_CONFIG is deliberately absent: the adapter derives the fragment
    # from its own location, so the variable is not part of its interface.
    return (
        {
            "DEV_FAST_ARGV": str(argv_path),
            "DEV_FAST_CARGO": str(child),
            "DEV_FAST_CARGO_ENV": str(cargo_path),
        },
        argv_path,
        cargo_path,
    )


def _run_bridge(
    arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the controlled adapter process with captured diagnostics."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed adapter argv.
        [repo_root() / "tools/dev-fast/cargo", *arguments],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env={**os.environ, **environment},
        text=True,
    )


def _run_bridge_relatively(
    arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the adapter exactly as the Makefile does: by relative path."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed adapter argv.
        [f"./{FRAGMENT.rsplit('/', 1)[0]}/cargo", *arguments],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env={**os.environ, **environment},
        text=True,
    )


def test_bridge_resolves_the_fragment_from_a_relative_invocation(
    tmp_path: Path,
) -> None:
    """A relative adapter path resolves to the repository fragment, not cwd."""
    environment, argv_path, _ = _bridge_environment(tmp_path)
    result = _run_bridge_relatively(["rustc", "--lib"], environment)
    assert result.returncode == 47, "the relative invocation must reach Cargo"
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "--config",
        _adapter_fragment(),
        "rustc",
        "--lib",
    ], "the adapter must resolve its fragment independently of the invocation form"


def test_bridge_injects_one_fragment_and_preserves_child_exit(tmp_path: Path) -> None:
    """The Maturin adapter preserves Cargo's argv, environment, and status."""
    environment, argv_path, cargo_path = _bridge_environment(tmp_path)
    result = _run_bridge(["rustc", "--lib"], environment)
    assert result.returncode == 47, "the adapter must preserve the Cargo exit status"
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "--config",
        _adapter_fragment(),
        "rustc",
        "--lib",
    ], "the adapter must prepend only its approved fragment"
    assert (
        cargo_path.read_text(encoding="utf-8").strip() == environment["DEV_FAST_CARGO"]
    ), "Cargo children must receive the real executable, not the adapter"


@settings(max_examples=24, deadline=None)
@given(st.lists(SAFE_CARGO_ARGUMENTS, min_size=1, max_size=7))
def test_bridge_preserves_generated_cargo_arguments(arguments: list[str]) -> None:
    """The adapter preserves each safe generated Cargo argv sequence exactly."""
    with tempfile.TemporaryDirectory() as directory:
        environment, argv_path, _ = _bridge_environment(Path(directory))
        result = _run_bridge(arguments, environment)
        assert result.returncode == 47, f"the child status changed for {arguments!r}"
        assert argv_path.read_text(encoding="utf-8").splitlines() == [
            "--config",
            _adapter_fragment(),
            *arguments,
        ], f"the adapter changed generated Cargo arguments {arguments!r}"


@pytest.mark.parametrize("config_flag", ["--config", "--config=other.toml"])
def test_bridge_rejects_duplicate_configuration(
    tmp_path: Path, config_flag: str
) -> None:
    """A caller cannot override or duplicate the approved configuration."""
    environment, argv_path, _ = _bridge_environment(tmp_path)
    command = ["rustc", config_flag]
    if config_flag == "--config":
        command.append("other.toml")
    result = _run_bridge(command, environment)
    assert result.returncode == 2, "a duplicate configuration must be rejected"
    assert "Cargo configuration is selected" in result.stderr, (
        "the duplicate configuration diagnostic must explain the owning boundary"
    )
    assert not argv_path.exists(), "the adapter must reject before starting Cargo"


@settings(max_examples=24, deadline=None)
@given(
    st.lists(SAFE_CARGO_ARGUMENTS, max_size=4),
    st.sampled_from(("--config", "--config=other.toml")),
    st.lists(SAFE_CARGO_ARGUMENTS, max_size=4),
)
def test_bridge_rejects_generated_configuration_flags(
    prefix: list[str], config_flag: str, suffix: list[str]
) -> None:
    """Every generated configuration spelling is rejected before Cargo starts."""
    with tempfile.TemporaryDirectory() as directory:
        environment, argv_path, _ = _bridge_environment(Path(directory))
        arguments = [
            *prefix,
            config_flag,
            *(["other.toml"] if config_flag == "--config" else []),
            *suffix,
        ]
        result = _run_bridge(arguments, environment)
        assert result.returncode == 2, (
            f"configuration flag escaped rejection: {arguments!r}"
        )
        assert not argv_path.exists(), "a rejected invocation must not start Cargo"


def test_bridge_rejects_missing_required_environment(tmp_path: Path) -> None:
    """The adapter refuses incomplete configuration before executing Cargo."""
    environment, argv_path, _ = _bridge_environment(tmp_path)
    del environment["DEV_FAST_CARGO"]
    result = _run_bridge(["rustc"], environment)
    assert result.returncode == 2, "missing DEV_FAST_CARGO must fail with usage status"
    assert "DEV_FAST_CARGO must name" in result.stderr, (
        "missing DEV_FAST_CARGO must name the required environment variable"
    )
    assert not argv_path.exists(), "incomplete adapter state must not start Cargo"


def test_bridge_ignores_a_caller_supplied_configuration(tmp_path: Path) -> None:
    """The adapter's fragment follows from its location, not the environment.

    Both candidates are offered, because they fail differently: an existing
    file outside the repository defeats a bare `-f` existence guard, and a
    path inside the repository defeats an attempt to shadow the fragment
    without leaving the checkout.
    """
    outside = tmp_path / "other-config.toml"
    outside.write_text("[profile.dev]\n", encoding="utf-8")
    for redirect in (outside, repo_root() / "other-config.toml"):
        environment, argv_path, _ = _bridge_environment(tmp_path)
        environment["DEV_FAST_CONFIG"] = str(redirect)
        result = _run_bridge(["rustc"], environment)
        assert result.returncode == 47, (
            f"a caller-supplied DEV_FAST_CONFIG must not change behaviour: {redirect}"
        )
        assert argv_path.read_text(encoding="utf-8").splitlines() == [
            "--config",
            _adapter_fragment(),
            "rustc",
        ], f"the adapter must ignore {redirect} and select its own fragment"
