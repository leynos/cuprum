"""Given/When step definitions for the structured-events scenarios.

The companion module ``test_structured_events.py`` owns the ``Then`` steps as
well: those carry the assertions, and ``assert`` statements are permitted only
in ``test_*.py`` files. The scenario declarations also live there, so every
decorated step this module defines is reachable from a collected test.

Each helper takes the two or three arguments its step needs rather than a whole
scenario-state mapping, which keeps the step bodies to a single call.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as typ

from pytest_bdd import given, when

from tests.behaviour._structured_events_support import (
    catalogue_and_builder,
    catalogue_of,
    command_state,
    join_script,
    run_captured_and_echoed,
    run_observed,
    run_twice_with_distinct_contexts,
    runnable_of,
)

if typ.TYPE_CHECKING:
    from tests.behaviour._structured_events_support import (
        CommandCatalogue,
        Runnable,
    )

# The run helpers share one signature, so the ``when`` steps below can resolve
# the scenario's command once and hand it to whichever helper they name.
if typ.TYPE_CHECKING:
    type _RunHelper = cabc.Callable[
        [dict[str, object], CommandCatalogue, Runnable],
        None,
    ]
else:
    type _RunHelper = cabc.Callable


@given(
    "a safe command that writes to stdout and stderr",
    target_fixture="observed_command",
)
def given_observed_command() -> dict[str, object]:
    """Build a SafeCmd that writes to stdout and stderr.

    Returns
    -------
    dict[str, object]
        Scenario state holding the ``catalogue`` and ``cmd`` entries for
        the configured command.
    """
    return command_state(
        "-c",
        join_script(
            "import sys",
            "print('out1')",
            "print('out2')",
            "print('err1', file=sys.stderr)",
        ),
    )


@given(
    "an observed command writes repeated and empty lines to both streams",
    target_fixture="observed_command",
)
def given_repeated_and_empty_lines(
    behaviour_state: dict[str, object],
) -> dict[str, object]:
    """Build a command whose output repeats lines and includes an empty one.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue, command, and the exact expected
        per-stream sequences.
    """
    state = command_state(
        "-c",
        join_script(
            "import sys",
            "sys.stdout.write('same\\n\\nsame\\nout-tail')",
            "sys.stdout.flush()",
            "sys.stderr.write('same\\n')",
            "sys.stderr.write('err-tail\\n')",
            "sys.stderr.flush()",
        ),
    )
    behaviour_state["expected_stdout"] = ["same", "", "same", "out-tail"]
    behaviour_state["expected_stderr"] = ["same", "err-tail"]
    return state


@given(
    "an observed two stage pipeline that writes to both streams",
    target_fixture="observed_command",
)
def given_observed_pipeline(
    behaviour_state: dict[str, object],
) -> dict[str, object]:
    """Build a two-stage pipeline whose final stage writes to both streams.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue, pipeline, and expected lines.
    """
    catalogue, build = catalogue_and_builder()
    producer = build("-c", "import sys; sys.stdout.write('one\\ntwo\\n')")
    consumer = build(
        "-c",
        (
            "import sys;"
            "lines = sys.stdin.read().splitlines();"
            "[sys.stdout.write('got:' + line + chr(10)) for line in lines];"
            "sys.stderr.write('notes-from-stage-two' + chr(10))"
        ),
    )
    behaviour_state["expected_stdout"] = ["got:one", "got:two"]
    behaviour_state["expected_stderr"] = ["notes-from-stage-two"]
    return {"catalogue": catalogue, "cmd": producer | consumer}


@given(
    "an observed command that echoes its own stdin",
    target_fixture="observed_command",
)
def given_stdin_echoing_command() -> dict[str, object]:
    """Build a command that echoes back exactly what it reads on stdin.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue and a stdin-echoing command.
    """
    return command_state(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    )


@given(
    "an observed command writes unterminated and CRLF output",
    target_fixture="observed_command",
)
def given_unterminated_and_crlf(
    behaviour_state: dict[str, object],
) -> dict[str, object]:
    """Build a command whose final fragment is unterminated after CRLF lines.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue, command, and expected lines.
    """
    state = command_state(
        "-c",
        join_script(
            "import sys",
            r"sys.stdout.write('crlf\r\nplain\n')",
            r"sys.stdout.write('no-newline')",
            "sys.stdout.flush()",
        ),
    )
    # CRLF is a terminator, not content: the splitter strips it whole, so the
    # first line is 'crlf' rather than 'crlf\r'. The final fragment carries no
    # terminator at all and must still be delivered.
    behaviour_state["expected_stdout"] = ["crlf", "plain", "no-newline"]
    behaviour_state["expected_stderr"] = []
    behaviour_state["expected_exit_code"] = 0
    return state


@given(
    "an observed command that writes nothing to either stream",
    target_fixture="observed_command",
)
def given_silent_command(behaviour_state: dict[str, object]) -> dict[str, object]:
    """Build a command that produces no output at all.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue and an output-free command.
    """
    state = command_state("-c", "pass")
    behaviour_state["expected_stdout"] = []
    behaviour_state["expected_stderr"] = []
    behaviour_state["expected_exit_code"] = 0
    return state


@given(
    "an observed command that writes to both streams and exits non-zero",
    target_fixture="observed_command",
)
def given_failing_command(behaviour_state: dict[str, object]) -> dict[str, object]:
    """Build a command that writes output and then exits with status 7.

    Returns
    -------
    dict[str, object]
        Scenario state holding the catalogue, command, and expected values.
    """
    state = command_state(
        "-c",
        join_script(
            "import sys",
            "sys.stdout.write('before-failure\\n')",
            "sys.stderr.write('warning\\n')",
            "sys.stdout.flush()",
            "sys.stderr.flush()",
            "sys.exit(7)",
        ),
    )
    behaviour_state["expected_stdout"] = ["before-failure"]
    behaviour_state["expected_stderr"] = ["warning"]
    behaviour_state["expected_exit_code"] = 7
    return state


@given("the observer retains events until asynchronous callbacks settle")
def given_observer_retains(behaviour_state: dict[str, object]) -> None:
    """Note that retention is established by the synchronous run boundary.

    The step exists because the specification states the retention contract
    explicitly; ``run_captured_and_echoed`` is where it is honoured, since
    ``run_sync`` returns only once every observer task has settled.
    """
    behaviour_state["retains"] = True


def _run_scenario(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
    helper: _RunHelper,
) -> None:
    """Run the scenario's command through ``helper``, resolving it once."""
    helper(
        behaviour_state,
        catalogue_of(observed_command),
        runnable_of(observed_command),
    )


@when("I run the command with an observe hook")
def when_run_with_observe_hook(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
) -> None:
    """Run the command while collecting observe events."""
    _run_scenario(behaviour_state, observed_command, run_observed)


@when("the command runs with captured and echoed output")
def when_command_runs_captured_and_echoed(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
) -> None:
    """Run with capture on so the streams are drained and echoed."""
    _run_scenario(behaviour_state, observed_command, run_captured_and_echoed)


@when("the pipeline runs with captured and echoed output")
def when_pipeline_runs_captured_and_echoed(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
) -> None:
    """Run the pipeline under the same capture-and-echo settings.

    The step is the command step's name one stage up the specification: both
    run with identical settings, which is why it delegates rather than repeats.
    """
    _run_scenario(behaviour_state, observed_command, run_captured_and_echoed)


@when("the same command runs twice under distinct execution contexts")
def when_same_command_runs_twice(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
) -> None:
    """Run one command twice, tagging each run differently."""
    _run_scenario(
        behaviour_state,
        observed_command,
        run_twice_with_distinct_contexts,
    )
