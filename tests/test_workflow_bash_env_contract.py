"""Contract for the ``BASH_ENV`` policy of both workflow ``run_bash`` helpers.

``tests/helpers/release_workflow.py`` and ``tests/helpers/workflow_steps.py``
execute checked-in ``run:`` bodies under ``bash -c`` so the contract exercises
the workflow's own script rather than a restatement of it. Bash sources the
file named by ``BASH_ENV`` for every non-interactive shell, so an ambient value
from a developer's host would run setup ahead of that script and silently
change what the step does. GitHub Actions does not set the variable, so neither
helper forwards an ambient one; both drop it and then layer the caller's
``env`` over the result, which keeps an explicit value working.

Neither rule is visible in the workflow YAML, and neither is exercised by the
step tests that happen to run the helpers: those pass no ``BASH_ENV`` and run
on hosts that set none, so both the drop and the preserve could be deleted
without moving a single assertion. These tests pin both directions directly.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers import release_workflow, workflow_steps

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_MARKER = "CUPRUM_BASH_ENV_SOURCED"
#: A startup file the checked-in script must not have run for it.
_AMBIENT_STARTUP = f"export {_MARKER}=ambient"
#: A startup file a caller asked for by name, which must still run.
_EXPLICIT_STARTUP = f"export {_MARKER}=explicit"
_REPORT = f"printenv {_MARKER}"


def _helpers() -> cabc.Iterator[tuple[str, cabc.Callable[..., typ.Any]]]:
    """Yield each helper's ``run_bash`` under a name naming its module."""
    yield "release_workflow", release_workflow.run_bash
    yield "workflow_steps", workflow_steps.run_bash


@pytest.mark.parametrize(
    ("name", "run_bash"),
    list(_helpers()),
    ids=["release_workflow", "workflow_steps"],
)
def test_ambient_bash_env_is_not_sourced(
    name: str,
    run_bash: cabc.Callable[..., typ.Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient ``BASH_ENV`` never reaches the checked-in step.

    The variable is set in this process's own environment, exactly as a
    developer's shell profile would, and ``tmp_path`` plays the checked-in
    ``cwd``. If the helper forwarded it, Bash would source the file before
    running the script and ``printenv`` would report ``ambient``.
    """
    startup = tmp_path / "ambient-startup.sh"
    startup.write_text(_AMBIENT_STARTUP, encoding="utf-8")
    monkeypatch.setenv("BASH_ENV", str(startup))

    completed = run_bash(_REPORT, tmp_path)

    assert completed.returncode == 1, (
        f"{name} must drop an ambient BASH_ENV, so the marker it sets must be "
        f"absent: printenv exits non-zero, got {completed.returncode}"
    )
    # ``stdout`` is ``str`` for a text-mode run, so truthiness is exactly the
    # empty check; ``== ""`` is C1804, which this repository enables.
    assert not completed.stdout, (
        f"{name} sourced the ambient startup file it was supposed to drop"
    )


@pytest.mark.parametrize(
    ("name", "run_bash"),
    list(_helpers()),
    ids=["release_workflow", "workflow_steps"],
)
def test_explicit_bash_env_is_sourced(
    name: str,
    run_bash: cabc.Callable[..., typ.Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``BASH_ENV`` in ``env`` still reaches the checked-in step.

    Dropping the ambient variable must not turn into refusing the setting
    outright: the ``env`` mapping is layered over the scrubbed environment
    afterwards, so a caller that names a startup file gets it. A step that
    deliberately needs one would otherwise be untestable.
    """
    startup = tmp_path / "explicit-startup.sh"
    startup.write_text(_EXPLICIT_STARTUP, encoding="utf-8")
    # An ambient value is present too, so the assertion distinguishes "the
    # caller's file won" from "nothing was sourced at all".
    monkeypatch.setenv("BASH_ENV", str(tmp_path / "ambient-startup.sh"))

    completed = run_bash(_REPORT, tmp_path, {"BASH_ENV": str(startup)})

    assert completed.returncode == 0, (
        f"{name} must preserve a caller-supplied BASH_ENV, so the marker it "
        f"sets must be present, got {completed.returncode}"
    )
    assert completed.stdout.strip() == "explicit", (
        f"{name} must prefer the caller's BASH_ENV over the ambient one"
    )
