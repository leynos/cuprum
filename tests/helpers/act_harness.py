"""Run the real `changes` job under `act` and report what it decided.

The contract tests read `ci.yml`; the behavioural tests extract one `run:`
block and execute it under `bash`. Neither touches the boundary that matters:
whether `dorny/paths-filter` actually produces the `bench` output the gate
assumes, whether an event payload reaches the gate the way a real event would,
and whether a failing detector still leaves a decision recorded.

This module supplies that boundary. It builds a throwaway repository whose
history *is* the changed-path set under test, copies the real workflow and the
action's dependencies into it, and invokes `act` against it. The step summary,
the named outputs, and the step verdicts are read back from `act`'s JSON stream
by `tests.helpers.act_stream`, because the in-container summary file is
truncated once it has been uploaded.

Scenarios use local Git history and no credentials: `github.token` is emptied so
the pinned `dorny/paths-filter` takes its local `git diff` path, and the
container is bound to the temporary clone rather than to the developer's
checkout. See `docs/adr-015-actions-runner-integration-harness.md` for why the
harness exists and `docs/local-validation-of-github-actions-with-act-and-pytest.md`
for the manual recipe it automates.

Five modules, one seam each: this one says what a scenario *is*. It never asks
whether the host can run one (`tests.helpers.act_runtime`), how to read `act`'s
output (`tests.helpers.act_stream`), how the workflow it runs is derived from
the repository's own (`tests.helpers.act_workflow`), or what event is being
replayed into it (`tests.helpers.act_event`).
"""

from __future__ import annotations

import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - required act process boundary with explicit argv and timeout.
import typing as typ

from tests.helpers.act_event import (
    Event,
    EventName,
    event_payload,
)
from tests.helpers.act_runtime import (
    DOCKER_HOST_ENV,
    REQUIRE_ACT_ENV,
    SKIP_REASON_ENV,
    docker_host,
    git,
    git_commit,
    harness_skip_reason,
)
from tests.helpers.act_stream import ActRun
from tests.helpers.act_workflow import (
    break_detector_step,
    copy_actions,
    copy_workflow,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = (
    "CHANGES_JOB",
    "CI_WORKFLOW",
    "DEFAULT_BRANCH",
    "DOCKER_HOST_ENV",
    "IMAGE",
    "REQUIRE_ACT_ENV",
    "SKIP_REASON_ENV",
    "ActRun",
    "Event",
    "EventName",
    "branch",
    "break_detector",
    "commit_paths",
    "event_payload",
    "harness_skip_reason",
    "prepare_repository",
    "run_act",
    "stage_repository",
)

#: The workflow under test, relative to the repository root.
CI_WORKFLOW = ".github/workflows/ci.yml"
#: The branch `prepare_repository` leaves checked out. It is the base every
#: pull-request scenario branches from, and it matches the
#: `repository.default_branch` the payload builder declares.
DEFAULT_BRANCH = "main"
#: The job that owns the benchmark gate decision.
CHANGES_JOB = "changes"
#: The pinned runner image. `act` maps a workflow's `runs-on` label onto this
#: through `-P`; the immutable digest makes scenarios reproducible.
IMAGE = (
    "catthehacker/ubuntu:act-latest@sha256:"
    "c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43"
)
#: Bound on one scenario. Warm runs measured at 15-27s on the development
#: machine, and a cold image pull is the only thing that takes longer, so a
#: hung container is the realistic failure this catches.
_TIMEOUT_SECONDS = 300.0
#: Where the event payload is written inside the scenario's repository.
_EVENT_PATH = ".act-event.json"
#: `owner/name` the scenario reports as its repository, matching the
#: `full_name` the payload builder fills in.
_REPOSITORY = "cuprum/act-harness"


def run_act(
    repository: pth.Path, event: Event, *, job: str = CHANGES_JOB, image: str = IMAGE
) -> ActRun:
    """Run the `changes` job under `act` and return what it produced.

    Parameters
    ----------
    repository : pathlib.Path
        Repository to bind into the container. It must already hold the
        workflow, its local actions, and be checked out at the event's head
        commit.
    event : Event
        Event to replay.
    job : str
        Job to execute, including its dependencies.
    image : str
        Immutable runner image reference.

    Returns
    -------
    ActRun
        The exit status, JSON stream, diagnostics, and argv.
    """
    payload = repository / _EVENT_PATH
    payload.write_text(json.dumps(event_payload(event, _REPOSITORY)), encoding="utf-8")
    argv = ("act", *_act_argv(event, job, image))
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - explicit harness argv; no shell interpretation.
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        cwd=repository,
        env={**os.environ, DOCKER_HOST_ENV: docker_host()},
    )
    return ActRun(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        argv=argv,
    )


def prepare_repository(root: pth.Path, worktree: pth.Path) -> pth.Path:
    """Build a repository carrying only the workflow under test.

    The point of this function is one property, and everything else follows
    from it: **the workflow is present in the base commit**.
    `.github/workflows/ci.yml` is itself one of the `bench` filter's patterns,
    so a repository that added the workflow in the head commit would register
    every scenario as a performance-relevant change — including the empty one —
    and the harness would be measuring its own scaffolding.

    Committing the workflow to a base commit first makes its later presence
    part of the repository's history rather than part of the diff, so each
    scenario's changed-path set is exactly the paths its commits touch.

    Parameters
    ----------
    root : pathlib.Path
        Directory to initialize.
    worktree : pathlib.Path
        Repository to copy the workflow and local actions from.

    Returns
    -------
    pathlib.Path
        ``root``, with one commit holding the staged workflow.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", f"--initial-branch={DEFAULT_BRANCH}", "--quiet")
    stage_repository(root, worktree)
    # The scenario repository is what `dorny/paths-filter` diffs, and it binds
    # no remote: with an empty `github.token` the action resolves its base from
    # the local branch, so a `refs/remotes/origin/*` reference would only add an
    # ambiguity for it to resolve.
    git(root, "add", "--all")
    git_commit(root, message="stage the workflow under test")
    return root


def branch(repository: pth.Path, name: str) -> None:
    """Create ``name`` off the current commit and check it out.

    The checked-out branch is what the detector diffs, not `github.sha`: with
    an empty `github.token` the pinned `dorny/paths-filter` falls back to
    `git diff <base> <current-branch>`. A scenario that wants the relevant and
    mixed changed-path sets therefore has to move the repository onto a branch
    whose name is not the default branch — otherwise the action diffs the
    default branch against a commit that already contains every scenario
    commit, observes no changes at all, and reports `bench=false` for a
    scenario that is plainly relevant.

    Parameters
    ----------
    repository : pathlib.Path
        Repository to branch. It must be clean and on the default branch.
    name : str
        Branch name to create and check out.
    """
    git(repository, "checkout", "--quiet", "-b", name)


def stage_repository(target: pth.Path, worktree: pth.Path) -> None:
    """Copy the workflow and the local actions into a scenario's repository.

    Both are taken from the repository under test rather than from a fixture,
    so a scenario runs the workflow that is actually checked in. The local
    actions are copied for the same reason: a composite action the job calls
    is part of the boundary, and a repository that omits it would fail the
    scenario rather than exercise it.

    Parameters
    ----------
    target : pathlib.Path
        Destination root. It must not already hold a workflow directory.
    worktree : pathlib.Path
        Repository to copy from.
    """
    copy_workflow(target, worktree, CI_WORKFLOW)
    copy_actions(target, worktree)


def commit_paths(repository: pth.Path, paths: list[str], *, message: str) -> str:
    """Commit ``paths`` into ``repository`` and return the resulting commit.

    A scenario's changed-path set has to be a real diff: the pinned
    `dorny/paths-filter` reads the repository's history with `git diff`, not a
    list of names handed to it. Materializing the set as a commit is what makes
    the scenario measure the detector rather than the fixture.

    A path named here is a file. Directories are not supported, because a
    scenario's intent is a set of *paths*, and a directory's contents would
    change the set as the repository evolves.

    Parameters
    ----------
    repository : pathlib.Path
        Repository to commit into, already prepared.
    paths : list[str]
        Repository-relative paths to create and commit. An empty list is the
        empty changed-path set, and still produces a commit.
    message : str
        Commit message, which also names the scenario in `git log`.

    Returns
    -------
    str
        The new commit's SHA, which is what an `Event` must carry as its head.
    """
    for path in paths:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path}\n", encoding="utf-8")
    git(repository, "add", "--all")
    git_commit(repository, message=message, allow_empty=True)
    return git(repository, "rev-parse", "HEAD").strip()


def break_detector(repository: pth.Path) -> str:
    """Make the pinned `dorny/paths-filter` step fail, and commit that.

    The detector-failure path is the one the gate exists for: when the detector
    fails, `bench` is empty rather than `false`, and the gate's decision has to
    say so rather than mistake "no answer" for "no relevant changes". Covering
    it means producing a real failure, not a mocked one; the edit itself lives
    in `tests.helpers.act_workflow`, and this wraps it in the commit that makes
    it a scenario's changed-path set.

    Parameters
    ----------
    repository : pathlib.Path
        Scenario repository holding the staged workflow.

    Returns
    -------
    str
        The new commit's SHA, so the caller can report it as the event head.
    """
    break_detector_step(repository, CI_WORKFLOW)
    git(repository, "add", "--all")
    git_commit(repository, message="break the detector")
    return git(repository, "rev-parse", "HEAD").strip()


def _act_argv(event: Event, job: str, image: str) -> list[str]:
    """Build the `act` arguments for one scenario."""
    return [
        event.name,
        # `-W` names the workflow explicitly. act otherwise runs every
        # workflow it finds, and the repository under test defines many.
        "-W",
        CI_WORKFLOW,
        "-j",
        job,
        # Every projected job uses the same immutable runner image.
        "-P",
        f"ubuntu-latest={image}",
        # An empty token routes `dorny/paths-filter` onto its local `git diff`
        # path instead of the GitHub API.
        "-s",
        "GITHUB_TOKEN=",
        "-e",
        _EVENT_PATH,
        "--json",
    ]
