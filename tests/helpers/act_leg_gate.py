"""Run the real leg flag of `typecheck-test` under `act`, one leg at a time.

The contracts in `tests/test_ci_experimental_leg.py` read the flag and the
step guards as text. They cannot say what the runner makes of them: whether
`${{ !(...) }}` renders into `env` as the string the guards compare against,
and whether the guards then skip the experimental leg on a pull request while
every other leg, and every leg on a push, still does its work.

This module answers that by projecting the job into a throwaway workflow and
running one matrix leg under `act`. The projection keeps everything that
decides which steps run, the job's `env`, its matrix, and each step's `id`,
`name` and `if:`, and replaces each step's body with `true`. With no body left
that needs a container, the leg runs in `act`'s host mode, so a scenario costs
seconds and pulls no image. It is the same move `tests.helpers.act_workflow`
makes for the benchmark job's expensive prerequisites.

Scope: `ci.yml`'s `typecheck-test` only. The scenario repository, the event and
the stream reader are the harness's own (`tests.helpers.act_harness`,
`tests.helpers.act_event`, `tests.helpers.act_stream`).
"""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - required act process boundary with explicit argv and timeout.
import typing as typ

import yaml

from tests.helpers.act_event import Event, event_payload
from tests.helpers.act_harness import CI_WORKFLOW
from tests.helpers.act_stream import ActRun
from tests.helpers.workflow import job, parse_workflow, steps

if typ.TYPE_CHECKING:
    import pathlib as pth

__all__ = ("LEG_JOB", "ran_steps", "run_leg", "stage_leg_probe")

#: The job whose legs the flag gates.
LEG_JOB: typ.Final = "typecheck-test"
#: The label every projected leg runs on, and `act`'s host-mode mapping for it.
_PROBE_LABEL = "ubuntu-latest"
_HOST_MODE = f"{_PROBE_LABEL}=-self-hosted"
#: A probe step's whole body: it succeeds and does nothing.
_PROBE_BODY = "true"
#: The job keys a leg's step selection depends on. `runs-on` is replaced, since
#: the real label is a paid runner `act` cannot provide.
_KEPT_JOB_KEYS = ("name", "continue-on-error", "strategy", "env")
#: The step keys that decide whether a step runs, or that a guard refers to.
_KEPT_STEP_KEYS = ("id", "name", "if")
#: The steps `act` reports for every job, whatever its guards say.
_FRAME_STEPS = frozenset({"Set up job", "Complete job"})
#: Where the event payload is written inside the scenario's repository.
_EVENT_PATH = ".act-event.json"
#: `owner/name` the scenario reports as its repository.
_REPOSITORY = "cuprum/act-harness"
#: Bound on one leg. A host-mode leg of twenty probes takes a few seconds.
_TIMEOUT_SECONDS = 120.0


def stage_leg_probe(target: pth.Path, worktree: pth.Path) -> None:
    """Write `typecheck-test`, its step bodies replaced by probes, into ``target``.

    The job is read from the repository rather than from a fixture, so a
    scenario runs the flag and the guards that are checked in.

    Parameters
    ----------
    target : pathlib.Path
        Destination root. Its `.github/workflows` directory is created.
    worktree : pathlib.Path
        Repository to copy from.
    """
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    parsed = parse_workflow((worktree / CI_WORKFLOW).read_text(encoding="utf-8"))
    real = job(parsed, LEG_JOB)
    probe: dict[str, object] = {key: real[key] for key in _KEPT_JOB_KEYS if key in real}
    probe["runs-on"] = _PROBE_LABEL
    probe["steps"] = [
        {
            **{key: step[key] for key in _KEPT_STEP_KEYS if key in step},
            "run": _PROBE_BODY,
        }
        for step in steps(parsed, LEG_JOB)
    ]
    document: dict[str, object] = dict(parsed)
    document["jobs"] = {LEG_JOB: probe}
    (target / CI_WORKFLOW).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def run_leg(repository: pth.Path, event: Event, label: str) -> ActRun:
    """Run one leg of the projected job under `act` in host mode.

    Parameters
    ----------
    repository : pathlib.Path
        Repository holding the projected workflow, at the event's head.
    event : Event
        Event to replay.
    label : str
        The leg's `python-label` matrix value, such as ``"3.15a"``.

    Returns
    -------
    ActRun
        The exit status, JSON stream, diagnostics, and argv.
    """
    payload = repository / _EVENT_PATH
    payload.write_text(json.dumps(event_payload(event, _REPOSITORY)), encoding="utf-8")
    argv = (
        "act",
        event.name,
        "-W",
        CI_WORKFLOW,
        "-j",
        LEG_JOB,
        "-P",
        _HOST_MODE,
        "--matrix",
        f"python-label:{label}",
        "-e",
        _EVENT_PATH,
        "--json",
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - explicit harness argv; no shell interpretation.
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        cwd=repository,
    )
    return ActRun(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        argv=argv,
    )


def ran_steps(run: ActRun) -> set[str]:
    """Return the names of the workflow's own steps that ran.

    `act` reports a verdict only for a step it ran, so a skipped step is simply
    absent. The frame every job gets is removed, leaving the job's own steps.

    Parameters
    ----------
    run : ActRun
        The finished `act` run whose step verdicts to read.

    Returns
    -------
    set[str]
        The names of the steps that ran, whatever their verdict.
    """
    return set(run.step_results) - _FRAME_STEPS
