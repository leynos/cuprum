"""Both workflow-step helpers must drop an ambient ``BASH_ENV``.

Bash sources the file named by ``BASH_ENV`` for every non-interactive shell, so
a host that sets it runs its own setup ahead of the checked-in step the tests
mean to exercise. The failure that motivated this contract was concrete: a
harness exported ``BASH_ENV`` pointing at a file that prepended a ``gh`` wrapper
to ``PATH``, and the release-pipeline suite's recording fake was displaced by
that wrapper, so thirteen tests died with ``fatal: not a git repository``.
GitHub Actions does not set the variable, so a helper that forwards the
developer's copy tests something the runner never would.

The helpers under test are ``run_bash`` in ``tests/helpers/release_workflow.py``
and its twin in ``tests/helpers/workflow_steps.py``. Both must scrub the ambient
value; neither may scrub an explicit one a caller passed, because that is how a
test would deliberately exercise the variable.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as typ

import pytest

from tests.helpers.release_workflow import run_bash as release_run_bash
from tests.helpers.workflow_steps import run_bash as steps_run_bash

if typ.TYPE_CHECKING:
    import pathlib as pth
    import subprocess

type RunBash = cabc.Callable[..., subprocess.CompletedProcess[str]]

#: Written to the file ``BASH_ENV`` names, so its output proves whether the
#: shell sourced it before running the script it was actually given.
_HOST_SETUP_MARKER = "HOST_SETUP_RAN"
#: Printed by the script under test, so a run that sourced nothing is still
#: distinguishable from a run that failed to start at all.
_SCRIPT_MARKER = "SCRIPT_RAN"


def _write_host_setup(directory: pth.Path) -> str:
    """Write a ``BASH_ENV`` file that announces itself, and return its path."""
    setup = directory / "host-setup.sh"
    setup.write_text(f"printf '%s\\n' {_HOST_SETUP_MARKER}\n", encoding="utf-8")
    return str(setup)


@pytest.mark.parametrize(
    "run_bash",
    [
        pytest.param(release_run_bash, id="release_workflow"),
        pytest.param(steps_run_bash, id="workflow_steps"),
    ],
)
def test_ambient_bash_env_is_scrubbed(
    run_bash: RunBash, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host ``BASH_ENV`` must not reach the shell the helper spawns."""
    monkeypatch.setenv("BASH_ENV", _write_host_setup(tmp_path))

    completed = run_bash(f"printf '%s\\n' {_SCRIPT_MARKER}", cwd=tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert _SCRIPT_MARKER in completed.stdout, "the helper must still run its script"
    assert _HOST_SETUP_MARKER not in completed.stdout, (
        "the helper forwarded the host's BASH_ENV, so host setup ran ahead of "
        "the script and changed what it did"
    )


@pytest.mark.parametrize(
    "run_bash",
    [
        pytest.param(release_run_bash, id="release_workflow"),
        pytest.param(steps_run_bash, id="workflow_steps"),
    ],
)
def test_explicit_bash_env_is_honoured(
    run_bash: RunBash, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``BASH_ENV`` the caller passes must still be sourced.

    Scrubbing the ambient value must not remove the ability to test the
    variable deliberately, which is the only way a caller would reach for it.
    """
    monkeypatch.delenv("BASH_ENV", raising=False)

    completed = run_bash(
        f"printf '%s\\n' {_SCRIPT_MARKER}",
        cwd=tmp_path,
        env={"BASH_ENV": _write_host_setup(tmp_path)},
    )

    assert completed.returncode == 0, completed.stderr
    assert _HOST_SETUP_MARKER in completed.stdout, (
        "the helper dropped a BASH_ENV the caller passed explicitly"
    )
