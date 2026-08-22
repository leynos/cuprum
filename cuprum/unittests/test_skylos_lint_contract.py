"""Contract tests for Skylos dead-code detection in Make and CI.

Skylos is invoked through variables and recipes whose order is significant:
the scanner accepts ``--config-file`` before a scan path, while the standalone
``whitelist`` subcommand must appear immediately after ``skylos``. Skylos also
uses its own Python AST, so it must run with Python 3.14 to understand the
project syntax. Makeutil parses the Makefile into structured rules and
variables, so these tests assert that interface without depending on
whitespace or nearby source text.
"""

from __future__ import annotations

import functools
import json
import shlex
import subprocess  # noqa: S404 - contract test invokes the pinned parser.
import tomllib
import typing as typ

import yaml

from tests.helpers.docs import repo_root

_MAKEUTIL_COMMAND: typ.Final = ("makeutil", "parse", "Makefile")
_RUNTIME_PARAMETER_ENTRY_POINTS: typ.Final = frozenset({
    "cuprum.adapters.metrics_adapter.InMemoryMetrics.inc_counter.labels",
    "cuprum.adapters.metrics_adapter.InMemoryMetrics.observe_histogram.labels",
})


@functools.cache
def _makefile_report() -> dict[str, object]:
    """Return Makeutil's complete, successfully parsed Makefile report."""
    completed = subprocess.run(  # noqa: S603 - fixed parser command.
        _MAKEUTIL_COMMAND,
        capture_output=True,
        check=True,
        cwd=repo_root(),
        text=True,
    )
    report = typ.cast("dict[str, object]", json.loads(completed.stdout))
    parse = _mapping(report.get("parse"), subject="parse report")
    assert parse.get("status") == "complete", (
        f"makeutil did not complete the Makefile parse: {parse!r}"
    )
    return report


def _mapping(value: object, *, subject: str) -> dict[str, object]:
    """Return a JSON object, naming the unexpected `subject` on failure."""
    assert isinstance(value, dict), f"expected {subject} to be a JSON object"
    return typ.cast("dict[str, object]", value)


def _objects(value: object, *, subject: str) -> list[dict[str, object]]:
    """Return a JSON object array, naming the unexpected `subject` on failure."""
    assert isinstance(value, list), f"expected {subject} to be a JSON array"
    return [_mapping(item, subject=f"{subject} item") for item in value]


def _text_sequence(value: object, *, subject: str) -> tuple[str, ...]:
    """Return a JSON string array, naming the unexpected `subject` on failure."""
    assert isinstance(value, list), f"expected {subject} to be a JSON array"
    assert all(isinstance(item, str) for item in value), (
        f"expected {subject} to contain only JSON strings"
    )
    return tuple(typ.cast("list[str]", value))


def _sole_variable(name: str) -> dict[str, object]:
    """Return Makeutil's sole variable fact for `name`."""
    variables = _objects(_makefile_report().get("variables"), subject="variables")
    matches = [variable for variable in variables if variable.get("name") == name]
    assert len(matches) == 1, (
        f"expected one Makefile variable named {name!r}, found {len(matches)}"
    )
    return matches[0]


def _sole_recipe_rule(target: str) -> dict[str, object]:
    """Return the only parsed rule for `target` that has recipes."""
    rules = _objects(_makefile_report().get("rules"), subject="rules")
    matches = [
        rule
        for rule in rules
        if target in _text_sequence(rule.get("targets"), subject="rule targets")
        and _objects(rule.get("recipes"), subject="rule recipes")
    ]
    assert len(matches) == 1, (
        f"expected one recipe-bearing Makefile rule named {target!r}, found "
        f"{len(matches)}"
    )
    return matches[0]


def _variable_tokens(name: str) -> tuple[str, ...]:
    """Return shell-like tokens from Makeutil's raw variable value."""
    value = _sole_variable(name).get("raw_value")
    assert isinstance(value, str), f"expected {name!r} to have a string value"
    return tuple(shlex.split(value))


def _recipe_tokens(target: str) -> tuple[tuple[str, ...], ...]:
    """Return shell-like tokens for every recipe in `target`."""
    recipes = _objects(
        _sole_recipe_rule(target).get("recipes"), subject=f"{target} recipes"
    )
    return tuple(
        tuple(shlex.split(recipe_text))
        for recipe in recipes
        if isinstance(recipe_text := recipe.get("text"), str)
    )


def _sole_workflow_step(job_name: str, step_name: str) -> dict[str, object]:
    """Return the sole named CI step from `job_name`."""
    workflow = yaml.safe_load((repo_root() / ".github/workflows/ci.yml").read_text())
    workflow_mapping = _mapping(workflow, subject="CI workflow")
    jobs = _mapping(workflow_mapping.get("jobs"), subject="CI workflow jobs")
    job = _mapping(jobs.get(job_name), subject=f"CI job {job_name!r}")
    steps = _objects(job.get("steps"), subject=f"CI job {job_name!r} steps")
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, (
        f"expected one {step_name!r} step in CI job {job_name!r}, found {len(matches)}"
    )
    return matches[0]


def test_lint_recipe_runs_the_production_dead_code_gate() -> None:
    """`make lint` must scan production code with Skylos's strict gate."""
    skylos_commands = [
        command for command in _recipe_tokens("lint") if command[:1] == ("$(SKYLOS)",)
    ]

    assert skylos_commands == [
        (
            "$(SKYLOS)",
            "$(SKYLOS_PRODUCTION_TARGETS)",
            "--exclude",
            "$(SKYLOS_EXCLUDE_FOLDERS)",
            "--category",
            "dead_code",
            "--gate",
            "--format",
            "concise",
            "--no-upload",
            "--no-provenance",
            "--no-grep-verify",
        )
    ]


def test_whitelist_target_uses_skylos_subcommand_contract() -> None:
    """`skylos whitelist` must precede the name and have no scan options."""
    assert _variable_tokens("SKYLOS_CLI") == (
        "$(UV_RUN_ENV)",
        "uv",
        "tool",
        "run",
        "--python",
        "3.14",
        "--from",
        "skylos==$(SKYLOS_VERSION)",
        "skylos",
    )
    assert _variable_tokens("SKYLOS") == (
        "$(SKYLOS_CLI)",
        "--config-file",
        "pyproject.toml",
    )

    whitelist_commands = [
        command
        for command in _recipe_tokens("skylos-allow")
        if command[:1] == ("$(SKYLOS_CLI)",)
    ]
    assert whitelist_commands == [
        (
            "$(SKYLOS_CLI)",
            "whitelist",
            "$${SKYLOS_SYMBOL}",
            "--reason",
            "$${SKYLOS_REASON}",
        )
    ]


def test_skylos_configuration_models_implicit_runtime_callers() -> None:
    """Each current false positive must be a typed, explained entry point."""
    with (repo_root() / "pyproject.toml").open("rb") as configuration_file:
        configuration = tomllib.load(configuration_file)

    tool = _mapping(configuration.get("tool"), subject="tool configuration")
    skylos = _mapping(tool.get("skylos"), subject="Skylos configuration")
    dead_code = _mapping(
        skylos.get("dead_code"), subject="Skylos dead-code configuration"
    )
    entry_points = _objects(dead_code.get("entrypoints"), subject="Skylos entry points")

    entry_point_names = frozenset(
        name
        for entry_point in entry_points
        for name in _text_sequence(
            entry_point.get("full_name"), subject="entry-point name"
        )
    )
    assert entry_point_names == _RUNTIME_PARAMETER_ENTRY_POINTS
    for entry_point in entry_points:
        assert entry_point.get("type") == "parameter"
        reason = entry_point.get("reason")
        assert isinstance(reason, str)
        assert reason


def test_ci_runs_the_lint_target_and_installs_makeutil() -> None:
    """CI must run the same lint target and provide its Makefile parser."""
    lint_step = _sole_workflow_step(
        "lint-test", "Run lint, including Skylos dead-code detection"
    )
    assert lint_step.get("run") == "make lint"

    parser_step = _sole_workflow_step("typecheck-test", "Install Makefile parser")
    parser_run = parser_step.get("run")
    assert isinstance(parser_run, str)
    assert tuple(shlex.split(parser_run.replace("\\\n", ""))) == (
        "rustup",
        "toolchain",
        "install",
        "${MAKEUTIL_TOOLCHAIN}",
        "--profile",
        "minimal",
        "RUSTFLAGS=-Zpolonius=next",
        "cargo",
        "+${MAKEUTIL_TOOLCHAIN}",
        "install",
        "--git",
        "https://github.com/leynos/makeutil",
        "--rev",
        "${MAKEUTIL_REVISION}",
        "--locked",
        "--force",
        "makeutil",
    )
