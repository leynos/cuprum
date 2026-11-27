"""Contract for the advisory macOS process-lifecycle workflow.

The workflow's evidence is only as good as the path filter that starts it, the
runner it lands on, and the command it runs. A leg that omitted a manifest
target, drifted to a Linux label, or built the Rust extension would still be
green while proving something other than macOS behaviour. These assertions pin
the declaration; the runtime contract belongs to
``tests/helpers/required_outcomes.py`` and its controls.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import MACOS_LABEL
from tests.helpers.ci_workflows import (
    job,
    jobs,
    step_inputs,
    steps,
    workflow_document,
)
from tests.helpers.macos_lifecycle import TARGETS
from tests.helpers.workflow_shell import script_runs_command

WORKFLOW = "macos-lifecycle.yml"
LIFECYCLE_JOB = "lifecycle"
CHECKOUT_PIN = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_PIN = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
SETUP_UV_PIN = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
BACKEND_ENV = "CUPRUM_REQUIRE_RUST_EXTENSION"
#: The support helpers and production modules the selected cases import. A
#: change to any of them can change what the leg measures.
SUPPORT_PATHS = frozenset({
    "tests/helpers/macos_lifecycle.py",
    "tests/helpers/required_outcomes.py",
    "tests/helpers/catalogue.py",
    "tests/helpers/detached.py",
    "tests/helpers/execution.py",
    "tests/helpers/process_exit.py",
    "tests/helpers/process_state.py",
    "tests/helpers/timeouts.py",
    "cuprum/unittests/_adapter_test_support.py",
    "cuprum/unittests/_cqrs_fixtures.py",
    "cuprum/unittests/_pipeline_deadline_support.py",
    "cuprum/unittests/_post_exit_teardown_support.py",
    "cuprum/unittests/_process_group_ownership_support.py",
    "cuprum/unittests/_process_signal_identity_support.py",
    "cuprum/unittests/_start_ownership_support.py",
    "cuprum/unittests/_startup_abandon_support.py",
})
PRODUCTION_PATHS = frozenset({
    "cuprum/_process*.py",
    "cuprum/_group_teardown.py",
    "cuprum/_detached*.py",
    "cuprum/_subprocess*.py",
    "cuprum/_pipeline*.py",
    "cuprum/_stream_drain*.py",
    "cuprum/_wait4_process*.py",
    "cuprum/_line_stream/**",
    "cuprum/line_stream_events.py",
    "cuprum/line_stream_observation.py",
})


def _triggers() -> dict[object, object]:
    """Return the trigger mapping despite YAML 1.1's boolean ``on`` key."""
    document = workflow_document(WORKFLOW)
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), f"{WORKFLOW} must declare its triggers"
    return triggers


def _pull_request_paths() -> set[str]:
    """Return the pull-request path filter as a set of literal patterns."""
    triggers = _triggers()
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), (
        f"{WORKFLOW} must run on a path-filtered pull request"
    )
    paths = pull_request.get("paths")
    assert isinstance(paths, list), (
        f"{WORKFLOW} must declare a pull-request path filter"
    )
    assert paths, f"{WORKFLOW} must not declare an empty path filter"
    assert all(isinstance(path, str) for path in paths), (
        "every pull-request path filter entry must be a string"
    )
    return {str(path) for path in paths}


def _run_scripts() -> list[str]:
    """Return every run script in the lifecycle job."""
    return [
        typ.cast("str", step["run"])
        for step in steps(WORKFLOW, LIFECYCLE_JOB)
        if isinstance(step.get("run"), str)
    ]


def test_triggers_are_the_weekly_advisory_lane() -> None:
    """The leg runs on its own weekly schedule, a dispatch, and filtered PRs."""
    triggers = _triggers()
    assert triggers.get("schedule") == [{"cron": "41 5 * * 3"}], (
        "the advisory leg must keep one weekly schedule"
    )
    assert "push" not in triggers, (
        "the leg is advisory: a push trigger would spend a Mac runner on every "
        "merge while gating nothing"
    )
    assert "workflow_dispatch" in triggers, "manual dispatch must remain enabled"
    assert triggers["workflow_dispatch"] is None, "dispatch must take no inputs"


def test_the_schedule_does_not_compete_with_the_boundary_verifiers() -> None:
    """A long macOS queue must not start beside the Monday verifier lane."""
    boundaries = workflow_document("rust-boundaries.yml")
    boundary_triggers = boundaries.get("on", boundaries.get(True))
    assert isinstance(boundary_triggers, dict), (
        "rust-boundaries.yml must declare its triggers"
    )
    boundary_schedule = boundary_triggers.get("schedule")
    lifecycle_schedule = _triggers().get("schedule")
    assert lifecycle_schedule != boundary_schedule, (
        "the macOS leg must use a schedule distinct from the boundary verifiers"
    )


def test_the_path_filter_covers_every_case_and_the_modules_it_exercises() -> None:
    """A target or support module outside the filter would never start the leg."""
    paths = _pull_request_paths()
    required = (
        {f".github/workflows/{WORKFLOW}"}
        | set(TARGETS)
        | SUPPORT_PATHS
        | PRODUCTION_PATHS
        | {"pyproject.toml", "uv.lock"}
    )
    missing = sorted(required - paths)
    assert not missing, (
        f"{WORKFLOW}'s pull-request paths must cover these entries: {missing!r}"
    )


def test_the_job_is_one_hosted_macos_leg_with_a_ceiling() -> None:
    """The leg must run on a real Mac and be bounded, with no silent failure."""
    assert list(jobs(WORKFLOW)) == [LIFECYCLE_JOB], (
        "the advisory workflow must keep exactly one job"
    )
    declared = job(WORKFLOW, LIFECYCLE_JOB)
    assert declared.get("runs-on") == MACOS_LABEL, (
        f"{WORKFLOW}:{LIFECYCLE_JOB} must run on {MACOS_LABEL}"
    )
    ceiling = declared.get("timeout-minutes")
    assert type(ceiling) is int, "the macOS leg must declare an integer timeout-minutes"
    assert ceiling > 0, "the macOS leg's timeout must be positive"
    assert "needs" not in declared, (
        "the advisory leg must not wait on another job: no Ubicloud lane may "
        "depend on it, and it must not depend on one either"
    )
    assert "continue-on-error" not in declared, (
        "a leg that tolerates its own failure gates nothing"
    )
    assert declared.get("permissions") == {"contents": "read"}, (
        "the job must retain least-privilege contents access"
    )
    assert workflow_document(WORKFLOW).get("permissions") == {}, (
        "workflow permissions must be empty, with the job opting in"
    )


def test_the_steps_use_the_pinned_tools_and_the_locked_environment() -> None:
    """Neither an unpinned action nor an unlocked sync may enter the leg."""
    declared_steps = steps(WORKFLOW, LIFECYCLE_JOB)
    invoked = {
        typ.cast("str", step["uses"]): step
        for step in declared_steps
        if isinstance(step.get("uses"), str)
    }
    assert CHECKOUT_PIN in invoked, "the leg must use the pinned checkout"
    checkout_inputs = step_inputs(invoked[CHECKOUT_PIN], "the checkout step inputs")
    assert checkout_inputs.get("persist-credentials") is False, (
        "the checkout token must not stay where the suite can reach it"
    )
    assert SETUP_PYTHON_PIN in invoked, "the leg must use the pinned setup-python"
    python_inputs = step_inputs(
        invoked[SETUP_PYTHON_PIN], "the setup-python step inputs"
    )
    assert python_inputs.get("python-version") == "${{ env.PYTHON_VERSION }}", (
        "the interpreter must come from the workflow env, not a literal"
    )
    assert SETUP_UV_PIN in invoked, "the leg must use the pinned setup-uv"

    environment = workflow_document(WORKFLOW).get("env")
    assert isinstance(environment, dict), f"{WORKFLOW} must declare its env"
    assert environment.get("PYTHON_VERSION") == "3.13", "the leg must pin Python 3.13"
    assert environment.get("UV_PYTHON") == "3.13", (
        "UV_PYTHON must bind uv to the same interpreter"
    )
    assert BACKEND_ENV not in environment, (
        "the leg runs the Python backend deliberately; requiring the compiled "
        "extension here would fail every case instead of exercising them"
    )

    scripts = _run_scripts()
    assert any(
        script_runs_command(script, "uv sync --locked --group dev")
        for script in scripts
    ), "the leg must install the locked development environment"
    assert any(
        script_runs_command(script, "uv run python -m tests.helpers.macos_lifecycle")
        for script in scripts
    ), "the leg must execute the guarded manifest"


def test_the_leg_builds_no_rust_extension_and_runs_no_make_target() -> None:
    """The evidence comes from the Python backend, on a Mac, and nothing else."""
    forbidden = {
        "cargo": "no Rust build belongs in this leg",
        "maturin": "the compiled extension is deliberately absent",
        "make ": "the leg runs the manifest directly, not a Make target",
    }
    offenders = [
        f"{needle!r} ({reason}) in {script!r}"
        for script in _run_scripts()
        for needle, reason in forbidden.items()
        if needle in script
    ]
    assert not offenders, "the macOS leg must stay Python-only: " + "; ".join(offenders)


def test_the_result_summary_runs_even_when_the_leg_fails() -> None:
    """A failed leg must still leave a triage summary and a named outcome."""
    summaries = [
        step
        for step in steps(WORKFLOW, LIFECYCLE_JOB)
        if step.get("name") == "Record the lifecycle result"
    ]
    assert len(summaries) == 1, "the leg must declare one result summary"
    summary = summaries[0]
    assert summary.get("if") == "always()", (
        "the summary must run after a failure, which is when it matters"
    )
    script = summary.get("run")
    assert isinstance(script, str), "the summary step must be a script"
    assert "GITHUB_STEP_SUMMARY" in script, (
        "the summary must be published to the job summary"
    )
    assert "LIFECYCLE_OUTCOME" in script, (
        "the summary must name the guarded step's outcome"
    )
