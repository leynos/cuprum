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
    return (
        {
            "DEV_FAST_ARGV": str(argv_path),
            "DEV_FAST_CARGO": str(child),
            "DEV_FAST_CARGO_ENV": str(cargo_path),
            "DEV_FAST_CONFIG": str(repo_root() / FRAGMENT),
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


def test_bridge_injects_one_fragment_and_preserves_child_exit(tmp_path: Path) -> None:
    """The Maturin adapter preserves Cargo's argv, environment, and status."""
    environment, argv_path, cargo_path = _bridge_environment(tmp_path)
    result = _run_bridge(["rustc", "--lib"], environment)
    assert result.returncode == 47, "the adapter must preserve the Cargo exit status"
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "--config",
        environment["DEV_FAST_CONFIG"],
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
            environment["DEV_FAST_CONFIG"],
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


@pytest.mark.parametrize(
    ("missing", "expected_diagnostic"),
    [
        pytest.param("DEV_FAST_CARGO", "DEV_FAST_CARGO must name", id="missing_cargo"),
        pytest.param(
            "DEV_FAST_CONFIG", "DEV_FAST_CONFIG must name", id="missing_config"
        ),
    ],
)
def test_bridge_rejects_missing_required_environment(
    tmp_path: Path, missing: str, expected_diagnostic: str
) -> None:
    """The adapter refuses incomplete configuration before executing Cargo."""
    environment, argv_path, _ = _bridge_environment(tmp_path)
    del environment[missing]
    result = _run_bridge(["rustc"], environment)
    assert result.returncode == 2, f"missing {missing} must fail with usage status"
    assert expected_diagnostic in result.stderr, (
        f"missing {missing} must name the required environment variable"
    )
    assert not argv_path.exists(), "incomplete adapter state must not start Cargo"


def test_bridge_rejects_a_nonexistent_configuration(tmp_path: Path) -> None:
    """The adapter checks its selected configuration path before executing Cargo."""
    environment, argv_path, _ = _bridge_environment(tmp_path)
    environment["DEV_FAST_CONFIG"] = str(tmp_path / "missing.toml")
    result = _run_bridge(["rustc"], environment)
    assert result.returncode == 2, "a missing fragment must fail with usage status"
    assert "DEV_FAST_CONFIG must name" in result.stderr, (
        "the missing fragment diagnostic must name the broken boundary"
    )
    assert not argv_path.exists(), "an invalid fragment must not start Cargo"
