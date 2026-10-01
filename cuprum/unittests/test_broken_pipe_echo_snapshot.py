"""Snapshot coverage for the bounded projections of a tolerated broken pipe.

Issue #435 adds an opt-in ``BrokenPipePolicy.BEST_EFFORT`` whose handled
disablement is reported through four independent channels: the drain-level
result records that become ``CommandResult.relay_fallbacks``, the echo
observation hook, a structured ``cuprum.stream`` warning, and the
``ECHO_BROKEN_PIPE_TOTAL`` counter. The multivariant output format of those
channels is what the repository's snapshot policy asks to be pinned, so each
policy and stream pair below is locked with ``syrupy``. The field-by-field
semantics stay owned by ``test_broken_pipe_echo_guard.py`` (drain level) and
``test_broken_pipe_result_diagnostics.py`` (result level); this module only
adds the format lock and the cross-channel vocabulary check.

Nothing is redacted, and nothing needs to be. Every snapshotted value is a
frozen closed-set member of :class:`~cuprum.echo_events.EchoStream` or
:class:`~cuprum.echo_events.EchoErrorCategory`, a fixed metric name, or a
literal from the handler's own vocabulary. The projections carry no pid, path,
timestamp, duration, sink identity, exception text, or subprocess payload, so
there is no nondeterministic field to hide. The log projection deliberately
keeps only four bounded ``cuprum_*`` keys and drops the message text, logger
name, level, ``exc_info``, and any other extra: those are asserted
semantically, by the guard module that owns them.

Each case drives one real drain, so the four projections describe one event
rather than four handwritten dicts. The policy is part of the parametrization
on purpose: under ``STRICT`` the drain propagates and every projection stays
empty, under ``BEST_EFFORT`` it carries exactly one record, and the ``.ambr``
artefact then holds both arms of that contrast side by side. The result-level
mirror of the same projection is pinned separately by driving the public
``SafeCmd.run`` path, so the snapshot cannot drift away from what a caller
actually receives.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import BrokenPipePolicy
from cuprum._streams import _drain, _RelayDiagnostics, _StreamConfig
from cuprum.adapters.echo_metrics import ECHO_BROKEN_PIPE_TOTAL, EchoMetricsHook
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

    from cuprum.sh import SafeCmd

_CLOSED_READER = "closed presentation destination"

#: The four bounded extras the ``cuprum.stream`` warning carries. Snapshotting
#: exactly these keeps an unbounded or free-text field from reaching the
#: artefact through the projection.
_LOG_EXTRA_KEYS = (
    "cuprum_operation",
    "cuprum_stream",
    "cuprum_transition",
    "cuprum_error_category",
)

#: One child program per echoed stream, so the public-path case exercises the
#: stream it names rather than whichever one the interpreter prefers.
_STREAM_PROGRAM = {
    EchoStream.STDOUT: "print('hello')",
    EchoStream.STDERR: "import sys; print('hello', file=sys.stderr)",
}


class _BrokenPipeSink:
    """Text-only sink whose destination has closed under the drain.

    A local minimal double rather than an import: the sibling modules that
    define it are being edited on this branch, and this module needs only the
    write-side break.
    """

    def __init__(self) -> None:
        """Record each attempted write before failing."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the attempt, then fail the way a closed reader does."""
        self.attempts.append(payload)
        raise BrokenPipeError(_CLOSED_READER)

    def flush(self) -> None:
        """Model the flush call on a broken stream."""


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub for the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _config(
    sink: typ.IO[str],
    *,
    stream: EchoStream,
    policy: BrokenPipePolicy,
) -> _StreamConfig:
    """Build a UTF-8 config for one stream and broken-pipe policy."""
    return _StreamConfig(
        capture_output=True,
        echo_output=True,
        sink=sink,
        encoding="utf-8",
        errors="replace",
        stream=stream,
        broken_pipe_policy=policy,
    )


def _stream_category(stream: EchoStream, category: EchoErrorCategory) -> dict[str, str]:
    """Project one (stream, error category) pair into plain strings.

    The same two bounded keys reach the result records, the echo events, and
    the metric labels, so projecting them through one helper is what makes the
    cross-channel agreement assertion below meaningful.

    Returns
    -------
    dict[str, str]
        That pair as ``str`` values, which is what the snapshot artefact
        serializes and what the equality assertions compare.
    """
    return {"stream": str(stream), "error_category": str(category)}


def _log_extras(record: logging.LogRecord) -> dict[str, str]:
    """Project the four bounded ``cuprum_*`` extras of one log record."""
    return {key: str(vars(record)[key]) for key in _LOG_EXTRA_KEYS}


def _sink_context(stream: EchoStream, sink: typ.IO[str]) -> ExecutionContext:
    """Point the echoed stream's sink at the closed destination."""
    if stream is EchoStream.STDOUT:
        return ExecutionContext(stdout_sink=sink)
    return ExecutionContext(stderr_sink=sink)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


@pytest.mark.parametrize(
    "policy",
    [pytest.param(policy, id=policy.value) for policy in BrokenPipePolicy],
)
@pytest.mark.parametrize(
    "stream",
    [pytest.param(stream, id=stream.value) for stream in EchoStream],
)
def test_broken_pipe_projections_lock_the_diagnostics_contract(
    policy: BrokenPipePolicy,
    stream: EchoStream,
    caplog: pytest.LogCaptureFixture,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot: the four bounded projections stay stable for both policies.

    One drain per policy and stream produces every projection, so the snapshot
    describes one event rather than four independent dicts. Under ``STRICT`` the
    drain propagates the sink's ``BrokenPipeError`` and the projections are all
    empty; under ``BEST_EFFORT`` it carries exactly one record. The strict arm is
    not vacuous: any projection that starts carrying a record without the caller
    opting in changes the snapshot, and the semantic ``pytest.raises`` beside it
    pins the propagation the default must keep.
    """
    chunks = (b"one ", b"two ", b"three")
    relay_diagnostics = _RelayDiagnostics()
    collector = RecordingCollector()
    events: list[EchoEvent] = []
    drain = _drain(
        _reader(chunks),
        _config(
            typ.cast("typ.IO[str]", _BrokenPipeSink()),
            stream=stream,
            policy=policy,
        ),
        relay_diagnostics=relay_diagnostics,
    )

    with (
        caplog.at_level(logging.WARNING, logger="cuprum.stream"),
        observe_echo(events.append),
        observe_echo(EchoMetricsHook(collector)),
    ):
        if policy is BrokenPipePolicy.STRICT:
            with pytest.raises(BrokenPipeError, match=_CLOSED_READER):
                asyncio.run(drain)
        else:
            asyncio.run(drain)
    relay_diagnostics.settle()

    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]
    projections = {
        "relay_fallbacks": [
            _stream_category(fallback.stream, fallback.error_category)
            for fallback in relay_diagnostics.snapshot()
        ],
        "echo_events": [
            _stream_category(event.stream, event.error_category) for event in events
        ],
        "log_extras": [_log_extras(record) for record in warnings],
        "metrics": [
            (name, dict(labels)) for name, _value, labels in collector.counters
        ],
    }

    expected_records = int(policy is BrokenPipePolicy.BEST_EFFORT)
    assert len(projections["relay_fallbacks"]) == expected_records, (
        "only the opted-in policy may record a handled disablement, got "
        f"{projections['relay_fallbacks']!r}"
    )
    assert projections["echo_events"] == projections["relay_fallbacks"], (
        "the observation channel must carry the same bounded pair as the record, "
        f"got events={projections['echo_events']!r} from "
        f"records={projections['relay_fallbacks']!r}"
    )
    assert [dict(labels) for _name, labels in projections["metrics"]] == (
        projections["relay_fallbacks"]
    ), (
        "the metric labels must use the record's own vocabulary, got "
        f"metrics={projections['metrics']!r} from "
        f"records={projections['relay_fallbacks']!r}"
    )
    assert [
        {
            "stream": extras["cuprum_stream"],
            "error_category": extras["cuprum_error_category"],
        }
        for extras in projections["log_extras"]
    ] == projections["relay_fallbacks"], (
        "the warning's stream and category extras must match the record, got "
        f"extras={projections['log_extras']!r} from "
        f"records={projections['relay_fallbacks']!r}"
    )
    assert all(
        set(extras) == set(_LOG_EXTRA_KEYS) for extras in projections["log_extras"]
    ), "the log projection must keep exactly the four bounded keys"
    assert [name for name, _labels in projections["metrics"]] == [
        ECHO_BROKEN_PIPE_TOTAL
    ] * expected_records, (
        "a tolerated broken pipe must count on its own series, so a misencoded "
        f"sink stays distinguishable from a closing reader, got "
        f"metrics={projections['metrics']!r}"
    )
    assert projections == snapshot, (
        "the broken-pipe diagnostics' four projections must match the snapshot "
        "for both policies and both streams"
    )


@pytest.mark.parametrize(
    "stream",
    [pytest.param(stream, id=stream.value) for stream in EchoStream],
)
def test_result_records_project_to_the_snapshot_locked_shape(
    stream: EchoStream,
    python_builder: cabc.Callable[..., SafeCmd],
    snapshot: SnapshotAssertion,
) -> None:
    """The public run path surfaces the projection the snapshot locks.

    The snapshot above reads the drain-level collector the command settles into
    its result. This case drives ``SafeCmd.run`` instead, so the projection a
    caller actually receives is pinned to the same shape: one labelled record
    under ``BEST_EFFORT`` for whichever stream was echoed.
    """
    sink = _BrokenPipeSink()

    async def run_case() -> CommandResult:
        """Echo ``stream`` into a closed destination under BEST_EFFORT."""
        return await python_builder("-c", _STREAM_PROGRAM[stream]).run(
            output=RunOutputOptions(
                capture=True,
                echo_stdout=stream is EchoStream.STDOUT,
                echo_stderr=stream is EchoStream.STDERR,
                broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
            ),
            context=_sink_context(stream, typ.cast("typ.IO[str]", sink)),
        )

    result = asyncio.run(run_case())
    # Only the projection this case actually drives is snapshotted: an empty
    # placeholder for the other three channels would record "nothing was
    # observed" rather than "the channel agreed", which is the vacuous shape
    # the repository's snapshot policy rules out.
    projections = {
        "relay_fallbacks": [
            _stream_category(fallback.stream, fallback.error_category)
            for fallback in result.relay_fallbacks
        ],
    }

    assert projections["relay_fallbacks"] == [
        _stream_category(stream, EchoErrorCategory.BROKEN_PIPE)
    ], (
        "a best-effort run must surface one labelled record for the echoed "
        f"stream, got {projections['relay_fallbacks']!r}"
    )
    assert projections == snapshot, (
        "the result-level projection must match the snapshot locked for the "
        "drain-level record"
    )
