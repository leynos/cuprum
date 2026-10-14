"""Contract tests for the GitHub Actions lint target.

The repository's workflows and composite actions are declarative
configuration, so malformed YAML or an incorrectly scoped recipe could
otherwise reach CI before any project gate evaluates them. These tests drive
the Makefile target with fake tools, asserting the exact arguments it produces,
its loud missing-tool failure, and the shared YAML policy every linted file
obeys. CI's own provisioning of those tools is covered separately by
``test_workflow_lint_provisioning.py``.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Makefile targets.
import typing as typ

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_WORKFLOW_DIRECTORY = ".github/workflows"
_ACTION_DIRECTORY = ".github/actions"
#: The directories the yamllint-backed half of the target covers, in order.
#: Spelled out rather than derived from the constants above: this is the value
#: the target must produce, so deriving it would let a mutated directory
#: constant agree with a mutated recipe and leave the contract unchecked.
_LINTED_DIRECTORIES = (".github/workflows", ".github/actions")
#: The actionlint invocation, which disables shellcheck to dodge the v1.7.12
#: stdin deadlock (rhysd/actionlint#702, #704, #712) and match CI, which
#: installs no shellcheck binary.
_ACTIONLINT_INVOCATION = (
    "actionlint\t-config-file\t.github/actionlint.yaml\t-shellcheck="
)


def _write_fake_tool(
    directory: pth.Path,
    tool: str,
    *,
    exit_code: int = 0,
    output: str = "",
) -> None:
    """Write an executable that records its name and arguments."""
    executable = directory / tool
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "${0##*/}" >> "${LINT_INVOCATION_LOG}"\n'
        'for argument in "$@"; do\n'
        '  printf \'\\t%s\' "${argument}" >> "${LINT_INVOCATION_LOG}"\n'
        "done\n"
        "printf '\\n' >> \"${LINT_INVOCATION_LOG}\"\n"
        f"printf '%s' {shlex.quote(output)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def _make_environment(
    tmp_path: pth.Path,
    *,
    tools: cabc.Iterable[str],
    exit_codes: cabc.Mapping[str, int] | None = None,
    outputs: cabc.Mapping[str, str] | None = None,
) -> tuple[dict[str, str], pth.Path]:
    """Create a Makefile environment with only the requested fake tools."""
    tool_directory = tmp_path / "tools"
    tool_directory.mkdir()
    invocation_log = tmp_path / "invocations.log"
    expected_exit_codes = exit_codes or {}
    expected_outputs = outputs or {}
    for tool in tools:
        _write_fake_tool(
            tool_directory,
            tool,
            exit_code=expected_exit_codes.get(tool, 0),
            output=expected_outputs.get(tool, ""),
        )
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "LINT_INVOCATION_LOG": str(invocation_log),
        "PATH": os.pathsep.join((str(tool_directory), os.defpath)),
    }
    return environment, invocation_log


def _run_make(
    *targets: str,
    environment: dict[str, str],
    makefiles: cabc.Iterable[pth.Path] = (),
) -> subprocess.CompletedProcess[str]:
    """Run fixed Makefile targets and capture their outcome."""
    command = ["make"]
    for makefile in makefiles:
        command.extend(("-f", str(makefile)))
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed command.
        [*command, *targets],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env=environment,
        text=True,
    )


def test_the_workflow_lint_target_runs_both_linters(tmp_path: pth.Path) -> None:
    """The target validates YAML policy before GitHub Actions semantics."""
    environment, invocation_log = _make_environment(
        tmp_path, tools=("yamllint", "actionlint")
    )

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode == 0, completed.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        "yamllint\t--strict\t--config-file\t.yamllint.yml\t"
        + "\t".join(_LINTED_DIRECTORIES),
        _ACTIONLINT_INVOCATION,
    ]


def test_the_lint_target_runs_the_workflow_linters(tmp_path: pth.Path) -> None:
    """The aggregate lint target reaches both GitHub Actions linters."""
    environment, invocation_log = _make_environment(
        tmp_path,
        tools=("uv", "yamllint", "actionlint", "mold", "rustup"),
        outputs={
            "mold": "mold 2.41.0\n",
            "rustup": (
                "rustc-codegen-cranelift-x86_64-unknown-linux-gnu (installed)\n"
                "clippy-x86_64-unknown-linux-gnu (installed)\n"
            ),
        },
    )
    overrides = tmp_path / "lint-target-overrides.mk"
    overrides.write_text(
        # Stubbing a target suppresses its recipe but not its prerequisites, so
        # the merged lint graph needs a stub for each heavy target it reaches.
        # dev-fast-check and the Pylint verification targets stay real: they are
        # the cheap probes whose invocations are asserted below, and stubbing a
        # parent does not spare them.
        ".PHONY: python-lint rust-lint lint-clippy lint-whitaker spelling\n"
        "python-lint:\n\t@:\nrust-lint:\n\t@:\n"
        "lint-clippy:\n\t@:\n"
        "lint-whitaker:\n\t@:\n"
        "spelling:\n\t@:\n",
        encoding="utf-8",
    )

    completed = _run_make(
        "lint",
        environment=environment,
        makefiles=(repo_root() / "Makefile", overrides),
    )

    assert completed.returncode == 0, completed.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("pylint" in invocation for invocation in invocations), (
        "the aggregate lint target must retain its Pylint contract"
    )
    workflow_invocations = [
        "uv\trun\twhich\truff",
        "mold\t--version",
        "rustup\tcomponent\tlist\t--installed\t--toolchain\tnightly-2026-08-23",
        "yamllint\t--strict\t--config-file\t.yamllint.yml\t"
        + "\t".join(_LINTED_DIRECTORIES),
        _ACTIONLINT_INVOCATION,
    ]
    positions = [invocations.index(invocation) for invocation in workflow_invocations]
    assert positions == sorted(positions)


def test_the_workflow_lint_target_rejects_a_missing_linter(tmp_path: pth.Path) -> None:
    """A missing yamllint fails before Actionlint could make the target pass."""
    environment, invocation_log = _make_environment(tmp_path, tools=("actionlint",))
    environment["PATH"] = os.pathsep.join((str(tmp_path / "tools"), os.defpath))

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode != 0, "missing yamllint unexpectedly passed"
    assert "Error: 'yamllint' is required, but not installed" in completed.stderr
    assert not invocation_log.exists(), "actionlint ran after yamllint was missing"


def test_the_workflow_lint_target_rejects_actionlint_failure(
    tmp_path: pth.Path,
) -> None:
    """Actionlint failure makes the workflow lint target fail after yamllint."""
    environment, invocation_log = _make_environment(
        tmp_path,
        tools=("yamllint", "actionlint"),
        exit_codes={"actionlint": 31},
    )

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode == 2
    assert "Error 31" in completed.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        "yamllint\t--strict\t--config-file\t.yamllint.yml\t"
        + "\t".join(_LINTED_DIRECTORIES),
        _ACTIONLINT_INVOCATION,
    ]


def test_all_linted_yaml_declares_document_starts() -> None:
    """The shared YAML policy has a compatible marker in every linted file."""
    linted_paths = sorted((repo_root() / _WORKFLOW_DIRECTORY).glob("*.yml")) + sorted(
        (repo_root() / _ACTION_DIRECTORY).glob("*/action.yml")
    )

    assert linted_paths, "expected at least one GitHub Actions YAML file"
    assert any(path.parent.parent.name == "actions" for path in linted_paths), (
        "expected at least one composite action under the linted directories"
    )
    for linted_path in linted_paths:
        assert linted_path.read_text(encoding="utf-8").startswith("---\n"), (
            f"{linted_path.relative_to(repo_root())} must begin with a YAML "
            "document start"
        )
