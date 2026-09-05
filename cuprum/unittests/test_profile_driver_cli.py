"""Integration tests for the legacy profile-driver CLI."""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - integration tests exercise fixed CLI commands.
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import pathlib as pth


def _run_profile_cli(*args: str) -> int:
    """Invoke the profile driver module and return its exit code."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-m", "benchmarks.profile_tee_hotpath", *args],
        check=False,
        timeout=30,
    )
    return completed.returncode


@pytest.mark.parametrize(
    ("subcommand_args", "description"),
    [
        pytest.param(
            ("run-scenario", "--scenario", "echo-devnull-nocb-s1"),
            "run-scenario with missing fixture",
            id="scenario-worker-failure",
        ),
        pytest.param(
            ("run",),
            "run matrix with missing fixtures",
            id="matrix-failure",
        ),
    ],
)
def test_profile_cli_returns_failure_exit_code(
    tmp_path: pth.Path,
    subcommand_args: tuple[str, ...],
    description: str,
) -> None:
    """Profile CLI returns non-zero when the worker fails."""
    missing = tmp_path / "no_such_fixture.b64"
    wrapped = tmp_path / "no_such_wrapped.b64"
    exit_code = _run_profile_cli(
        "--fixture",
        str(missing),
        "--wrapped-fixture",
        str(wrapped),
        "--output-dir",
        str(tmp_path / "profiles"),
        "--profiler",
        "none",
        "--warmup-count",
        "0",
        "--repeat-count",
        "1",
        *subcommand_args,
    )
    assert exit_code != 0, (
        f"expected non-zero exit code for {description}, got {exit_code}"
    )
