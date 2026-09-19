"""Exercise dev-fast prerequisites and one routed build command end to end."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled Make argv.
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path


FRAGMENT = "tools/dev-fast/config.toml"


@pytest.fixture
def mold_version() -> str:
    """Return the linker version recorded by the checked-in pin."""
    return (repo_root() / "tools/mold/VERSION").read_text(encoding="utf-8").strip()


def _installed_components(*components: str) -> str:
    """Emit the component listing a provisioned toolchain reports."""
    listed = " ".join(f"'{component}'" for component in components)
    return f"printf '%s\\n' {listed}"


def _write_program(directory: Path, name: str, body: str) -> None:
    """Create one controlled executable used to mutate a prerequisite."""
    program = directory / name
    program.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    program.chmod(0o755)


class _FailureCase(typ.NamedTuple):
    """One prerequisite mutation and the diagnostic its failure must report."""

    programs: dict[str, str]
    variables: dict[str, str]
    expected_diagnostic: str


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _FailureCase({}, {}, "mold {version} is required"),
            id="missing_mold",
        ),
        pytest.param(
            _FailureCase(
                {
                    "mold": "printf '%s\\n' 'mold 2.41.00'",
                    "rustup": "exit 0",
                },
                {},
                "mold {version} is required",
            ),
            id="wrong_mold_version",
        ),
        pytest.param(
            _FailureCase(
                {
                    "mold": "printf '%s\\n' 'mold {version}'",
                    "rustup": "exit 0",
                },
                {},
                "install rustc-codegen-cranelift",
            ),
            id="missing_component",
        ),
        pytest.param(
            _FailureCase(
                {
                    "mold": "printf '%s\\n' 'mold {version}'",
                    "rustup": _installed_components("rustc-codegen-cranelift"),
                },
                {},
                "install clippy",
            ),
            id="missing_clippy",
        ),
    ],
)
def test_prerequisite_recipe_fails_closed_for_missing_dependencies(
    tmp_path: Path, case: _FailureCase, mold_version: str
) -> None:
    """Missing or mismatched prerequisites are hard failures with useful output."""
    for name, body in case.programs.items():
        _write_program(tmp_path, name, body.format(version=mold_version))
    make = shutil.which("make")
    assert make is not None, "the prerequisite contract requires GNU Make on PATH"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "dev-fast-check",
            "DEV_FAST_HOST_IS_LINUX=yes",
            *(f"{key}={value}" for key, value in case.variables.items()),
        ],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        text=True,
    )
    assert result.returncode != 0, "missing prerequisites must fail closed"
    assert case.expected_diagnostic.format(version=mold_version) in result.stderr, (
        f"the prerequisite failure must explain how to repair it: {result.stderr}"
    )


def test_prerequisite_recipe_accepts_the_pinned_dependencies(
    tmp_path: Path, mold_version: str
) -> None:
    """The closed prerequisite gate accepts exactly the pinned linker and components."""
    _write_program(tmp_path, "mold", f"printf '%s\\n' 'mold {mold_version}'")
    _write_program(
        tmp_path,
        "rustup",
        _installed_components("rustc-codegen-cranelift", "clippy"),
    )
    make = shutil.which("make")
    assert make is not None, "the prerequisite contract requires GNU Make on PATH"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [make, "dev-fast-check", "DEV_FAST_HOST_IS_LINUX=yes"],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "delivery",
    [
        pytest.param("command_line", id="command_line"),
        pytest.param("environment", id="environment"),
    ],
)
def test_recipe_ignores_an_overridden_configuration(
    tmp_path: Path, delivery: str
) -> None:
    """The evaluated route keeps the repository fragment whatever the caller sets.

    Both delivery mechanisms are exercised. A definition that merely looks safe
    can still yield to an environment variable or to a command-line variable,
    so proving only one of them leaves the other as an open redirect.
    """
    redirection = str(tmp_path / "other-config.toml")
    variables = [f"DEV_FAST_RUST_CONFIG={redirection}"]
    environment = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}
    if delivery == "command_line":
        extra_arguments = variables
    else:
        extra_arguments = []
        environment["DEV_FAST_RUST_CONFIG"] = redirection
    make = shutil.which("make")
    assert make is not None, "the override contract requires GNU Make on PATH"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--dry-run",
            "dev-build",
            "DEV_FAST_HOST_IS_LINUX=yes",
            *extra_arguments,
        ],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert redirection not in result.stdout, (
        "an overridden configuration path must not reach the evaluated command"
    )
    assert f"--config ../{FRAGMENT}" in result.stdout, (
        "the route must select the repository fragment despite the override"
    )


def test_dev_build_executes_the_approved_fragment(
    tmp_path: Path, mold_version: str
) -> None:
    """A Linux debug build invokes Cargo once with the explicit approved fragment."""
    _write_program(tmp_path, "mold", f"printf '%s\\n' 'mold {mold_version}'")
    _write_program(
        tmp_path,
        "rustup",
        _installed_components("rustc-codegen-cranelift", "clippy"),
    )
    invocation = tmp_path / "cargo-invocation"
    cargo = tmp_path / "cargo"
    cargo.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\n\' "$@" > "$DEV_FAST_TEST_CARGO_ARGV"\n',
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    make = shutil.which("make")
    assert make is not None, "the routed-build contract requires GNU Make on PATH"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "dev-build",
            f"CARGO={cargo}",
            "DEV_FAST_HOST_IS_LINUX=yes",
        ],
        check=False,
        capture_output=True,
        cwd=repo_root(),
        env={
            **os.environ,
            "DEV_FAST_TEST_CARGO_ARGV": str(invocation),
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert invocation.read_text(encoding="utf-8").splitlines() == [
        "--config",
        "../tools/dev-fast/config.toml",
        "build",
        "--all-targets",
        "--all-features",
    ], "the executed debug build must select exactly the approved configuration"
