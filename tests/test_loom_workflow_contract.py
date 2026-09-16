"""Contract tests for Cuprum's non-vacuous scheduled Loom lane."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import yaml

from tests.helpers.workflow_shell import script_runs_command

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "loom.yml"
DRIVER_PATH = ROOT / "scripts" / "run_loom.py"


def _load() -> dict[typ.Any, typ.Any]:
    """Parse the scheduled workflow without losing YAML's ``on`` key."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "the Loom workflow must parse to a mapping"
    return workflow


def _triggers(workflow: dict[typ.Any, typ.Any]) -> dict[typ.Any, typ.Any]:
    """Return the trigger mapping despite PyYAML's YAML 1.1 boolean key."""
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "the Loom workflow must declare triggers"
    return triggers


def _loom_job(workflow: dict[typ.Any, typ.Any]) -> dict[str, object]:
    """Return the single bounded-model job with validated mapping shape."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the Loom workflow must declare jobs"
    job = jobs.get("loom")
    assert isinstance(job, dict), "the Loom workflow must define the loom job"
    return typ.cast("dict[str, object]", job)


def _run_steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return run steps from a valid job definition."""
    steps = job.get("steps")
    assert isinstance(steps, list), "the Loom job must declare steps"
    return [step for step in steps if isinstance(step, dict) and "run" in step]


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


def test_smoke_job_uses_the_same_loom_shape_and_driver() -> None:
    """Relevant pull requests compile and execute the smaller deterministic set."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert isinstance(ci, dict), "ci.yml must parse to a mapping"
    jobs = ci.get("jobs")
    assert isinstance(jobs, dict), "ci.yml must declare jobs"
    smoke = jobs.get("loom-smoke")
    assert isinstance(smoke, dict), "ci.yml must define loom-smoke"
    assert smoke.get("needs") == "changes", "smoke must await path detection"
    assert smoke.get("if") == "needs.changes.outputs.rust == 'true'", (
        "smoke must run for Rust and Loom changes"
    )
    assert smoke.get("runs-on") == "ubuntu-latest", "smoke must use hosted Linux"
    assert smoke.get("timeout-minutes") == 10, "smoke needs an explicit budget"
    steps = smoke.get("steps")
    assert isinstance(steps, list), "smoke must declare steps"
    cache_step = next(
        step for step in steps if step.get("name") == "Compute cache keys"
    )
    assert cache_step["with"]["compiler-shape"] == "loom", (
        "smoke must use the isolated Loom cache family"
    )
    run_step = next(
        step for step in steps if step.get("name") == "Run Loom smoke models"
    )
    assert script_runs_command(
        run_step["run"],
        "uv run scripts/run_loom.py --mode smoke --summary loom-summary.md",
    ), "smoke must execute the bounded Loom driver"
