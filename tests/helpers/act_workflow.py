"""Stage the workflow under test, and edit it to induce a detector failure.

Two things here are about the workflow *file* rather than about a scenario:

- copying the real `ci.yml` — and any local actions it calls — into a
  throwaway repository, so a scenario runs what is actually checked in rather
  than a copy that has drifted; and
- breaking the detector step, so the gate's `detector_status=failure` path can
  be exercised against a real failure instead of a mocked one.

Both are line-oriented edits to YAML source with no `act` process and no git
history involved, which is what separates them from
`tests/helpers/act_harness.py`: that module says what a scenario *is*, this one
says how the workflow it runs is derived from the repository's own.
"""

from __future__ import annotations

import shutil
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = ("break_detector_step", "copy_actions", "copy_workflow")

#: The action whose failure the gate has to survive.
_DETECTOR_ACTION = "uses: dorny/paths-filter@"
#: The key that opens that action's inputs.
_DETECTOR_WITH = "with:"
#: The input whose misuse makes the detector fail, and the value that does it.
#: The action validates `list-files` against an enum, so an unknown value fails
#: the step before it diffs anything — which is the failure the gate exists for.
_DETECTOR_BREAKER = "list-files: bogus"


def copy_workflow(target: pth.Path, worktree: pth.Path, workflow: str) -> None:
    """Copy the workflow under test from the repository into a scenario.

    The workflow is taken from the repository rather than from a fixture, so a
    scenario runs the file that is actually checked in. A fixture would let the
    workflow and the harness drift apart while every scenario kept passing.

    Parameters
    ----------
    target : pathlib.Path
        Destination root. Its `.github/workflows` directory is created.
    worktree : pathlib.Path
        Repository to copy from.
    workflow : str
        Repository-relative path of the workflow to copy.
    """
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(worktree / workflow, target / workflow)


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

    The edit is confined to the detector step: the key is appended to that
    step's own `with:` block, at the block's indentation, so the rest of the
    workflow — including the gate step under test — is untouched. The caller
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
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    action = next(
        (index for index, line in enumerate(lines) if _DETECTOR_ACTION in line), None
    )
    message = (
        f"{workflow} has no {_DETECTOR_ACTION} step; the staged workflow changed "
        "shape and this scenario must be updated"
    )
    if action is None:
        raise AssertionError(message)
    # The step's own indentation anchors the edit, so the inserted key lands at
    # the depth of the keys already under `with:` however the step is indented
    # today. `with:` sits at the same depth as `uses:`; the search stops at the
    # next step so a `with:` further down the file cannot be mistaken for this
    # step's.
    step = lines[action]
    indent = step[: len(step) - len(step.lstrip())]
    marker = next(
        (
            index
            for index in range(action + 1, _step_end(lines, action, indent))
            if lines[index] == f"{indent}{_DETECTOR_WITH}\n"
        ),
        None,
    )
    message = (
        f"{workflow}'s {_DETECTOR_ACTION} step has no {_DETECTOR_WITH} block; the "
        "staged workflow changed shape and this scenario must be updated"
    )
    if marker is None:
        raise AssertionError(message)
    lines[marker] += f"{indent}  {_DETECTOR_BREAKER}\n"
    source.write_text("".join(lines), encoding="utf-8")


def _step_end(lines: list[str], start: int, indent: str) -> int:
    """Return the index just past the step that begins at ``start``.

    A step ends where the next one begins: the next list item indented less
    than this step's own keys. Anything else a step might contain is either
    deeper-indented or a blank line, so this bound is what keeps a search for
    one of the step's keys inside the step.

    Parameters
    ----------
    lines : list[str]
        Workflow source, split but not stripped, so indentation survives.
    start : int
        Index of the line the step's `uses:` or `run:` key sits on.
    indent : str
        That line's leading whitespace, which its sibling keys share.

    Returns
    -------
    int
        The exclusive end of the step, which is ``len(lines)`` for the last.
    """
    for index in range(start + 1, len(lines)):
        line = lines[index]
        outer = len(line) - len(line.lstrip())
        if line.strip().startswith("- ") and outer < len(indent):
            return index
    return len(lines)
