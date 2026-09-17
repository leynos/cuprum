"""Unit tests for the reader that turns an `act --json` stream into a verdict.

These run everywhere, with no container runtime: the input is a recorded stream
in `tests/fixtures/`, so the parsing is checked in the default suite and the
scenarios that need `act` (`tests/integration/test_workflow_integration.py`)
are the only part that can skip.

The fixtures are real recordings, reduced to the events the parser reads. They
are what makes a change to `act`'s output fail here — visibly and near the
cause — instead of surfacing as an integration scenario that quietly reads the
wrong value.
"""

from __future__ import annotations

import pathlib as pth

import pytest

from tests.helpers.act_stream import ActRun, shell_join

FIXTURES = pth.Path(__file__).resolve().parents[1] / "fixtures"
#: The healthy path: a performance-relevant pull request, detector succeeded.
PULL_REQUEST = FIXTURES / "act_stream_pull_request.jsonl"
#: The detector failed, so `bench` was never set and the gate had to decide
#: from the failure rather than from a changed-path set.
DETECTOR_FAILURE = FIXTURES / "act_stream_detector_failure.jsonl"
#: The only shape that carries one output name more than once: a step writing
#: through the legacy `::set-output::` command as well as `$GITHUB_OUTPUT`.
REPEATED_OUTPUT = FIXTURES / "act_stream_repeated_output.jsonl"


def recorded(path: pth.Path, *, exit_code: int = 0) -> ActRun:
    """Return the fixture at ``path`` as an `ActRun`.

    The stream is read from disk rather than captured per test, so the parsing
    tests are deterministic and cheap. Blank and comment lines stay in the
    input: the parser must tolerate lines that are not JSON, and a fixture that
    had already stripped them would not exercise that.

    Parameters
    ----------
    path : pathlib.Path
        Fixture holding one recorded stream.
    exit_code : int
        `act`'s exit status for the run. The detector-failure recording is the
        only fixture that produced a non-zero one.

    Returns
    -------
    ActRun
        The recording, ready to parse.
    """
    return ActRun(
        exit_code=exit_code,
        stdout=path.read_text(encoding="utf-8"),
        stderr="",
        argv=("act", "pull_request", "--json"),
    )


def test_the_recorded_streams_parse_as_act_output() -> None:
    """Fail loudly if a fixture is replaced with something act never emits."""
    for path in (PULL_REQUEST, DETECTOR_FAILURE, REPEATED_OUTPUT):
        events = recorded(path).events
        assert events, f"{path.name} yielded no events"
        assert all(isinstance(event, dict) for event in events), (
            f"{path.name} yielded a non-mapping event: {events!r}"
        )


def test_non_json_lines_are_skipped_rather_than_fatal() -> None:
    """Keep a stray banner or comment from discarding the whole stream."""
    run = ActRun(
        exit_code=0,
        stdout='not json\n{"command": "set-output", "name": "a", "arg": "1"}\n\n',
        stderr="",
        argv=("act",),
    )
    assert run.events == [{"command": "set-output", "name": "a", "arg": "1"}], (
        f"non-JSON lines must be skipped, got {run.events!r}"
    )


def test_the_fixtures_carry_comment_lines_that_are_not_events() -> None:
    """Prove the fixtures exercise the non-JSON path they are documented for."""
    for path in (PULL_REQUEST, DETECTOR_FAILURE, REPEATED_OUTPUT):
        text = path.read_text(encoding="utf-8")
        comments = [line for line in text.splitlines() if line.startswith("#")]
        assert comments, f"{path.name} has no comment lines"
        assert all(not event.get("comment") for event in recorded(path).events), (
            f"{path.name} turned a comment line into an event"
        )


def test_outputs_take_the_last_value_when_a_name_repeats() -> None:
    """Report the live output when act reports an output more than once.

    This is what the legacy `::set-output::` path produces, and the recorded
    workflow read back the *later* value. A reader that took the first match
    would report an output the run had already replaced.
    """
    run = recorded(REPEATED_OUTPUT)
    assert run.output("value") == "from-file-again", (
        f"the later set-output must win, got {run.output('value')!r}"
    )
    assert run.outputs() == {"value": "from-file-again"}, (
        f"the outputs mapping must report the later value, got {run.outputs()!r}"
    )


def test_the_repeated_name_is_genuinely_repeated_in_the_recording() -> None:
    """Make the last-value-wins test above non-vacuous.

    Without this, a fixture that had been reduced to a single event would leave
    the test passing for the wrong reason — it would be asserting nothing about
    repetition at all.
    """
    names = [
        event["name"]
        for event in recorded(REPEATED_OUTPUT).events
        if event.get("command") == "set-output"
    ]
    assert names == ["value", "value"], (
        f"the recording must report the same name twice, got {names!r}"
    )


def test_an_output_that_was_never_set_reads_as_none() -> None:
    """Distinguish "no answer" from an answer of `false`.

    The detector-failure path is exactly this case: `bench` is never set, and
    the gate's decision depends on telling that apart from a detector that
    answered `false`.
    """
    failure = recorded(DETECTOR_FAILURE, exit_code=1)
    assert failure.output("bench") is None, (
        f"a detector that failed must not answer, got {failure.output('bench')!r}"
    )
    assert "bench" not in failure.outputs(), (
        f"bench must be absent from the outputs, got {sorted(failure.outputs())!r}"
    )


def test_the_healthy_recording_reports_the_detectors_answer() -> None:
    """Read the detector's output, which is what the gate's `run` rests on."""
    run = recorded(PULL_REQUEST)
    assert run.output("bench") == "true", (
        f"the detector must answer true for this recording, got {run.output('bench')!r}"
    )
    assert run.output("event_class") == "pull_request", (
        f"the recording is a pull request, got {run.output('event_class')!r}"
    )
    assert run.output("detector_status") == "success", (
        f"the detector succeeded in this recording, got "
        f"{run.output('detector_status')!r}"
    )
    assert run.output("decision") == "run", (
        f"a healthy pull request must admit the ratchet, got {run.output('decision')!r}"
    )


def test_the_detector_failure_recording_still_records_a_decision() -> None:
    """Assert the gate decided despite the detector having failed.

    This is the property the whole harness exists for: a failed detector must
    leave a recorded decision, not an empty output and not a silent skip.
    """
    run = recorded(DETECTOR_FAILURE, exit_code=1)
    assert run.output("detector_status") == "failure", (
        f"the detector must have failed, got {run.output('detector_status')!r}"
    )
    assert run.output("decision") == "skip-detector-failed", (
        f"a failed detector must skip rather than run, got {run.output('decision')!r}"
    )


def test_step_verdicts_name_the_step_that_failed() -> None:
    """Report the failing step by name, which is what a triage needs first."""
    failure = recorded(DETECTOR_FAILURE, exit_code=1)
    assert failure.failed_steps == ["Detect performance-relevant changes"], (
        f"the failing step must be named, got {failure.failed_steps!r}"
    )
    assert failure.step_results["Record the benchmark gate decision"] == "success", (
        f"the gate must have recorded its decision, got "
        f"{failure.step_results['Record the benchmark gate decision']!r}"
    )
    assert recorded(PULL_REQUEST).failed_steps == [], (
        f"the healthy recording must have no failed steps, got "
        f"{recorded(PULL_REQUEST).failed_steps!r}"
    )


def test_the_summary_comes_from_the_stream_not_the_container_file() -> None:
    """Recover the summary from the stream, which survives the upload.

    `$GITHUB_STEP_SUMMARY` is truncated inside the container once the summary
    has been uploaded, so the file is not a source the parser can read.
    """
    summary = recorded(PULL_REQUEST).summary
    assert summary.startswith("### Benchmark gate"), (
        f"the summary must open with its heading, got {summary!r}"
    )
    assert "| pull_request | success | true | run |" in summary, (
        f"the summary must record the healthy decision, got {summary!r}"
    )
    assert "| pull_request | failure | unknown | skip-detector-failed |" in (
        recorded(DETECTOR_FAILURE, exit_code=1).summary
    ), "the summary must record the detector-failure decision"


def test_a_run_with_no_summary_reports_an_empty_one() -> None:
    """Return an empty summary rather than raising when none was written."""
    empty = ActRun(exit_code=0, stdout="", stderr="", argv=("act",))
    assert not empty.summary, (
        f"a run with no summary must report '', got {empty.summary!r}"
    )
    assert not empty.events, (
        f"a run with no stdout must report no events, got {empty.events!r}"
    )


#: The facets of a failure message that make a failed scenario reproducible:
#: the exit status, the step that failed, the command to paste back, and the
#: two streams to read. Each is asserted on its own so a failure names the one
#: that went missing rather than printing the whole rendered message.
CONTEXT_FACETS = [
    pytest.param("act exited 1", id="exit-status"),
    pytest.param("Detect performance-relevant changes", id="failing-step"),
    pytest.param("command: act pull_request --json", id="pasted-command"),
    pytest.param("stdout:", id="stdout-section"),
    pytest.param("stderr:", id="stderr-section"),
]


@pytest.mark.parametrize("fragment", CONTEXT_FACETS)
def test_the_failure_context_names_the_command_and_the_failing_step(
    fragment: str,
) -> None:
    """Make a failed scenario reproducible from its message alone."""
    context = recorded(DETECTOR_FAILURE, exit_code=1).failure_context()
    assert fragment in context, (
        f"failure_context() must name {fragment!r} so a failed scenario can be "
        f"read back from its message; it rendered:\n{context}"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["act"], "act"),
        (["act", "pull_request"], "act pull_request"),
        (["act", "-e", "a b.json"], "act -e 'a b.json'"),
        (["act", "-e", "it's.json"], """act -e 'it'"'"'s.json'"""),
    ],
)
def test_commands_are_rendered_so_they_can_be_pasted_back(
    argv: list[str], expected: str
) -> None:
    """Quote an argument before it goes into a pasted command line."""
    assert shell_join(argv) == expected, (
        f"{argv!r} must render as {expected!r}, got {shell_join(argv)!r}"
    )
