"""Behavioural coverage for per-stream capture policies on a pipeline (#443).

A capture policy bounds or moves what a *run reports* about a stream. It must
not touch any other view of the same bytes: neither what a stage hands to the
stage after it, nor what an echo sink receives. The relay is the child's own
pipe, which the parent never reads, so no capture policy can reach it; the
echo mirror is the parent's own copy of the chunks it reads beside the
capture, so a bound that leaked into the echo would be visible there and
nowhere else. Each distinction needs its own run: a single command has no
downstream stage to starve, and only an echoing run has a mirror to compare.

Every scenario derives its expected values from module constants — the
payload, the bound, and the relay's stderr — so what a run must report is a
value the test computes rather than a substring it hopes for. Both the
payload and the relay's stderr are strictly larger than the bound, so a
scenario that asserts a whole stream cannot pass by retaining a tail that
happened to fit. In the relay-measurement scenario the last stage writes
fewer bytes than the bound allows, so a bound that leaked into the relay
would show up as a shortened count rather than as a captured tail nothing
compares against. The echo scenario asserts the mirror holds the whole
payload, which is only a meaningful comparison because the payload exceeds
the bound. The remaining scenarios end in the relay stage, whose stdout is
the whole payload and whose stderr is longer still.
"""

from __future__ import annotations

import io
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ExecutionContext, ScopeConfig, scoped
from cuprum.capture_policy import BoundedCapture, CapturedStream, FullCapture
from cuprum.sh import Pipeline, PipelineResult, RunOutputOptions, SafeCmd
from tests.helpers.catalogue import build_python_catalogue_env

# The payload the producer writes, the bound applied to it, and what follows
# from the two: the tail a bounded capture must retain, and the count it must
# report as dropped.
_PAYLOAD = "abcdefghijklmnop"
_PAYLOAD_LEN = len(_PAYLOAD)
_BOUND = 8
_TAIL = _PAYLOAD[-_BOUND:]
_DROPPED = _PAYLOAD_LEN - _BOUND
_ANNOUNCEMENT = str(_PAYLOAD_LEN)

# The relay's stderr is longer than the bound on purpose: a whole-stream
# assertion needs a case where the whole differs from the tail. The marker is
# the one byte a run would retain if it bounded this stream, and combining it
# with the announcement makes the two values differ in both content and length.
_STDERR_MARKER = "e"
_STDERR_LEN = 4 * _BOUND

# The producer writes its payload and exits; it neither reads nor reports.
_PRODUCER_SOURCE = f"import sys; sys.stdout.write({_PAYLOAD!r})"

# Relays stdin to stdout unchanged while announcing the payload's length on
# stderr. Two assertions ride on this: the relayed stdout proves the second
# stage read the whole stream, and the stderr announcement proves a second
# stream's policy ran independently of the first's.
_STDERR_FILLER_COUNT = _STDERR_LEN - len(_ANNOUNCEMENT)
_RELAY_SOURCE = (
    "import sys; data = sys.stdin.read(); "
    "sys.stdout.write(data); "
    f"sys.stderr.write(str(len(data)) + {_STDERR_MARKER!r} * {_STDERR_FILLER_COUNT})"
)

# What the relay's stderr must hold: the announcement followed by the filler,
# so its length is *not* the announcement's. A run that applied the stdout
# bound here would report a tail shorter than the bound (because the retained
# tail is bounded), and one that reported only the announcement would fail the
# exact comparison.
_STDERR_TEXT = _ANNOUNCEMENT + _STDERR_MARKER * (_STDERR_LEN - len(_ANNOUNCEMENT))

# The consumer of the relay scenario: reports only the number of bytes it read.
# Its whole output is a couple of bytes, far below the bound, so what it
# reports is a measurement of the relay rather than of any policy.
_REPORTER_SOURCE = "import sys; sys.stdout.write(str(len(sys.stdin.read())))"


def _run_pipeline(
    stages: list[str],
    options: RunOutputOptions,
    *,
    context: ExecutionContext | None = None,
) -> PipelineResult:
    """Build and run *stages* under one catalogue scope, applying *options*.

    ``SafeCmd.__or__`` returns a ``Pipeline`` while the fold starts at a single
    ``SafeCmd``, so the local is typed at the wider union the reassignment
    actually produces and narrowed once the fold is done. The assertion is what
    makes the narrowing honest rather than assumed: every scenario here runs a
    real two-stage pipeline, and a fold that had somehow left a bare command
    would have nothing to relay between.

    Parameters
    ----------
    stages : list[str]
        The Python source for each stage, in pipeline order.
    options : RunOutputOptions
        The output behaviour the scenario exercises.
    context : ExecutionContext | None
        Where the run's echo sinks go. ``None`` keeps the default, which
        writes to the test process's own streams; the echo scenario passes
        sinks so it can read what was mirrored.

    Returns
    -------
    PipelineResult
        The completed run, holding every stage and both streams' captures.
    """
    env = build_python_catalogue_env()
    commands: list[SafeCmd] = [env.builder("-c", source) for source in stages]
    pipeline: SafeCmd | Pipeline = commands[0]
    for command in commands[1:]:
        pipeline |= command
    assert isinstance(pipeline, Pipeline), (
        f"these scenarios run pipelines, so the fold must produce one; got {pipeline!r}"
    )
    with scoped(ScopeConfig(allowlist=frozenset({env.program}))):
        return pipeline.run_sync(output=options, context=context)


def _capture(result: PipelineResult, stream: str) -> CapturedStream:
    """Return the record *result* reports for *stream*, asserting there is one."""
    matching = [record for record in result.captures if record.stream == stream]
    assert len(matching) == 1, (
        f"the run must report exactly one {stream} capture; "
        f"got {list(result.captures)!r}"
    )
    return matching[0]


@scenario(
    "../features/bounded_capture.feature",
    "A bounded capture retains the tail of a stage's own output",
)
def test_bounded_capture_retains_a_tail() -> None:
    """Behavioural coverage for a bounded capture's own report."""


@scenario(
    "../features/bounded_capture.feature",
    "A bound never reaches the relay",
)
def test_bound_does_not_reach_the_relay() -> None:
    """Behavioural coverage for the relay being independent of the capture."""


@scenario(
    "../features/bounded_capture.feature",
    "A bound on stdout leaves stderr complete, though both exceed it",
)
def test_streams_are_configured_independently() -> None:
    """Behavioural coverage for per-stream policy independence."""


@scenario(
    "../features/bounded_capture.feature",
    "A bound never reaches the echo mirror",
)
def test_bound_does_not_reach_the_echo() -> None:
    """Behavioural coverage for the echo mirror being independent of capture."""


@given("a curated Python command for testing")
def given_curated_command() -> None:
    """Record that the catalogue is built by the run step, not here."""


@given(
    "a producer stage whose payload exceeds the bound",
    target_fixture="stages",
)
def given_producer_stage() -> list[str]:
    """Start a stage list whose producer writes more than the bound allows."""
    assert _PAYLOAD_LEN > _BOUND, (
        "these scenarios need a payload strictly larger than the bound, or a "
        "leaking bound would be indistinguishable from a correct one"
    )
    return [_PRODUCER_SOURCE]


@given("a stage that relays its input to stdout and to stderr")
def given_relay_stage(stages: list[str]) -> None:
    """Append a stage that relays its input and announces its length."""
    stages.append(_RELAY_SOURCE)


@given("a stage that reports how many bytes it read")
def given_reporter_stage(stages: list[str]) -> None:
    """Append a stage whose only output is the number of bytes it read."""
    stages.append(_REPORTER_SOURCE)


@when(
    "I run the pipeline with stderr left uncaptured",
    target_fixture="result",
)
def when_run_without_stderr(stages: list[str]) -> PipelineResult:
    """Run with stdout bounded and stderr not captured at all."""
    return _run_pipeline(
        stages,
        RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=_BOUND),
            capture_stderr=False,
        ),
    )


@when(
    "I run the pipeline with stderr captured in full",
    target_fixture="result",
)
def when_run_with_full_stderr(stages: list[str]) -> PipelineResult:
    """Run with stdout bounded and stderr deliberately left complete."""
    return _run_pipeline(
        stages,
        RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=_BOUND),
            capture_stderr=FullCapture(),
        ),
    )


@when(
    "I run the pipeline with stdout bounded and echoed",
    target_fixture="mirror",
)
def when_run_echoing_stdout(stages: list[str]) -> io.StringIO:
    """Bound and echo the final stage's stdout, returning the echo mirror.

    The mirror is the parent's own copy of what it read, so it is the one
    place a bound could leak into a *second* view of the same bytes. The sink
    receives the whole payload only if the echo tap sees the chunk untrimmed:
    the accumulator trims a retained tail at finalization, and a tap that
    echoed *its* output instead would write only the bound-long tail.

    Returns
    -------
    io.StringIO
        The sink the run mirrored its echoed stdout into.
    """
    mirror = io.StringIO()
    _run_pipeline(
        stages,
        RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=_BOUND),
            capture_stderr=False,
            echo_stdout=True,
            echo_stderr=False,
        ),
        context=ExecutionContext(
            stdout_sink=typ.cast("typ.IO[str]", mirror),
            stderr_sink=typ.cast("typ.IO[str]", io.StringIO()),
        ),
    )
    return mirror


@then("the echo mirror holds the whole payload")
def then_echo_saw_everything(mirror: io.StringIO) -> None:
    """Assert the echoed copy is the whole payload, not the bounded tail.

    A mirrored view is a *second* view of bytes the capture policy also
    retained, so a bound applied before the echo tap would shorten it while
    leaving the capture record correct. Comparing against the payload — and
    against its length — is what separates the two.
    """
    echoed = mirror.getvalue()
    assert echoed == _PAYLOAD, (
        "the echo mirror must hold every byte the child wrote, not the "
        f"bounded tail; got {echoed!r} rather than {_PAYLOAD!r}"
    )
    assert len(echoed) > _BOUND, (
        "the payload must exceed the bound, or a leak into the mirror would "
        f"be indistinguishable from a correct one; got {len(echoed)} bytes"
    )


@then("the reported stdout capture is the tail of the payload")
def then_stdout_is_the_tail(result: PipelineResult) -> None:
    """Assert the final stage reports exactly the payload's last bytes."""
    assert result.stdout == _TAIL, (
        f"a bounded stdout must report the payload's tail {_TAIL!r}; "
        f"got {result.stdout!r}"
    )
    record = _capture(result, "stdout")
    assert record.policy == "bounded", (
        f"stdout was bounded, so the run must report 'bounded'; got {record.policy!r}"
    )
    assert record.total_bytes == _PAYLOAD_LEN, (
        "the record must count the child's whole stream, not what was kept; "
        f"got {record.total_bytes} of {_PAYLOAD_LEN}"
    )


@then("the reported stdout capture counts the bytes it dropped")
def then_stdout_counts_dropped(result: PipelineResult) -> None:
    """Assert the drop count is the exact difference, not a flag."""
    record = _capture(result, "stdout")
    assert record.dropped_bytes == _DROPPED, (
        f"a bound of {_BOUND} on {_PAYLOAD_LEN} bytes drops {_DROPPED}; "
        f"got {record.dropped_bytes}"
    )
    assert record.storage == "memory", "a bounded capture stays in the parent"
    assert record.owned is False, "a bounded capture owns no file to remove"


@then("no stderr capture is reported")
def then_stderr_is_absent(result: PipelineResult) -> None:
    """Assert an uncaptured stream reports nothing, not an empty record.

    This is what makes the third scenario's stderr assertion meaningful: the
    two runs differ only in stderr's policy, so a stderr record appearing here
    would mean the field is resolved from something other than its own value.
    """
    assert [record.stream for record in result.captures] == ["stdout"], (
        f"an uncaptured stderr must contribute no record; got {list(result.captures)!r}"
    )


@then("the downstream stage reports the payload's whole length")
def then_downstream_saw_everything(result: PipelineResult) -> None:
    """Assert the stage after the bound read the whole payload, not the tail.

    This is the assertion the obligation exists for. The reporter's whole
    output is the length it read, and its own output is far below the bound, so
    a bound that had leaked into the relay would show here as a shortened
    count rather than as a captured tail nothing compares against.
    """
    assert result.stdout == _ANNOUNCEMENT, (
        "the downstream stage must have read the producer's whole payload; "
        f"it reported {result.stdout!r} rather than {_ANNOUNCEMENT}"
    )
    relayed = _capture(result, "stdout")
    assert relayed.dropped_bytes == 0, (
        "the reporter's own output is below the bound, so nothing may be "
        f"dropped; got {relayed.dropped_bytes} bytes dropped"
    )


@then("the reported stderr capture is the whole stderr, which exceeds the bound")
def then_stderr_is_complete(result: PipelineResult) -> None:
    """Assert stderr is complete while stdout is bounded, in one run.

    The two policies in this run differ, so a resolution that collapsed them
    onto one field — the joint switch this obligation excludes — would apply
    the bound to stderr as well. That would be visible twice over: the
    reported text would be the bound-long tail rather than the whole
    ``_STDERR_TEXT``, and the record would count dropped bytes. Asserting
    both, against a stderr longer than the bound, is what keeps a record that
    merely named the policy from passing.
    """
    record = _capture(result, "stderr")
    assert record.policy == "full", (
        "stderr was declared complete, so the run must report 'full'; "
        f"got {record.policy!r}"
    )
    assert result.final.stderr == _STDERR_TEXT, (
        "the relay must announce the payload's length and then its filler; "
        f"got {result.final.stderr!r} rather than {_STDERR_TEXT!r}"
    )
    assert record.dropped_bytes == 0, (
        f"a complete stderr capture drops nothing; got {record.dropped_bytes}"
    )
    assert record.total_bytes == _STDERR_LEN, (
        f"a complete stderr capture counts all {_STDERR_LEN} bytes; "
        f"got {record.total_bytes}"
    )
