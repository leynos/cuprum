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

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

ACTION = ".github/actions/setup-sccache"
START_STEP = "Start the sccache server"
#: The four signals of a fallback. The title and the summary line are what
#: ``sccache-fallbacks.py`` and a person reading the run page search for, so
#: renaming either is a contract change, not a refactor.
FALLBACK_TITLE = "::warning title=sccache-fallback::"
FALLBACK_SUMMARY = "sccache: FALLBACK (cache disabled for this job)"


#: What the fake `--stop-server` does, keyed by the situation it stands for. The
#: messages are sccache's own: with no server it prints "couldn't connect to
#: server" and exits 1, which is the expected case on a fresh runner.
STOP_BEHAVIOURS: typ.Final = {
    "no-server": (1, "Error: couldn't connect to server"),
    "stopped": (0, "Stopping sccache server..."),
    "error": (1, "Error: Failed to send data to or receive data from server"),
    # A server that accepts the connection and never replies: sccache's client
    # has no read timeout, so only the step's own bound ends it. `exec` keeps it
    # one process, as the real client is, so the bound's signal ends it.
    "stalls": (0, "Stopping sccache server..."),
}


def _run_start(
    tmp_path: Path, *, starts: bool, zeroes: bool = True, stop: str = "no-server"
) -> tuple[StepResult, Path, Path]:
    """Run the start step against a fake binary that does or does not start.

    The fake records each argument it receives, one per line, so a test can
    tell whether the step zeroed counters, and the ``SCCACHE_CONF`` it saw when
    asked to start a server, so a test can tell whether the timeout reached the
    process that needed it and not merely ``GITHUB_ENV``. ``zeroes=False``
    makes ``--zero-stats`` fail, as it can when the server died after starting.
    ``stop`` picks what ``--stop-server`` does: ``no-server`` fails with
    sccache's own "couldn't connect to server" as on a fresh runner, ``stopped``
    succeeds, and ``error`` fails for any other reason.

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
    stop_status, stop_message = STOP_BEHAVIOURS[stop]
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$1" >> "{calls}"\n'
        'if [ "$1" = "--start-server" ]; then\n'
        f'  echo "${{SCCACHE_CONF-unset}}" > "{seen_conf}"\n'
        f"  exit {start_status}\n"
        "fi\n"
        f'[ "$1" = "--zero-stats" ] && exit {zero_status}\n'
        'if [ "$1" = "--stop-server" ]; then\n'
        + ("  exec sleep 60\n" if stop == "stalls" else "")
        + f'  echo "{stop_message}" >&2\n'
        f"  exit {stop_status}\n"
        "fi\n"
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
            "SETUP_SCCACHE_STOP_TIMEOUT": "1",
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


class Fallback(typ.NamedTuple):
    """One way the server can be unusable, and what the step must say about it."""

    starts: bool
    zeroes: bool
    touches_server_again: bool
    metric: str
    warning: str
    stop: str = "no-server"


#: The two failed operations. The annotation title and summary line are shared,
#: so a detector counts both, while the warning text and the bounded metric value
#: say which operation failed: a start that never worked is not a server that
#: started and then could not be zeroed.
FALLBACKS: typ.Final = {
    "start-fails": Fallback(
        starts=False,
        zeroes=True,
        touches_server_again=False,
        metric="metric setup-sccache.server=start-failed",
        warning="sccache server did not start within 60 s",
    ),
    "stop-fails": Fallback(
        starts=True,
        zeroes=True,
        touches_server_again=False,
        metric="metric setup-sccache.server=stop-failed",
        warning="sccache server could not be stopped",
        stop="error",
    ),
    "stop-stalls": Fallback(
        starts=True,
        zeroes=True,
        touches_server_again=False,
        metric="metric setup-sccache.server=stop-failed",
        warning="sccache server could not be stopped",
        stop="stalls",
    ),
    "zero-stats-fails-after-start": Fallback(
        starts=True,
        zeroes=False,
        touches_server_again=True,
        metric="metric setup-sccache.server=zero-stats-failed",
        warning="sccache server started but could not be zeroed",
    ),
}


@pytest.mark.parametrize("fallback", FALLBACKS.values(), ids=FALLBACKS.keys())
def test_a_server_that_cannot_be_used_falls_back_without_failing(
    tmp_path: Path, fallback: Fallback
) -> None:
    """Every signal of a fallback is present and the step still succeeds.

    ``--zero-stats`` starts a server when none is running, so one that died
    after ``--start-server`` makes it try again and can fail. Under ``set -e``
    that would fail the job, which is the same loss as a start that never
    worked and must be absorbed the same way, but named for what failed.
    """
    result, outputs, calls = _run_start(
        tmp_path, starts=fallback.starts, zeroes=fallback.zeroes, stop=fallback.stop
    )

    assert result.returncode == 0, (
        f"a cache is an optimization; the step must not fail the job: {result.stderr}"
    )
    if fallback.stop in {"error", "stalls"}:
        assert "--start-server" not in _calls(calls), (
            "a server that could not be stopped must not be followed by a start "
            f"that would leave two configurations in play; calls {_calls(calls)}"
        )
    else:
        _assert_timeout_reached_the_server(tmp_path, result)
    assert FALLBACK_TITLE in result.stdout, (
        f"the annotation title must be sccache-fallback, got {result.stdout!r}"
    )
    assert FALLBACK_SUMMARY in result.summary, (
        f"the run page must carry the fallback line, got {result.summary!r}"
    )
    assert fallback.metric in result.stdout.splitlines(), (
        f"a fallback must log its bounded metric {fallback.metric!r}, got "
        f"{result.stdout!r}"
    )
    assert fallback.warning in result.stdout, (
        f"the warning must say which operation failed ({fallback.warning!r}), "
        f"got {result.stdout!r}"
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
    assert ("--zero-stats" in _calls(calls)) is fallback.touches_server_again, (
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


@pytest.mark.parametrize("stop", ["no-server", "stopped"])
def test_a_stop_that_is_expected_does_not_stop_the_start(
    tmp_path: Path, stop: str
) -> None:
    """A stop that finds no server, or stops one, lets the start carry on.

    ``--stop-server`` exits non-zero with "couldn't connect to server" when
    nothing is running, which is the normal case on a fresh runner and must not
    fail or fall back a healthy job. Only that message is expected: any other
    failure is the fallback tested above.
    """
    result, outputs, calls = _run_start(tmp_path, starts=True, stop=stop)

    assert result.returncode == 0, result.stderr
    assert _calls(calls)[:2] == ["--stop-server", "--start-server"], (
        f"the start must follow a stop that failed, got {_calls(calls)}"
    )
    assert "status=started" in outputs.read_text(encoding="utf-8"), (
        "a started server must publish status=started after a failed stop"
    )


#: The four outcomes whose user-visible output is a stable contract.
OUTCOMES: typ.Final = {
    "started": (True, True, "no-server"),
    "start-fails": (False, True, "no-server"),
    "zero-stats-fails": (True, False, "no-server"),
    "stop-fails": (True, True, "error"),
}


@pytest.mark.parametrize("outcome", OUTCOMES.values(), ids=OUTCOMES.keys())
def test_the_user_visible_output_is_stable(
    tmp_path: Path, outcome: tuple[bool, bool, str], snapshot: SnapshotAssertion
) -> None:
    """Snapshot what a person or a detector sees: log, run page and outputs.

    The annotation title, the run-page line and the bounded metric are search
    keys, so a wording change to any of them should be a reviewed diff, not an
    incidental one. Nothing here is nondeterministic: the log carries no path.
    """
    starts, zeroes, stop = outcome
    result, outputs, _ = _run_start(tmp_path, starts=starts, zeroes=zeroes, stop=stop)

    visible = {
        "log": result.stdout.splitlines(),
        "run_page": result.summary.splitlines(),
        "outputs": outputs.read_text(encoding="utf-8").splitlines(),
        "wrapper": "kept" if "RUSTC_WRAPPER" not in result.exported else "cleared",
    }

    assert visible == snapshot, (
        "the user-visible output changed; review the snapshot diff"
    )
