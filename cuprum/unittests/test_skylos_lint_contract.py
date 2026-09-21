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

import json
import shlex
import subprocess  # ruff: ignore[suspicious-subprocess-import] - contract test invokes the pinned parser.
import tomllib
import typing as typ

import yaml

from tests.helpers.docs import repo_root

_MAKEUTIL_COMMAND: typ.Final = ("makeutil", "parse", "Makefile")
_TYPOS_CONFIG_BUILDER_VERSION_TOKENS: typ.Final = ("v0.1.2",)
_TYPOS_CONFIG_BUILDER_TOKENS: typ.Final = (
    "$(UV_RUN_ENV)",
    "uv",
    "tool",
    "run",
    "--python",
    "3.14",
    "--from",
    "\n",
    "git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)",
    "\n",
    "typos-config-builder",
)
_SPELLING_GATE_TOKENS: typ.Final = (
    "$(TYPOS_CONFIG_BUILDER)",
    "gate",
    "--repository",
    ".",
    "--scope",
    "all",
)
_SKYLOS_VERSION_TOKENS: typ.Final = ("4.33.2",)
_SKYLOS_PRODUCTION_TARGET_TOKENS: typ.Final = ("cuprum",)
_SKYLOS_EXCLUSION_TOKENS: typ.Final = ("cuprum/unittests",)
_SKYLOS_CLI_TOKENS: typ.Final = (
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
_SKYLOS_SCAN_TOKENS: typ.Final = (
    "$(SKYLOS_CLI)",
    "--config-file",
    "pyproject.toml",
)
_SKYLOS_DEAD_CODE_ARGUMENTS: typ.Final = (
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
_SKYLOS_LINT_TOKENS: typ.Final = (
    "$(SKYLOS)",
    "$(SKYLOS_PRODUCTION_TARGETS)",
    *_SKYLOS_DEAD_CODE_ARGUMENTS,
)
_SKYLOS_WHITELIST_TOKENS: typ.Final = (
    "flock",
    "$(SKYLOS_WHITELIST_LOCK)",
    "env",
    "$(SKYLOS_CLI)",
    "whitelist",
    "$${SKYLOS_SYMBOL}",
    "--reason",
    "$${SKYLOS_REASON}",
)
_SKYLOS_WHITELIST_LOCK_TOKENS: typ.Final = (".skylos-whitelist.lock",)
_LINT_PREREQUISITES: typ.Final = ("python-lint", "rust-lint", "github-actions-lint")
# `lint` carries no recipe: the leaves are prerequisites, and `.NOTPARALLEL`
# serializes them. Running the leaves in the same Make process is what keeps a
# caller's `-f` override file effective without forwarding `MAKEFILE_LIST`,
# which cannot distinguish `-f` inputs from files pulled in by `include`.
_DOCUMENTED_WHITELIST_NAMES: typ.Final = frozenset({"_check_rust_available"})
_RUNTIME_PARAMETER_ENTRY_POINTS: typ.Final = frozenset({
    "cuprum.adapters.metrics_adapter.InMemoryMetrics.inc_counter.labels",
    "cuprum.adapters.metrics_adapter.InMemoryMetrics.observe_histogram.labels",
})
_RUNTIME_METHOD_ENTRY_POINTS: typ.Final = frozenset({
    "cuprum.adapters.logging_adapter._StructuredLoggingHook.report_pipeline_wait",
    "cuprum._pipeline_native_pump_runtime._PooledNativePumpExecutor.submit",
    "cuprum._pipeline_native_pump_runtime._PooledNativePumpExecutor._start_worker",
    "cuprum._pipeline_native_pump_runtime._PooledNativePumpExecutor._take_worker",
    "cuprum._pipeline_native_pump_runtime._PooledNativePumpExecutor._retire_or_reuse",
    "cuprum._pipeline_native_pump_runtime._PooledNativePumpExecutor._worker_loop",
    "cuprum._pipeline_native_pump_types._RustPumpState.complete_cleanup",
    "cuprum._pipeline_native_pump_types._RustPumpState.defer_cleanup",
    "cuprum._pipeline_stream_fds._BlockingModeGuard.restore",
})
_RUNTIME_FUNCTION_ENTRY_POINTS: typ.Final = frozenset({
    "cuprum.adapters._tracing_line_stream._line_stream_attributes",
    "cuprum._pipeline_native_pump_runtime._settle_native_pump_future",
    "cuprum._pipeline_stream_fds._restore_stream_fd_blocking",
    "cuprum._streams_pump._override_read_size",
})


def _makefile_report() -> dict[str, object]:
    """Return Makeutil's complete, successfully parsed Makefile report."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed parser command.
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


def _sole_recipe_rule(
    target: str, *, require_recipes: bool = True
) -> dict[str, object]:
    """Return the only parsed rule for `target`, optionally requiring recipes."""
    rules = _objects(_makefile_report().get("rules"), subject="rules")
    matches = [
        rule
        for rule in rules
        if target in _text_sequence(rule.get("targets"), subject="rule targets")
        and (
            not require_recipes or _objects(rule.get("recipes"), subject="rule recipes")
        )
    ]
    assert len(matches) == 1, (
        f"expected one Makefile rule named {target!r}, found {len(matches)}"
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


def _sole_workflow_step(
    job_name: str,
    step_name: str,
    *,
    workflow_path: str = ".github/workflows/ci.yml",
) -> dict[str, object]:
    """Return the sole named CI step from `job_name`."""
    job = _workflow_job(workflow_path, job_name)
    steps = _objects(
        job.get("steps"), subject=f"{workflow_path} job {job_name!r} steps"
    )
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, (
        f"expected one {step_name!r} step in {workflow_path} job {job_name!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


def _workflow_job(workflow_path: str, job_name: str) -> dict[str, object]:
    """Return the named job from a repository workflow."""
    workflow = yaml.safe_load((repo_root() / workflow_path).read_text())
    workflow_mapping = _mapping(workflow, subject=f"{workflow_path} workflow")
    jobs = _mapping(workflow_mapping.get("jobs"), subject=f"{workflow_path} jobs")
    return _mapping(jobs.get(job_name), subject=f"{workflow_path} job {job_name!r}")


def _skylos_configuration() -> dict[str, object]:
    """Load the repository's Skylos configuration."""
    with (repo_root() / "pyproject.toml").open("rb") as configuration_file:
        configuration = tomllib.load(configuration_file)
    tool = _mapping(configuration.get("tool"), subject="tool configuration")
    return _mapping(tool.get("skylos"), subject="Skylos configuration")


def _documented_whitelist_names(skylos: dict[str, object]) -> frozenset[str]:
    """Return the documented Skylos whitelist names, if configured."""
    whitelist = _mapping(skylos.get("whitelist", {}), subject="Skylos whitelist")
    documented = _mapping(
        whitelist.get("documented", {}), subject="Skylos whitelist entries"
    )
    return frozenset(documented)


def test_lint_recipe_runs_the_production_dead_code_gate() -> None:
    """`make lint` must scan production code with Skylos's strict gate."""
    test_prerequisites = _text_sequence(
        _sole_recipe_rule("test", require_recipes=False).get("prerequisites"),
        subject="test target prerequisites",
    )
    assert "makeutil" in test_prerequisites, (
        "Make test prerequisite contract must require makeutil"
    )
    assert _variable_tokens("SKYLOS_VERSION") == _SKYLOS_VERSION_TOKENS, (
        "Skylos version contract must pin 4.33.2"
    )
    assert (
        _variable_tokens("SKYLOS_PRODUCTION_TARGETS")
        == _SKYLOS_PRODUCTION_TARGET_TOKENS
    ), "Skylos production-target contract must scan cuprum"
    assert _variable_tokens("SKYLOS_EXCLUDE_FOLDERS") == _SKYLOS_EXCLUSION_TOKENS, (
        "Skylos exclusion contract must omit unit tests"
    )
    lint_prerequisites = _text_sequence(
        _sole_recipe_rule("lint", require_recipes=False).get("prerequisites"),
        subject="lint target prerequisites",
    )
    assert lint_prerequisites == _LINT_PREREQUISITES, (
        "lint must sequence its leaves as prerequisites in a single Make process"
    )
    lint_recipes = _sole_recipe_rule("lint", require_recipes=False).get("recipes")
    assert _objects(lint_recipes, subject="lint recipes") == [], (
        "lint must delegate to its leaves as prerequisites, not recursive sub-makes"
    )
    skylos_commands = [
        command
        for command in _recipe_tokens("python-lint")
        if command[:1] == _SKYLOS_LINT_TOKENS[:1]
    ]

    assert skylos_commands == [_SKYLOS_LINT_TOKENS], (
        "Skylos lint command contract must scan production dead code strictly"
    )


def test_spelling_gate_uses_the_pinned_config_builder() -> None:
    """The spelling gate must use the pinned policy builder introduced by main."""
    assert _variable_tokens("TYPOS_CONFIG_BUILDER_VERSION") == (
        _TYPOS_CONFIG_BUILDER_VERSION_TOKENS
    ), "Spelling-builder version contract must retain the pinned release"
    assert _variable_tokens("TYPOS_CONFIG_BUILDER") == _TYPOS_CONFIG_BUILDER_TOKENS, (
        "Spelling-builder command contract must retain the pinned Python 3.14 CLI"
    )
    assert _recipe_tokens("spelling") == (_SPELLING_GATE_TOKENS,), (
        "Spelling gate contract must invoke the builder for the full repository"
    )


def test_whitelist_target_uses_skylos_subcommand_contract() -> None:
    """`skylos whitelist` must precede the name and have no scan options."""
    assert _variable_tokens("SKYLOS_CLI") == _SKYLOS_CLI_TOKENS, (
        "Skylos CLI contract must pin Python 3.14 and its tool release"
    )
    assert _variable_tokens("SKYLOS") == _SKYLOS_SCAN_TOKENS, (
        "Skylos scan command contract must add only the configuration file"
    )
    assert _variable_tokens("SKYLOS_WHITELIST_LOCK") == _SKYLOS_WHITELIST_LOCK_TOKENS, (
        "Skylos whitelist contract must use a repository-local lock"
    )

    whitelist_commands = [
        command
        for command in _recipe_tokens("skylos-allow")
        if command[:4] == _SKYLOS_WHITELIST_TOKENS[:4]
    ]
    assert whitelist_commands == [_SKYLOS_WHITELIST_TOKENS], (
        "Skylos whitelist command contract must lock and dispatch before --reason"
    )


def test_skylos_configuration_models_implicit_runtime_callers() -> None:
    """Each current false positive must be a typed, explained entry point."""
    skylos = _skylos_configuration()
    gate = _mapping(skylos.get("gate"), subject="Skylos gate configuration")
    assert gate.get("strict") is True, (
        "Skylos gate configuration must enable strict mode"
    )
    dead_code = _mapping(
        skylos.get("dead_code"), subject="Skylos dead-code configuration"
    )
    assert _documented_whitelist_names(skylos) == _DOCUMENTED_WHITELIST_NAMES, (
        "Skylos documented-whitelist contract must preserve reviewed exceptions"
    )
    entry_points = _objects(dead_code.get("entrypoints"), subject="Skylos entry points")

    entry_point_names = frozenset(
        name
        for entry_point in entry_points
        for name in _text_sequence(
            entry_point.get("full_name"), subject="entry-point name"
        )
    )
    assert entry_point_names == (
        _RUNTIME_PARAMETER_ENTRY_POINTS
        | _RUNTIME_METHOD_ENTRY_POINTS
        | _RUNTIME_FUNCTION_ENTRY_POINTS
    ), "Skylos entry-point contract must preserve every runtime caller exclusion"
    for entry_point in entry_points:
        names = frozenset(
            _text_sequence(entry_point.get("full_name"), subject="entry-point name")
        )
        entry_point_type = "parameter"
        if names & _RUNTIME_METHOD_ENTRY_POINTS:
            entry_point_type = "method"
        elif names & _RUNTIME_FUNCTION_ENTRY_POINTS:
            entry_point_type = "function"
        assert entry_point.get("type") == entry_point_type, (
            "Skylos entry-point contract must classify each implicit runtime caller"
        )
        reason = entry_point.get("reason")
        assert isinstance(reason, str), (
            "Skylos entry-point contract must provide a textual reason"
        )
        assert reason, "Skylos entry-point contract must provide a non-empty reason"


def test_ci_runs_the_lint_target() -> None:
    """CI must run the same lint target.

    The Makefile parser CI provides for these contracts is pinned and cached
    under ``tests/test_ci_makeutil_install.py``.
    """
    lint_step = _sole_workflow_step(
        "lint-test", "Run lint, including Skylos dead-code detection"
    )
    assert lint_step.get("run") == (
        '/usr/bin/make ACTIONLINT="$GITHUB_WORKSPACE/actionlint" lint'
    ), "CI lint-step contract must invoke the shared make lint target"
