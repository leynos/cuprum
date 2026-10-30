"""Run the modern-stable Make route and read its expanded recipes.

The modern-stable contract tests need two things from `make` that the Makefile
text alone cannot give: the commands a target will actually run after every
variable is expanded, and which of those variables a caller is forbidden to
override. Both are claims about `make`, not about the route's policy, so they
live here and leave the test modules to assert the policy.

`dry_run` and `makefile_text` read the repository's own Makefile from
`repo_root()`. `assert_command_variables_are_protected` asks `make` for the
`$(origin ...)` of the guard and test commands after a caller supplies hostile
values; `origin` reports `override` only for an `override` directive, which is
the property under test.

Scope: the modern-stable route contract. Other Make-contract modules keep
their own local dry-run helpers; consolidating those is separate work, and
this module deliberately makes no claim about routes it does not exercise.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Make argv.

from tests.helpers.docs import repo_root

__all__ = (
    "assert_command_variables_are_protected",
    "dry_run",
    "makefile_text",
)

#: A throwaway target that reports how `make` resolved the two command
#: variables. It is passed with `--eval`, so the repository Makefile is never
#: edited. Each `$(origin ...)` is paired with a `$(value ...)` probe so a
#: variable that stayed overridden for some other reason still fails.
_OVERRIDE_INSPECTION_TARGET = (
    "modern-stable-override-contract:\n"
    "\t@printf '%s\\n' "
    "'$(origin MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND)' "
    "'$(if $(findstring rustup component list,$(value "
    "MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND)),preserved,missing)' "
    "'$(origin MODERN_STABLE_TEST_COMMAND)' "
    "'$(if $(findstring nextest run,$(value "
    "MODERN_STABLE_TEST_COMMAND)),preserved,missing)'\n"
)


def dry_run(*goals: str, variables: dict[str, str] | None = None) -> str:
    """Return Make's expanded recipes for ``goals``.

    Parameters
    ----------
    *goals : str
        Make goals to expand, in invocation order.
    variables : dict[str, str] | None
        Command-line variable overrides simulating a caller's environment.

    Returns
    -------
    str
        Standard output of the dry run, one expanded recipe line per command.

    Raises
    ------
    AssertionError
        If GNU Make is not on `PATH`.
    """
    make = shutil.which("make")
    if make is None:
        message = "the routing contract requires GNU Make on PATH"
        raise AssertionError(message)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "CARGO=probe-cargo",
            *(f"{key}={value}" for key, value in (variables or {}).items()),
            *goals,
        ],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    return completed.stdout


def makefile_text() -> str:
    """Return the repository Makefile text for static route assertions."""
    return (repo_root() / "Makefile").read_text(encoding="utf-8")


def assert_command_variables_are_protected() -> None:
    """Require caller values not to replace the guard or test command.

    Raises
    ------
    AssertionError
        If GNU Make is unavailable, the inspection target fails, or either
        command variable is resolved without the Makefile's `override`.
    """
    make = shutil.which("make")
    if make is None:
        message = "the route contract requires GNU Make"
        raise AssertionError(message)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--no-print-directory",
            "MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND=true",
            "MODERN_STABLE_TEST_COMMAND=true",
            "--eval",
            _OVERRIDE_INSPECTION_TARGET,
            "modern-stable-override-contract",
        ],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    observed = completed.stdout.splitlines()
    expected = ["override", "preserved", "override", "preserved"]
    if observed != expected:
        message = (
            "caller command variables must not replace the toolchain guard or test run"
        )
        raise AssertionError(message)
