"""Contract tests for GitHub Actions workflow validation.

The repository's workflows are declarative configuration, so malformed YAML,
invalid Actions expressions, and shell errors could otherwise reach CI before
any project gate evaluates them. These tests cover the Makefile boundary that
invokes both linters, its loud missing-tool failure, and the CI provisioning
that makes the same checked binaries available to the lint job.
"""

from __future__ import annotations

import os
import stat
import subprocess  # noqa: S404 - runs fixed Makefile targets under test.
import typing as typ

import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_CI_WORKFLOW = ".github/workflows/ci.yml"
_WORKFLOW_DIRECTORY = ".github/workflows"


_Step = typ.TypedDict(
    "_Step",
    {
        "id": object,
        "if": object,
        "name": object,
        "run": object,
        "with": dict[str, object],
    },
    total=False,
)
"""The CI step fields this contract reads."""


class _LintJob(typ.TypedDict, total=False):
    """The lint job fields this contract reads."""

    env: dict[str, object]
    steps: list[_Step]


def _write_fake_tool(directory: pth.Path, tool: str) -> None:
    """Write an executable that records its name and arguments."""
    executable = directory / tool
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "${0##*/}" >> "${LINT_INVOCATION_LOG}"\n'
        'for argument in "$@"; do\n'
        '  printf \'\\t%s\' "${argument}" >> "${LINT_INVOCATION_LOG}"\n'
        "done\n"
        "printf '\\n' >> \"${LINT_INVOCATION_LOG}\"\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def _make_environment(
    tmp_path: pth.Path, *, tools: cabc.Iterable[str]
) -> tuple[dict[str, str], pth.Path]:
    """Create a Makefile environment with only the requested fake tools."""
    tool_directory = tmp_path / "tools"
    tool_directory.mkdir()
    invocation_log = tmp_path / "invocations.log"
    for tool in tools:
        _write_fake_tool(tool_directory, tool)
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "LINT_INVOCATION_LOG": str(invocation_log),
        "PATH": f"{tool_directory}:{os.environ['PATH']}",
    }
    return environment, invocation_log


def _run_make(
    *targets: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run fixed Makefile targets and capture their outcome."""
    return subprocess.run(  # noqa: S603 - fixed Makefile test command.
        ["make", *targets],  # noqa: S607 - `make` resolves from the test PATH.
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env=environment,
        text=True,
    )


def _lint_job() -> _LintJob:
    """Return the CI lint job with the fields this contract requires."""
    parsed = yaml.safe_load((repo_root() / _CI_WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{_CI_WORKFLOW} must parse to a mapping"
    jobs = parsed.get("jobs")
    assert isinstance(jobs, dict), f"{_CI_WORKFLOW} must declare jobs"
    lint_job = jobs.get("lint-test")
    assert isinstance(lint_job, dict), f"{_CI_WORKFLOW} must declare lint-test"
    return typ.cast("_LintJob", lint_job)


def _step_named(name: str) -> _Step:
    """Return the named lint-job step, or report the declared step names."""
    steps = _lint_job().get("steps")
    assert isinstance(steps, list), "lint-test must declare a steps list"
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, (
        f"expected one lint-test step named {name!r}, found {len(matches)}"
    )
    return matches[0]


def test_the_workflow_lint_target_runs_both_linters(tmp_path: pth.Path) -> None:
    """The target validates YAML policy before GitHub Actions semantics."""
    environment, invocation_log = _make_environment(
        tmp_path, tools=("yamllint", "actionlint")
    )

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode == 0, completed.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        f"yamllint\t{_WORKFLOW_DIRECTORY}",
        "actionlint",
    ]


def test_the_workflow_lint_target_rejects_a_missing_linter(tmp_path: pth.Path) -> None:
    """A missing yamllint fails before Actionlint could make the target pass."""
    environment, invocation_log = _make_environment(tmp_path, tools=("actionlint",))
    environment["PATH"] = f"{tmp_path / 'tools'}:/usr/bin:/bin"

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode != 0, "missing yamllint unexpectedly passed"
    assert "Error: 'yamllint' is required, but not installed" in completed.stderr
    assert not invocation_log.exists(), "actionlint ran after yamllint was missing"


def test_ci_provisions_the_pinned_workflow_linters() -> None:
    """CI caches and verifies the exact linters used by its trusted Make run."""
    policy = (repo_root() / ".yamllint.yml").read_text(encoding="utf-8")
    assert "allowed-values: ['true', 'false']" in policy
    assert "check-keys: false" in policy
    actionlint_policy = yaml.safe_load(
        (repo_root() / ".github/actionlint.yaml").read_text(encoding="utf-8")
    )
    assert actionlint_policy == {
        "self-hosted-runner": {"labels": ["ubicloud-standard-2"]},
        "config-variables": ["CODESCENE_CLI_SHA256"],
    }

    environment = _lint_job().get("env")
    assert isinstance(environment, dict), "lint-test must declare environment"
    assert environment.get("YAMLLINT_VERSION") == "1.38.0"

    yamllint_cache = _step_named("Cache yamllint")
    assert (
        yamllint_cache.get("with", {}).get("path") == ".uv-cache\n.uv-tools\n.uv-bin\n"
    )

    yamllint_install = _step_named("Install yamllint").get("run")
    assert isinstance(yamllint_install, str), "Install yamllint must run a script"
    assert 'uv tool install "yamllint==${YAMLLINT_VERSION}"' in yamllint_install
    assert 'echo "${UV_TOOL_BIN_DIR}" >> "$GITHUB_PATH"' in yamllint_install

    actionlint_cache = _step_named("Cache actionlint")
    assert actionlint_cache.get("id") == "cache_actionlint"
    assert actionlint_cache.get("with", {}).get("path") == "actionlint"
    assert actionlint_cache.get("with", {}).get("key") == (
        "actionlint-${{ runner.os }}-${{ runner.arch }}-1.7.12"
    )

    actionlint_download = _step_named("Download actionlint")
    assert (
        actionlint_download.get("if")
        == "steps.cache_actionlint.outputs.cache-hit != 'true'"
    )
    download_script = actionlint_download.get("run")
    assert isinstance(download_script, str), "Download actionlint must run a script"
    assert "readonly ACTIONLINT_VERSION='1.7.12'" in download_script
    assert (
        "readonly ACTIONLINT_INSTALLER_COMMIT="
        "'914e7df21a07ef503a81201c76d2b11c789d3fca'" in download_script
    )
    assert "sha256sum --check --" in download_script
    assert (
        'bash "${ACTIONLINT_INSTALLER_PATH}" "${ACTIONLINT_VERSION}"' in download_script
    )

    lint_command = _step_named("Run lint").get("run")
    assert (
        lint_command == '/usr/bin/make ACTIONLINT="$GITHUB_WORKSPACE/actionlint" lint'
    )
