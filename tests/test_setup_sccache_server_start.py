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
from pathlib import Path

import pytest

from tests.helpers.composite_actions import (
    StepResult,
    action_document,
    run_step,
    step_script,
)

ACTION = ".github/actions/setup-sccache"
START_STEP = "Start the sccache server"
#: The four signals of a fallback. The title and the summary line are what
#: ``sccache-fallbacks.py`` and a person reading the run page search for, so
#: renaming either is a contract change, not a refactor.
FALLBACK_TITLE = "::warning title=sccache-fallback::"
FALLBACK_SUMMARY = "sccache: FALLBACK (cache disabled for this job)"


def _run_start(
    tmp_path: Path, *, starts: bool, zeroes: bool = True
) -> tuple[StepResult, Path, Path]:
    """Run the start step against a fake binary that does or does not start.

    The fake records each argument it receives, one per line, so a test can
    tell whether the step zeroed counters, and the ``SCCACHE_CONF`` it saw when
    asked to start a server, so a test can tell whether the timeout reached the
    process that needed it and not merely ``GITHUB_ENV``. ``zeroes=False``
    makes ``--zero-stats`` fail, as it can when the server died after starting.

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
    zero_status = 0 if zeroes else 1
    seen_conf = tmp_path / "sccache-start-conf"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$1" >> "{calls}"\n'
        'if [ "$1" = "--start-server" ]; then\n'
        f'  echo "${{SCCACHE_CONF-unset}}" > "{seen_conf}"\n'
        f"  exit {start_status}\n"
        "fi\n"
        f'[ "$1" = "--zero-stats" ] && exit {zero_status}\n'
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


def _conf_seen_by_the_server(tmp_path: Path) -> str:
    """Return the ``SCCACHE_CONF`` the fake saw when asked to start a server."""
    return (tmp_path / "sccache-start-conf").read_text(encoding="utf-8").strip()


def _assert_timeout_reached_the_server(tmp_path: Path, result: StepResult) -> None:
    """Assert the server process sees the timeout, not only ``GITHUB_ENV``.

    A step that wrote the file and the ``GITHUB_ENV`` line but dropped
    ``export SCCACHE_CONF`` would pass every assertion on the exported
    variables while the server started with sccache's own 10 s timeout.
    """
    seen = _conf_seen_by_the_server(tmp_path)
    assert seen == result.exported.get("SCCACHE_CONF"), (
        f"the server must start with SCCACHE_CONF set to the exported path, "
        f"saw {seen!r}, exported {result.exported.get('SCCACHE_CONF')!r}"
    )
    assert "server_startup_timeout_ms = 60000" in Path(seen).read_text(
        encoding="utf-8"
    ), "the config the server reads must set a 60 s startup timeout"


def test_a_server_that_starts_gets_a_sixty_second_timeout_and_zero_counters(
    tmp_path: Path,
) -> None:
    """The success path configures the start, zeroes and keeps the wrapper."""
    result, outputs, calls = _run_start(tmp_path, starts=True)

    assert result.returncode == 0, result.stderr
    assert "SCCACHE_CONF" in result.exported, (
        f"the step must export SCCACHE_CONF, got {result.exported}"
    )
    _assert_timeout_reached_the_server(tmp_path, result)
    assert "status=started" in outputs.read_text(encoding="utf-8"), (
        "a started server must publish status=started"
    )
    assert "--zero-stats" in _calls(calls), "a started server must be zeroed"
    assert "metric setup-sccache.server=started" in result.stdout, (
        f"a started server must log its bounded metric, got {result.stdout!r}"
    )
    assert "RUSTC_WRAPPER" not in result.exported, (
        "a started server must leave the wrapper in place"
    )
    assert FALLBACK_TITLE not in result.stdout, "a started server must not warn"
    assert FALLBACK_SUMMARY not in result.summary, (
        "a started server must not write the fallback line"
    )


@pytest.mark.parametrize(
    ("starts", "zeroes", "touches_server_again"),
    [(False, True, False), (True, False, True)],
    ids=["start-fails", "zero-stats-fails-after-start"],
)
def test_a_server_that_cannot_be_used_falls_back_without_failing(
    tmp_path: Path, *, starts: bool, zeroes: bool, touches_server_again: bool
) -> None:
    """Every signal of a fallback is present and the step still succeeds.

    ``--zero-stats`` starts a server when none is running, so one that died
    after ``--start-server`` makes it try again and can fail. Under ``set -e``
    that would fail the job, which is the same loss as a start that never
    worked and must be absorbed the same way.
    """
    result, outputs, calls = _run_start(tmp_path, starts=starts, zeroes=zeroes)

    assert result.returncode == 0, (
        f"a cache is an optimization; the step must not fail the job: {result.stderr}"
    )
    _assert_timeout_reached_the_server(tmp_path, result)
    assert FALLBACK_TITLE in result.stdout, (
        f"the annotation title must be sccache-fallback, got {result.stdout!r}"
    )
    assert FALLBACK_SUMMARY in result.summary, (
        f"the run page must carry the fallback line, got {result.summary!r}"
    )
    assert "metric setup-sccache.server=start-failed" in result.stdout, (
        f"a fallback must log its bounded metric, got {result.stdout!r}"
    )
    assert "status=fallback" in outputs.read_text(encoding="utf-8"), (
        "a fallback must publish status=fallback"
    )
    assert "status=started" not in outputs.read_text(encoding="utf-8"), (
        "a fallback must not also publish status=started"
    )
    assert "RUSTC_WRAPPER" in result.exported, (
        f"the step must export RUSTC_WRAPPER, got {result.exported}"
    )
    assert not result.exported["RUSTC_WRAPPER"], (
        "the wrapper must be cleared so Cargo compiles with plain rustc, got "
        f"{result.exported}"
    )
    assert ("--zero-stats" in _calls(calls)) is touches_server_again, (
        "a start that failed must not be followed by --zero-stats, which would "
        f"start another server; calls were {_calls(calls)}"
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
