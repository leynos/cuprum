"""Behavioural tests for structured execution events and telemetry hooks.

The scenarios are declared here, and so are the ``Then`` steps: those carry the
assertions, which only ``test_*.py`` files may use. The ``Given`` and ``When``
steps live in ``_structured_events_steps.py``, loaded below as a plugin, and
the shared scaffolding they and these steps both need lives in
``_structured_events_support.py``.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import parsers, scenario, then

from tests.behaviour._structured_events_support import (
    LIFECYCLE_PHASES,
    LINE_PHASES,
    RUN_TAG,
    expected_line_events,
    normalize_event,
    observed_line_events,
    retained_events,
    run_lifecycle_probe,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from cuprum.events import ExecEvent

# pytest-bdd resolves a step by looking for a fixture that it created in the
# *calling module's* namespace, so a step defined in another module is invisible
# unless that module defines the scenario too. Loading the step module as a
# plugin makes pytest collect its fixtures, which is what registers its
# Given/When steps here without restating the step text.
pytest_plugins = ("tests.behaviour._structured_events_steps",)


@scenario(
    "../features/structured_events.feature",
    "Observe hook receives output events and timing metadata",
)
def test_observe_hook_receives_output_and_timing() -> None:
    """Behavioural coverage for structured output and timing events."""


@scenario(
    "../features/structured_events.feature",
    "Retained events preserve stream metadata and line order",
)
def test_retained_events_preserve_order_and_metadata() -> None:
    """Behavioural coverage for per-stream order under retained delivery."""


@scenario(
    "../features/structured_events.feature",
    "A pipeline preserves per-stream order and execution identity",
)
def test_pipeline_preserves_per_stream_order() -> None:
    """Behavioural coverage for pipeline line observation."""


@scenario(
    "../features/structured_events.feature",
    "Concurrent runs of one command keep their events separate",
)
def test_concurrent_runs_keep_events_separate() -> None:
    """Behavioural coverage for execution-token isolation."""


@scenario(
    "../features/structured_events.feature",
    "Unterminated and CRLF fragments survive line framing",
)
def test_unterminated_and_crlf_fragments() -> None:
    """Behavioural coverage for line framing at chunk boundaries."""


@scenario(
    "../features/structured_events.feature",
    "A silent command emits lifecycle events without line events",
)
def test_silent_command_emits_no_line_events() -> None:
    """Behavioural coverage for the zero-line case."""


@scenario(
    "../features/structured_events.feature",
    "A failing command still reports its exit event",
)
def test_failing_command_reports_exit_event() -> None:
    """Behavioural coverage for non-zero exit reporting."""


# The ``observed_command`` fixture each scenario consumes is *not* declared
# here: the ``@given(target_fixture="observed_command")`` decorators in the step
# module supply it per scenario, and a module-level fixture of the same name
# would shadow that injection.
@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios.

    Returns
    -------
    dict[str, object]
        An empty mapping seeded fresh for each scenario.
    """
    return {}


@then("the observe hook sees stdout and stderr line events")
def then_observe_sees_output_events(behaviour_state: dict[str, object]) -> None:
    """Validate observed output line events."""
    events = retained_events(behaviour_state)
    stdout_lines = {ev.line for ev in events if ev.phase == "stdout"}
    stderr_lines = {ev.line for ev in events if ev.phase == "stderr"}
    assert "out1" in stdout_lines, 'Expected "out1" in stdout_lines'
    assert "out2" in stdout_lines, 'Expected "out2" in stdout_lines'
    assert "err1" in stderr_lines, 'Expected "err1" in stderr_lines'


@then("the observe hook sees timing and tag metadata")
def then_observe_sees_timing_and_tags(behaviour_state: dict[str, object]) -> None:
    """Validate event timing and tag metadata."""
    events = retained_events(behaviour_state)
    start = next(ev for ev in events if ev.phase == "start")
    exit_ = next(ev for ev in events if ev.phase == "exit")

    assert start.pid is not None, "Expected start.pid is not None"
    assert start.pid > 0, "Expected start.pid > 0"
    assert exit_.exit_code == 0, "Expected exit_.exit_code == 0"
    assert exit_.duration_s is not None, "Expected exit_.duration_s is not None"
    assert exit_.duration_s >= 0.0, "Expected exit_.duration_s >= 0.0"

    for ev in (start, exit_):
        assert ev.tags["run_id"] == RUN_TAG, f"Expected {RUN_TAG!r} run tag"
        assert "project" in ev.tags, 'Expected "project" in ev.tags'


@then("each stream has exactly the expected ordered line sequence")
def then_each_stream_has_expected_sequence(
    behaviour_state: dict[str, object],
) -> None:
    """Assert the exact per-stream line order, with no extra or missing lines.

    Ordering is asserted *per stream*. No global stdout/stderr interleaving is
    claimed, because the two readers are independent tasks and the kernel
    offers no cross-stream ordering guarantee.
    """
    expected_stdout, expected_stderr = expected_line_events(behaviour_state)
    line_events = observed_line_events(behaviour_state)

    observed_stdout = [ev.line for ev in line_events if ev.phase == "stdout"]
    observed_stderr = [ev.line for ev in line_events if ev.phase == "stderr"]

    assert observed_stdout == expected_stdout, (
        f"stdout sequence differed: {observed_stdout!r} != {expected_stdout!r}"
    )
    assert observed_stderr == expected_stderr, (
        f"stderr sequence differed: {observed_stderr!r} != {expected_stderr!r}"
    )


@then("every line event retains its original line and timestamp")
def then_line_events_retain_payload(behaviour_state: dict[str, object]) -> None:
    """Each retained event still carries a finite, non-negative timestamp.

    This is the property a reused-event implementation would break: if one
    event object were handed to every line, the retained payloads would all
    show the last line. The exact line text is asserted by the sequence step;
    what is checked here is that the payload survived retention intact.
    """
    line_events = observed_line_events(behaviour_state)

    for event in line_events:
        assert isinstance(event.line, str), "a line event must carry a line"
        assert event.timestamp >= 0.0, (
            f"line event timestamp must be non-negative, got {event.timestamp!r}"
        )
    fresh_ids = {id(event) for event in line_events}
    assert len(fresh_ids) == len(line_events), (
        "each line must be a distinct event object; a reused event would "
        "rewrite the payloads a hook retained"
    )


@then("all line events carry the spawned process and resolved metadata")
def then_line_events_carry_process_metadata(
    behaviour_state: dict[str, object],
) -> None:
    """Every line event names this run's PID, program, argv, and tags."""
    line_events = observed_line_events(behaviour_state)
    events = retained_events(behaviour_state)
    # A pipeline observes every stage, so a line event carries the PID *and*
    # the execution token of the stage that wrote it, not one run-wide pair.
    # Binding both together is what catches a hoist that cached the first
    # stage's metadata and reused it for the rest.
    starts = {ev.pid: ev.exec_id for ev in events if ev.phase == "start"}

    assert starts, "the run must report at least one spawned process"
    for event in line_events:
        assert event.pid in starts, (
            f"line event pid {event.pid!r} is not a spawned pid {set(starts)!r}"
        )
        assert event.exec_id == starts[event.pid], (
            f"line event on pid {event.pid!r} carries token "
            f"{event.exec_id!r}, but that stage planned {starts[event.pid]!r}"
        )
        assert event.pid is not None, (
            "a line event must carry the spawned process identifier, not None"
        )
        assert event.pid > 0, (
            f"a spawned process identifier must be positive, got {event.pid!r}"
        )
        assert event.argv, "line events must carry the resolved argv"
        assert event.argv[0] == str(event.program), "argv[0] must be the program name"
        assert event.tags["run_id"] == RUN_TAG, (
            f"line event must inherit the run tag, got {event.tags!r}"
        )
        assert "project" in event.tags, 'Expected "project" in event.tags'


@then("every stage has a plan with no process identifier and its own exit token")
def then_plan_and_exit_share_the_token(behaviour_state: dict[str, object]) -> None:
    """Each observed stage plans before spawning and exits under one token.

    A pipeline observes every stage, so there is one plan/exit pair per stage
    rather than one per run. The per-line hoist must not disturb that shape:
    the line events are additional, not a replacement for the lifecycle pair.
    """
    events = retained_events(behaviour_state)
    plans = [ev for ev in events if ev.phase == "plan"]
    exits = [ev for ev in events if ev.phase == "exit"]

    assert plans, "every observed stage must emit a plan event"
    for plan in plans:
        assert plan.pid is None, "a plan event fires before the process is spawned"
    assert {ev.exec_id for ev in exits} == {ev.exec_id for ev in plans}, (
        "each stage's exit must retain its own plan's execution token"
    )


@then("each run's events carry that run's own execution token")
def then_each_run_has_its_own_token(behaviour_state: dict[str, object]) -> None:
    """Two runs of one command share no execution token."""
    runs = _runs(behaviour_state)
    tokens = {run_id: {ev.exec_id for ev in events} for run_id, events in runs.items()}

    for run_id, run_tokens in tokens.items():
        assert run_tokens, f"run {run_id!r} must have emitted events"
        assert len(run_tokens) == 1, (
            f"run {run_id!r} must use exactly one execution token, got {run_tokens!r}"
        )
    assert tokens["first"] != tokens["second"], (
        "two runs must not share an execution token"
    )


@then("each run sees only its own tagged lines")
def then_each_run_sees_its_own_lines(behaviour_state: dict[str, object]) -> None:
    """Assert a run sees only its own tag and its own echoed stdin."""
    for run_id, events in _runs(behaviour_state).items():
        assert events, f"run {run_id!r} must have emitted events"
        for event in events:
            assert event.tags["run_id"] == run_id, (
                f"run {run_id!r} saw an event tagged {event.tags['run_id']!r}"
            )
        echoed = [ev.line for ev in events if ev.phase == "stdout"]
        assert echoed == [f"{run_id}-input"], (
            f"run {run_id!r} must echo only its own stdin, got {echoed!r}"
        )


@then("the observe hook sees no line events for either stream")
def then_no_line_events(behaviour_state: dict[str, object]) -> None:
    """Assert a silent command emits lifecycle events and no line events."""
    events = retained_events(behaviour_state)

    line_events = [ev for ev in events if ev.phase in LINE_PHASES]
    assert line_events == [], (
        f"a silent command must emit no line events, got {line_events!r}"
    )
    assert any(ev.phase == "plan" for ev in events), "expected a plan event"
    assert any(ev.phase == "exit" for ev in events), "expected an exit event"


@then(parsers.parse("that command exits with the expected status"))
def then_command_exits_with_expected_status(
    behaviour_state: dict[str, object],
) -> None:
    """Assert the exit event reports the status the command returned."""
    events = retained_events(behaviour_state)
    exit_ = next(ev for ev in events if ev.phase == "exit")
    expected = typ.cast("int", behaviour_state["expected_exit_code"])

    assert exit_.exit_code == expected, (
        f"exit code {exit_.exit_code!r} != expected {expected!r}"
    )


def _runs(behaviour_state: dict[str, object]) -> dict[str, list[ExecEvent]]:
    """Return the per-run event lists the concurrent scenario retained."""
    return typ.cast("dict[str, list[ExecEvent]]", behaviour_state["runs"])


def test_normalized_lifecycle_payload_is_stable(
    snapshot: SnapshotAssertion,
) -> None:
    """A normalized *lifecycle* payload matches its snapshot.

    The snapshot covers the plan/start/exit triple — the lifecycle contract the
    per-line hoist must not disturb — rather than the line events, whose
    ordering is asserted explicitly by the sequence step and whose exact
    interleaving across streams is not guaranteed. Only genuinely volatile
    values are masked; phases, defaults, tags, and stage ownership stay in the
    snapshot, because those are what a careless hoist would change.
    """
    events = run_lifecycle_probe()
    lifecycle = [ev for ev in events if ev.phase in LIFECYCLE_PHASES]

    assert [ev.phase for ev in lifecycle] == ["plan", "start", "exit"], (
        "the probe must produce exactly one plan/start/exit triple"
    )
    assert [normalize_event(ev) for ev in lifecycle] == snapshot, (
        "the normalized lifecycle payload changed; confirm the change is "
        "intended and update with --snapshot-update"
    )


def test_the_probe_run_preserves_per_stream_line_order() -> None:
    """The same probe run delivers each stream's lines in write order.

    Stated per stream, because the two readers are independent tasks: the
    kernel guarantees no ordering *between* streams, and asserting one would
    make this test fail on a scheduling change rather than on a regression.
    """
    events = run_lifecycle_probe()
    stdout = [ev.line for ev in events if ev.phase == "stdout"]
    stderr = [ev.line for ev in events if ev.phase == "stderr"]

    assert stdout == ["beta", "alpha"], f"stdout order changed: {stdout!r}"
    assert stderr == ["gamma"], f"stderr order changed: {stderr!r}"

    # The anti-vacuity witness: the probe wrote two lines to stdout and one to
    # stderr, so both assertions above compared real sequences.
    assert len(stdout) + len(stderr) == 3, (
        f"the probe wrote three lines total, got {len(stdout) + len(stderr)}"
    )
