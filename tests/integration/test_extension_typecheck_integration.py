"""Run `extension-tests`' own guards under `act` and read the order its steps ran.

`tests/test_ci_extension_typecheck.py` holds the job's shape as text. These
scenarios execute it. Each projects the checked-in job into a throwaway
workflow, keeping every step's `id`, `name` and `if:` and replacing each body
with `true`, and runs it in `act`'s host mode, with no container and no image.
The restore steps then report nothing, which is a tool-cache miss. The runner's
own expression engine decides every guard, and the scenario reads which steps
ran, and in what order.

Like the other scenarios, these need `act` and are opt-in (`make test-act`).
"""

from __future__ import annotations

import json
import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - required act process boundary with explicit argv and timeout.
import typing as typ

import pytest
import yaml

from tests.helpers.act_event import Event, EventName, event_payload
from tests.helpers.act_harness import DEFAULT_BRANCH, commit_paths, harness_skip_reason
from tests.helpers.act_runtime import git, git_commit
from tests.helpers.act_stream import ActRun
from tests.helpers.ci_workflows import steps

#: The repository under test.
WORKTREE = pth.Path(__file__).resolve().parents[2]
#: A host-mode job of probe steps takes seconds.
SCENARIO_TIMEOUT = 120
JOB: typ.Final = "extension-tests"
WORKFLOW: typ.Final = ".github/workflows/probe.yml"
EVENT_FILE: typ.Final = ".act-event.json"
SUITE: typ.Final = "Run extension-gated tests"
TYPECHECK: typ.Final = "Run typechecker"
INSTALL: typ.Final = "Install Makefile parser"
TOOL_SAVE: typ.Final = "Save the installed tools"
#: The steps `act` reports for every job, whatever its guards say.
FRAME: typ.Final = frozenset({"Set up job", "Complete job"})


def _stage(root: pth.Path) -> None:
    """Write the projected job into ``root`` and commit it on the default branch."""
    projected = [
        {
            **{
                key: value
                for key, value in typ.cast("dict[str, object]", step).items()
                if key in {"id", "name", "if"}
            },
            "run": "true",
        }
        for step in steps("ci.yml", JOB)
    ]
    document = {
        "name": "probe",
        "on": ["push", "pull_request"],
        "jobs": {JOB: {"runs-on": "ubuntu-latest", "steps": projected}},
    }
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / WORKFLOW).write_text(yaml.safe_dump(document), encoding="utf-8")
    git(root, "init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}")
    git(root, "add", "--all")
    git_commit(root, message="stage the projected job")


def _event(root: pth.Path, name: EventName) -> Event:
    """Return a pull request from a feature branch, or a push to main."""
    fixture = WORKTREE / "tests" / "fixtures" / "events" / f"{name}-empty.event.json"
    payload = typ.cast("dict[str, object]", json.loads(fixture.read_text()))
    if name is EventName.PULL_REQUEST:
        git(root, "checkout", "--quiet", "-b", "feature")
        head = commit_paths(root, [], message="scenario change")
        return Event(name, payload, "refs/pull/1/merge", head, "feature")
    head = commit_paths(root, [], message="scenario change")
    return Event(name, payload, f"refs/heads/{DEFAULT_BRANCH}", head, DEFAULT_BRANCH)


def _run(root: pth.Path, event: Event) -> ActRun:
    """Run the projected job in host mode for ``event``."""
    (root / EVENT_FILE).write_text(
        json.dumps(event_payload(event, "cuprum/act-harness")), encoding="utf-8"
    )
    argv = (
        "act",
        str(event.name),
        "-W",
        WORKFLOW,
        "-j",
        JOB,
        "-P",
        "ubuntu-latest=-self-hosted",
        "-e",
        EVENT_FILE,
        "--json",
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - explicit harness argv; no shell interpretation.
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=SCENARIO_TIMEOUT,
        cwd=root,
    )
    return ActRun(result.returncode, result.stdout, result.stderr, argv)


def _ran_in_order(run: ActRun) -> list[str]:
    """Return the job's own steps that ran, in the order `act` reported them."""
    return [name for name in run.step_results if name not in FRAME]


@pytest.mark.timeout(SCENARIO_TIMEOUT)
@pytest.mark.parametrize("event_name", list(EventName))
def test_the_typecheck_follows_the_suite_and_the_parser_precedes_the_save(
    tmp_path: pth.Path, event_name: EventName
) -> None:
    """On a miss the job installs the parser, runs the suite, then typechecks.

    Only a push saves the tool family, and the parser is installed before that
    save, so the archive a consumer's exact hit restores carries it.
    """
    reason = harness_skip_reason()
    if reason:
        pytest.skip(reason)
    root = tmp_path / "repo"
    root.mkdir()
    _stage(root)
    run = _run(root, _event(root, event_name))
    assert run.exit_code == 0, run.failure_context()
    ran = _ran_in_order(run)
    for name in (INSTALL, SUITE, TYPECHECK):
        assert name in ran, f"{JOB} must run {name!r} on {event_name}: {ran}"
    assert ran.index(SUITE) < ran.index(TYPECHECK), (
        f"{JOB} must typecheck after its gated modules: {ran}"
    )
    is_push = event_name is EventName.PUSH
    assert (TOOL_SAVE in ran) == is_push, (
        f"{JOB} must save the tool family on a push only: {ran}"
    )
    if is_push:
        assert ran.index(INSTALL) < ran.index(TOOL_SAVE), (
            f"{JOB} must install the parser before saving the tool family: {ran}"
        )
