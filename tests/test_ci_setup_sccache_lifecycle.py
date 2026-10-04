"""Hold the order of the setup-sccache steps, and what the server sees.

sccache reads its cache configuration once, when the server starts, and never
rebinds it. So the start has to be the action's last step, after the backend
selection and the wrapper export, or the server binds whatever was configured
before them. `test_ci_setup_sccache_server_start.py` runs the start step alone and
cannot see where it sits in the manifest; `test_ci_setup_sccache_action.py` runs
the install step alone and cannot see whether a later step receives what it
exported. These do both.

The first test reads the manifest's whole step list. The second runs the
install step, hands the variables it wrote to ``GITHUB_ENV`` to the start step
the way the runner does between steps, and asserts the server process itself
saw them. Moving the start ahead of the install would fail the first; a start
that did not receive the exports would fail the second.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.composite_actions import action_document, run_step, step_script
from tests.test_ci_setup_sccache_action import (
    ACTION,
    INSTALL_STEP,
    PROXY_CREDENTIALS,
    _run_install,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

START_STEP = "Start the sccache server"


def _step_names() -> list[str]:
    """Return the action's step names in manifest order."""
    steps = typ.cast(
        "list[dict[str, object]]",
        typ.cast("dict[str, object]", action_document(ACTION)["runs"])["steps"],
    )
    return [str(step["name"]) for step in steps]


def test_the_server_start_is_the_last_step_and_follows_the_install() -> None:
    """The start comes after every export that could change what it binds."""
    names = _step_names()

    assert names[-1] == START_STEP, (
        f"the server must start in the action's last step, got order {names}"
    )
    assert names.index(INSTALL_STEP) < names.index(START_STEP), (
        f"the install step must precede the start, got order {names}"
    )


@pytest.mark.parametrize(
    ("backend", "credentials", "expected"),
    [
        ("local", {}, "SCCACHE_DIR"),
        ("gha", PROXY_CREDENTIALS, "SCCACHE_GHA_ENABLED"),
    ],
    ids=["local-directory", "actions-service"],
)
def test_the_server_process_sees_what_the_install_exported(
    tmp_path: Path, backend: str, credentials: dict[str, str], expected: str
) -> None:
    """The start runs in a later process, so it sees only what was exported."""
    installed = _run_install(tmp_path, {"backend": backend}, credentials)
    assert installed.returncode == 0, installed.stderr
    binary = tmp_path / ".local" / "bin" / "sccache"
    seen = tmp_path / "environment-at-start"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--start-server" ]; then\n'
        f'  env > "{seen}"\n'
        "fi\n"
        'if [ "$1" = "--stop-server" ]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    outputs = tmp_path / "github_output"
    outputs.touch()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)

    started = run_step(
        step_script(ACTION, START_STEP),
        workdir=tmp_path,
        environment={
            **installed.exported,
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(outputs),
        },
    )

    assert started.returncode == 0, started.stderr
    environment_at_start = dict(
        line.split("=", 1)
        for line in seen.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert expected in environment_at_start, (
        f"the server must start with {expected} from the install step in its "
        f"environment, saw {sorted(environment_at_start)}"
    )
    assert environment_at_start.get("RUSTC_WRAPPER") == installed.exported.get(
        "RUSTC_WRAPPER"
    ), "the server must see the wrapper the install step exported"
