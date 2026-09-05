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
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Makefile targets.
import typing as typ

import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_CI_WORKFLOW = ".github/workflows/ci.yml"
_WORKFLOW_DIRECTORY = ".github/workflows"
_ACTIONLINT_INSTALLER_LINES = (
    "readonly ACTIONLINT_VERSION='1.7.12'",
    (
        "readonly ACTIONLINT_SHA256="
        "'8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8'"
    ),
    "readonly ACTIONLINT_INSTALLER_COMMIT='914e7df21a07ef503a81201c76d2b11c789d3fca'",
    'readonly ACTIONLINT_ARCHIVE="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"',
    "readonly ACTIONLINT_RAW_BASE='https://raw.githubusercontent.com/rhysd/actionlint'",
    "readonly ACTIONLINT_SCRIPT='scripts/download-actionlint.bash'",
    (
        'readonly ACTIONLINT_INSTALLER_URL="${ACTIONLINT_RAW_BASE}/'
        '${ACTIONLINT_INSTALLER_COMMIT}/${ACTIONLINT_SCRIPT}"'
    ),
    (
        "readonly ACTIONLINT_RELEASE_ROOT="
        "'https://github.com/rhysd/actionlint/releases/download'"
    ),
    (
        'readonly ACTIONLINT_RELEASE_BASE="${ACTIONLINT_RELEASE_ROOT}/'
        'v${ACTIONLINT_VERSION}"'
    ),
    (
        'readonly ACTIONLINT_RELEASE_URL="${ACTIONLINT_RELEASE_BASE}/'
        '${ACTIONLINT_ARCHIVE}"'
    ),
    (
        "command curl --fail --location --show-error --output "
        '"${ACTIONLINT_INSTALLER_PATH}"'
    ),
    'command curl --fail --location --show-error --output "${ACTIONLINT_ARCHIVE_PATH}"',
    "sha256sum --check --",
    'bash "${ACTIONLINT_INSTALLER_PATH}" "${ACTIONLINT_VERSION}"',
)


_Step = typ.TypedDict(
    "_Step",
    {
        "id": object,
        "if": object,
        "name": object,
        "run": object,
        "uses": object,
        "with": dict[str, object],
    },
    total=False,
)
"""The CI step fields this contract reads."""


class _LintJob(typ.TypedDict, total=False):
    """The lint job fields this contract reads."""

    env: dict[str, object]
    steps: list[_Step]


def _write_fake_tool(directory: pth.Path, tool: str, *, exit_code: int = 0) -> None:
    """Write an executable that records its name and arguments."""
    executable = directory / tool
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "${0##*/}" >> "${LINT_INVOCATION_LOG}"\n'
        'for argument in "$@"; do\n'
        '  printf \'\\t%s\' "${argument}" >> "${LINT_INVOCATION_LOG}"\n'
        "done\n"
        "printf '\\n' >> \"${LINT_INVOCATION_LOG}\"\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def _make_environment(
    tmp_path: pth.Path,
    *,
    tools: cabc.Iterable[str],
    exit_codes: cabc.Mapping[str, int] | None = None,
) -> tuple[dict[str, str], pth.Path]:
    """Create a Makefile environment with only the requested fake tools."""
    tool_directory = tmp_path / "tools"
    tool_directory.mkdir()
    invocation_log = tmp_path / "invocations.log"
    expected_exit_codes = exit_codes or {}
    for tool in tools:
        _write_fake_tool(
            tool_directory,
            tool,
            exit_code=expected_exit_codes.get(tool, 0),
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


def _assert_yamllint_provisioning() -> None:
    """Assert that CI installs the pinned yamllint executable on PATH."""
    environment = _lint_job().get("env")
    assert isinstance(environment, dict), "lint-test must declare environment"
    assert environment.get("YAMLLINT_VERSION") == "1.38.0"

    yamllint_cache = _step_named("Cache yamllint")
    assert yamllint_cache.get("uses") == (
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
    )
    assert (
        yamllint_cache.get("with", {}).get("path") == ".uv-cache\n.uv-tools\n.uv-bin\n"
    )

    yamllint_install = _step_named("Install yamllint").get("run")
    assert isinstance(yamllint_install, str), "Install yamllint must run a script"
    assert 'uv tool install "yamllint==${YAMLLINT_VERSION}"' in yamllint_install
    assert 'echo "${UV_TOOL_BIN_DIR}" >> "$GITHUB_PATH"' in yamllint_install


def _assert_actionlint_provisioning() -> None:
    """Assert that CI verifies actionlint before its installer can run."""
    actionlint_cache = _step_named("Cache actionlint")
    assert actionlint_cache.get("id") == "cache_actionlint"
    assert actionlint_cache.get("uses") == (
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
    )
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
    assert actionlint_download.get("shell") == "bash"
    for expected_script_line in _ACTIONLINT_INSTALLER_LINES:
        assert expected_script_line in download_script
    assert (
        "printf '%s  %s\\n' \"${ACTIONLINT_SHA256}\" "
        '"${ACTIONLINT_ARCHIVE_PATH}" | sha256sum --check --'
    ) in download_script
    assert download_script.index("sha256sum --check --") < download_script.index(
        'bash "${ACTIONLINT_INSTALLER_PATH}" "${ACTIONLINT_VERSION}"'
    )


def _assert_linter_step_order() -> None:
    """Assert that CI provisions both linters before invoking Make."""
    step_names = [
        "Install uv",
        "Cache yamllint",
        "Install yamllint",
        "Cache actionlint",
        "Download actionlint",
        "Run lint",
    ]
    steps = _lint_job().get("steps")
    assert isinstance(steps, list), "lint-test must declare a steps list"
    positions = [
        next(
            position for position, step in enumerate(steps) if step.get("name") == name
        )
        for name in step_names
    ]
    assert positions == sorted(positions)


def test_the_workflow_lint_target_runs_both_linters(tmp_path: pth.Path) -> None:
    """The target validates YAML policy before GitHub Actions semantics."""
    environment, invocation_log = _make_environment(
        tmp_path, tools=("yamllint", "actionlint")
    )

    completed = _run_make("github-actions-lint", environment=environment)

    assert completed.returncode == 0, completed.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        f"yamllint\t--config-file\t.yamllint.yml\t{_WORKFLOW_DIRECTORY}",
        "actionlint",
    ]


def test_the_lint_target_runs_the_workflow_linters(tmp_path: pth.Path) -> None:
    """The aggregate lint target reaches both GitHub Actions linters."""
    environment, invocation_log = _make_environment(
        tmp_path, tools=("uv", "yamllint", "actionlint")
    )
    overrides = tmp_path / "lint-target-overrides.mk"
    overrides.write_text(
        ".PHONY: python-lint rust-lint\npython-lint:\n\t@:\nrust-lint:\n\t@:\n",
        encoding="utf-8",
    )

    completed = _run_make(
        "lint",
        environment=environment,
        makefiles=(repo_root() / "Makefile", overrides),
    )

    assert completed.returncode == 0, completed.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        "uv\trun\twhich\truff",
        f"yamllint\t--config-file\t.yamllint.yml\t{_WORKFLOW_DIRECTORY}",
        "actionlint",
    ]


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
        f"yamllint\t--config-file\t.yamllint.yml\t{_WORKFLOW_DIRECTORY}",
        "actionlint",
    ]


def test_all_workflows_declare_yaml_document_starts() -> None:
    """The shared YAML policy has a compatible marker in every workflow."""
    workflow_paths = sorted((repo_root() / _WORKFLOW_DIRECTORY).glob("*.yml"))

    assert workflow_paths, "expected at least one GitHub Actions workflow"
    for workflow_path in workflow_paths:
        assert workflow_path.read_text(encoding="utf-8").startswith("---\n"), (
            f"{workflow_path.relative_to(repo_root())} must begin with a YAML "
            "document start"
        )


def test_ci_provisions_the_pinned_workflow_linters() -> None:
    """CI caches and verifies the exact linters used by its trusted Make run."""
    policy = yaml.safe_load((repo_root() / ".yamllint.yml").read_text(encoding="utf-8"))
    assert policy == {
        "extends": "default",
        "rules": {
            "document-start": {"level": "error", "present": True},
            "line-length": {"max": 120},
            "truthy": {
                "allowed-values": ["true", "false"],
                "check-keys": False,
            },
        },
    }
    actionlint_policy = yaml.safe_load(
        (repo_root() / ".github/actionlint.yaml").read_text(encoding="utf-8")
    )
    assert actionlint_policy == {
        "self-hosted-runner": {"labels": ["ubicloud-standard-2"]},
        "config-variables": ["CODESCENE_CLI_SHA256"],
    }

    _assert_yamllint_provisioning()
    _assert_actionlint_provisioning()
    _assert_linter_step_order()

    lint_command = _step_named("Run lint").get("run")
    assert (
        lint_command == '/usr/bin/make ACTIONLINT="$GITHUB_WORKSPACE/actionlint" lint'
    )
