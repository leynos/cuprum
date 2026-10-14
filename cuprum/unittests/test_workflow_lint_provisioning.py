"""Contract tests for CI's provisioning of the GitHub Actions linters.

The lint gate only validates workflows if the job that runs it installs the
pinned, checksum-verified linters on `PATH` first. These tests pin the
policies, the cached archives, and the install-before-invoke ordering for the
CI `lint-test` job, complementing the Makefile boundary asserted by
``test_workflow_lint.py``.
"""

from __future__ import annotations

import typing as typ

import yaml

from tests.helpers.docs import repo_root

_CI_WORKFLOW = ".github/workflows/ci.yml"
_RUN_LINT_STEP = "Run lint, including Skylos dead-code detection"
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
        _RUN_LINT_STEP,
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
        "config-variables": [],
    }

    _assert_yamllint_provisioning()
    _assert_actionlint_provisioning()
    _assert_linter_step_order()

    lint_command = _step_named(_RUN_LINT_STEP).get("run")
    assert (
        lint_command == '/usr/bin/make ACTIONLINT="$GITHUB_WORKSPACE/actionlint" lint'
    )
