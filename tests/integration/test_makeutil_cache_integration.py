"""Run each consumer's makeutil install guard under `act` for a hit and a miss.

`tests/test_ci_makeutil_install.py` holds each consumer's guard as text. These
scenarios evaluate it with the runner's own expression engine. Each projects
one consumer job into a throwaway workflow: the job keeps its `env`, its
tool-cache restore becomes a step with the same `id` that reports a chosen
`cache-hit`, and its real "Install Makefile parser" step keeps its `if:` with
its body replaced by `true`. The `env` matters because `typecheck-test`'s guard
also reads its leg flag, which the projection runs outside any matrix leg, so
the flag renders as it does for a required leg. `act` then runs the job in
host mode, with no container and no image, and the scenario reads whether the
install ran.

`actions/cache` reports `true` for an exact hit, `false` for a restore-key hit,
and nothing on a total miss, so the install must be skipped only for `true`.

Like the other scenarios, these need `act` and are opt-in (`make test-act`).
"""

from __future__ import annotations

import pathlib as pth
import subprocess  # ruff: ignore[suspicious-subprocess-import] - required act process boundary with explicit argv and timeout.
import typing as typ

import pytest
import yaml

from tests.helpers.act_harness import harness_skip_reason
from tests.helpers.act_runtime import git, git_commit
from tests.helpers.act_stream import ActRun
from tests.helpers.ci_workflows import job_env, steps

#: The repository under test.
WORKTREE = pth.Path(__file__).resolve().parents[2]
#: A host-mode job of two probes takes seconds.
SCENARIO_TIMEOUT = 120
TOOL_CACHE_ID: typ.Final = "tool-cache"
INSTALL_STEP: typ.Final = "Install Makefile parser"
PROBE_JOB: typ.Final = "consumer"
PROBE_WORKFLOW: typ.Final = ".github/workflows/probe.yml"
#: Every job that installs makeutil.
CONSUMERS: typ.Final = (
    ("ci.yml", "typecheck-test"),
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)
#: What `actions/cache` reports, and whether the install must then run.
OUTCOMES: typ.Final = (("true", False), ("false", True), ("", True))


def _stage(root: pth.Path, workflow_name: str, job_name: str) -> None:
    """Write the consumer's restore and install, projected, into ``root``."""
    job_steps = steps(workflow_name, job_name)
    environment = job_env(workflow_name, job_name)
    [install] = [step for step in job_steps if step.get("name") == INSTALL_STEP]
    assert any(step.get("id") == TOOL_CACHE_ID for step in job_steps), (
        f"{workflow_name}:{job_name} must restore the tool cache"
    )
    document = {
        "name": "probe",
        "on": "push",
        "jobs": {
            PROBE_JOB: {
                "runs-on": "ubuntu-latest",
                "env": environment,
                "steps": [
                    {
                        "id": TOOL_CACHE_ID,
                        "name": "Restore the installed tools",
                        "run": 'echo "cache-hit=${HIT}" >> "$GITHUB_OUTPUT"',
                    },
                    {"name": INSTALL_STEP, "if": install.get("if"), "run": "true"},
                ],
            }
        },
    }
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / PROBE_WORKFLOW).write_text(yaml.safe_dump(document), encoding="utf-8")
    git(root, "init", "--quiet", "--initial-branch=main")
    git(root, "add", "--all")
    git_commit(root, message="stage the projected consumer")


def _run(root: pth.Path, hit: str) -> ActRun:
    """Run the projected job in host mode with the restore reporting ``hit``."""
    argv = (
        "act",
        "push",
        "-W",
        PROBE_WORKFLOW,
        "-j",
        PROBE_JOB,
        "-P",
        "ubuntu-latest=-self-hosted",
        "--env",
        f"HIT={hit}",
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
    return ActRun(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        argv=argv,
    )


@pytest.mark.timeout(SCENARIO_TIMEOUT)
@pytest.mark.parametrize("consumer", CONSUMERS, ids="{0[0]}:{0[1]}".format)
@pytest.mark.parametrize("outcome", OUTCOMES, ids=lambda outcome: repr(outcome[0]))
def test_the_install_runs_exactly_when_the_tool_cache_missed(
    tmp_path: pth.Path, consumer: tuple[str, str], outcome: tuple[str, bool]
) -> None:
    """An exact hit skips the build; a restore-key hit or a miss runs it."""
    reason = harness_skip_reason()
    if reason:
        pytest.skip(reason)
    workflow_name, job_name = consumer
    hit, should_install = outcome
    _stage(tmp_path, workflow_name, job_name)
    run = _run(tmp_path, hit)
    assert run.exit_code == 0, run.failure_context()
    installed = INSTALL_STEP in run.step_results
    assert installed == should_install, (
        f"{workflow_name}:{job_name} with cache-hit={hit!r} must "
        f"{'run' if should_install else 'skip'} the install"
    )
