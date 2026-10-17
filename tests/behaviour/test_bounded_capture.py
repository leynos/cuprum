"""Behavioural coverage for per-stream capture policies on a pipeline (#443).

A capture policy bounds or moves what a *run reports* about a stream. It must
not touch what a stage hands to the stage after it: the relay is the child's
own pipe, and a bound the parent applies while reading beside it cannot reach
into it. That distinction is invisible to a single command, which has no
downstream stage to have been starved, so these scenarios run real pipelines
and read the second stage's verdict.

Every scenario derives its expected values from two module constants, the
payload and the bound, so what a run must report is a value the test computes
rather than a substring it hopes for. The payload is strictly larger than the
bound, and the last stage of the reporting scenarios writes fewer bytes than
the bound allows — so a bound that leaked into the relay would show up as a
shortened count rather than as a captured tail nothing compares against.
"""

from __future__ import annotations

from pytest_bdd import given, scenario, then, when

from cuprum import ScopeConfig, scoped
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

# The producer writes its payload and exits; it neither reads nor reports.
_PRODUCER_SOURCE = f"import sys; sys.stdout.write({_PAYLOAD!r})"

# Relays stdin to stdout unchanged while announcing the payload's length on
# stderr. Two assertions ride on this: the relayed stdout proves the second
# stage read the whole stream, and the stderr announcement proves a second
# stream's policy ran independently of the first's.
_RELAY_SOURCE = (
    "import sys; data = sys.stdin.read(); "
    "sys.stdout.write(data); sys.stderr.write(str(len(data)))"
)

# The consumer of the relay scenario: reports only the number of bytes it read.
# Its whole output is a couple of bytes, far below the bound, so what it
# reports is a measurement of the relay rather than of any policy.
_REPORTER_SOURCE = "import sys; sys.stdout.write(str(len(sys.stdin.read())))"


def _run_pipeline(stages: list[str], options: RunOutputOptions) -> PipelineResult:
    """Build and run *stages* under one catalogue scope, applying *options*.

    ``SafeCmd.__or__`` returns a ``Pipeline`` while the fold starts at a single
    ``SafeCmd``, so the local is typed at the wider union the reassignment
    actually produces and narrowed once the fold is done. The assertion is what
    makes the narrowing honest rather than assumed: every scenario here runs a
    real two-stage pipeline, and a fold that had somehow left a bare command
    would have nothing to relay between.

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
        return pipeline.run_sync(output=options)


def _capture(result: PipelineResult, stream: str) -> CapturedStream:
    """Return the record *result* reports for *stream*, asserting there is one."""
    matching = [record for record in result.captures if record.stream == stream]
    assert matching, (
        f"the run must report a {stream} capture; got {list(result.captures)!r}"
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
    "A stage's streams are configured independently",
)
def test_streams_are_configured_independently() -> None:
    """Behavioural coverage for per-stream policy independence."""


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


@then("the reported stderr capture is the payload's whole length")
def then_stderr_is_complete(result: PipelineResult) -> None:
    """Assert stderr is complete while stdout is bounded, in one run.

    The two policies in this run differ, so a resolution that collapsed them
    onto one field — the joint switch this obligation excludes — would apply
    the bound to stderr as well. The length is a value the relay computed, so
    the assertion is against the payload rather than against a substring.
    """
    record = _capture(result, "stderr")
    assert record.policy == "full", (
        "stderr was declared complete, so the run must report 'full'; "
        f"got {record.policy!r}"
    )
    assert result.final.stderr == _ANNOUNCEMENT, (
        f"the relay must announce the whole payload length {_ANNOUNCEMENT!r}; "
        f"got {result.final.stderr!r}"
    )
    assert record.dropped_bytes == 0, (
        f"a complete stderr capture drops nothing; got {record.dropped_bytes}"
    )
    assert record.total_bytes == len(_ANNOUNCEMENT), (
        f"the relay announced {_PAYLOAD_LEN}, so stderr holds "
        f"{len(_ANNOUNCEMENT)} bytes; got {record.total_bytes}"
    )
