"""Regression coverage for result-level echo fallback diagnostics (#356).

The canonical drain contract lives in ``test_stream_drain.py`` and the drain
guard itself in ``test_stream_echo_guard.py``; this module pins the additive
result surface built on top of the #350 echo guard: ``CommandResult`` and each
pipeline stage expose the handled echo-disablement records of their own
streams, in a deterministic stdout-then-stderr order, with nothing recorded
for binary passthrough, disabled echo, or a healthy sink. It also pins the
privacy contract: the ``cuprum.stream`` warning carries only closed-set
categorical values, never the rejected payload, the sink encoding, or the
original exception object.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

import pytest

from cuprum import Program, RelayFallback, TimeoutExpired
from cuprum._streams import _drain, _StreamConfig
from cuprum.adapters.echo_metrics import ECHO_ENCODING_FAILURES_TOTAL
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.metrics_adapter import MetricsCollector
    from cuprum.sh import SafeCmd


_EXPECTED_STDOUT_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_EXPECTED_STDERR_FALLBACK = RelayFallback(
    stream=EchoStream.STDERR,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_NON_ENCODABLE = "héllo ś"


class _Cp1252TextOnlySink:
    """Text-only sink rejecting payloads CP1252 cannot represent."""

    def __init__(self) -> None:
        """Record each attempted write payload."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _PassthroughSink:
    """Sink that accepts every write, modelling a healthy echo target."""

    def __init__(self) -> None:
        """Collect written text for assertions."""
        self.written: list[str] = []

    def write(self, payload: str) -> int:
        """Accept the payload unchanged."""
        self.written.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _RecordingBinaryBuffer:
    """Binary buffer capturing the raw bytes a drain hands to the sink."""

    def __init__(self) -> None:
        """Start with no captured bytes."""
        self.raw: list[bytes] = []

    def write(self, payload: bytes) -> int:
        """Capture raw bytes; CP1252 is deliberately never applied."""
        self.raw.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a buffered writer."""


class _BinaryBufferSink:
    """Sink exposing a writable ``buffer``, modelling the binary fast path."""

    def __init__(self) -> None:
        """Track any text write; the drain must never take that path."""
        self.text_writes: list[str] = []
        self.buffer = _RecordingBinaryBuffer()

    def write(self, payload: str) -> int:
        """Record any text write attempt."""
        self.text_writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


def _reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub for the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _echo_context(sink: typ.IO[str]) -> ExecutionContext:
    """Build a context echoing stdout into ``sink``."""
    return ExecutionContext(stdout_sink=sink)


class _MetricsProbe:
    """Echo hook delegating to :class:`EchoMetricsHook` for one collector."""

    def __init__(self, collector: MetricsCollector) -> None:
        """Bind the probe to its collector."""
        self._hook = _echo_metrics_hook(collector)

    def __call__(self, event: EchoEvent) -> None:
        """Increment the collector's echo counter for ``event``."""
        self._hook(event)


def _echo_metrics_hook(
    collector: MetricsCollector,
) -> cabc.Callable[[EchoEvent], None]:
    """Build the real metrics hook without a module-level import cycle."""
    from cuprum.adapters.echo_metrics import EchoMetricsHook

    return EchoMetricsHook(collector)


def test_binary_buffer_sink_receives_original_bytes() -> None:
    """A sink with a writable binary buffer gets raw bytes, no diagnostics."""
    sink = _BinaryBufferSink()
    chunks = ("safé ".encode(), "wörld ś".encode())

    captured = asyncio.run(
        _drain(
            _reader(chunks),
            _StreamConfig(
                capture_output=False,
                echo_output=True,
                sink=typ.cast("typ.IO[str]", sink),
                encoding="utf-8",
                errors="replace",
                stream=EchoStream.STDOUT,
            ),
        ),
    )

    assert captured is None
    assert b"".join(sink.buffer.raw) == b"".join(chunks), (
        "the binary fast path must forward the original child bytes unchanged"
    )
    assert sink.text_writes == [], "no text write may be attempted"


def test_text_only_failure_records_once_across_all_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One disablement yields one warning with categorical extras only."""
    sink = _Cp1252TextOnlySink()
    chunks = (b"plain ", "ś".encode(), b" tail")

    with caplog.at_level(logging.WARNING, logger="cuprum.stream"):
        captured = asyncio.run(
            _drain(
                _reader(chunks),
                _StreamConfig(
                    capture_output=True,
                    echo_output=True,
                    sink=typ.cast("typ.IO[str]", sink),
                    encoding="utf-8",
                    errors="replace",
                    stream=EchoStream.STDOUT,
                ),
            ),
        )

    assert captured == b"".join(chunks).decode("utf-8", errors="replace"), (
        "capture must complete even though the sink rejected the output"
    )
    assert sink.attempts == ["plain ", "ś"], (
        "the rejecting write must be the last echo attempt for the drain"
    )
    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]
    assert len(warnings) == 1, f"exactly one warning expected, got {caplog.records!r}"
    record = warnings[0]
    assert record.getMessage() == "echo_disabled_stream_rejected_output"
    assert record.exc_info is None, (
        "the original exception object must not ride on the record"
    )
    assert record.args in {None, ()}, (
        f"positional args must stay empty, got {record.args!r}"
    )
    fields = vars(record)
    assert fields["cuprum_operation"] == "echo_chunk"
    assert fields["cuprum_stream"] == "stdout"
    assert fields["cuprum_transition"] == "echo_disabled"
    assert fields["cuprum_error_category"] == "unicode_encode"
    rendered = record.getMessage()
    assert "ś" not in rendered, "the rejected payload must not reach the log"
    assert "cp1252" not in rendered, "the sink encoding must not reach the log"
    assert "cuprum_encoding" not in fields
    assert "cuprum_sink_type" not in fields
    assert "cuprum_error_type" not in fields


def test_drain_collects_diagnostics_without_observers_or_capture() -> None:
    """Diagnostics are collected with no observer registered, no capture."""
    sink = _Cp1252TextOnlySink()

    with _null_scope():
        captured = asyncio.run(
            _drain(
                _reader(("ś".encode(), b" tail")),
                _StreamConfig(
                    capture_output=False,
                    echo_output=True,
                    sink=typ.cast("typ.IO[str]", sink),
                    encoding="utf-8",
                    errors="replace",
                    stream=EchoStream.STDOUT,
                ),
            ),
        )

    assert captured is None, "capture-disabled drains still return None"
    assert sink.attempts == ["ś"], (
        "the disablement must be the first and only echo attempt"
    )


class _NullScope:
    """Context manager standing in for 'no observer registered'."""

    def __enter__(self) -> None:
        """Enter the no-op scope."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit the no-op scope."""


def _null_scope() -> _NullScope:
    """Return the no-op scope."""
    return _NullScope()


def test_metrics_hook_increments_once_per_disablement() -> None:
    """The existing EchoMetricsHook counts one increment per transition."""
    collector = RecordingCollector()
    sink = _Cp1252TextOnlySink()

    with observe_echo(_MetricsProbe(collector)):
        asyncio.run(
            _drain(
                _reader((b"plain ", "ś".encode(), b" more")),
                _StreamConfig(
                    capture_output=True,
                    echo_output=True,
                    sink=typ.cast("typ.IO[str]", sink),
                    encoding="utf-8",
                    errors="replace",
                    stream=EchoStream.STDOUT,
                ),
            ),
        )

    assert len(collector.counters) == 1, (
        f"one disablement must increment once, found {collector.counters}"
    )
    name, value, labels = collector.counters[0]
    assert name == ECHO_ENCODING_FAILURES_TOTAL
    assert value == 1.0  # ruff: ignore[float-equality-comparison] - exact increment
    assert labels == {"stream": "stdout", "error_category": "unicode_encode"}


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_single_command_result_carries_stdout_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A streamed run reports the stdout drain's disablement once."""
    sink = _Cp1252TextOnlySink()

    async def run_case() -> CommandResult:
        """Echo stdout into a rejecting sink while capturing."""
        with observe_echo(lambda _event: None):
            return await python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", sink)),
            )

    result = asyncio.run(run_case())

    assert result.stdout == f"{_NON_ENCODABLE}\n", (
        "capture must survive the echo disablement"
    )
    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        f"exactly the stdout record is expected, got {result.relay_fallbacks!r}"
    )
    assert result.ok, "an echo failure must not change the child's exit status"


def test_stderr_echo_failure_is_recorded_against_stderr(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A stderr sink failure produces a stderr-labelled record only."""
    stderr_sink = _Cp1252TextOnlySink()

    async def run_case() -> CommandResult:
        """Echo stderr into the rejecting sink while stdout stays healthy."""
        with observe_echo(lambda _event: None):
            return await python_builder(
                "-c",
                "import sys; sys.stderr.write('wörld ś\\n'); sys.stderr.flush()",
            ).run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", stderr_sink)
                ),
            )

    result = asyncio.run(run_case())

    assert result.relay_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the stderr record must name stderr, got {result.relay_fallbacks!r}"
    )
    assert result.ok


def test_run_sync_exposes_the_same_result_shape(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The synchronous API returns the same diagnostics as the async API."""
    sink = _Cp1252TextOnlySink()

    with observe_echo(lambda _event: None):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,)
    assert result.stdout == f"{_NON_ENCODABLE}\n"


def test_normal_and_disabled_echo_produce_empty_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Healthy runs — echoing or not — report no fallback records."""
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run once with echo enabled and once with echo disabled."""
        with observe_echo(lambda _event: None):
            echoed = await python_builder("-c", "print('plain')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
            silent = await python_builder("-c", "print('plain')").run(
                output=RunOutputOptions(capture=True),
            )
        return echoed.relay_fallbacks, silent.relay_fallbacks

    echoed_fallbacks, silent_fallbacks = asyncio.run(run_case())

    assert echoed_fallbacks == (), (
        f"healthy echo records nothing, got {echoed_fallbacks!r}"
    )
    assert silent_fallbacks == (), "disabled echo records nothing"


def test_concurrent_commands_do_not_share_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Concurrent runs each report only their own fallbacks."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run one failing and one healthy echo command concurrently."""
        with observe_echo(lambda _event: None):
            failing = asyncio.create_task(
                python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                    output=RunOutputOptions(capture=True, echo=True),
                    context=_echo_context(typ.cast("typ.IO[str]", rejecting)),
                ),
            )
            healthy = asyncio.create_task(
                python_builder("-c", "print('plain')").run(
                    output=RunOutputOptions(capture=True, echo=True),
                    context=_echo_context(typ.cast("typ.IO[str]", accepting)),
                ),
            )
            failing_result, healthy_result = await asyncio.gather(failing, healthy)
        return failing_result.relay_fallbacks, healthy_result.relay_fallbacks

    failing_fallbacks, healthy_fallbacks = asyncio.run(run_case())

    assert failing_fallbacks == (_EXPECTED_STDOUT_FALLBACK,)
    assert healthy_fallbacks == (), (
        "the healthy run must not acquire the failing run's record"
    )


def test_nested_commands_keep_their_own_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A command nested after another does not inherit its diagnostics."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Run a healthy command, then a failing one, in one context."""
        with observe_echo(lambda _event: None):
            outer = await python_builder("-c", "print('outer plain')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
            inner = await python_builder("-c", f"print('{_NON_ENCODABLE}')").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", rejecting)),
            )
        return outer.relay_fallbacks, inner.relay_fallbacks

    outer_fallbacks, inner_fallbacks = asyncio.run(run_case())

    assert outer_fallbacks == (), "the outer run must stay clean"
    assert inner_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        f"the inner run must own its record, got {inner_fallbacks!r}"
    )


def test_pipeline_final_stage_owns_its_stdout_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The final stage reports its stdout disablement; earlier stages none."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Pipe two stages; the final stage's stdout echoes to the bad sink."""
        with observe_echo(lambda _event: None):
            pipeline = python_builder("-c", "print('stage one')") | python_builder(
                "-c",
                f"import sys; print(sys.stdin.read().strip() + ' {_NON_ENCODABLE}')",
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stdout_sink=typ.cast("typ.IO[str]", accepting),
                    stderr_sink=typ.cast("typ.IO[str]", rejecting),
                ),
            )
        return result.stages[0].relay_fallbacks, result.stages[1].relay_fallbacks

    first_fallbacks, final_fallbacks = asyncio.run(run_case())

    assert first_fallbacks == (), (
        f"the first stage has no echo of its own stdout, got {first_fallbacks!r}"
    )
    # The final stage's stdout echoes through the context's stdout sink, which
    # is healthy here, so both stages report empty records for this wiring.
    assert final_fallbacks == (), (
        f"the final stage's stdout used the healthy sink, got {final_fallbacks!r}"
    )


def test_pipeline_stage_results_keep_stage_order(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Stage order is preserved while diagnostics stay per stage."""
    rejecting = _Cp1252TextOnlySink()

    async def run_case() -> tuple[
        int, tuple[RelayFallback, ...], tuple[RelayFallback, ...]
    ]:
        """Run a two-stage pipeline echoing every stderr to one sink."""
        with observe_echo(lambda _event: None):
            pipeline = python_builder(
                "-c", "import sys; sys.stderr.write('wörld ś\\n'); print('mid')"
            ) | python_builder(
                "-c", "import sys; sys.stderr.write('zażółć\\n'); print('done')"
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", rejecting)
                ),
            )
        return (
            len(result.stages),
            result.stages[0].relay_fallbacks,
            result.stages[1].relay_fallbacks,
        )

    stage_count, first_fallbacks, second_fallbacks = asyncio.run(run_case())

    assert stage_count == 2, "stage order and count must be preserved"
    assert first_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the first stage's stderr failure must be recorded, got {first_fallbacks!r}"
    )
    assert second_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the second stage's stderr failure must be recorded, got {second_fallbacks!r}"
    )


def test_timeout_with_pre_expiry_disablement_observes_event(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An echo failure observed before expiry stays on the event channel."""
    events: list[EchoEvent] = []
    sink = _Cp1252TextOnlySink()

    async def run_case() -> None:
        """Timeout a run whose child first trips the echo sink."""
        with observe_echo(events.append):
            await python_builder(
                "-c",
                f"import time; print('{_NON_ENCODABLE}', flush=True); time.sleep(5)",
            ).run(
                timeout=1.0,
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", sink)),
            )

    with pytest.raises(TimeoutExpired):
        asyncio.run(run_case())

    assert len(events) == 1, (
        f"the pre-timeout disablement must stay observable, found {events!r}"
    )
    assert events[0].stream == EchoStream.STDOUT


def test_failing_echo_observer_does_not_change_results(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A raising observer leaves result collection and capture untouched."""
    sink = _Cp1252TextOnlySink()

    def exploding_observer(_event: EchoEvent) -> None:
        """Model a broken metrics backend."""
        msg = "observer exploded"
        raise RuntimeError(msg)

    with observe_echo(exploding_observer):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert result.stdout == f"{_NON_ENCODABLE}\n", (
        "capture must survive a broken observer"
    )
    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        "the diagnostic collector is independent of the observer registry"
    )


def test_non_encoding_sink_error_still_propagates() -> None:
    """Sink failures other than UnicodeEncodeError are not relay fallbacks."""

    class _OSErrorSink:
        """Text-only sink failing with a non-encoding I/O error."""

        def write(self, _payload: str) -> int:
            """Model an unreachable sink device."""
            msg = "device unreachable"
            raise OSError(msg)

        def flush(self) -> None:
            """Model the flush call on a text stream."""

    with pytest.raises(OSError, match="device unreachable"):
        asyncio.run(
            _drain(
                _reader((b"payload",)),
                _StreamConfig(
                    capture_output=True,
                    echo_output=True,
                    sink=typ.cast("typ.IO[str]", _OSErrorSink()),
                    encoding="utf-8",
                    errors="replace",
                ),
            ),
        )


def test_command_result_field_order_and_positional_compatibility() -> None:
    """relay_fallbacks stays the trailing defaulted field of CommandResult."""
    fields = [field.name for field in dc.fields(CommandResult)]
    assert fields[-1] == "relay_fallbacks", f"field order changed: {fields}"
    assert fields[:6] == [
        "program",
        "argv",
        "exit_code",
        "pid",
        "stdout",
        "stderr",
    ], f"existing positional slots must not move: {fields}"
    positional = CommandResult(Program("echo"), (), 0, 4242, "out", "err")
    assert positional.relay_fallbacks == (), (
        "six-argument positional construction must keep working and default to ()"
    )
    assert positional.ok


def test_relay_fallback_is_exported_from_its_definition_site() -> None:
    """The package-root RelayFallback is the echo_events definition."""
    import cuprum as c
    from cuprum import echo_events

    assert c.RelayFallback is echo_events.RelayFallback


def test_diagnostic_record_is_frozen_with_bounded_fields() -> None:
    """RelayFallback is immutable and carries only closed-set vocabulary."""
    fallback = _EXPECTED_STDOUT_FALLBACK
    assert dc.fields(RelayFallback)[0].name == "stream"
    assert dc.fields(RelayFallback)[1].name == "error_category"
    with pytest.raises(dc.FrozenInstanceError):
        fallback.stream = EchoStream.STDERR  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_no_fallback_record_for_program_without_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A command producing no output records nothing even when echoing."""
    accepting = _PassthroughSink()

    async def run_case() -> tuple[RelayFallback, ...]:
        """Echo silence through a healthy sink."""
        with observe_echo(lambda _event: None):
            result = await python_builder("-c", "pass").run(
                output=RunOutputOptions(capture=True, echo=True),
                context=_echo_context(typ.cast("typ.IO[str]", accepting)),
            )
        return result.relay_fallbacks

    assert asyncio.run(run_case()) == ()


def test_event_stream_identity_matches_diagnostic_stream(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The EchoEvent and the result record describe the same transition."""
    events: list[EchoEvent] = []
    sink = _Cp1252TextOnlySink()

    with observe_echo(events.append):
        result = python_builder("-c", f"print('{_NON_ENCODABLE}')").run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=_echo_context(typ.cast("typ.IO[str]", sink)),
        )

    assert len(events) == 1, f"one event for one transition, found {events!r}"
    assert result.relay_fallbacks == (
        RelayFallback(
            stream=events[0].stream,
            error_category=events[0].error_category,
        ),
    ), "the result record and the event must agree on the transition"
