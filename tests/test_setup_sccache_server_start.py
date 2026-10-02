"""Execute the setup-sccache action's server start under controlled fixtures.

sccache's server probes its cache backend as its first act, and on Ubicloud
that probe intermittently outlasts the fixed 10 s startup timeout, which used
to fail the whole job. The action now writes a 60 s config and treats a server
that will not start as a lost optimization, not a failed build. A green run
cannot show which of those happened, so these tests run the start step's own
``run`` body with a fake ``sccache`` and assert each signal a fallback must
leave: the annotation title, the run-page line, the ``status`` output and the
cleared wrapper.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.composite_actions import (
    StepResult,
    action_document,
    run_step,
    step_script,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION = ".github/actions/setup-sccache"
START_STEP = "Start the sccache server"
#: The four signals of a fallback. The title and the summary line are what
#: ``sccache-fallbacks.py`` and a person reading the run page search for, so
#: renaming either is a contract change, not a refactor.
FALLBACK_TITLE = "::warning title=sccache-fallback::"
FALLBACK_SUMMARY = "sccache: FALLBACK (cache disabled for this job)"


def _run_start(tmp_path: Path, *, starts: bool) -> tuple[StepResult, Path, Path]:
    """Run the start step against a fake binary that does or does not start.

    The fake records each argument it receives, one per line, so a test can
    tell whether the step zeroed counters or touched statistics.

    Returns
    -------
    tuple[StepResult, Path, Path]
        The step result, the file ``GITHUB_OUTPUT`` was appended to, and the
        fake's call log.
    """
    binary = tmp_path / ".local" / "bin" / "sccache"
    binary.parent.mkdir(parents=True)
    calls = tmp_path / "sccache-calls"
    start_status = 0 if starts else 1
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$1" >> "{calls}"\n'
        f'[ "$1" = "--start-server" ] && exit {start_status}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    outputs = tmp_path / "github_output"
    outputs.touch()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = run_step(
        step_script(ACTION, START_STEP),
        workdir=tmp_path,
        environment={
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(outputs),
        },
    )
    return result, outputs, calls


def _calls(calls: Path) -> list[str]:
    """Return the fake's recorded arguments, or none if it never ran."""
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


def test_the_action_publishes_the_start_status_as_an_output() -> None:
    """A caller can only guard its statistics step on an output that exists."""
    document = action_document(ACTION)
    outputs = typ.cast("dict[str, dict[str, str]]", document["outputs"])
    assert outputs["status"]["value"] == "${{ steps.server.outputs.status }}", (
        "the action's status output must read the start step, got "
        f"{outputs['status']['value']!r}"
    )


def test_a_server_that_starts_gets_a_sixty_second_timeout_and_zero_counters(
    tmp_path: Path,
) -> None:
    """The success path writes the config, exports it and keeps the wrapper."""
    result, outputs, calls = _run_start(tmp_path, starts=True)

    assert result.returncode == 0, result.stderr
    conf = result.exported.get("SCCACHE_CONF")
    assert conf is not None, f"the step must export SCCACHE_CONF, got {result.exported}"
    assert "server_startup_timeout_ms = 60000" in (
        tmp_path / conf.removeprefix(str(tmp_path) + "/")
    ).read_text(encoding="utf-8"), "the config must set a 60 s startup timeout"
    assert "status=started" in outputs.read_text(encoding="utf-8"), (
        "a started server must publish status=started"
    )
    assert "--zero-stats" in _calls(calls), "a started server must be zeroed"
    assert "RUSTC_WRAPPER" not in result.exported, (
        "a started server must leave the wrapper in place"
    )
    assert FALLBACK_TITLE not in result.stdout, "a started server must not warn"
    assert FALLBACK_SUMMARY not in result.summary, (
        "a started server must not write the fallback line"
    )


def test_a_server_that_will_not_start_falls_back_without_failing(
    tmp_path: Path,
) -> None:
    """Every signal of a fallback is present and the step still succeeds."""
    result, outputs, calls = _run_start(tmp_path, starts=False)

    assert result.returncode == 0, (
        f"a cache is an optimization; the step must not fail the job: {result.stderr}"
    )
    assert FALLBACK_TITLE in result.stdout, (
        f"the annotation title must be sccache-fallback, got {result.stdout!r}"
    )
    assert FALLBACK_SUMMARY in result.summary, (
        f"the run page must carry the fallback line, got {result.summary!r}"
    )
    assert "status=fallback" in outputs.read_text(encoding="utf-8"), (
        "a fallback must publish status=fallback"
    )
    assert "RUSTC_WRAPPER" in result.exported, (
        f"the step must export RUSTC_WRAPPER, got {result.exported}"
    )
    assert not result.exported["RUSTC_WRAPPER"], (
        "the wrapper must be cleared so Cargo compiles with plain rustc, got "
        f"{result.exported}"
    )
    assert "--zero-stats" not in _calls(calls), (
        "a fallback must not touch the server again; --zero-stats would restart it"
    )


@pytest.mark.parametrize("starts", [True, False])
def test_the_stale_server_is_stopped_before_the_start(
    tmp_path: Path, *, starts: bool
) -> None:
    """A server from an earlier step holds the backend it bound then."""
    _, _, calls = _run_start(tmp_path, starts=starts)

    recorded = _calls(calls)
    assert recorded[:2] == ["--stop-server", "--start-server"], (
        f"the step must stop any running server before starting, got {recorded}"
    )
