"""Validate the scheduled and smoke Loom workflow contracts.

Run ``pytest tests/test_loom_workflow_contract.py`` to ensure both lanes
invoke the bounded Loom driver with the required execution settings.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.helpers.workflow_shell import script_runs_command

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "loom.yml"
DRIVER_PATH = ROOT / "scripts" / "run_loom.py"
MAKEFILE_PATH = ROOT / "Makefile"


def _object_mapping(value: object, description: str) -> dict[object, object]:
    """Validate one dynamically parsed YAML mapping without using ``Any``."""
    assert isinstance(value, dict), f"{description} must be a mapping"
    return value


def _string_mapping(value: object, description: str) -> dict[str, object]:
    """Validate one YAML mapping whose consumers require string keys."""
    mapping = _object_mapping(value, description)
    assert all(isinstance(key, str) for key in mapping), (
        f"{description} must use string keys"
    )
    return {str(key): item for key, item in mapping.items()}


def _load() -> dict[object, object]:
    """Parse the scheduled workflow without losing YAML's ``on`` key."""
    workflow: object = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return _object_mapping(workflow, "the Loom workflow")


def _triggers(workflow: dict[object, object]) -> dict[object, object]:
    """Return the trigger mapping despite PyYAML's YAML 1.1 boolean key."""
    triggers = workflow.get("on", workflow.get(True))
    return _object_mapping(triggers, "the Loom workflow triggers")


def _loom_job(workflow: dict[object, object]) -> dict[str, object]:
    """Return the single bounded-model job with validated mapping shape."""
    jobs = _object_mapping(workflow.get("jobs"), "the Loom workflow jobs")
    job = jobs.get("loom")
    return _string_mapping(job, "the Loom workflow loom job")


def _run_steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return run steps from a valid job definition."""
    steps = job.get("steps")
    assert isinstance(steps, list), "the Loom job must declare steps"
    return [
        _string_mapping(step, "a Loom workflow step")
        for step in steps
        if isinstance(step, dict) and "run" in step
    ]


def test_schedule_dispatch_permissions_and_concurrency_are_fixed() -> None:
    """A future edit cannot silently remove the daily executable model lane."""
    workflow = _load()
    triggers = _triggers(workflow)

    assert triggers.get("schedule") == [{"cron": "15 17 * * *"}], (
        "the daily Loom schedule must remain at 17:15 UTC"
    )
    assert "workflow_dispatch" in triggers, "manual dispatch must remain enabled"
    assert triggers["workflow_dispatch"] is None, "dispatch must take no inputs"
    assert workflow.get("permissions") == {}, "workflow permissions must be empty"
    assert workflow.get("concurrency") == {
        "group": "loom-${{ github.ref }}",
        "cancel-in-progress": False,
    }, "runs must queue per ref without cancellation"
    job = _loom_job(workflow)
    assert job.get("runs-on") == "ubuntu-latest", "the job must be hosted Linux"
    assert job.get("timeout-minutes") == 30, "the full job needs an explicit budget"
    assert job.get("permissions") == {"contents": "read"}, (
        "the job must retain least-privilege contents access"
    )


def test_scheduled_main_run_is_the_only_loom_cache_writer() -> None:
    """The bounded full lane publishes its cache only after scheduled main runs."""
    job = _loom_job(_load())
    steps = job.get("steps")
    assert isinstance(steps, list), "the Loom job must declare steps"
    cache_saves = [
        _string_mapping(step, "a Loom workflow step")
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Save the compiler cache"
    ]
    assert len(cache_saves) == 1, "the Loom compiler cache must have one writer"
    assert cache_saves[0].get("if") == (
        "github.event_name == 'schedule' && github.ref == 'refs/heads/main'"
    ), "only scheduled main runs may publish the trusted Loom compiler cache"


def test_execution_step_runs_the_driver_and_driver_executes_loom() -> None:
    """A green compile-only, wrong-target, or zero-model lane is rejected."""
    steps = _run_steps(_loom_job(_load()))
    execution = [
        step["run"]
        for step in steps
        if step.get("name") == "Run full Loom models" and isinstance(step["run"], str)
    ]
    assert len(execution) == 1, "the workflow must retain one full Loom step"
    assert script_runs_command(
        execution[0], "uv run scripts/run_loom.py --mode full --summary loom-summary.md"
    ), "the scheduled lane must execute the full driver, not compile only"
    driver = DRIVER_PATH.read_text(encoding="utf-8")
    required_fragments = (
        '"RUSTFLAGS": "--cfg loom -D warnings"',
        '"--test",',
        "LOOM_TARGET",
        "_count_discovered",
        "_count_executed",
        "executed zero tests",
    )
    missing = [fragment for fragment in required_fragments if fragment not in driver]
    assert not missing, f"the driver is missing execution safeguards: {missing!r}"


def test_make_loom_runs_the_full_driver() -> None:
    """The documented local command must execute models rather than just compile."""
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "loom: build ## Run the full bounded Loom model suite" in makefile, (
        "make loom must remain a documented full-model target"
    )
    assert "uv run scripts/run_loom.py --mode full" in makefile, (
        "make loom must execute the full driver"
    )


def test_smoke_job_uses_the_same_loom_shape_and_driver() -> None:
    """Relevant pull requests compile and execute the smaller deterministic set."""
    ci: object = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = _object_mapping(_object_mapping(ci, "ci.yml").get("jobs"), "ci.yml jobs")
    smoke = jobs.get("loom-smoke")
    smoke = _string_mapping(smoke, "ci.yml loom-smoke")
    assert smoke.get("needs") == "changes", "smoke must await path detection"
    assert smoke.get("if") == "needs.changes.outputs.rust == 'true'", (
        "smoke must run for Rust and Loom changes"
    )
    assert smoke.get("runs-on") == "ubuntu-latest", "smoke must use hosted Linux"
    assert smoke.get("timeout-minutes") == 10, "smoke needs an explicit budget"
    steps = smoke.get("steps")
    assert isinstance(steps, list), "smoke must declare steps"
    typed_steps = [_string_mapping(step, "a smoke workflow step") for step in steps]
    cache_step = next(
        (step for step in typed_steps if step.get("name") == "Compute cache keys"),
        None,
    )
    assert cache_step is not None, "the smoke job must compute cache keys"
    cache_inputs = _string_mapping(cache_step["with"], "the cache-key inputs")
    assert cache_inputs["compiler-shape"] == "loom", (
        "smoke must use the isolated Loom cache family"
    )
    run_step = next(
        (step for step in typed_steps if step.get("name") == "Run Loom smoke models"),
        None,
    )
    assert run_step is not None, "the smoke job must execute Loom models"
    smoke_command = run_step["run"]
    assert isinstance(smoke_command, str), "the smoke run step must be a script"
    assert script_runs_command(
        smoke_command,
        "uv run scripts/run_loom.py --mode smoke --summary loom-summary.md",
    ), "smoke must execute the bounded Loom driver"
