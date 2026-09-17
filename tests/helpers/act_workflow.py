"""Stage the workflow under test, and edit it to induce a detector failure.

Two things here are about the workflow *file* rather than about a scenario:

- copying the real `ci.yml` — and any local actions it calls — into a
  throwaway repository, so a scenario runs what is actually checked in rather
  than a copy that has drifted; and
- breaking the detector step, so the gate's `detector_status=failure` path can
  be exercised against a real failure instead of a mocked one.

Both are structural edits to YAML source with no `act` process and no git
history involved, which is what separates them from
`tests/helpers/act_harness.py`: that module says what a scenario *is*, this one
says how the workflow it runs is derived from the repository's own.
"""

from __future__ import annotations

import shutil
import typing as typ

import yaml

from tests.helpers.workflow import job, mapping, parse_workflow, steps

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = ("break_detector_step", "copy_actions", "copy_workflow")


def copy_workflow(target: pth.Path, worktree: pth.Path, workflow: str) -> None:
    """Project the real detector and admission boundary into a scenario.

    The workflow is taken from the repository rather than from a fixture, so a
    scenario preserves the changes job and benchmark needs/condition. Expensive
    prerequisite and benchmark bodies become success/admission probes. A fixture
    would let the
    workflow and the harness drift apart while every scenario kept passing.

    Parameters
    ----------
    target : pathlib.Path
        Destination root. Its `.github/workflows` directory is created.
    worktree : pathlib.Path
        Repository to copy from.
    workflow : str
        Repository-relative path of the workflow to copy.

    Raises
    ------
    AssertionError
        If the benchmark dependency list is malformed.
    """
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    parsed = parse_workflow((worktree / workflow).read_text(encoding="utf-8"))
    steps(parsed, "changes")
    benchmark = job(parsed, "benchmark-ratchet")
    dependencies = benchmark.get("needs")
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        message = "benchmark-ratchet must declare a list of dependency names"
        raise AssertionError(message)
    projected: dict[str, object] = {"changes": job(parsed, "changes")}
    for dependency in typ.cast("list[str]", dependencies):
        job(parsed, dependency)
        if dependency != "changes":
            projected[dependency] = {
                "runs-on": "ubuntu-latest",
                "steps": [{"run": "true"}],
            }
    projected["benchmark-ratchet"] = {
        "runs-on": "ubuntu-latest",
        "needs": benchmark["needs"],
        "if": benchmark["if"],
        "steps": [
            {
                "name": "Record benchmark admission",
                "run": 'echo "benchmark_admitted=true" >> "$GITHUB_OUTPUT"',
            }
        ],
    }
    document: dict[str, object] = dict(parsed)
    document["jobs"] = projected
    (target / workflow).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def copy_actions(target: pth.Path, worktree: pth.Path) -> None:
    """Copy the repository's local composite actions into a scenario.

    A composite action the job calls is part of the boundary under test, so a
    repository that omitted it would fail the scenario rather than exercise it.
    A repository with no local actions is not an error.

    Parameters
    ----------
    target : pathlib.Path
        Destination root. It must already hold a workflow directory.
    worktree : pathlib.Path
        Repository to copy from.
    """
    source = worktree / ".github" / "actions"
    if not source.is_dir():
        return
    shutil.copytree(source, target / ".github" / "actions", dirs_exist_ok=True)


def break_detector_step(repository: pth.Path, workflow: str) -> None:
    """Make the pinned `dorny/paths-filter` step fail, in place.

    The detector-failure path is the one the gate exists for: when the detector
    fails, `bench` is empty rather than `false`, and the gate's decision has to
    say so rather than mistake "no answer" for "no relevant changes". Covering
    it means producing a real failure, not a mocked one, so this appends an
    invalid value for one of the action's own `with:` inputs. The action
    validates the input against a fixed set and exits before it diffs anything,
    which is exactly the shape of a detector that cannot answer.

    The structural edit changes only the detector's input mapping. The caller
    commits the result.

    Parameters
    ----------
    repository : pathlib.Path
        Scenario repository holding the staged workflow.
    workflow : str
        Repository-relative path of the workflow to edit.

    Raises
    ------
    AssertionError
        If the staged workflow no longer has the shape this expects. A silent
        no-op here would turn the scenario into a duplicate of the healthy
        detector case and still pass.
    """
    source = repository / workflow
    document = parse_workflow(source.read_text(encoding="utf-8"))
    detectors = [
        step
        for step in steps(document, "changes")
        if str(step.get("uses", "")).startswith("dorny/paths-filter@")
    ]
    if len(detectors) != 1:
        message = "expected exactly one real detector step"
        raise AssertionError(message)
    inputs = mapping(detectors[0].get("with"), "detector must declare its inputs")
    inputs["list-files"] = "bogus"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
