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

Everything here is offline and credential-free: `github.token` is emptied so
the pinned `dorny/paths-filter` takes its local `git diff` path, and the
container is bound to the temporary clone rather than to the developer's
checkout. See `docs/adr-012-actions-runner-integration-harness.md` for why the
harness exists and `docs/local-validation-of-github-actions-with-act-and-pytest.md`
for the manual recipe it automates.

Three modules, one seam each: this one says what a scenario *is*, and neither
asks whether the host can run one (`tests.helpers.act_runtime`) nor how to read
`act`'s output (`tests.helpers.act_stream`).
"""

from __future__ import annotations

import dataclasses as dc
import json
import shutil
import typing as typ

from cuprum.sh import ExecutionContext
from tests.helpers.act_runtime import (
    ACT_AVAILABLE_ENV,
    SKIP_REASON_ENV,
    docker_host,
    git,
    git_commit,
    harness_skip_reason,
    run,
)
from tests.helpers.act_stream import ActRun

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = (
    "ACT_AVAILABLE_ENV",
    "CHANGES_JOB",
    "CI_WORKFLOW",
    "IMAGE",
    "SKIP_REASON_ENV",
    "ActRun",
    "Event",
    "commit_paths",
    "event_payload",
    "harness_skip_reason",
    "prepare_repository",
    "run_act",
    "stage_repository",
)

#: The workflow under test, relative to the repository root.
CI_WORKFLOW = ".github/workflows/ci.yml"
#: The job that owns the benchmark gate decision.
CHANGES_JOB = "changes"
#: The pinned runner image. `act` maps a workflow's `runs-on` label onto this
#: through `-P`; pinning it by tag is what makes a scenario reproducible.
IMAGE = "catthehacker/ubuntu:act-latest"
#: Bound on one scenario. Warm runs measured at 15-27s on the development
#: machine, and a cold image pull is the only thing that takes longer, so a
#: hung container is the realistic failure this catches.
_TIMEOUT_SECONDS = 300.0
#: Where the event payload is written inside the scenario's repository.
_EVENT_PATH = ".act-event.json"
#: `owner/name` the scenario reports as its repository, matching the
#: `full_name` the payload builder fills in.
_REPOSITORY = "cuprum/act-harness"


@dc.dataclass(frozen=True, slots=True)
class Event:
    """One GitHub event to replay through the workflow.

    Attributes
    ----------
    name : str
        Event name, as `github.event_name` would carry it.
    payload : dict[str, object]
        Webhook payload written to the event-path file. `act` injects
        `github.event_name`, so the payload carries only the body.
    ref : str
        Value for `github.ref`.
    sha : str
        Value for `github.sha`. A pull request is checked out at its head;
        a push is checked out at the commit that was pushed.
    branch : str
        Value for `github.ref_name`, without the `refs/heads/` prefix.
    """

    name: str
    payload: dict[str, object]
    ref: str
    sha: str
    branch: str


def event_payload(event: Event, repository: str) -> dict[str, object]:
    """Return the event body `act` should deliver, with its repository filled in.

    Parameters
    ----------
    event : Event
        The event to deliver. Its payload is shallow-copied, never mutated.
    repository : str
        ``owner/name`` for the temporary repository the scenario runs in. The
        workflow reads `repository.default_branch` to resolve a push's base.

    Returns
    -------
    dict[str, object]
        The complete webhook payload.
    """
    payload = dict(event.payload)
    payload["repository"] = {
        "full_name": repository,
        "default_branch": "main",
        "html_url": f"https://github.com/{repository}",
    }
    payload["sender"] = {"login": "act-harness"}
    return payload


def run_act(repository: pth.Path, event: Event) -> ActRun:
    """Run the `changes` job under `act` and return what it produced.

    Parameters
    ----------
    repository : pathlib.Path
        Repository to bind into the container. It must already hold the
        workflow, its local actions, and be checked out at the event's head
        commit.
    event : Event
        Event to replay.

    Returns
    -------
    ActRun
        The exit status, JSON stream, diagnostics, and argv.
    """
    payload = repository / _EVENT_PATH
    payload.write_text(json.dumps(event_payload(event, _REPOSITORY)), encoding="utf-8")
    # The scenario runs through Cuprum itself, so the repository's own command
    # runner is the thing exercising its own workflow. The overlay is layered
    # onto the live environment, so the rest of `os.environ` still reaches
    # `act`; only the runtime socket is pinned.
    context = ExecutionContext(cwd=str(repository), env={"DOCKER_HOST": docker_host()})
    result = run("act", *_act_argv(event)).run_sync(
        timeout=_TIMEOUT_SECONDS, context=context
    )
    return ActRun(
        exit_code=result.exit_code,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        argv=(str(result.program), *result.argv),
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
    git(root, "init", "--initial-branch=main", "--quiet")
    stage_repository(root, worktree)
    # The scenario repository is what `dorny/paths-filter` diffs, and it binds
    # no remote: with an empty `github.token` the action resolves its base from
    # the local branch, so a `refs/remotes/origin/*` reference would only add an
    # ambiguity for it to resolve.
    git(root, "add", "--all")
    git_commit(root, message="stage the workflow under test")
    return root


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
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(worktree / CI_WORKFLOW, target / CI_WORKFLOW)
    source = worktree / ".github" / "actions"
    if source.is_dir():
        shutil.copytree(source, target / ".github" / "actions", dirs_exist_ok=True)


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


def _act_argv(event: Event) -> list[str]:
    """Build the `act` arguments for one scenario."""
    return [
        event.name,
        # `-W` names the workflow explicitly. act otherwise runs every
        # workflow it finds, and the repository under test defines many.
        "-W",
        CI_WORKFLOW,
        "-j",
        CHANGES_JOB,
        # One pinned label suffices: `changes` is the only job this runs, and
        # it declares `runs-on: ubuntu-latest`.
        "-P",
        f"ubuntu-latest={IMAGE}",
        # An empty token routes `dorny/paths-filter` onto its local `git diff`
        # path instead of the GitHub API, which is what makes this offline.
        "-s",
        "GITHUB_TOKEN=",
        "-e",
        _EVENT_PATH,
        "--json",
    ]
